"""Shared, resumeless and non-destructive server creation backend."""

import shutil
import subprocess
import unicodedata
from dataclasses import dataclass

import psutil

from . import backups, deployment, installers
from .config import Error, Paths, memory_mb
from .platforms import windows
from .sources import latest_compatible


def validate_name(name):
    if (
        not name.strip()
        or len(name) > 80
        or any(unicodedata.category(c).startswith("C") for c in name)
    ):
        raise Error("Название: от 1 до 80 символов, без управляющих символов.")


def suggested_memory():
    available = psutil.virtual_memory().available // 1024**2
    safe = min(6144, (available - 1024) * 3 // 5)
    if safe < 512:
        raise Error(
            "Недостаточно доступной RAM: освободите память, оставив 1 GiB системе и минимум 512 MiB серверу."
        )
    rounded = max(512, safe // 256 * 256)
    return f"{rounded // 1024}G" if rounded % 1024 == 0 else f"{rounded}M"


@dataclass
class CreateOptions:
    name: str = "My uwucraft server"
    version: str = ""
    loader: str = "paper"
    online_mode: bool = True
    memory: str = ""
    port: int = 25565
    max_players: int = 20
    accept_eula: bool = False
    start: bool = True
    # Offline ("pirate") setups are exposed to forged account names; an
    # optional whitelist narrows who can join. Never forced, only offered.
    public_access: str = "relay"
    whitelist: bool = False

    def validate(self):
        validate_name(self.name)
        if self.public_access not in {"relay", "direct"}:
            raise Error("Публичный доступ: relay или direct.")
        if self.loader not in installers.LOADERS:
            raise Error("Ядро: " + ", ".join(installers.LOADERS))
        if not 1024 <= self.port <= 65535 or not 1 <= self.max_players <= 10000:
            raise Error("Порт: 1024–65535; количество игроков: 1–10000.")
        if not isinstance(self.online_mode, bool):
            raise Error("Режим аккаунтов должен быть лицензия/пиратка.")
        if self.memory:
            memory_mb(self.memory)
        if self.version and (
            len(self.version) > 64
            or any(
                c
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._"
                for c in self.version
            )
        ):
            raise Error("Недопустимая версия Minecraft.")
        if not self.accept_eula:
            raise Error(
                "Сначала явно примите Minecraft EULA: https://aka.ms/MinecraftEULA (--accept-eula)."
            )


def dependencies(public_access="relay"):
    """Java is provisioned per-user; iproute2 is the only missing host utility."""
    if windows():
        if not shutil.which("powershell.exe"):
            raise Error("Нужен Windows PowerShell (Windows 10/11).")
        return []
    if not shutil.which("systemctl") or not shutil.which("loginctl"):
        raise Error("Нужна система с работающим systemd и пользовательской сессией.")
    if public_access == "direct" and not shutil.which("ip"):
        if not shutil.which("pacman"):
            return [
                "Установите iproute2: команда ip необходима для определения маршрутизатора."
            ]
        from . import privilege

        # sudo itself asks for the password when the cache is empty, so the
        # wizard does not need a manual `sudo -v` restart; pkexec is the
        # graphical fallback. uwucraft never sees the password.
        problem = privilege.ensure_interactive("установка iproute2")
        if problem:
            return [problem]
        try:
            result = subprocess.run(
                privilege.elevated(
                    ["pacman", "-S", "--needed", "--noconfirm", "iproute2"]
                ),
                capture_output=True,
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError):
            return [
                "Не удалось установить iproute2. Установите его с правами администратора и повторите network configure."
            ]
        if result.returncode:
            return [
                "Не удалось установить iproute2. Выполните sudo -v и повторите network configure."
            ]


def create(options, paths=None, progress=None):
    paths = paths or Paths()
    progress = progress or (lambda message: None)
    options.validate()
    if paths.file.exists() or (paths.server.exists() and any(paths.server.iterdir())):
        raise Error(
            "Сервер уже существует и сохранён. Для повторной настройки публичного доступа: uwucraft network configure. Для другого сервера используйте uwucraft --profile ИМЯ create."
        )
    options.memory = options.memory or suggested_memory()
    progress("Проверка зависимостей и доступных ресурсов")
    dependency_errors = dependencies(options.public_access) or []
    options.version = options.version or latest_compatible(options.loader)
    progress("Установка совместимой Java и выбранного ядра")
    installers.install(
        options.loader,
        options.version,
        options.memory,
        options.port,
        True,
        paths=paths,
        local_runtime=True,
        offline_mode=not options.online_mode,
        name=options.name,
        max_players=options.max_players,
        whitelist=options.whitelist,
        progress=progress,
    )
    with paths.lock():
        config = paths.load()
        config.public_access = options.public_access
        paths.save(config)
    # No automatic op accounts: offline users cannot impersonate an administrator
    # before the owner has registered one.
    if not options.online_mode and options.loader in {"paper", "purpur"}:
        from .auth import install_authme

        progress("Установка AuthMe для режима без проверки аккаунтов")
        install_authme(paths)
    progress(
        "Настройка автозапуска Windows"
        if windows()
        else "Настройка systemd и автозапуска"
    )
    try:
        warning = deployment.enable_server(paths)
    except Error as exc:
        if not windows():
            raise
        warning = "Автозапуск Windows не настроен: " + str(exc)
    backups.create(paths)
    try:
        deployment.disable_legacy_service()
        migration_error = ""
    except (Error, OSError, subprocess.SubprocessError) as exc:
        migration_error = str(exc)
    if options.start:
        progress("Запуск и ожидание подтверждения готовности из логов")
        deployment.start_server(paths)
    report = deployment.configure(paths, progress)
    report["errors"].extend(dependency_errors)
    if not options.online_mode:
        # Shown once, briefly: offline mode trusts any nickname.
        report["warnings"].append(
            "online-mode=false: владение аккаунтом не проверяется, ник можно подделать. "
            "На публичном сервере включите whitelist"
            + (" (уже включён)." if options.whitelist else ": uwucraft whitelist on.")
        )
    if warning:
        report["errors"].append(warning)
    if migration_error:
        report["errors"].append(migration_error)
    report["name"] = options.name
    report["version"] = options.version
    import json

    from .config import atomic

    atomic(
        paths.data / "deployment.json", json.dumps(report, ensure_ascii=False, indent=2)
    )
    return report
