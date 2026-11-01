import os
import re
import tempfile
import tomllib
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import tomli_w

from .platforms import file_lock, windows


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
    max_players: int = 20
    online_mode: bool = True
    server_id: str = ""
    backup_keep: int = 7
    name: str = "My uwucraft server"


class Paths:
    def __init__(self):
        config_home = (
            Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
            if windows()
            else Path.home() / ".config"
        )
        data_home = (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
            if windows()
            else Path.home() / ".local/share"
        )
        self.config = Path(os.environ.get("XDG_CONFIG_HOME", config_home)) / "uwucraft"
        self.data = Path(os.environ.get("XDG_DATA_HOME", data_home)) / "uwucraft"
        self.server = self.data / "server"
        self.backups = self.data / "backups"
        self.file = self.config / "config.toml"
        self.socket = self.data / "console.sock"
        self.state = self.data / "state.json"

    def load(self):
        if not self.file.exists():
            return Config()
        values = tomllib.loads(self.file.read_text(encoding="utf-8"))
        properties = self.server / "server.properties"
        if properties.exists():
            for line in properties.read_text(encoding="utf-8").splitlines():
                if line.startswith("online-mode=") and line.split("=", 1)[1] in {
                    "true",
                    "false",
                }:
                    values["online_mode"] = line.split("=", 1)[1] == "true"
                if (
                    line.startswith("max-players=")
                    and line.split("=", 1)[1].isdecimal()
                ):
                    values["max_players"] = int(line.split("=", 1)[1])
        known = {field.name for field in fields(Config)}
        return Config(**{key: value for key, value in values.items() if key in known})

    def save(self, config):
        memory_mb(config.memory)
        if not 1024 <= config.port <= 65535:
            raise Error("Порт должен быть в диапазоне 1024–65535.")
        if not 1 <= config.max_players <= 10000:
            raise Error("Количество игроков: 1–10000.")
        atomic(self.file, tomli_w.dumps(asdict(config)))

    @contextmanager
    def lock(self):
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.data / "operation.lock").open("a+") as file:
            try:
                with file_lock(file):
                    yield
            except (BlockingIOError, PermissionError):
                raise Error("Другая операция уже выполняется.") from None
