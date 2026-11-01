import fcntl
import os
import re
import tempfile
import tomllib
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

import tomli_w


class Error(Exception):
    """An actionable, safe-to-display application error."""


def atomic(path: Path, data: str | bytes, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data.encode() if isinstance(data, str) else data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def memory_mb(value: str) -> int:
    match = re.fullmatch(r"([1-9]\d*)([MG])", value.upper())
    if not match:
        raise Error("Память: целое число и M/G, например 4G или 2048M.")
    return int(match[1]) * (1024 if match[2] == "G" else 1)


@dataclass
class Config:
    loader: str = "paper"
    version: str = ""
    memory: str = "2G"
    port: int = 25565
    java: str = "java"
    theme: str = "uwu-dark"
    public_ip_url: str = "https://api.ipify.org"
    public_address: str = ""
    playit_address: str = ""
    backup_keep: int = 7
    name: str = "My uwucraft server"


class Paths:
    def __init__(self):
        self.config = (
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "uwucraft"
        )
        self.data = (
            Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
            / "uwucraft"
        )
        self.server = self.data / "server"
        self.backups = self.data / "backups"
        self.file = self.config / "config.toml"
        self.socket = self.data / "console.sock"
        self.state = self.data / "state.json"

    def load(self):
        return (
            Config(**tomllib.loads(self.file.read_text()))
            if self.file.exists()
            else Config()
        )

    def save(self, config):
        memory_mb(config.memory)
        if not 1024 <= config.port <= 65535:
            raise Error("Порт должен быть в диапазоне 1024–65535.")
        atomic(self.file, tomli_w.dumps(asdict(config)))

    @contextmanager
    def lock(self):
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.data / "operation.lock").open("a") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Error("Другая операция уже выполняется.") from None
            try:
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)
