import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


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
    def __init__(self, checksum_file: str = "checksums.md5"):
        self.checksum_file = checksum_file

    def compute_md5(self, filepath: str, read_fn: Optional[Callable[[str], bytes]] = None) -> str:
        if read_fn:
            data = read_fn(filepath)
            return hashlib.md5(data).hexdigest()
        return self._compute_local_md5(filepath)

    def _compute_local_md5(self, filepath: str) -> str:
        md5_hash = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                md5_hash.update(chunk)
        return md5_hash.hexdigest()

    def generate_checksums(
        self,
        files: list[str],
        base_path: str = "",
        read_fn: Optional[Callable[[str], bytes]] = None,
    ) -> list[ChecksumEntry]:
        entries = []
        base = Path(base_path) if base_path else None

        for filepath in sorted(files):
            md5 = self.compute_md5(filepath, read_fn)
            if base and base_path:
                try:
                    rel_path = str(Path(filepath).relative_to(base))
                except ValueError:
                    rel_path = filepath
                entries.append(ChecksumEntry(filepath=rel_path, md5=md5))
            else:
                entries.append(ChecksumEntry(filepath=filepath, md5=md5))
        return entries

    def write_checksums(self, entries: list[ChecksumEntry], output_path: Optional[str] = None) -> None:
        path = output_path or self.checksum_file
        with open(path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(entry.to_line() + "\n")

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
    ) -> tuple[list[str], list[str], list[str]]:
        ok = []
        failed = []
        missing = []
        base = Path(base_path) if base_path else Path.cwd()

        for entry in entries:
            full_path = str(base / entry.filepath)
            if not Path(full_path).exists() and read_fn is None:
                missing.append(entry.filepath)
                continue

            try:
                actual_md5 = self.compute_md5(full_path, read_fn)
                if actual_md5 == entry.md5:
                    ok.append(entry.filepath)
                else:
                    failed.append(entry.filepath)
            except Exception:
                missing.append(entry.filepath)

        return ok, failed, missing
