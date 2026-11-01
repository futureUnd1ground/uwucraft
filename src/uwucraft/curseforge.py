"""Official CurseForge API; distribution restrictions are never bypassed."""

import json
import os
import tempfile
import tomllib
from pathlib import Path

from .backups import require_stopped, safe_name
from .config import Error, Paths, atomic
from .content import inventory
from .platforms import windows
from .sources import download, metadata

API = "https://api.curseforge.com/v1"


def api(path, paths=None, **kwargs):
    paths = paths or Paths()
    key = os.environ.get("CURSEFORGE_API_KEY", "")
    file = paths.config / "secrets.toml"
    if not key and file.exists():
        if not windows() and file.stat().st_mode & 0o077:
            raise Error(
                "Секреты доступны другим пользователям. Выполните chmod 600 secrets.toml."
            )
        key = tomllib.loads(file.read_text(encoding="utf-8")).get(
            "curseforge_api_key", ""
        )
    if not key:
        raise Error(
            "CurseForge требует CURSEFORGE_API_KEY или curseforge_api_key в secrets.toml (chmod 600)."
        )
    return metadata(API + path, headers={"x-api-key": key}, **kwargs)["data"]


def install(project_id, trust_server=False, paths=None):
    paths = paths or Paths()
    if not trust_server:
        raise Error(
            "CurseForge API не гарантирует server-side совместимость. Проверьте описание и повторите с --trust-server."
        )
    config = paths.load()
    loaders = {"forge": 1, "fabric": 4, "neoforge": 6}
    if config.loader not in loaders:
        raise Error("CurseForge mods требуют Fabric/Forge/NeoForge.")
    records = inventory(paths)
    planned = {}

    def plan(identifier):
        identifier = int(identifier)
        if identifier in planned:
            return
        project = api(f"/mods/{identifier}", paths)
        files = api(
            f"/mods/{identifier}/files",
            paths,
            params={
                "gameVersion": config.version,
                "modLoaderType": loaders[config.loader],
                "pageSize": 50,
            },
        )
        files = [
            f
            for f in files
            if f["releaseType"] == 1 and config.version in f["gameVersions"]
        ]
        if not files:
            raise Error(
                "Нет совместимой стабильной версии CurseForge проекта "
                + str(identifier)
            )
        file = files[0]
        if not file.get("downloadUrl"):
            raise Error(
                "Автор запретил стороннее распространение: скачайте файл вручную с CurseForge."
            )
        safe_name(file["fileName"])
        if Path(file["fileName"]).name != file["fileName"] or not file[
            "fileName"
        ].endswith(".jar"):
            raise Error("Некорректное имя CurseForge файла.")
        planned[identifier] = (project, file)
        for dependency in file["dependencies"]:
            if dependency["relationType"] == 3:
                plan(dependency["modId"])
            if (
                dependency["relationType"] == 5
                and "cf:" + str(dependency["modId"]) in records
            ):
                raise Error("Конфликт CurseForge зависимости с установленным модом.")

    with paths.lock():
        require_stopped(paths)
        plan(project_id)
        with tempfile.TemporaryDirectory(dir=paths.data) as directory:
            directory = Path(directory)
            for identifier, (_, file) in planned.items():
                hashes = {
                    {1: "sha1", 2: "md5"}[h["algo"]]: h["value"]
                    for h in file["hashes"]
                    if h["algo"] in {1, 2}
                }
                download(file["downloadUrl"], directory / str(identifier), hashes)
                destination = paths.server / "mods" / file["fileName"]
                if destination.exists() and "cf:" + str(identifier) not in records:
                    raise Error("Файл уже существует.")
            for identifier, (project, file) in planned.items():
                key = "cf:" + str(identifier)
                destination = paths.server / "mods" / file["fileName"]
                old = records.get(key)
                destination.parent.mkdir(parents=True, exist_ok=True)
                (directory / str(identifier)).replace(destination)
                if old and old["path"] != str(destination.relative_to(paths.server)):
                    safe_name(old["path"])
                    (paths.server / old["path"]).unlink(missing_ok=True)
                records[key] = {
                    "title": project["name"],
                    "project_id": str(identifier),
                    "version_id": str(file["id"]),
                    "kind": "mod",
                    "source": "curseforge",
                    "path": str(destination.relative_to(paths.server)),
                }
            atomic(
                paths.config / "content.json",
                json.dumps(records, ensure_ascii=False, indent=2),
            )
    return [p["name"] for p, _ in planned.values()]
