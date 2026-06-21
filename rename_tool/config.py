import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class SSHConfig:
    host: str
    port: int = 22
    username: str = ""
    password: str = ""
    key_file: Optional[str] = None
    remote_path: str = ""

    def validate(self) -> list[str]:
        errors = []
        if not self.host:
            errors.append("SSH host is required")
        if not self.remote_path:
            errors.append("Remote path is required")
        if not self.username:
            errors.append("SSH username is required")
        if self.port <= 0 or self.port > 65535:
            errors.append("SSH port must be between 1 and 65535")
        return errors


@dataclass
class RenameRule:
    pattern: str
    replacement: str
    use_sequence: bool = False
    sequence_start: int = 1
    sequence_padding: int = 3
    sequence_prefix: str = ""

    def validate(self) -> list[str]:
        errors = []
        if not self.pattern:
            errors.append("Pattern is required")
        else:
            try:
                re.compile(self.pattern)
            except re.error as e:
                errors.append(f"Invalid regex pattern '{self.pattern}': {e}")
        if not self.replacement and not self.use_sequence:
            errors.append("Replacement or use_sequence must be specified")
        if self.sequence_padding < 1:
            errors.append("Sequence padding must be at least 1")
        return errors


@dataclass
class RenameConfig:
    rules: list[RenameRule] = field(default_factory=list)
    recursive: bool = True
    dry_run: bool = False
    ssh: Optional[SSHConfig] = None
    checksum_file: str = "checksums.md5"

    def validate(self) -> list[str]:
        errors = []
        if not self.rules:
            errors.append("At least one rename rule is required")
        for i, rule in enumerate(self.rules):
            rule_errors = rule.validate()
            for err in rule_errors:
                errors.append(f"Rule {i + 1}: {err}")
        if self.ssh:
            errors.extend(f"SSH: {err}" for err in self.ssh.validate())
        return errors


def load_config(config_path: str) -> RenameConfig:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    rules = []
    for rule_data in data.get("rules", []):
        rules.append(RenameRule(
            pattern=rule_data.get("pattern", ""),
            replacement=rule_data.get("replacement", ""),
            use_sequence=rule_data.get("use_sequence", False),
            sequence_start=rule_data.get("sequence_start", 1),
            sequence_padding=rule_data.get("sequence_padding", 3),
            sequence_prefix=rule_data.get("sequence_prefix", ""),
        ))

    ssh_config = None
    ssh_data = data.get("ssh")
    if ssh_data:
        ssh_config = SSHConfig(
            host=ssh_data.get("host", ""),
            port=ssh_data.get("port", 22),
            username=ssh_data.get("username", ""),
            password=ssh_data.get("password", ""),
            key_file=ssh_data.get("key_file"),
            remote_path=ssh_data.get("remote_path", ""),
        )

    config = RenameConfig(
        rules=rules,
        recursive=data.get("recursive", True),
        dry_run=data.get("dry_run", False),
        ssh=ssh_config,
        checksum_file=data.get("checksum_file", "checksums.md5"),
    )

    errors = config.validate()
    if errors:
        raise ValueError("Invalid configuration:\n" + "\n".join(errors))

    return config
