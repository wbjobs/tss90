import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import RenameConfig, RenameRule


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

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def has_conflicts(self) -> bool:
        return len(self.conflicts) > 0


class Renamer:
    def __init__(self, config: RenameConfig):
        self.config = config

    def scan_files(self, base_path: str, list_files_fn: Optional[Callable[[str, bool], list[str]]] = None) -> list[str]:
        if list_files_fn:
            return list_files_fn(base_path, self.config.recursive)
        return self._scan_local_files(base_path)

    def _scan_local_files(self, base_path: str) -> list[str]:
        base = Path(base_path)
        if not base.exists():
            raise FileNotFoundError(f"Path not found: {base_path}")
        if not base.is_dir():
            raise NotADirectoryError(f"Not a directory: {base_path}")

        files = []
        if self.config.recursive:
            for p in base.rglob("*"):
                if p.is_file():
                    files.append(str(p))
        else:
            for p in base.iterdir():
                if p.is_file():
                    files.append(str(p))
        return sorted(files)

    def generate_plan(self, files: list[str], base_path: str = "") -> RenamePlan:
        plan = RenamePlan()
        new_paths: set[str] = set()
        counters: dict[int, int] = {}

        for file_path in files:
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

    def execute_plan(self, plan: RenamePlan, rename_fn: Optional[Callable[[str, str], None]] = None) -> int:
        if plan.has_conflicts:
            raise ValueError("Cannot execute plan with conflicts")

        if self.config.dry_run:
            return plan.total

        rename_func = rename_fn or self._local_rename
        count = 0

        for item in plan.items:
            rename_func(item.old_path, item.new_path)
            count += 1

        return count

    def _local_rename(self, old_path: str, new_path: str) -> None:
        old = Path(old_path)
        new = Path(new_path)
        if new.exists():
            raise FileExistsError(f"Target already exists: {new_path}")
        old.rename(new)
