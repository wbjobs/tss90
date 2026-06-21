import argparse
import sys
from pathlib import Path

from .config import RenameConfig, load_config, RenameRule, SSHConfig, PerformanceConfig
from .renamer import Renamer, RenamePlan, RenameItem
from .validator import Validator, ChecksumEntry
from .journal import JournalManager


def print_plan(plan: RenamePlan, show_all: bool = False) -> None:
    print(f"\n=== Rename Plan ({plan.total} files) ===\n")

    display_items = plan.items if show_all or plan.total <= 50 else plan.items[:50]
    for i, item in enumerate(display_items, 1):
        print(f"  {i}. {item.old_name}  ->  {item.new_name}")
    if not show_all and plan.total > 50:
        print(f"  ... and {plan.total - 50} more (use --verbose to show all)")

    if plan.skipped:
        print(f"\nSkipped ({len(plan.skipped)}):")
        show_skipped = plan.skipped if show_all or len(plan.skipped) <= 10 else plan.skipped[:10]
        for path, reason in show_skipped:
            print(f"  - {Path(path).name} ({reason})")
        if not show_all and len(plan.skipped) > 10:
            print(f"  ... and {len(plan.skipped) - 10} more")

    if plan.conflicts:
        print(f"\nConflicts ({len(plan.conflicts)}):")
        for conflict in plan.conflicts:
            print(f"  ! {conflict}")

    print()


def _progress(current: int, total: int, prefix: str = "") -> None:
    pct = (current / total * 100) if total > 0 else 100
    bar_len = 30
    filled = int(bar_len * current // total) if total > 0 else bar_len
    bar = "#" * filled + "-" * (bar_len - filled)
    sys.stdout.write(f"\r  {prefix}[{bar}] {current}/{total} ({pct:.1f}%)")
    sys.stdout.flush()
    if current >= total:
        sys.stdout.write("\n")
        sys.stdout.flush()


def run_local(args: argparse.Namespace, config: RenameConfig) -> int:
    target_path = args.path or "."
    journal_path = str(Path(target_path) / config.journal_file)

    if args.rollback:
        jm = JournalManager(journal_path)
        if not jm.can_rollback():
            print("ERROR: No rollback available. Journal not found or no successful operations.")
            return 1
        j = jm.load()
        print(f"Found journal with {len(j.successful_entries)} operations to roll back.")
        if not args.yes:
            answer = input("Proceed with rollback? [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted.")
                return 0

        def progress(i, total, msg):
            _progress(i, total, msg + " ")

        def _local_rename(o, n):
            old = Path(o)
            new = Path(n)
            if new.exists():
                raise FileExistsError(f"Target already exists: {n}")
            old.rename(new)

        success, failed = jm.rollback(
            rename_fn=_local_rename,
            progress_cb=progress if not args.quiet else None,
        )
        print(f"\nRollback complete: {success} succeeded, {failed} failed")
        return 0 if failed == 0 else 1

    print(f"Scanning local directory: {target_path}")
    print(f"Recursive: {config.recursive}")
    print(f"Dry run: {config.dry_run}")
    print(f"Workers: {config.performance.workers}")

    renamer = Renamer(config)

    def scan_progress(count):
        sys.stdout.write(f"\r  Found {count} files...")
        sys.stdout.flush()

    files = renamer.scan_files(target_path, progress_cb=scan_progress if not args.quiet else None)
    if not args.quiet:
        sys.stdout.write("\n")
        sys.stdout.flush()
    print(f"Found {len(files)} files")

    plan = renamer.generate_plan(
        files,
        target_path,
        progress_cb=lambda c, t: _progress(c, t, "Planning ") if not args.quiet else None,
    )
    print_plan(plan, show_all=args.verbose)

    if plan.has_conflicts:
        print("ERROR: Plan has conflicts. Aborting.")
        return 1

    if plan.total == 0:
        print("No files to rename.")
    else:
        if not config.dry_run and not args.yes:
            answer = input("Proceed with rename? [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted.")
                return 0

        jm = JournalManager(journal_path) if config.enable_rollback and not config.dry_run else None
        success, failed = renamer.execute_plan(
            plan,
            journal_manager=jm,
            progress_cb=lambda i, t, m: _progress(i, t, m + " ") if not args.quiet else None,
        )
        if config.dry_run:
            print(f"[DRY RUN] Would rename {success} files")
        else:
            print(f"\nRenamed {success} files, {failed} failed")
            if failed > 0 and config.enable_rollback:
                print(f"Journal saved to: {journal_path}")
                print(f"Run with --rollback to undo successful operations")

    if args.checksum:
        validator = Validator(config.checksum_file, config.performance)
        renamer2 = Renamer(config)
        all_files = renamer2.scan_files(target_path)
        checksum_path = str(Path(target_path) / config.checksum_file)

        entries_iter = validator.generate_checksums_iter(
            all_files,
            target_path,
            progress_cb=lambda c, t: _progress(c, t, "Checksums ") if not args.quiet else None,
        )
        count = validator.write_checksums_stream(entries_iter, checksum_path)
        if not args.quiet:
            sys.stdout.write("\n")
        print(f"\nChecksum list written to: {checksum_path}")
        print(f"  Total: {count} files")

    return 0


def run_remote(args: argparse.Namespace, config: RenameConfig) -> int:
    if not config.ssh:
        print("ERROR: SSH configuration is required for remote mode.")
        return 1

    try:
        from .ssh_client import SSHClient
    except ImportError as e:
        print(f"ERROR: {e}")
        return 1

    ssh_config = config.ssh
    journal_path = config.journal_file

    if args.rollback:
        jm = JournalManager(journal_path)
        if not jm.can_rollback():
            print("ERROR: No rollback available. Journal not found or no successful operations.")
            return 1
        j = jm.load()
        print(f"Found journal with {len(j.successful_entries)} operations to roll back.")
        if not args.yes:
            answer = input("Proceed with rollback on remote? [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted.")
                return 0

        with SSHClient(ssh_config) as ssh:
            def progress(i, total, msg):
                _progress(i, total, msg + " ")

            success, failed = jm.rollback(
                rename_fn=ssh.rename_file,
                progress_cb=progress if not args.quiet else None,
            )
        print(f"\nRollback complete: {success} succeeded, {failed} failed")
        return 0 if failed == 0 else 1

    print(f"Connecting to {ssh_config.username}@{ssh_config.host}:{ssh_config.port}")
    print(f"Remote path: {ssh_config.remote_path}")
    print(f"Dry run: {config.dry_run}")
    print(f"Remote encoding: {ssh_config.remote_encoding} -> Local: {ssh_config.local_encoding}")
    print(f"Connection retries: {ssh_config.max_retries}")

    try:
        with SSHClient(ssh_config) as ssh:
            print("Connected.")

            print(f"\nScanning remote files...")

            def scan_progress(count):
                sys.stdout.write(f"\r  Found {count} files...")
                sys.stdout.flush()

            files = ssh.list_files(
                ssh_config.remote_path,
                config.recursive,
                progress_cb=scan_progress if not args.quiet else None,
            )
            if not args.quiet:
                sys.stdout.write("\n")
                sys.stdout.flush()
            print(f"Found {len(files)} files")

            renamer = Renamer(config)
            plan = renamer.generate_plan(
                files,
                ssh_config.remote_path,
                progress_cb=lambda c, t: _progress(c, t, "Planning ") if not args.quiet else None,
            )

            print(f"\n=== Preview (dry-run using real remote file list with encoding) ===")
            print_plan(plan, show_all=args.verbose)

            if plan.has_conflicts:
                print("ERROR: Plan has conflicts. Aborting.")
                return 1

            if plan.total == 0:
                print("No files to rename.")
                return 0

            if not args.yes:
                answer = input("Execute on remote server? [y/N]: ").strip().lower()
                if answer not in ("y", "yes"):
                    print("Aborted.")
                    return 0

            if config.dry_run:
                print("[DRY RUN] Skipping actual execution on remote.")
            else:
                jm = JournalManager(journal_path) if config.enable_rollback else None
                success, failed = renamer.execute_plan(
                    plan,
                    rename_fn=ssh.rename_file,
                    journal_manager=jm,
                    progress_cb=lambda i, t, m: _progress(i, t, m + " ") if not args.quiet else None,
                )
                print(f"\nRenamed {success} files on remote server, {failed} failed")
                if failed > 0 and config.enable_rollback:
                    print(f"Journal saved to: {journal_path}")
                    print(f"Run with --rollback to undo successful operations")

            if args.checksum:
                print(f"\nGenerating checksums on remote...")
                validator = Validator(config.checksum_file, config.performance)
                entries = []
                total = len(files)
                for i, filepath in enumerate(files, 1):
                    try:
                        md5 = ssh.compute_remote_md5(filepath)
                        rel_path = filepath[len(ssh_config.remote_path):].lstrip("/")
                        entries.append(ChecksumEntry(filepath=rel_path, md5=md5))
                    except Exception as e:
                        print(f"  Warning: could not checksum {filepath}: {e}")
                    if not args.quiet and (i % 100 == 0 or i == total):
                        _progress(i, total, "Checksums ")

                checksum_content = "\n".join(e.to_line() for e in entries) + "\n"
                remote_checksum_path = f"{ssh_config.remote_path}/{config.checksum_file}"
                ssh.write_remote_file(remote_checksum_path, checksum_content)
                if not args.quiet:
                    sys.stdout.write("\n")
                print(f"Checksum list written to remote: {remote_checksum_path}")
                print(f"  Total: {len(entries)} files")

                local_checksum_path = config.checksum_file
                with open(local_checksum_path, "w", encoding="utf-8") as f:
                    f.write(checksum_content)
                print(f"Local copy saved to: {local_checksum_path}")

    except Exception as e:
        print(f"ERROR: {e}")
        return 1

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="renamer",
        description="Batch file rename tool with local and SSH remote modes (performance optimized)",
    )
    subparsers = parser.add_subparsers(dest="mode", help="Operation mode")

    common_args = argparse.ArgumentParser(add_help=False)
    common_args.add_argument("-c", "--config", help="Path to config file")
    common_args.add_argument("--pattern", help="Regex pattern to match")
    common_args.add_argument("--replacement", help="Replacement string")
    common_args.add_argument("--sequence", action="store_true", help="Use sequential numbering")
    common_args.add_argument("--seq-start", type=int, default=1, help="Sequence start number")
    common_args.add_argument("--seq-padding", type=int, default=3, help="Sequence zero-padding")
    common_args.add_argument("--seq-prefix", default="", help="Sequence prefix")
    common_args.add_argument("--no-recursive", action="store_true", help="Do not scan subdirectories")
    common_args.add_argument("--dry-run", action="store_true", help="Show plan without executing")
    common_args.add_argument("--checksum", action="store_true", help="Generate MD5 checksum list")
    common_args.add_argument("--checksum-file", default="checksums.md5", help="Checksum file name")
    common_args.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    common_args.add_argument("-v", "--verbose", action="store_true", help="Show full plan details")
    common_args.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output")
    common_args.add_argument("--rollback", action="store_true", help="Rollback last operation from journal")
    common_args.add_argument("--no-rollback", action="store_true", help="Disable journal/rollback")
    common_args.add_argument("--journal-file", default="rename_journal.json", help="Journal file path")
    common_args.add_argument("--workers", type=int, default=4, help="Parallel workers for checksums")
    common_args.add_argument("--batch-size", type=int, default=1000, help="Batch size for progress reporting")
    common_args.add_argument("--md5-chunk", type=int, default=1024 * 1024, help="MD5 chunk size in bytes")

    local_parser = subparsers.add_parser("local", parents=[common_args], help="Local mode")
    local_parser.add_argument("path", nargs="?", default=".", help="Target directory")

    remote_parser = subparsers.add_parser("remote", parents=[common_args], help="Remote SSH mode")
    remote_parser.add_argument("--host", help="SSH host")
    remote_parser.add_argument("--port", type=int, default=22, help="SSH port")
    remote_parser.add_argument("--user", help="SSH username")
    remote_parser.add_argument("--password", help="SSH password")
    remote_parser.add_argument("--key-file", help="SSH private key file")
    remote_parser.add_argument("--remote-path", help="Remote directory path")
    remote_parser.add_argument("--connect-timeout", type=int, default=30, help="SSH connect timeout (seconds)")
    remote_parser.add_argument("--max-retries", type=int, default=3, help="SSH max retries")
    remote_parser.add_argument("--retry-delay", type=float, default=2.0, help="SSH retry delay base (seconds)")
    remote_parser.add_argument("--keepalive", type=int, default=30, help="SSH keepalive interval (seconds)")
    remote_parser.add_argument("--remote-encoding", default="utf-8", help="Remote filesystem encoding")
    remote_parser.add_argument("--local-encoding", default="utf-8", help="Local filesystem encoding")

    return parser


def build_config_from_args(args: argparse.Namespace) -> RenameConfig:
    if getattr(args, "rollback", False):
        config = RenameConfig(
            rules=[RenameRule(pattern=".*", replacement="\\1")],
            journal_file=getattr(args, "journal_file", "rename_journal.json"),
        )
        if args.mode == "remote":
            config.ssh = SSHConfig(
                host=getattr(args, "host", "") or "",
                port=getattr(args, "port", 22),
                username=getattr(args, "user", "") or "",
                password=getattr(args, "password", "") or "",
                key_file=getattr(args, "key_file", None),
                remote_path=getattr(args, "remote_path", "") or "",
                connect_timeout=getattr(args, "connect_timeout", 30),
                max_retries=getattr(args, "max_retries", 3),
                retry_delay=getattr(args, "retry_delay", 2.0),
                keepalive_interval=getattr(args, "keepalive", 30),
            )
            if args.config:
                loaded = load_config(args.config)
                if loaded.ssh:
                    config.ssh = loaded.ssh
        return config

    if args.config:
        config = load_config(args.config)
    else:
        rule = RenameRule(
            pattern=args.pattern or "",
            replacement=args.replacement or "",
            use_sequence=getattr(args, "sequence", False),
            sequence_start=getattr(args, "seq_start", 1),
            sequence_padding=getattr(args, "seq_padding", 3),
            sequence_prefix=getattr(args, "seq_prefix", ""),
        )

        ssh_config = None
        if args.mode == "remote":
            ssh_config = SSHConfig(
                host=args.host or "",
                port=args.port,
                username=args.user or "",
                password=args.password or "",
                key_file=args.key_file,
                remote_path=args.remote_path or "",
                connect_timeout=args.connect_timeout,
                max_retries=args.max_retries,
                retry_delay=args.retry_delay,
                keepalive_interval=args.keepalive,
                remote_encoding=args.remote_encoding,
                local_encoding=args.local_encoding,
            )

        config = RenameConfig(
            rules=[rule],
            recursive=not getattr(args, "no_recursive", False),
            dry_run=getattr(args, "dry_run", False),
            ssh=ssh_config,
            checksum_file=getattr(args, "checksum_file", "checksums.md5"),
            journal_file=getattr(args, "journal_file", "rename_journal.json"),
            enable_rollback=not getattr(args, "no_rollback", False),
        )

    if not args.config:
        config.performance.workers = getattr(args, "workers", config.performance.workers)
        config.performance.batch_size = getattr(args, "batch_size", config.performance.batch_size)
        config.performance.md5_chunk_size = getattr(args, "md5_chunk", config.performance.md5_chunk_size)
        config.journal_file = getattr(args, "journal_file", config.journal_file)
        config.enable_rollback = not getattr(args, "no_rollback", config.enable_rollback)
        if config.dry_run is False:
            config.dry_run = getattr(args, "dry_run", False)
        if args.mode == "remote" and config.ssh:
            config.ssh.max_retries = getattr(args, "max_retries", config.ssh.max_retries)
            config.ssh.retry_delay = getattr(args, "retry_delay", config.ssh.retry_delay)

    errors = config.validate()
    if errors:
        raise ValueError("Invalid arguments:\n" + "\n".join(errors))

    return config


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not args.mode:
        parser.print_help()
        return 1

    try:
        config = build_config_from_args(args)
    except (ValueError, FileNotFoundError) as e:
        print(f"ERROR: {e}")
        return 1

    if args.mode == "local":
        return run_local(args, config)
    elif args.mode == "remote":
        return run_remote(args, config)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
