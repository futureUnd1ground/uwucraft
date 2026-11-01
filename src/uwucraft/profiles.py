"""Independent server profiles; the historical installation stays in 'default'."""

import socket
from pathlib import Path

from .config import Error, Paths, atomic
from .platforms import windows


def listing():
    base = Paths(profile="default")
    result = [base]
    folder = base.root_config / "profiles"
    if folder.exists():
        for item in sorted(folder.iterdir()):
            if (
                item.is_dir()
                and not item.is_symlink()
                and (item / "config.toml").exists()
            ):
                try:
                    result.append(Paths(profile=item.name))
                except Error:
                    continue
    return result


def select(identifier):
    paths = Paths(profile=identifier)
    if not paths.file.exists():
        raise Error(
            "Профиль не установлен. Создайте: uwucraft --profile "
            + identifier
            + " create"
        )
    # Pin legacy units to default BEFORE changing the persistent selection.
    # Their environment predates profiles; otherwise a reboot could load a
    # newly selected world's files through the old default service.
    if not windows():
        import subprocess

        changed = False
        home = Path.home() / ".config/systemd/user"
        for name in [
            "uwucraft.service",
            "uwucraft-network.service",
            "uwucraft-backup.service",
        ]:
            unit = home / name
            if unit.exists():
                text = unit.read_text(encoding="utf-8")
                if "Description=uwucraft" in text and "UWUCRAFT_PROFILE=" not in text:
                    atomic(
                        unit,
                        text.replace(
                            "[Service]\n",
                            '[Service]\nEnvironment="UWUCRAFT_PROFILE=default"\n',
                            1,
                        ),
                    )
                    changed = True
        if changed:
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"], check=True, timeout=15
            )
    atomic(paths.root_config / "active-profile", identifier + "\n")
    return paths


def next_profile():
    base = Paths(profile="default")
    if not base.file.exists() and not base.server.exists():
        return base
    for number in range(2, 10000):
        paths = Paths(profile=f"server-{number}")
        if not paths.config.exists() and not paths.data.exists():
            return paths
    raise Error("Нет свободного ID профиля. Используйте uwucraft --profile ИМЯ create.")


def suggested_port():
    assigned = {p.load().port for p in listing() if p.file.exists()}
    for port in range(25565, 25666):
        if port in assigned:
            continue
        try:
            with socket.socket() as test:
                test.bind(("0.0.0.0", port))
            return port
        except OSError:
            continue
    raise Error("Порты 25565–25665 заняты. Укажите свободный порт явно.")
