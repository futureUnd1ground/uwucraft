"""Independent server profiles; the historical installation stays in 'default'."""

import shutil
import socket
import subprocess
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


def _clean_leftovers(paths):
    """Remove debris of an interrupted install (partial downloads, staging
    tmp dirs) so a slot can be reused; never touch a real installation —
    that guard stays in the installer itself. Directories still being
    written (a concurrent wizard) are left alone."""
    import time

    if not paths.data.is_dir():
        return
    for entry in paths.data.iterdir():
        try:
            if entry.name.startswith("tmp") and entry.is_dir():
                newest = entry.stat().st_mtime
                for child in entry.rglob("*"):
                    newest = max(newest, child.stat().st_mtime)
                if time.time() - newest > 600:
                    shutil.rmtree(entry)
            elif entry.name.endswith(".part"):
                if time.time() - entry.stat().st_mtime > 600:
                    entry.unlink(missing_ok=True)
        except OSError:
            pass


def next_profile():
    base = Paths(profile="default")
    if not base.file.exists():
        _clean_leftovers(base)
        if not base.server.exists() or not any(base.server.iterdir()):
            return base
    for number in range(2, 10000):
        paths = Paths(profile=f"server-{number}")
        if not paths.file.exists():
            _clean_leftovers(paths)
            if not paths.server.exists() or not any(paths.server.iterdir()):
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


def delete(identifier, purge_imported=False):
    """Unregister a profile: services first, then uwucraft metadata and files.

    A server created by uwucraft loses its world together with the profile; an
    imported server keeps its directory (management marker only) unless
    purge_imported=True. Other profiles, custom themes and the active-profile
    bookkeeping are never touched. Returns a list of warning strings.
    """
    paths = Paths(profile=identifier)
    if not paths.file.exists():
        raise Error("Профиль не установлен: " + identifier)
    from . import server

    if server.status(paths)["status"] not in {"Stopped", "Crashed"}:
        raise Error(
            "Сначала остановите сервер: uwucraft --profile " + identifier + " stop."
        )
    config = paths.load()
    warnings = []
    marker = paths.data / "server-location"
    imported_dir = None
    if marker.exists() and not marker.is_symlink():
        try:
            candidate = Path(marker.read_text(encoding="utf-8").strip())
            if candidate.is_absolute() and len(str(candidate)) > 4:
                imported_dir = candidate
        except (OSError, ValueError):
            imported_dir = None
    if windows():
        from .windows import task_name

        for kind in ("server", "network", "backup"):
            try:
                subprocess.run(
                    ["schtasks", "/Delete", "/TN", task_name(paths, kind), "/F"],
                    capture_output=True,
                    timeout=30,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            from .windows import powershell

            powershell(
                "Remove-NetFirewallRule -Name "
                + f"'uwucraft-TCP-{config.port}' -ErrorAction SilentlyContinue"
            )
        except Error:
            pass
    else:
        home = Path.home() / ".config/systemd/user"
        backup_base = (
            "uwucraft-backup"
            if paths.profile == "default"
            else "uwucraft-backup@" + paths.profile
        )
        names = [
            paths.unit_name,
            paths.network_unit_name,
            backup_base + ".service",
            backup_base + ".timer",
        ]
        changed = False
        for name in names:
            unit = home / name
            if unit.exists() and not unit.is_symlink():
                subprocess.run(
                    ["systemctl", "--user", "disable", "--now", name],
                    capture_output=True,
                    timeout=30,
                )
                unit.unlink(missing_ok=True)
                changed = True
        if changed:
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                capture_output=True,
                timeout=15,
            )
        # Root firewall helper only when no other profile keeps the same port.
        others = {
            item.load().port
            for item in listing()
            if item.profile != identifier and item.file.exists()
        }
        if config.port not in others:
            unit = Path(f"/etc/systemd/system/uwucraft-firewall-{config.port}.service")
            if unit.exists():
                try:
                    from . import privilege

                    subprocess.run(
                        privilege.elevated(["systemctl", "disable", unit.name]),
                        capture_output=True,
                        timeout=30,
                    )
                except (Error, OSError, subprocess.SubprocessError):
                    pass
                warnings.append(
                    f"Системное firewall-правило порта {config.port} осталось: "
                    f"sudo systemctl disable {unit.name}; sudo rm -f "
                    f"/etc/uwucraft/firewall-{config.port}.py {unit}. "
                    "Действующее правило nftables удалится после перезагрузки или вручную."
                )
    if imported_dir is not None:
        # Management was in place: drop only uwucraft metadata.
        if purge_imported:
            if imported_dir.is_dir() and not imported_dir.is_symlink():
                shutil.rmtree(imported_dir)
        else:
            warnings.append("Файлы сервера сохранены в каталоге: " + str(imported_dir))
    if paths.profile == "default":
        # The default roots also hold other profiles, themes and bookkeeping:
        # remove only this profile's entries.
        for entry in paths.config.iterdir():
            if entry.name not in {"profiles", "active-profile", "themes"}:
                _remove(entry)
        for entry in paths.data.iterdir():
            if entry.name != "profiles":
                _remove(entry)
    else:
        _remove(paths.config)
        _remove(paths.data)
    selected = paths.root_config / "active-profile"
    if selected.exists():
        try:
            current = selected.read_text(encoding="utf-8").strip()
        except OSError:
            current = ""
        if current == identifier:
            if identifier != "default" and Paths(profile="default").file.exists():
                atomic(selected, "default\n")
            else:
                selected.unlink(missing_ok=True)
    return warnings


def _remove(entry):
    try:
        if entry.is_symlink() or entry.is_file():
            entry.unlink(missing_ok=True)
        elif entry.is_dir():
            shutil.rmtree(entry)
    except OSError:
        pass
