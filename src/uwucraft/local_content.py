"""Transactional local file drops shared by the TUI and CLI."""

import copy
import hashlib
import json
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import content
from .backups import MAX_UNPACKED, extract_zip, require_stopped, safe_name
from .config import Error, Paths, atomic


def dropped_paths(text):
    """Decode paths, not shell commands: Windows quotes, POSIX escapes, file URIs."""
    if len(text) > 65536 or "\x00" in text:
        raise Error("Слишком большой список файлов.")
    text = text.strip()
    if not text:
        raise Error("Перетащите файлы или вставьте их пути.")
    if Path(text).is_file():
        return [Path(text)]
    lines = text.splitlines()
    tokens = (
        lines
        if len(lines) > 1 and all(Path(x).is_file() for x in lines)
        else re.findall(r""""[^"]*"|'(?:[^']|'')*'|(?:\\[\s'"()]|[^\s])+""", text)
    )
    result = []
    for token in tokens:
        token = token.strip()
        if token == "&":
            continue
        if token[:1] in {"'", '"'} and token[-1:] == token[:1]:
            token = token[1:-1].replace("''", "'")
        elif not re.match(r"^[A-Za-z]:[\\/]", token):
            token = re.sub(r"""\\([\s'"()])""", r"\1", token)
        if token.startswith("file:"):
            uri = urlsplit(token)
            if uri.netloc not in {"", "localhost"} or uri.query or uri.fragment:
                raise Error("Поддерживаются только локальные file:// пути.")
            token = unquote(uri.path)
            if re.match(r"^/[A-Za-z]:/", token):
                token = token[1:]
        path = Path(token).expanduser()
        if not path.is_file():
            raise Error(f"Файл не найден: {path}")
        if path not in result:
            result.append(path)
        if len(result) > 256:
            raise Error("За одну установку допускается до 256 файлов.")
    if not result:
        raise Error("Файлы не выбраны.")
    return result


def inspect_file(source, paths=None):
    paths = paths or Paths()
    config = paths.load()
    source = Path(source)
    safe_name(source.name)
    if source.suffix.lower() not in {".jar", ".zip", ".mrpack"}:
        raise Error(f"{source.name}: поддерживаются .jar, .zip, .mrpack.")
    try:
        with zipfile.ZipFile(source) as archive:
            items = archive.infolist()
            if len(items) > 100000 or sum(i.file_size for i in items) > MAX_UNPACKED:
                raise Error("Архив слишком большой.")
            for item in items:
                if item.flag_bits & 1:
                    raise Error("Зашифрованные архивы не поддерживаются.")
                safe_name(item.filename)
                if stat.S_ISLNK(item.external_attr >> 16):
                    raise Error("Ссылки в архиве запрещены.")
            names = {i.filename for i in items if not i.is_dir()}
            if len({n.casefold() for n in names}) != sum(not i.is_dir() for i in items):
                raise Error("Архив содержит повторяющиеся имена.")
            root = ""
            if source.suffix.lower() != ".jar" and names:
                first = {n.split("/", 1)[0] for n in names}
                if len(first) == 1 and all("/" in n for n in names):
                    candidate = next(iter(first)) + "/"
                    nested = {n[len(candidate) :] for n in names}
                    if {
                        "pack.mcmeta",
                        "level.dat",
                        "modrinth.index.json",
                    } & nested or any(
                        n.startswith(
                            (
                                "mods/",
                                "plugins/",
                                "config/",
                                "defaultconfigs/",
                                "kubejs/",
                                "scripts/",
                            )
                        )
                        for n in nested
                    ):
                        root, names = candidate, nested
            if "modrinth.index.json" in names:
                kind = "pack"
            elif source.suffix.lower() == ".jar":
                if "plugin.yml" in names or "paper-plugin.yml" in names:
                    kind = "plugin"
                    if config.loader not in {"paper", "purpur"}:
                        raise Error("Плагин требует Paper/Purpur.")
                else:
                    kind = "mod"
                    loaders = set()
                    if "fabric.mod.json" in names:
                        loaders.add("fabric")
                        if archive.getinfo("fabric.mod.json").file_size > 1024**2:
                            raise Error("Слишком большой manifest.")
                        manifest = json.loads(archive.read("fabric.mod.json"))
                        if not isinstance(manifest, dict):
                            raise Error("fabric.mod.json должен содержать JSON-объект.")
                        if manifest.get("environment") == "client":
                            raise Error("Мод предназначен только для клиента.")
                    if "META-INF/mods.toml" in names or "mcmod.info" in names:
                        loaders.add("forge")
                    if "META-INF/neoforge.mods.toml" in names:
                        loaders.add("neoforge")
                    if config.loader not in loaders:
                        raise Error("JAR не распознан как мод для выбранного ядра.")
            elif "pack.mcmeta" in names:
                data = any(n.startswith("data/") for n in names)
                assets = any(n.startswith("assets/") for n in names)
                if data == assets:
                    raise Error("Нельзя однозначно отличить datapack от resourcepack.")
                kind = "datapack" if data else "resourcepack"
            elif "level.dat" in names:
                kind = "world"
            elif any(
                n.startswith(
                    (
                        "mods/",
                        "plugins/",
                        "config/",
                        "defaultconfigs/",
                        "kubejs/",
                        "scripts/",
                    )
                )
                for n in names
            ):
                kind = "serverpack"
            else:
                raise Error(
                    "Неизвестная структура ZIP: нужны pack.mcmeta, level.dat, modrinth.index.json или каталоги mods/config/plugins."
                )
    except (
        OSError,
        zipfile.BadZipFile,
        ValueError,
        KeyError,
        RuntimeError,
        TypeError,
    ) as exc:
        raise Error(f"{source.name}: повреждённый архив или manifest.") from exc
    if source.suffix.lower() == ".mrpack" and kind != "pack":
        raise Error("В MRPACK отсутствует modrinth.index.json.")
    return {"source": source, "kind": kind, "name": source.name, "root": root}


def plan(sources, paths=None):
    paths = paths or Paths()
    if not paths.load().version:
        raise Error("Сначала создайте сервер.")
    sources = list(sources)
    if not 1 <= len(sources) <= 256:
        raise Error("Выберите от 1 до 256 файлов.")
    result = []
    for source in sources:
        try:
            result.append(inspect_file(source, paths))
        except Error as exc:
            raise Error(f"{Path(source).name}: {exc}") from exc
    return result


def install_many(sources, trust_files=False, paths=None):
    try:
        return _install_many(sources, trust_files, paths)
    except (zipfile.BadZipFile, KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise Error(
            "Повреждённый или неподдерживаемый архив/manifest: " + str(exc)
        ) from exc


def _install_many(sources, trust_files=False, paths=None):
    paths = paths or Paths()
    if not trust_files:
        raise Error(
            "Подтвердите доверие локальным файлам: --trust-files. JAR выполняет код; совместимость и зависимости проверяет оператор."
        )
    with paths.lock():
        require_stopped(paths)
        planned = plan(sources, paths)
        with tempfile.TemporaryDirectory(dir=paths.data) as temporary:
            stage = copy.copy(paths)
            stage.data = Path(temporary) / "data"
            stage.config = Path(temporary) / "config"
            stage.server = Path(temporary) / "server"
            stage.file = stage.config / "config.toml"
            stage.state = stage.data / "state.json"
            stage.data.mkdir()
            stage.save(paths.load())
            records = content.inventory(paths)
            additions = {}
            for index, item in enumerate(planned):
                source, kind = item["source"], item["kind"]
                if item["root"]:
                    normalized = Path(temporary) / f"normalized-{index}" / source.name
                    normalized.parent.mkdir()
                    with (
                        zipfile.ZipFile(source) as original,
                        zipfile.ZipFile(
                            normalized, "w", compression=zipfile.ZIP_DEFLATED
                        ) as output,
                    ):
                        for entry in original.infolist():
                            if entry.is_dir():
                                continue
                            with (
                                original.open(entry) as incoming,
                                output.open(
                                    entry.filename[len(item["root"]) :], "w"
                                ) as outgoing,
                            ):
                                shutil.copyfileobj(incoming, outgoing)
                    source = normalized
                before = {
                    f.relative_to(stage.server)
                    for f in stage.server.rglob("*")
                    if f.is_file()
                }
                if kind == "pack":
                    content.pack(str(source), stage)
                elif kind in content.KINDS:
                    root = content.KINDS[kind]
                    if kind == "datapack":
                        properties = paths.server / "server.properties"
                        if properties.exists():
                            for line in properties.read_text(
                                encoding="utf-8"
                            ).splitlines():
                                if line.startswith("level-name="):
                                    world = line.split("=", 1)[1]
                                    safe_name(world)
                                    if not world or world == ".":
                                        raise Error(
                                            "Некорректный level-name для datapack."
                                        )
                                    root = str(Path(world) / "datapacks")
                    relative = Path(root) / (source.stem + source.suffix.lower())
                    target = stage.server / relative
                    if target.exists():
                        raise Error(f"Повторяющееся имя: {relative}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                else:
                    extracted = Path(temporary) / ("unpack-" + str(index))
                    extract_zip(source, extracted)
                    for file in extracted.rglob("*"):
                        if not file.is_file():
                            continue
                        relative = file.relative_to(extracted)
                        if kind == "world":
                            safe_name(source.stem)
                            relative = Path("worlds") / source.stem / relative
                        else:
                            allowed = {
                                "mods",
                                "plugins",
                                "config",
                                "defaultconfigs",
                                "kubejs",
                                "scripts",
                                "resourcepacks",
                            }
                            if (
                                relative.parts[0] not in allowed
                                or len(relative.parts) < 2
                            ):
                                raise Error(
                                    f"Server pack пытается изменить запрещённый путь: {relative}"
                                )
                            if relative.parts[0] in {"mods", "plugins"}:
                                detected = inspect_file(file, paths)["kind"]
                                if detected != (
                                    "mod" if relative.parts[0] == "mods" else "plugin"
                                ):
                                    raise Error(
                                        "Неверный тип файла внутри server pack."
                                    )
                        target = stage.server / relative
                        if target.exists():
                            raise Error(f"Повторяющееся имя: {relative}")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(file, target)
                key = "local:" + source.name
                if key in records or key in additions:
                    raise Error(f"Уже установлен файл: {source.name}")
                installed = sorted(
                    f.relative_to(stage.server).as_posix()
                    for f in stage.server.rglob("*")
                    if f.is_file() and f.relative_to(stage.server) not in before
                )
                if not installed:
                    raise Error(f"{source.name}: нет серверных файлов.")
                additions[key] = {
                    "title": source.name,
                    "kind": kind,
                    "source": "import",
                    "version_id": "local/unverified",
                    "path": installed[0],
                    "paths": installed,
                }
            files = sorted(f for f in stage.server.rglob("*") if f.is_file())
            if not files:
                raise Error("В выбранных паках нет серверных файлов.")
            total = sum(f.stat().st_size for f in files)
            if total > MAX_UNPACKED:
                raise Error("Общий размер установки превышает 8 GiB.")
            for file in files:
                relative = file.relative_to(stage.server)
                destination = paths.server / relative
                if destination.exists() or destination.is_symlink():
                    raise Error(f"Существующий файл сохранён: {relative}")
                if not destination.resolve().is_relative_to(paths.server.resolve()):
                    raise Error(
                        "Путь установки выходит за каталог сервера через ссылку."
                    )
            for record in additions.values():
                record["hashes"] = {}
                for relative in record["paths"]:
                    with (stage.server / relative).open("rb") as stream:
                        record["hashes"][relative] = hashlib.file_digest(
                            stream, "sha256"
                        ).hexdigest()
            committed = []
            try:
                for file in files:
                    destination = paths.server / file.relative_to(stage.server)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    # Exclusive create: even an external writer cannot be overwritten.
                    with (
                        destination.open("xb") as output,
                        file.open("rb") as input_file,
                    ):
                        committed.append(destination)
                        shutil.copyfileobj(input_file, output)
                atomic(
                    paths.config / "content.json",
                    json.dumps({**records, **additions}, ensure_ascii=False, indent=2),
                )
            except BaseException:
                for destination in reversed(committed):
                    destination.unlink(missing_ok=True)
                raise
    return [f"{item['name']} → {item['kind']}" for item in planned]
