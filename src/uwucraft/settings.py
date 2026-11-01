"""Validated atomic server.properties updates."""

from .backups import require_stopped
from .config import Error, Paths, atomic

ALLOWED = {
    "difficulty": {"peaceful", "easy", "normal", "hard"},
    "gamemode": {"survival", "creative", "adventure", "spectator"},
    "pvp": {"true", "false"},
    "white-list": {"true", "false"},
    "online-mode": {"true", "false"},
    "enforce-secure-profile": {"true", "false"},
}


def properties(paths=None):
    paths = paths or Paths()
    file = paths.server / "server.properties"
    return (
        dict(
            line.split("=", 1)
            for line in file.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#") and "=" in line
        )
        if file.exists()
        else {}
    )


def set_property(key, value, allow_offline=False, paths=None):
    paths = paths or Paths()
    if key in ALLOWED:
        if value not in ALLOWED[key]:
            raise Error("Недопустимое значение: " + ", ".join(sorted(ALLOWED[key])))
    elif key in {"max-players", "view-distance", "simulation-distance", "server-port"}:
        limits = {
            "max-players": (1, 10000),
            "view-distance": (2, 32),
            "simulation-distance": (2, 32),
            "server-port": (1024, 65535),
        }
        low, high = limits[key]
        if not value.isdecimal() or not low <= int(value) <= high:
            raise Error(f"Допустимый диапазон: {low}–{high}.")
    elif key == "motd":
        if len(value) > 256:
            raise Error("MOTD: максимум 256 символов.")
    else:
        raise Error(
            "Это свойство не доступно через CLI; отредактируйте server.properties вручную."
        )
    if any(x in value for x in "\n\r\x00"):
        raise Error("Переводы строки запрещены.")
    if key == "online-mode" and value == "false" and not allow_offline:
        raise Error(
            "Отключение проверки аккаунтов требует --allow-offline. Имена можно подделать; установите auth-плагин."
        )
    with paths.lock():
        require_stopped(paths)
        file = paths.server / "server.properties"
        if not file.exists():
            raise Error("Сначала установите сервер.")
        lines = file.read_text(encoding="utf-8").splitlines()
        replaced = False
        result = []
        for line in lines:
            if line.startswith(key + "="):
                result.append(key + "=" + value)
                replaced = True
            else:
                result.append(line)
        if not replaced:
            result.append(key + "=" + value)
        atomic(file, "\n".join(result) + "\n")
        if key == "server-port":
            config = paths.load()
            config.port = int(value)
            paths.save(config)
