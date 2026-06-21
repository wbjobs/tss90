import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PurePath
from threading import Lock
from typing import Callable, Iterator, Optional

from .config import RenameConfig, RenameRule
from .journal import JournalManager, JournalEntry


@dataclass
class RenameItem:
    old_path: str
    new_path: str
    rule_index: int
    depth: int = 0

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

    def to_undo_mapping(self) -> list[dict]:
        return [{"old_path": item.old_path, "new_path": item.new_path} for item in self.items]


class UndoManager:
    def __init__(self, undo_path: str):
        self.undo_path = undo_path

    def write_undo(self, mapping: list[dict]) -> None:
        path = Path(self.undo_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)

    def load_undo(self) -> list[dict]:
        path = Path(self.undo_path)
        if not path.exists():
            raise FileNotFoundError(f"Undo file not found: {self.undo_path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("Invalid undo file format")
        return data

    def exists(self) -> bool:
        return Path(self.undo_path).exists()


def _compute_depth(path_str: str) -> int:
    return Path(path_str).parts.__len__()


def _is_ancestor_of(ancestor: str, descendant: str) -> bool:
    a = Path(ancestor)
    d = Path(descendant)
    try:
        d.relative_to(a)
        return True
    except ValueError:
        return False


def _topological_sort_items(items: list[RenameItem]) -> list[list[RenameItem]]:
    """
    Group items into dependency levels for parallel execution.

    Rules:
    - If item A's new_path is an ancestor of item B's old_path, A must run AFTER B.
      (Because renaming A first would invalidate B's path.)
    - If item A's old_path is an ancestor of item B's old_path, A must run AFTER B.
      (Renaming the parent dir first invalidates the child's path.)
    - Items at the same level have no dependency and can run in parallel.
    - Deeper paths (more path components) are processed first (children before parents).
    """
    if not items:
        return []

    sorted_items = sorted(items, key=lambda x: _compute_depth(x.old_path), reverse=True)

    levels: list[list[RenameItem]] = []
    assigned: dict[int, int] = {}

    for i, item in enumerate(sorted_items):
        min_level = 0

        for j, prev in enumerate(sorted_items):
            if i == j:
                continue
            if j in assigned:
                prev_level = assigned[j]
            else:
                continue

            if _is_ancestor_of(prev.new_path, item.old_path):
                min_level = max(min_level, prev_level + 1)

            if _is_ancestor_of(prev.old_path, item.old_path) and prev.old_path != item.old_path:
                min_level = max(min_level, prev_level + 1)

        while len(levels) <= min_level:
            levels.append([])
        levels[min_level].append(item)
        assigned[i] = min_level

    return levels


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
                    depth=_compute_depth(file_path),
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
        undo_manager: Optional[UndoManager] = None,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        parallel: int = 1,
    ) -> tuple[int, int]:
        if plan.has_conflicts:
            raise ValueError("Cannot execute plan with conflicts")

        if self.config.dry_run:
            if undo_manager:
                undo_manager.write_undo(plan.to_undo_mapping())
            return plan.total, 0

        rename_func = rename_fn or self._local_rename

        if undo_manager:
            undo_manager.write_undo(plan.to_undo_mapping())

        if journal_manager:
            journal_manager.start(plan.total)
            for item in plan.items:
                journal_manager.add_entry("rename", item.old_path, item.new_path)

        if parallel > 1 and plan.total > 1:
            success, failed = self._execute_parallel(
                plan, rename_func, journal_manager, progress_cb, parallel,
            )
        else:
            success, failed = self._execute_sequential(
                plan, rename_func, journal_manager, progress_cb,
            )

        if journal_manager:
            if failed == 0:
                journal_manager.complete()
            else:
                journal_manager.abort()

        return success, failed

    def _execute_sequential(
        self,
        plan: RenamePlan,
        rename_func: Callable[[str, str], None],
        journal_manager: Optional[JournalManager],
        progress_cb: Optional[Callable[[int, int, str], None]],
    ) -> tuple[int, int]:
        success_count = 0
        failed_count = 0

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
        except KeyboardInterrupt:
            if journal_manager:
                journal_manager.abort()
            raise

        return success_count, failed_count

    def _execute_parallel(
        self,
        plan: RenamePlan,
        rename_func: Callable[[str, str], None],
        journal_manager: Optional[JournalManager],
        progress_cb: Optional[Callable[[int, int, str], None]],
        workers: int,
    ) -> tuple[int, int]:
        levels = _topological_sort_items(plan.items)

        success_count = 0
        failed_count = 0
        completed = 0
        total = plan.total
        lock = Lock()

        try:
            for level_idx, level_items in enumerate(levels):
                if progress_cb:
                    print(f"  Level {level_idx + 1}/{len(levels)}: {len(level_items)} items (depth-sorted, safe to parallelize)")

                effective_workers = min(workers, len(level_items))

                if effective_workers <= 1 or len(level_items) <= 2:
                    for item in level_items:
                        with lock:
                            completed += 1
                            idx = completed

                        entry = journal_manager.journal.entries[plan.items.index(item)] if journal_manager else None
                        try:
                            if progress_cb:
                                progress_cb(idx, total, f"{item.old_name} -> {item.new_name}")
                            rename_func(item.old_path, item.new_path)
                            with lock:
                                success_count += 1
                            if entry and journal_manager:
                                journal_manager.mark_done(entry)
                        except Exception as e:
                            with lock:
                                failed_count += 1
                            if entry and journal_manager:
                                journal_manager.mark_failed(entry, str(e))
                            if progress_cb:
                                progress_cb(idx, total, f"FAILED: {e}")
                else:
                    with ThreadPoolExecutor(max_workers=effective_workers) as executor:
                        futures = {}
                        for item in level_items:
                            entry = journal_manager.journal.entries[plan.items.index(item)] if journal_manager else None
                            future = executor.submit(self._rename_single, rename_func, item, entry, journal_manager)
                            futures[future] = (item, entry)

                        for future in as_completed(futures):
                            item, entry = futures[future]
                            with lock:
                                completed += 1
                                idx = completed

                            try:
                                is_ok = future.result()
                                with lock:
                                    if is_ok:
                                        success_count += 1
                                    else:
                                        failed_count += 1
                                if progress_cb:
                                    msg = f"{item.old_name} -> {item.new_name}" if is_ok else f"FAILED: {item.old_name}"
                                    progress_cb(idx, total, msg)
                            except Exception as e:
                                with lock:
                                    failed_count += 1
                                if progress_cb:
                                    progress_cb(idx, total, f"FAILED: {e}")

        except KeyboardInterrupt:
            if journal_manager:
                journal_manager.abort()
            raise

        return success_count, failed_count

    def _rename_single(
        self,
        rename_func: Callable[[str, str], None],
        item: RenameItem,
        entry: Optional[JournalEntry],
        journal_manager: Optional[JournalManager],
    ) -> bool:
        try:
            rename_func(item.old_path, item.new_path)
            if entry and journal_manager:
                journal_manager.mark_done(entry)
            return True
        except Exception as e:
            if entry and journal_manager:
                journal_manager.mark_failed(entry, str(e))
            return False

    def _local_rename(self, old_path: str, new_path: str) -> None:
        old = Path(old_path)
        new = Path(new_path)
        if new.exists():
            raise FileExistsError(f"Target already exists: {new_path}")
        old.rename(new)
