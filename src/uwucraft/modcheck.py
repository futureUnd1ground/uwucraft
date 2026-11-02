"""Conservative metadata checks, never a substitute for the loader's resolver."""

import io
import json
import re
import tomllib
import zipfile

from .backups import safe_name
from .config import Error, Paths


def numeric(value):
    value = str(value).split("+", 1)[0]
    if len(value) > 256:
        return None
    return (
        tuple(int(n) for n in value.split("."))
        if re.fullmatch(r"\d+(?:\.\d+)*", value)
        else None
    )


def matches(version, requirement):
    """True/False for understood ranges, None when loader semantics are needed."""
    if isinstance(requirement, list):
        results = [matches(version, r) for r in requirement]
        return True if True in results else None if None in results else False
    requirement = str(requirement).strip()
    if requirement in {"", "*"}:
        return True
    actual = numeric(version)
    if actual is None:
        return None

    def compare(other):
        wanted = numeric(other)
        if wanted is None:
            return None
        width = max(len(actual), len(wanted))
        a, b = (
            actual + (0,) * (width - len(actual)),
            wanted + (0,) * (width - len(wanted)),
        )
        return (a > b) - (a < b)

    exact = re.fullmatch(r"\[([^,\[\]]+)\]", requirement)
    if exact:
        result = compare(exact[1])
        return None if result is None else result == 0
    interval = re.fullmatch(r"([\[(])([^,]*),([^,]*)([\])])", requirement)
    if interval:
        lower, upper = interval[2].strip(), interval[3].strip()
        lo, hi = compare(lower) if lower else 1, compare(upper) if upper else -1
        if lo is None or hi is None:
            return None
        return (lo >= 0 if interval[1] == "[" else lo > 0) and (
            hi <= 0 if interval[4] == "]" else hi < 0
        )
    if " " in requirement:
        values = [matches(version, part) for part in requirement.split()]
        return False if False in values else None if None in values else True
    operator = re.fullmatch(r"(>=|<=|>|<|=)?(\d+(?:\.\d+)*(?:\.[xX*])?)", requirement)
    if operator:
        expected = operator[2]
        if expected.endswith((".x", ".X", ".*")):
            prefix = numeric(expected.rsplit(".", 1)[0])
            return actual[: len(prefix)] == prefix
        result = compare(expected)
        return {
            ">=": result >= 0,
            "<=": result <= 0,
            ">": result > 0,
            "<": result < 0,
            "=": result == 0,
            None: result == 0,
        }[operator[1]]
    return None


def read_mods(file, loader, depth=0, budget=None):
    budget = budget if budget is not None else [128 * 1024**2]
    if depth > 4:
        raise Error("Слишком глубокие вложенные JAR.")
    result = []
    try:
        with zipfile.ZipFile(file) as archive:
            names = set(archive.namelist())

            def read(name):
                if archive.getinfo(name).file_size > 2 * 1024**2:
                    raise Error("Слишком большой manifest мода.")
                return archive.read(name).decode("utf-8-sig")

            nested = []
            if loader == "fabric" and "fabric.mod.json" in names:
                data = json.loads(read("fabric.mod.json"))
                result.append(
                    {
                        "id": data["id"],
                        "version": str(data.get("version", "?")),
                        "depends": data.get("depends", {}),
                        "breaks": data.get("breaks", {}),
                        "client_only": data.get("environment") == "client",
                        "provides": data.get("provides", []),
                        "scheme": "fabric",
                    }
                )
                nested = [j["file"] for j in data.get("jars", [])]
            elif loader in {"forge", "neoforge"}:
                name = (
                    "META-INF/neoforge.mods.toml"
                    if "META-INF/neoforge.mods.toml" in names
                    else "META-INF/mods.toml"
                )
                if name in names:
                    data = tomllib.loads(read(name))
                    implementation = "?"
                    if "META-INF/MANIFEST.MF" in names:
                        match = re.search(
                            r"(?m)^Implementation-Version:\s*(\S+)",
                            read("META-INF/MANIFEST.MF"),
                        )
                        if match:
                            implementation = match[1]
                    for mod in data.get("mods", []):
                        dependencies = data.get("dependencies", {}).get(
                            mod["modId"], []
                        )
                        requires, breaks = {}, {}
                        for dep in dependencies:
                            if dep.get("side", "BOTH") == "CLIENT":
                                continue
                            if dep.get("mandatory", dep.get("type") == "required"):
                                requires[dep["modId"]] = dep.get("versionRange", "*")
                            if dep.get("type") == "incompatible":
                                breaks[dep["modId"]] = dep.get("versionRange", "*")
                        result.append(
                            {
                                "id": mod["modId"],
                                "version": str(mod.get("version", "?")).replace(
                                    "${file.jarVersion}", implementation
                                ),
                                "depends": requires,
                                "breaks": breaks,
                                "client_only": False,
                                "provides": [],
                                "scheme": "maven",
                            }
                        )
                if "META-INF/jarjar/metadata.json" in names:
                    nested = [
                        j["path"]
                        for j in json.loads(read("META-INF/jarjar/metadata.json")).get(
                            "jars", []
                        )
                    ]
            for name in nested:
                safe_name(name)
                size = archive.getinfo(name).file_size
                budget[0] -= size
                if size > 64 * 1024**2 or budget[0] < 0:
                    raise Error("Слишком большой набор вложенных JAR.")
                result.extend(
                    read_mods(io.BytesIO(archive.read(name)), loader, depth + 1, budget)
                )
    except (
        OSError,
        zipfile.BadZipFile,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RuntimeError,
    ) as exc:
        raise Error("Не удалось прочитать metadata мода: " + str(exc)) from exc
    for item in result:
        item.setdefault("nested", depth > 0)
        if not isinstance(item.get("id"), str) or not re.fullmatch(
            r"[a-z][a-z0-9_-]*", item["id"]
        ):
            raise Error("Некорректный mod ID в manifest.")
        if not isinstance(item["depends"], dict) or not isinstance(
            item["breaks"], dict
        ):
            raise Error("Некорректные зависимости в manifest.")
        if not isinstance(item["provides"], list) or any(
            not isinstance(alias, str) for alias in item["provides"]
        ):
            raise Error("Некорректные provides в manifest.")
        if (
            len(item["id"]) > 128
            or len(item["version"]) > 256
            or any(
                len(str(r)) > 4096
                for r in [*item["depends"].values(), *item["breaks"].values()]
            )
        ):
            raise Error("Слишком большие поля metadata.")
    return result


def loader_versions(paths):
    config = paths.load()
    result = {"minecraft": config.version}
    for loader, folder in [
        ("forge", "net/minecraftforge/forge"),
        ("neoforge", "net/neoforged/neoforge"),
        ("fabricloader", "net/fabricmc/fabric-loader"),
    ]:
        root = paths.server / "libraries" / folder
        versions = (
            [p.name for p in root.iterdir() if p.is_dir()] if root.exists() else []
        )
        if len(versions) == 1:
            value = versions[0]
            result[loader] = (
                value[len(config.version) + 1 :]
                if value.startswith(config.version + "-")
                else value
            )
    return result


def audit(paths=None, extra=()):
    paths = paths or Paths()
    config = paths.load()
    report = {"errors": [], "warnings": [], "mods": []}
    if config.loader not in {"fabric", "forge", "neoforge"}:
        return report
    installed = list((paths.server / "mods").glob("*.jar"))
    files = installed + list(extra)
    available = loader_versions(paths)
    owners = {}
    for file in files:
        try:
            mods = read_mods(file, config.loader)
        except Error as exc:
            report["errors"].append(f"{file.name}: {exc}")
            continue
        if not mods:
            report["warnings"].append(
                f"{file.name}: metadata не распознаны; проверит загрузчик."
            )
        if re.search(r"(?:^|[-_])client(?:[-_.]|$)", file.name, re.I):
            report["warnings"].append(
                f"{file.name}: имя похоже на клиентскую сборку. Проверьте наличие отдельного server JAR."
            )
        for mod in mods:
            identity = mod["id"]
            if identity in owners:
                previous = next(m for m in report["mods"] if m["id"] == identity)
                if mod["nested"] or previous["nested"]:
                    old_version, new_version = (
                        numeric(available[identity]),
                        numeric(mod["version"]),
                    )
                    if old_version is not None and new_version is not None:
                        if new_version > old_version:
                            available[identity] = mod["version"]
                    else:
                        available[identity] = "?"
                    report["warnings"].append(
                        f"Вложенная библиотека {identity}: окончательный выбор версии выполняет загрузчик."
                    )
                    continue
                report["errors"].append(
                    f"Дублируется mod ID {identity}: {owners[identity]} и {file.name}."
                )
            owners[identity] = file.name
            mod = {**mod, "file": file.name}
            report["mods"].append(mod)
            available[identity] = mod["version"]
            for alias in mod["provides"]:
                available[alias] = mod["version"]
            if mod["client_only"]:
                report["errors"].append(
                    f"{file.name}: {identity} предназначен только для клиента."
                )
    builtins = {"forge", "neoforge", "fabricloader", "java"}
    for mod in report["mods"]:
        if mod["nested"]:
            continue
        for identity, requirement in mod["depends"].items():
            if identity not in available:
                if identity in builtins:
                    report["warnings"].append(
                        f"{mod['id']}: версия {identity} не определена, проверит загрузчик."
                    )
                else:
                    report["errors"].append(
                        f"{mod['id']}: отсутствует обязательный мод {identity} {requirement}."
                    )
            else:
                match = matches(available[identity], requirement)
                if (
                    mod["scheme"] == "maven"
                    and isinstance(requirement, str)
                    and requirement not in {"", "*"}
                    and not requirement.startswith(("[", "("))
                ):
                    match = None
                if match is False:
                    report["errors"].append(
                        f"{mod['id']}: требуется {identity} {requirement}, установлен {available[identity]}."
                    )
                elif match is None:
                    report["warnings"].append(
                        f"{mod['id']}: диапазон {identity} {requirement} проверит загрузчик."
                    )
        for identity, requirement in mod["breaks"].items():
            if (
                identity in available
                and matches(available[identity], requirement) is True
            ):
                report["errors"].append(
                    f"{mod['id']}: несовместим с {identity} {available[identity]}."
                )
    return report


def require_compatible(paths=None, extra=()):
    report = audit(paths, extra)
    if report["errors"]:
        raise Error("Проверка модов:\n" + "\n".join(report["errors"]))
    return report
