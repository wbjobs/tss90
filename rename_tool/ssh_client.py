import io
from pathlib import Path
from typing import Optional

from .config import SSHConfig


class SSHClient:
    def __init__(self, config: SSHConfig):
        self.config = config
        self._client = None
        self._sftp = None

    def connect(self) -> None:
        try:
            import paramiko
        except ImportError:
            raise ImportError(
                "paramiko is required for SSH mode. Install with: pip install paramiko"
            )

        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": self.config.host,
            "port": self.config.port,
            "username": self.config.username,
        }

        if self.config.key_file:
            connect_kwargs["key_filename"] = self.config.key_file
        elif self.config.password:
            connect_kwargs["password"] = self.config.password

        self._client.connect(**connect_kwargs)
        self._sftp = self._client.open_sftp()

    def close(self) -> None:
        if self._sftp:
            self._sftp.close()
            self._sftp = None
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self) -> "SSHClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def list_files(self, remote_path: str, recursive: bool = True) -> list[str]:
        if not self._sftp:
            raise RuntimeError("SSH not connected")

        files = []
        self._walk_remote(remote_path, recursive, files)
        return sorted(files)

    def _walk_remote(self, path: str, recursive: bool, files: list[str]) -> None:
        try:
            for entry in self._sftp.listdir_attr(path):
                full_path = f"{path}/{entry.filename}" if path != "/" else f"/{entry.filename}"
                full_path = full_path.replace("//", "/")
                if self._is_dir(full_path):
                    if recursive:
                        self._walk_remote(full_path, recursive, files)
                else:
                    files.append(full_path)
        except IOError:
            pass

    def _is_dir(self, path: str) -> bool:
        if not self._sftp:
            return False
        try:
            return self._sftp.stat(path).st_mode & 0o40000 != 0
        except IOError:
            return False

    def rename_file(self, old_path: str, new_path: str) -> None:
        if not self._sftp:
            raise RuntimeError("SSH not connected")
        self._sftp.rename(old_path, new_path)

    def read_file(self, remote_path: str) -> bytes:
        if not self._sftp:
            raise RuntimeError("SSH not connected")
        with self._sftp.file(remote_path, "rb") as f:
            return f.read()

    def compute_remote_md5(self, remote_path: str) -> str:
        if not self._client:
            raise RuntimeError("SSH not connected")

        stdin, stdout, stderr = self._client.exec_command(f"md5sum '{remote_path}'")
        output = stdout.read().decode("utf-8").strip()
        error = stderr.read().decode("utf-8").strip()

        if error and "No such file" in error:
            raise FileNotFoundError(f"Remote file not found: {remote_path}")

        if output:
            return output.split()[0]

        import hashlib
        data = self.read_file(remote_path)
        return hashlib.md5(data).hexdigest()

    def write_remote_file(self, remote_path: str, content: str) -> None:
        if not self._sftp:
            raise RuntimeError("SSH not connected")
        with self._sftp.file(remote_path, "w") as f:
            f.write(content)
