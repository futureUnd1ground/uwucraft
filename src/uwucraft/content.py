"""Modrinth content, required dependencies, local audited files and mrpacks."""

import json
import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote

from .backups import extract_zip, require_stopped, safe_name
from .config import Error, Paths, atomic
from .sources import download, metadata

API = "https://api.modrinth.com/v2"
KINDS = {
    "mod": "mods",
    "plugin": "plugins",
    "datapack": "world/datapacks",
    "resourcepack": "resourcepacks",
}


def inventory(paths=None):
    paths = paths or Paths()
    file = paths.config / "content.json"
    return json.loads(file.read_text(encoding="utf-8")) if file.exists() else {}


def install(source, kind="mod", paths=None):
    paths = paths or Paths()
    with paths.lock():
        require_stopped(paths)
        config = paths.load()
        if not config.version:
            raise Error("Сначала установите сервер.")
        if kind == "mod" and config.loader not in {"fabric", "forge", "neoforge"}:
            raise Error("Для модов требуется Fabric, Forge или NeoForge.")
        if kind == "plugin" and config.loader not in {"paper", "purpur"}:
            raise Error("Плагины требуют Paper или Purpur.")
        if kind not in KINDS:
            raise Error("Неизвестный тип контента.")
        if Path(source).is_file() or source.startswith("https://"):
            raise Error(
                "Для непроверенного локального/HTTPS файла используйте import-file --trust-file; совместимость подтверждает оператор."
            )
        records = inventory(paths)
        visited = set()
        planned = []

        def plan(project_id=None, version_id=None, requested_kind=kind):
            identity = version_id or project_id
            if identity in visited:
                return
            visited.add(identity)
            if version_id:
                version = metadata(f"{API}/version/{quote(version_id, safe='')}")
                project_id = version["project_id"]
            project = metadata(f"{API}/project/{quote(project_id, safe='')}")
            if requested_kind == "mod" and project.get("server_side") == "unsupported":
                raise Error(f"{project['title']} — только клиентский мод.")
            loader = (
                "paper"
                if requested_kind == "plugin"
                else "datapack"
                if requested_kind == "datapack"
                else config.loader
            )
            if not version_id:
                versions = metadata(
                    f"{API}/project/{project['id']}/version",
                    params={
                        "game_versions": json.dumps([config.version]),
                        "loaders": json.dumps([loader]),
                    },
                )
                if not versions:
                    raise Error(f"Нет совместимой версии {project['title']}.")
                version = versions[0]
            if (
                config.version not in version["game_versions"]
                or loader not in version["loaders"]
            ):
                raise Error(f"Несовместимая зависимость {project['title']}.")
            for dependency in version["dependencies"]:
                if dependency["dependency_type"] == "required":
                    if not dependency.get("project_id") and not dependency.get(
                        "version_id"
                    ):
                        raise Error("Зависимость без ID: требуется ручная проверка.")
                    plan(
                        dependency.get("project_id"),
                        dependency.get("version_id"),
                        requested_kind,
                    )
                if dependency["dependency_type"] == "incompatible" and dependency.get(
                    "project_id"
                ) in [r.get("project_id") for r in records.values()]:
                    raise Error("Конфликт с установленным контентом.")
            item = next(
                (f for f in version["files"] if f.get("primary")), version["files"][0]
            )
            safe_name(item["filename"])
            if Path(item["filename"]).name != item["filename"]:
                raise Error("Некорректное имя файла.")
            planned.append((project, version, item, requested_kind))

        plan(source)
        with tempfile.TemporaryDirectory(dir=paths.data) as temporary:
            temporary = Path(temporary)
            for index, (_, _, file, _) in enumerate(planned):
                download(file["url"], temporary / str(index), file["hashes"])
            # Validate every destination before committing any downloaded file.
            destinations = set()
            for project, _, file, requested_kind in planned:
                destination = paths.server / KINDS[requested_kind] / file["filename"]
                if destination in destinations:
                    raise Error("Конфликт имён файлов зависимостей.")
                destinations.add(destination)
                if destination.exists() and project["slug"] not in records:
                    raise Error("Файл уже существует и не управляется uwucraft.")
            for index, (project, version, file, requested_kind) in enumerate(planned):
                key = project["slug"]
                destination = paths.server / KINDS[requested_kind] / file["filename"]
                old = records.get(key)
                if destination.exists() and not old:
                    raise Error("Файл уже существует и не управляется uwucraft.")
                destination.parent.mkdir(parents=True, exist_ok=True)
                (temporary / str(index)).replace(destination)
                if old and old["path"] != str(destination.relative_to(paths.server)):
                    safe_name(old["path"])
                    (paths.server / old["path"]).unlink(missing_ok=True)
                records[key] = {
                    "project_id": project["id"],
                    "version_id": version["id"],
                    "kind": requested_kind,
                    "path": str(destination.relative_to(paths.server)),
                    "title": project["title"],
                    "dependencies": [
                        d["project_id"]
                        for d in version["dependencies"]
                        if d["dependency_type"] == "required" and d.get("project_id")
                    ],
                }
            atomic(
                paths.config / "content.json",
                json.dumps(records, ensure_ascii=False, indent=2),
            )
    return [p[0]["title"] for p in planned]


def remove(slug, paths=None):
    paths = paths or Paths()
    with paths.lock():
        require_stopped(paths)
        records = inventory(paths)
        if slug not in records:
            raise Error("Контент не найден.")
        dependants = [
            name
            for name, record in records.items()
            if name != slug
            and records[slug].get("project_id") in record.get("dependencies", [])
        ]
        if dependants:
            raise Error(
                "Мод требуется: "
                + ", ".join(dependants)
                + ". Сначала удалите зависимый контент."
            )
        record = records[slug]
        if record["kind"] == "world":
            raise Error(
                "Удаление импортированного мира через контент запрещено; сначала сохраните backup."
            )
        files = record.get("paths") or [record["path"]]
        targets = []
        for relative in files:
            safe_name(relative)
            target = paths.server / relative
            if not target.resolve().is_relative_to(paths.server.resolve()):
                raise Error("Путь контента выходит за каталог сервера.")
            expected = record.get("hashes", {}).get(relative)
            if expected and target.exists():
                import hashlib

                with target.open("rb") as stream:
                    actual = hashlib.file_digest(stream, "sha256").hexdigest()
                if actual != expected:
                    raise Error("Файл изменён после импорта и сохранён: " + relative)
            targets.append(target)
        for target in targets:
            target.unlink(missing_ok=True)
        del records[slug]
        atomic(
            paths.config / "content.json",
            json.dumps(records, ensure_ascii=False, indent=2),
        )


def import_file(source, kind, trust_file, paths=None):
    paths = paths or Paths()
    if not trust_file:
        raise Error(
            "Совместимость локальных файлов нельзя доказать: нужен --trust-file после ручной проверки."
        )
    if kind not in KINDS:
        raise Error("Неизвестный тип контента.")
    with paths.lock():
        require_stopped(paths)
        name = Path(source.split("?")[0]).name
        if Path(name).suffix not in {".jar", ".zip"}:
            raise Error("Ожидается .jar или .zip.")
        target = paths.server / KINDS[kind] / name
        if target.exists():
            raise Error("Файл уже существует.")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.startswith("https://"):
            download(source, target)
        else:
            atomic(target, Path(source).read_bytes())
        records = inventory(paths)
        records["local:" + name] = {
            "title": name,
            "kind": kind,
            "version_id": "local/unverified",
            "source": "import",
            "path": str(target.relative_to(paths.server)),
        }
        atomic(
            paths.config / "content.json",
            json.dumps(records, ensure_ascii=False, indent=2),
        )
        return target


def pack(source, paths=None):
    paths = paths or Paths()
    require_stopped(paths)
    config = paths.load()
    with paths.lock(), tempfile.TemporaryDirectory(dir=paths.data) as directory:
        temporary = Path(directory)
        archive = Path(source)
        if not archive.is_file():
            project = metadata(f"{API}/project/{quote(source, safe='')}")
            versions = metadata(
                f"{API}/project/{project['id']}/version",
                params={
                    "game_versions": json.dumps([config.version]),
                    "loaders": json.dumps([config.loader]),
                },
            )
            if project["project_type"] != "modpack" or not versions:
                raise Error("Совместимый модпак не найден.")
            item = next(
                f for f in versions[0]["files"] if f["filename"].endswith(".mrpack")
            )
            archive = temporary / "pack.mrpack"
            download(item["url"], archive, item["hashes"])
        extracted = temporary / "unpacked"
        extract_zip(archive, extracted)
        index = json.loads(
            (extracted / "modrinth.index.json").read_text(encoding="utf-8")
        )
        deps = index["dependencies"]
        mapping = {"fabric": "fabric-loader", "forge": "forge", "neoforge": "neoforge"}
        if (
            deps.get("minecraft") != config.version
            or mapping.get(config.loader) not in deps
        ):
            raise Error("Модпак не соответствует версии Minecraft и загрузчику.")
        staging = temporary / "files"
        staging.mkdir()
        for item in index["files"]:
            safe_name(item["path"])
            if item.get("env", {}).get("server") == "unsupported":
                continue
            if not item["path"].startswith(("mods/", "config/", "resourcepacks/")):
                raise Error("Модпак пытается изменить файл вне разрешённых каталогов.")
            url = item["downloads"][0]
            if not url.startswith("https://cdn.modrinth.com/"):
                raise Error(
                    "Модпак содержит внешний источник; требуется ручная проверка."
                )
            download(url, staging / item["path"], item["hashes"])
        for override in ["overrides", "server-overrides"]:
            folder = extracted / override
            if folder.exists():
                for file in folder.rglob("*"):
                    relative = file.relative_to(folder)
                    if relative.parts[0] not in {"mods", "config", "resourcepacks"}:
                        raise Error("Override вне разрешённых каталогов.")
                    if file.is_file():
                        target = staging / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(file, target)
        files = [f for f in staging.rglob("*") if f.is_file()]
        if any((paths.server / f.relative_to(staging)).exists() for f in files):
            raise Error(
                "Модпак перезаписал бы существующие файлы; применяйте к чистой установке."
            )
        for file in files:
            destination = paths.server / file.relative_to(staging)
            destination.parent.mkdir(parents=True, exist_ok=True)
            file.replace(destination)
        atomic(
            paths.config / "pack.json",
            json.dumps({"name": index["name"], "version": index["versionId"]}),
        )
        return index["name"]


def search(query, kind="mod", paths=None):
    paths = paths or Paths()
    config = paths.load()
    loader = "paper" if kind == "plugin" else config.loader
    facets = [
        [f"versions:{config.version}"],
        [f"categories:{loader}"],
        ["project_type:modpack" if kind == "pack" else "project_type:mod"],
    ]
    hits = metadata(
        API + "/search",
        params={"query": query, "facets": json.dumps(facets), "limit": 12},
    )["hits"]
    return (
        "\n".join(
            f"{item['slug']} — {item['title']}\n  {item['description'][:120]}"
            for item in hits
        )
        or "Ничего совместимого не найдено."
    )
