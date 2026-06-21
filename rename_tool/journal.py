import json
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class JournalEntry:
    operation: str
    old_path: str
    new_path: str
    timestamp: float = field(default_factory=time.time)
    status: str = "pending"
    error: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "JournalEntry":
        return cls(
            operation=data.get("operation", "rename"),
            old_path=data.get("old_path", ""),
            new_path=data.get("new_path", ""),
            timestamp=data.get("timestamp", time.time()),
            status=data.get("status", "pending"),
            error=data.get("error", ""),
        )


@dataclass
class Journal:
    entries: list[JournalEntry] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    status: str = "idle"

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "entries": [asdict(e) for e in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Journal":
        return cls(
            entries=[JournalEntry.from_dict(e) for e in data.get("entries", [])],
            started_at=data.get("started_at", time.time()),
            completed_at=data.get("completed_at"),
            status=data.get("status", "idle"),
        )

    @property
    def successful_entries(self) -> list[JournalEntry]:
        return [e for e in self.entries if e.status == "done"]

    @property
    def pending_entries(self) -> list[JournalEntry]:
        return [e for e in self.entries if e.status == "pending"]

    @property
    def failed_entries(self) -> list[JournalEntry]:
        return [e for e in self.entries if e.status == "failed"]


class JournalManager:
    def __init__(self, journal_path: str):
        self.journal_path = journal_path
        self.journal = Journal()

    def start(self, total: int) -> None:
        self.journal = Journal()
        self.journal.status = "running"
        self._save()

    def add_entry(self, operation: str, old_path: str, new_path: str) -> JournalEntry:
        entry = JournalEntry(
            operation=operation,
            old_path=old_path,
            new_path=new_path,
            status="pending",
        )
        self.journal.entries.append(entry)
        return entry

    def mark_done(self, entry: JournalEntry) -> None:
        entry.status = "done"
        self._save()

    def mark_failed(self, entry: JournalEntry, error: str) -> None:
        entry.status = "failed"
        entry.error = str(error)
        self._save()

    def complete(self) -> None:
        self.journal.status = "completed"
        self.journal.completed_at = time.time()
        self._save()

    def abort(self) -> None:
        self.journal.status = "aborted"
        self.journal.completed_at = time.time()
        self._save()

    def _save(self) -> None:
        path = Path(self.journal_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.journal.to_dict(), f, ensure_ascii=False, indent=2)

    def load(self) -> Journal:
        path = Path(self.journal_path)
        if not path.exists():
            self.journal = Journal()
            return self.journal
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.journal = Journal.from_dict(data)
        return self.journal

    def exists(self) -> bool:
        return Path(self.journal_path).exists()

    def can_rollback(self) -> bool:
        self.load()
        return self.journal.status in ("aborted", "completed", "failed") and len(self.journal.successful_entries) > 0

    def rollback(
        self,
        rename_fn: Callable[[str, str], None],
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> tuple[int, int]:
        self.load()
        entries_to_rollback = list(reversed(self.journal.successful_entries))
        total = len(entries_to_rollback)
        success = 0
        failed = 0

        for i, entry in enumerate(entries_to_rollback, 1):
            try:
                if progress_cb:
                    progress_cb(i, total, f"Rolling back: {entry.new_path} -> {entry.old_path}")
                rename_fn(entry.new_path, entry.old_path)
                entry.status = "rolled_back"
                success += 1
            except Exception as e:
                entry.status = "rollback_failed"
                entry.error = str(e)
                failed += 1
                if progress_cb:
                    progress_cb(i, total, f"Rollback failed: {e}")

        self.journal.status = "rolled_back"
        self.journal.completed_at = time.time()
        self._save()
        return success, failed
