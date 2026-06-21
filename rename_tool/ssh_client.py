import io
import time
from pathlib import Path
from typing import Callable, Iterator, Optional

from .config import SSHConfig


def _retry_on_failure(
    func: Callable,
    max_retries: int,
    retry_delay: float,
    *args,
    **kwargs,
):
    """Retry a function call with exponential backoff."""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                sleep_time = retry_delay * (2 ** attempt)
                time.sleep(sleep_time)
            else:
                raise
    raise last_exc


class SSHClient:
    def __init__(self, config: SSHConfig):
        self.config = config
        self._client = None
        self._sftp = None
        self._transport = None

    def connect(self) -> None:
        try:
            import paramiko
        except ImportError:
            raise ImportError(
                "paramiko is required for SSH mode. Install with: pip install paramiko"
            )

        def _do_connect():
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            connect_kwargs = {
                "hostname": self.config.host,
                "port": self.config.port,
                "username": self.config.username,
                "timeout": self.config.connect_timeout,
                "banner_timeout": self.config.connect_timeout,
                "auth_timeout": self.config.connect_timeout,
            }

            if self.config.key_file:
                connect_kwargs["key_filename"] = self.config.key_file
            elif self.config.password:
                connect_kwargs["password"] = self.config.password

            client.connect(**connect_kwargs)

            transport = client.get_transport()
            if transport and self.config.keepalive_interval > 0:
                transport.set_keepalive(self.config.keepalive_interval)

            return client, client.open_sftp(), transport

        self._client, self._sftp, self._transport = _retry_on_failure(
            _do_connect,
            max_retries=self.config.max_retries,
            retry_delay=self.config.retry_delay,
        )

    def close(self) -> None:
        if self._sftp:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        self._transport = None

    def __enter__(self) -> "SSHClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _ensure_connected(self) -> None:
        if not self._sftp or not self._client:
            self.connect()
            return
        if self._transport and self._transport.is_active():
            return
        self.close()
        self.connect()

    def _decode_filename(self, raw_name: str) -> str:
        """Decode remote filename considering encoding differences."""
        remote_enc = self.config.remote_encoding
        local_enc = self.config.local_encoding

        if remote_enc.lower() == local_enc.lower():
            return raw_name

        try:
            raw_bytes = raw_name.encode(remote_enc, errors="surrogateescape")
            return raw_bytes.decode(local_enc, errors="replace")
        except (UnicodeEncodeError, UnicodeDecodeError, LookupError):
            return raw_name

    def _encode_filename(self, local_name: str) -> str:
        """Encode local filename for remote filesystem."""
        remote_enc = self.config.remote_encoding
        local_enc = self.config.local_encoding

        if remote_enc.lower() == local_enc.lower():
            return local_name

        try:
            raw_bytes = local_name.encode(local_enc, errors="surrogateescape")
            return raw_bytes.decode(remote_enc, errors="replace")
        except (UnicodeEncodeError, UnicodeDecodeError, LookupError):
            return local_name

    def list_files(
        self,
        remote_path: str,
        recursive: bool = True,
        progress_cb: Optional[Callable[[int], None]] = None,
    ) -> list[str]:
        self._ensure_connected()
        files = []
        count = 0

        def _walk(path: str) -> None:
            nonlocal count
            try:
                entries = _retry_on_failure(
                    self._sftp.listdir_attr,
                    max_retries=self.config.max_retries,
                    retry_delay=self.config.retry_delay,
                    path=path,
                )
                for entry in entries:
                    decoded_name = self._decode_filename(entry.filename)
                    full_path = f"{path}/{decoded_name}" if path != "/" else f"/{decoded_name}"
                    full_path = full_path.replace("//", "/")
                    if self._is_dir(full_path):
                        if recursive:
                            _walk(full_path)
                    else:
                        files.append(full_path)
                        count += 1
                        if progress_cb and count % 1000 == 0:
                            progress_cb(count)
            except IOError:
                pass

        _walk(remote_path)
        if progress_cb:
            progress_cb(count)
        return sorted(files)

    def list_files_iter(self, remote_path: str, recursive: bool = True) -> Iterator[str]:
        for f in self.list_files(remote_path, recursive):
            yield f

    def _is_dir(self, path: str) -> bool:
        self._ensure_connected()
        try:
            stat = _retry_on_failure(
                self._sftp.stat,
                max_retries=self.config.max_retries,
                retry_delay=self.config.retry_delay,
                path=path,
            )
            return stat.st_mode & 0o40000 != 0
        except IOError:
            return False

    def rename_file(self, old_path: str, new_path: str) -> None:
        self._ensure_connected()
        remote_old = self._encode_filename(old_path)
        remote_new = self._encode_filename(new_path)

        def _do_rename():
            self._sftp.rename(remote_old, remote_new)

        _retry_on_failure(
            _do_rename,
            max_retries=self.config.max_retries,
            retry_delay=self.config.retry_delay,
        )

    def read_file(self, remote_path: str) -> bytes:
        self._ensure_connected()
        remote_p = self._encode_filename(remote_path)
        with self._sftp.file(remote_p, "rb") as f:
            return f.read()

    def compute_remote_md5(self, remote_path: str) -> str:
        self._ensure_connected()
        remote_p = self._encode_filename(remote_path)

        def _do_md5sum():
            stdin, stdout, stderr = self._client.exec_command(f"md5sum '{remote_p}'", timeout=self.config.connect_timeout * 3)
            output = stdout.read().decode("utf-8", errors="replace").strip()
            if output:
                return output.split()[0]
            return None

        try:
            result = _retry_on_failure(
                _do_md5sum,
                max_retries=1,
                retry_delay=self.config.retry_delay,
            )
            if result:
                return result
        except Exception:
            pass

        import hashlib
        data = self.read_file(remote_path)
        return hashlib.md5(data).hexdigest()

    def write_remote_file(self, remote_path: str, content: str) -> None:
        self._ensure_connected()
        remote_p = self._encode_filename(remote_path)
        with self._sftp.file(remote_p, "w") as f:
            f.write(content)
