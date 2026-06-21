import argparse
import sys
from pathlib import Path

from .config import RenameConfig, load_config, RenameRule, SSHConfig
from .renamer import Renamer, RenamePlan
from .validator import Validator, ChecksumEntry


def print_plan(plan: RenamePlan) -> None:
    print(f"\n=== Rename Plan ({plan.total} files) ===\n")

    for i, item in enumerate(plan.items, 1):
        print(f"  {i}. {item.old_name}  ->  {item.new_name}")

    if plan.skipped:
        print(f"\nSkipped ({len(plan.skipped)}):")
        for path, reason in plan.skipped[:10]:
            print(f"  - {Path(path).name} ({reason})")
        if len(plan.skipped) > 10:
            print(f"  ... and {len(plan.skipped) - 10} more")

    if plan.conflicts:
        print(f"\nConflicts ({len(plan.conflicts)}):")
        for conflict in plan.conflicts:
            print(f"  ! {conflict}")

    print()


def run_local(args: argparse.Namespace, config: RenameConfig) -> int:
    target_path = args.path or "."

    print(f"Scanning local directory: {target_path}")
    print(f"Recursive: {config.recursive}")
    print(f"Dry run: {config.dry_run}")

    renamer = Renamer(config)
    files = renamer.scan_files(target_path)
    print(f"Found {len(files)} files")

    plan = renamer.generate_plan(files, target_path)
    print_plan(plan)

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

        count = renamer.execute_plan(plan)
        if config.dry_run:
            print(f"[DRY RUN] Would rename {count} files")
        else:
            print(f"Renamed {count} files")

    if args.checksum:
        validator = Validator(config.checksum_file)
        all_files = renamer.scan_files(target_path)
        entries = validator.generate_checksums(all_files, target_path)
        checksum_path = str(Path(target_path) / config.checksum_file)
        validator.write_checksums(entries, checksum_path)
        print(f"\nChecksum list written to: {checksum_path}")
        print(f"  Total: {len(entries)} files")

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
    print(f"Connecting to {ssh_config.username}@{ssh_config.host}:{ssh_config.port}")
    print(f"Remote path: {ssh_config.remote_path}")
    print(f"Dry run: {config.dry_run}")

    try:
        with SSHClient(ssh_config) as ssh:
            print("Connected.")

            print(f"\nScanning remote files...")
            files = ssh.list_files(ssh_config.remote_path, config.recursive)
            print(f"Found {len(files)} files")

            renamer = Renamer(config)
            plan = renamer.generate_plan(files, ssh_config.remote_path)

            print(f"\n=== Preview (dry-run on local copy of file list) ===")
            print_plan(plan)

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
                count = renamer.execute_plan(plan, rename_fn=ssh.rename_file)
                print(f"Renamed {count} files on remote server")

            if args.checksum:
                print(f"\nGenerating checksums on remote...")
                validator = Validator(config.checksum_file)
                entries = []
                for filepath in files:
                    try:
                        md5 = ssh.compute_remote_md5(filepath)
                        rel_path = filepath[len(ssh_config.remote_path):].lstrip("/")
                        entries.append(ChecksumEntry(
                            filepath=rel_path, md5=md5
                        ))
                    except Exception as e:
                        print(f"  Warning: could not checksum {filepath}: {e}")

                checksum_content = "\n".join(e.to_line() for e in entries) + "\n"
                remote_checksum_path = f"{ssh_config.remote_path}/{config.checksum_file}"
                ssh.write_remote_file(remote_checksum_path, checksum_content)
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
        description="Batch file rename tool with local and SSH remote modes",
    )
    subparsers = parser.add_subparsers(dest="mode", help="Operation mode")

    local_parser = subparsers.add_parser("local", help="Local mode")
    local_parser.add_argument("path", nargs="?", default=".", help="Target directory")
    local_parser.add_argument("-c", "--config", help="Path to config file")
    local_parser.add_argument("--pattern", help="Regex pattern to match")
    local_parser.add_argument("--replacement", help="Replacement string")
    local_parser.add_argument("--sequence", action="store_true", help="Use sequential numbering")
    local_parser.add_argument("--seq-start", type=int, default=1, help="Sequence start number")
    local_parser.add_argument("--seq-padding", type=int, default=3, help="Sequence zero-padding")
    local_parser.add_argument("--seq-prefix", default="", help="Sequence prefix")
    local_parser.add_argument("--no-recursive", action="store_true", help="Do not scan subdirectories")
    local_parser.add_argument("--dry-run", action="store_true", help="Show plan without executing")
    local_parser.add_argument("--checksum", action="store_true", help="Generate MD5 checksum list")
    local_parser.add_argument("--checksum-file", default="checksums.md5", help="Checksum file name")
    local_parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    remote_parser = subparsers.add_parser("remote", help="Remote SSH mode")
    remote_parser.add_argument("-c", "--config", help="Path to config file")
    remote_parser.add_argument("--host", help="SSH host")
    remote_parser.add_argument("--port", type=int, default=22, help="SSH port")
    remote_parser.add_argument("--user", help="SSH username")
    remote_parser.add_argument("--password", help="SSH password")
    remote_parser.add_argument("--key-file", help="SSH private key file")
    remote_parser.add_argument("--remote-path", help="Remote directory path")
    remote_parser.add_argument("--pattern", help="Regex pattern to match")
    remote_parser.add_argument("--replacement", help="Replacement string")
    remote_parser.add_argument("--sequence", action="store_true", help="Use sequential numbering")
    remote_parser.add_argument("--seq-start", type=int, default=1, help="Sequence start number")
    remote_parser.add_argument("--seq-padding", type=int, default=3, help="Sequence zero-padding")
    remote_parser.add_argument("--seq-prefix", default="", help="Sequence prefix")
    remote_parser.add_argument("--no-recursive", action="store_true", help="Do not scan subdirectories")
    remote_parser.add_argument("--dry-run", action="store_true", help="Show plan without executing")
    remote_parser.add_argument("--checksum", action="store_true", help="Generate MD5 checksum list")
    remote_parser.add_argument("--checksum-file", default="checksums.md5", help="Checksum file name")
    remote_parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    return parser


def build_config_from_args(args: argparse.Namespace) -> RenameConfig:
    if args.config:
        return load_config(args.config)

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
        )

    config = RenameConfig(
        rules=[rule],
        recursive=not getattr(args, "no_recursive", False),
        dry_run=getattr(args, "dry_run", False),
        ssh=ssh_config,
        checksum_file=getattr(args, "checksum_file", "checksums.md5"),
    )

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
