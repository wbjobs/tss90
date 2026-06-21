import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

from .config import RenameConfig, PerformanceConfig


@dataclass
class ChecksumEntry:
    filepath: str
    md5: str

    def to_line(self) -> str:
        return f"{self.md5}  {self.filepath}"

    @classmethod
    def from_line(cls, line: str) -> "ChecksumEntry":
        line = line.rstrip("\n").rstrip("\r")
        parts = line.split("  ", 1)
        if len(parts) != 2:
            parts = line.split(None, 1)
            if len(parts) != 2:
                raise ValueError(f"Invalid checksum line format: {line}")
        return cls(md5=parts[0].strip(), filepath=parts[1].strip())


class Validator:
    def __init__(self, checksum_file: str = "checksums.md5", config: Optional[PerformanceConfig] = None):
        self.checksum_file = checksum_file
        self.config = config or PerformanceConfig()

    def compute_md5(
        self,
        filepath: str,
        read_fn: Optional[Callable[[str], bytes]] = None,
        chunk_size: Optional[int] = None,
    ) -> str:
        chunk = chunk_size or self.config.md5_chunk_size
        if read_fn:
            data = read_fn(filepath)
            return hashlib.md5(data).hexdigest()
        return self._compute_local_md5_streaming(filepath, chunk)

    def _compute_local_md5_streaming(self, filepath: str, chunk_size: int) -> str:
        md5_hash = hashlib.md5()
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                md5_hash.update(chunk)
        return md5_hash.hexdigest()

    def _compute_single_file(
        self,
        filepath: str,
        base_path: str,
        read_fn: Optional[Callable[[str], bytes]],
    ) -> ChecksumEntry:
        md5 = self.compute_md5(filepath, read_fn)
        if base_path:
            base = Path(base_path)
            try:
                rel_path = str(Path(filepath).relative_to(base))
            except ValueError:
                rel_path = filepath
            return ChecksumEntry(filepath=rel_path, md5=md5)
        return ChecksumEntry(filepath=filepath, md5=md5)

    def generate_checksums_iter(
        self,
        files: list[str],
        base_path: str = "",
        read_fn: Optional[Callable[[str], bytes]] = None,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> Iterator[ChecksumEntry]:
        total = len(files)
        workers = min(self.config.workers, total) if total > 0 else 1

        if workers <= 1 or total <= 10:
            for i, f in enumerate(files, 1):
                yield self._compute_single_file(f, base_path, read_fn)
                if progress_cb and (i % self.config.batch_size == 0 or i == total):
                    progress_cb(i, total)
        else:
            completed = 0
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(self._compute_single_file, f, base_path, read_fn): f
                    for f in files
                }
                for future in as_completed(futures):
                    yield future.result()
                    completed += 1
                    if progress_cb and (completed % self.config.batch_size == 0 or completed == total):
                        progress_cb(completed, total)

    def generate_checksums(
        self,
        files: list[str],
        base_path: str = "",
        read_fn: Optional[Callable[[str], bytes]] = None,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> list[ChecksumEntry]:
        entries = []
        for entry in self.generate_checksums_iter(files, base_path, read_fn, progress_cb):
            entries.append(entry)
        entries.sort(key=lambda e: e.filepath)
        return entries

    def write_checksums(
        self,
        entries_iter: Iterator[ChecksumEntry] | list[ChecksumEntry],
        output_path: Optional[str] = None,
    ) -> int:
        path = output_path or self.checksum_file
        count = 0
        with open(path, "w", encoding="utf-8") as f:
            for entry in entries_iter:
                f.write(entry.to_line() + "\n")
                count += 1
        return count

    def write_checksums_stream(
        self,
        entries_iter: Iterator[ChecksumEntry],
        output_path: Optional[str] = None,
    ) -> int:
        return self.write_checksums(entries_iter, output_path)

    def load_checksums(self, checksum_file: Optional[str] = None) -> list[ChecksumEntry]:
        path = checksum_file or self.checksum_file
        entries = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    entries.append(ChecksumEntry.from_line(line))
        return entries

    def verify_checksums(
        self,
        entries: list[ChecksumEntry],
        base_path: str = "",
        read_fn: Optional[Callable[[str], bytes]] = None,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> tuple[list[str], list[str], list[str]]:
        ok = []
        failed = []
        missing = []
        base = Path(base_path) if base_path else Path.cwd()
        total = len(entries)

        for i, entry in enumerate(entries, 1):
            full_path = str(base / entry.filepath)
            if not Path(full_path).exists() and read_fn is None:
                missing.append(entry.filepath)
                if progress_cb and (i % self.config.batch_size == 0 or i == total):
                    progress_cb(i, total)
                continue

            try:
                actual_md5 = self.compute_md5(full_path, read_fn)
                if actual_md5 == entry.md5:
                    ok.append(entry.filepath)
                else:
                    failed.append(entry.filepath)
            except Exception:
                missing.append(entry.filepath)

            if progress_cb and (i % self.config.batch_size == 0 or i == total):
                progress_cb(i, total)

        return ok, failed, missing
