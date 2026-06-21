import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

from .config import RenameConfig, RenameRule
from .journal import JournalManager, JournalEntry


@dataclass
class RenameItem:
    old_path: str
    new_path: str
    rule_index: int

    @property
    def old_name(self) -> str:
        return Path(self.old_path).name

    @property
    def new_name(self) -> str:
        return Path(self.new_path).name


@dataclass
class RenamePlan:
    items: list[RenameItem] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    total_count: int = 0
    scanned_count: int = 0

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def has_conflicts(self) -> bool:
        return len(self.conflicts) > 0


class Renamer:
    def __init__(self, config: RenameConfig):
        self.config = config

    def scan_files_iter(self, base_path: str) -> Iterator[str]:
        base = Path(base_path)
        if not base.exists():
            raise FileNotFoundError(f"Path not found: {base_path}")
        if not base.is_dir():
            raise NotADirectoryError(f"Not a directory: {base_path}")

        if self.config.recursive:
            for p in base.rglob("*"):
                if p.is_file():
                    yield str(p)
        else:
            for p in base.iterdir():
                if p.is_file():
                    yield str(p)

    def scan_files(
        self,
        base_path: str,
        list_files_fn: Optional[Callable[[str, bool], list[str]]] = None,
        progress_cb: Optional[Callable[[int], None]] = None,
    ) -> list[str]:
        if list_files_fn:
            return list_files_fn(base_path, self.config.recursive)

        files = []
        count = 0
        for f in self.scan_files_iter(base_path):
            files.append(f)
            count += 1
            if progress_cb and count % self.config.performance.batch_size == 0:
                progress_cb(count)
        if progress_cb:
            progress_cb(count)
        return files

    def generate_plan(
        self,
        files: list[str],
        base_path: str = "",
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> RenamePlan:
        plan = RenamePlan(total_count=len(files))
        new_paths: set[str] = set()
        counters: dict[int, int] = {}

        for idx, file_path in enumerate(files, 1):
            matched = False
            file_name = Path(file_path).name
            file_dir = str(Path(file_path).parent)

            for rule_idx, rule in enumerate(self.config.rules):
                match = re.search(rule.pattern, file_name)
                if not match:
                    continue

                if rule_idx not in counters:
                    counters[rule_idx] = rule.sequence_start

                new_name = self._apply_rule(file_name, rule, match, counters[rule_idx])
                new_full_path = str(Path(file_dir) / new_name)

                if new_full_path == file_path:
                    plan.skipped.append((file_path, "name unchanged"))
                    matched = True
                    break

                if new_full_path in new_paths or (Path(new_full_path).exists() and new_full_path != file_path):
                    plan.conflicts.append(f"Conflict: {file_name} -> {new_name}")
                    matched = True
                    break

                plan.items.append(RenameItem(
                    old_path=file_path,
                    new_path=new_full_path,
                    rule_index=rule_idx,
                ))
                new_paths.add(new_full_path)
                counters[rule_idx] += 1
                matched = True
                break

            if not matched:
                plan.skipped.append((file_path, "no matching rule"))

            plan.scanned_count = idx
            if progress_cb and idx % self.config.performance.batch_size == 0:
                progress_cb(idx, plan.total_count)

        if progress_cb:
            progress_cb(plan.scanned_count, plan.total_count)
        return plan

    def _apply_rule(self, filename: str, rule: RenameRule, match: re.Match, seq_num: int) -> str:
        stem = Path(filename).stem
        suffix = Path(filename).suffix

        if rule.use_sequence:
            seq_str = str(seq_num).zfill(rule.sequence_padding)
            new_stem = f"{rule.sequence_prefix}{seq_str}"
        else:
            new_stem = match.expand(rule.replacement)
            if not rule.replacement:
                new_stem = stem

        return f"{new_stem}{suffix}"

    def execute_plan(
        self,
        plan: RenamePlan,
        rename_fn: Optional[Callable[[str, str], None]] = None,
        journal_manager: Optional[JournalManager] = None,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> tuple[int, int]:
        if plan.has_conflicts:
            raise ValueError("Cannot execute plan with conflicts")

        if self.config.dry_run:
            return plan.total, 0

        rename_func = rename_fn or self._local_rename
        success_count = 0
        failed_count = 0

        if journal_manager:
            journal_manager.start(plan.total)
            for item in plan.items:
                journal_manager.add_entry("rename", item.old_path, item.new_path)

        try:
            for idx, item in enumerate(plan.items, 1):
                entry = journal_manager.journal.entries[idx - 1] if journal_manager else None
                try:
                    if progress_cb:
                        progress_cb(idx, plan.total, f"{item.old_name} -> {item.new_name}")
                    rename_func(item.old_path, item.new_path)
                    success_count += 1
                    if entry and journal_manager:
                        journal_manager.mark_done(entry)
                except Exception as e:
                    failed_count += 1
                    if entry and journal_manager:
                        journal_manager.mark_failed(entry, str(e))
                    if progress_cb:
                        progress_cb(idx, plan.total, f"FAILED: {e}")

            if journal_manager:
                if failed_count == 0:
                    journal_manager.complete()
                else:
                    journal_manager.abort()
        except KeyboardInterrupt:
            if journal_manager:
                journal_manager.abort()
            raise

        return success_count, failed_count

    def _local_rename(self, old_path: str, new_path: str) -> None:
        old = Path(old_path)
        new = Path(new_path)
        if new.exists():
            raise FileExistsError(f"Target already exists: {new_path}")
        old.rename(new)
