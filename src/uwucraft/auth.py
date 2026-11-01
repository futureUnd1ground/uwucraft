"""Opt-in AuthMe for offline-mode Paper/Purpur servers."""

import json

from .backups import require_stopped
from .config import Error, Paths, atomic
from .sources import download, metadata


def install_authme(paths=None):
    paths = paths or Paths()
    require_stopped(paths)
    if paths.load().loader not in {"paper", "purpur"}:
        raise Error(
            "AuthMe требует Paper/Purpur. Для модовых ядер установите подходящий auth-мод вручную."
        )
    release = metadata(
        "https://api.github.com/repos/AuthMe/AuthMeReloaded/releases/latest"
    )
    asset = next(
        (a for a in release["assets"] if a["name"].endswith("-Paper.jar")), None
    )
    if asset is None:
        raise Error("В релизе AuthMe нет Paper asset.")
    digest = asset.get("digest") or ""
    if not digest.startswith("sha256:"):
        raise Error(
            "GitHub не предоставил SHA-256; автоматическая установка AuthMe отклонена."
        )
    with paths.lock():
        require_stopped(paths)
        destination = paths.server / "plugins" / asset["name"]
        download(
            asset["browser_download_url"],
            destination,
            {"sha256": digest.split(":", 1)[1]},
        )
        for old in destination.parent.glob("AuthMe-*-Paper.jar"):
            if old != destination:
                old.unlink()
        atomic(
            paths.config / "auth.json",
            json.dumps(
                {
                    "plugin": "AuthMe",
                    "version": release["tag_name"],
                    "file": asset["name"],
                }
            ),
        )
    return (
        "AuthMe "
        + release["tag_name"]
        + " установлен. Первый вход: /register <пароль> <пароль>. Не выдавайте op до регистрации администратора."
    )


def reserve_admin(name, paths=None):
    """Register an offline administrator before exposing the server.

    A generated credential is kept in a private file, never returned or logged.
    Existing registered accounts are never modified.
    """
    import re
    import secrets
    import sqlite3
    import time

    from .server import command, status

    paths = paths or Paths()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", name):
        raise Error("Некорректное имя Minecraft.")
    if status(paths)["status"] != "Running":
        raise Error("Сначала запустите сервер и дождитесь Running.")
    database = paths.server / "plugins/AuthMe/authme.db"
    if not database.exists():
        raise Error("AuthMe SQLite database не найдена; проверьте загрузку плагина.")

    def registered():
        with sqlite3.connect(
            f"file:{database}?mode=ro", uri=True, timeout=3
        ) as connection:
            return bool(
                connection.execute(
                    "SELECT 1 FROM authme WHERE username=? AND password<>?",
                    (name.lower(), ""),
                ).fetchone()
            )

    if registered():
        return "Администратор уже зарегистрирован; пароль не изменён."
    password = secrets.token_urlsafe(16)
    credential = paths.config / "admin-login.txt"
    atomic(
        credential,
        f"Username: {name}\nPassword: {password}\nUse /login in Minecraft. Keep this file private.\n",
    )
    command(f"authme register {name} {password}", paths)
    for _ in range(60):
        if registered():
            # Refuse to leave a plaintext credential in any managed log.
            for file in [
                paths.server / "logs/latest.log",
                paths.data / "console.log",
                paths.server / "plugins/AuthMe/authme.log",
            ]:
                if file.exists() and password in file.read_text(errors="replace"):
                    atomic(
                        file,
                        file.read_text(errors="replace").replace(
                            password, "[REDACTED]"
                        ),
                    )
            return (
                "Администратор зарегистрирован. Пароль: " + str(credential) + " (0600)."
            )
        time.sleep(0.5)
    raise Error(
        "AuthMe не подтвердил регистрацию. Проверьте лог; пароль сохранён локально, повторная команда не отправлена."
    )
