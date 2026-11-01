"""Official Via plugins, installed together without changing a world's version."""

import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from . import backups, server
from .config import Error, Paths, atomic
from .sources import download, metadata


def validate(paths):
    if paths.load().loader not in {"paper", "purpur"}:
        raise Error(
            "Разные клиенты через Via требуют Paper/Purpur. Forge/Fabric-модпаки требуют совместимые версии клиента и модов."
        )
    result = subprocess.run(
        [paths.load().java, "-version"], capture_output=True, text=True, timeout=15
    )
    match = re.search(r'version "(?:1\.)?(\d+)', result.stdout + result.stderr)
    if result.returncode or not match or int(match[1]) < 17:
        raise Error(
            "Официальные текущие ViaVersion/ViaBackwards требуют Java 17+. Java старого ядра автоматически не меняется: создайте отдельный современный Paper/Purpur профиль."
        )


def install(paths=None, legacy=False):
    paths = paths or Paths()
    validate(paths)
    backups.require_stopped(paths)
    projects = ["ViaVersion", "ViaBackwards"] + (["ViaRewind"] if legacy else [])
    with paths.lock(), tempfile.TemporaryDirectory(dir=paths.data) as directory:
        backups.require_stopped(paths)
        stage = Path(directory)
        prepared = []
        for project in projects:
            release = metadata(
                f"https://api.github.com/repos/ViaVersion/{project}/releases/latest"
            )
            item = next(
                (
                    a
                    for a in release["assets"]
                    if a["name"].startswith(project + "-")
                    and a["name"].endswith(".jar")
                    and not a["name"].endswith(("-sources.jar", "-javadoc.jar"))
                ),
                None,
            )
            if not item or Path(item["name"]).name != item["name"]:
                raise Error("Нет официального Bukkit JAR для " + project)
            digest = item.get("digest") or ""
            if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
                raise Error("Нет SHA-256 для официального релиза " + project)
            source = stage / item["name"]
            download(
                item["browser_download_url"],
                source,
                {"sha256": digest.split(":", 1)[1]},
            )
            try:
                with zipfile.ZipFile(source) as archive:
                    if "plugin.yml" not in archive.namelist():
                        raise Error(
                            "Официальный JAR не содержит Bukkit plugin.yml: " + project
                        )
            except zipfile.BadZipFile as exc:
                raise Error("Повреждён JAR " + project) from exc
            prepared.append((project, release["tag_name"], source))
        folder = paths.server / "plugins"
        folder.mkdir(parents=True, exist_ok=True)
        old = list({p for project in projects for p in folder.glob(project + "-*.jar")})
        rollback = stage / "rollback"
        rollback.mkdir()
        for file in old:
            shutil.copy2(file, rollback / file.name)
        installed = []
        try:
            for _, _, source in prepared:
                destination = folder / source.name
                source.replace(destination)
                installed.append(destination)
            for file in old:
                if file not in installed:
                    file.unlink()
            atomic(
                paths.config / "compat.json",
                json.dumps(
                    {
                        "plugins": {
                            project: version for project, version, _ in prepared
                        },
                        "legacy_clients": legacy,
                    },
                    ensure_ascii=False,
                ),
            )
        except OSError:
            for file in installed:
                file.unlink(missing_ok=True)
            for file in rollback.iterdir():
                shutil.copy2(file, folder / file.name)
            raise
    return (
        " + ".join(projects)
        + " установлены. Таблица протоколов зависит от релиза плагинов; версия мира не меняется. Модовые клиенты требуют свои моды."
    )


def setup(paths=None, restart=False, legacy=False):
    paths = paths or Paths()
    validate(paths)  # reject unsupported cores BEFORE disconnecting players
    running = server.status(paths)["status"] in {"Running", "Starting"}
    if running and not restart:
        raise Error(
            "Сначала остановите сервер либо укажите compat-setup --restart (игроки будут отключены)."
        )
    if running:
        server.manage("stop", paths)
    try:
        backups.create(paths)
        result = install(paths, legacy)
    finally:
        if running:
            server.manage("start", paths)
            server.wait_ready(paths)
    return result
