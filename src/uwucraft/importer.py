"""Import an already downloaded Minecraft server without re-downloading or
moving it: detection reads only real on-disk evidence, registration writes
only uwucraft metadata, and the server directory is managed in place by
default (an optional --copy mode duplicates it into the uwucraft data root).
"""

import json
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from . import backups, deployment, profiles
from .config import Config, Error, Paths, atomic, memory_mb
from .creation import validate_name
from .installers import LOADERS

VERSION_RE = re.compile(r"1\.\d+(?:\.\d+)?")
XMX_RE = re.compile(r"-Xmx(\d+)([KkMmGg])")
SKIP_NAMES = {
    "plugins",
    "mods",
    "config",
    "logs",
    "cache",
    "libraries",
    "versions",
    "crash-reports",
    "backups",
    "backup",
    "datapacks",
    "resourcepacks",
    "node_modules",
    "bin",
    ".git",
    ".idea",
    ".vscode",
}
JAR_EXCLUDES = ("-installer", "-sources", "-javadoc", "-api")
IMPLEMENTATION_TITLES = {
    "paper": "paper",
    "purpur": "purpur",
    "fabric": "fabric",
    "neoforge": "neoforge",
    "minecraft server": "vanilla",
    "minecraftserver": "vanilla",
}


@dataclass
class Detected:
    directory: Path
    jar: Path | None = None
    loader: str = "unknown"
    version: str = ""
    port: int = 25565
    online_mode: bool = True
    motd: str = ""
    max_players: int = 20
    memory: str = ""
    eula: bool | None = None
    worlds: list = field(default_factory=list)
    mods: int = 0
    plugins: int = 0
    has_config: bool = False
    datapacks: int = 0
    resourcepacks: int = 0
    launch: str = ""


def parse_properties(text):
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def read_properties(file):
    try:
        return parse_properties(Path(file).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return {}


def _jar_facts(jar):
    facts = {"entries": set()}
    try:
        with zipfile.ZipFile(jar) as archive:
            facts["entries"] = set(archive.namelist())
            if "version.json" in facts["entries"]:
                try:
                    facts["version"] = json.loads(archive.read("version.json"))["id"]
                except (ValueError, KeyError):
                    pass
            if "META-INF/MANIFEST.MF" in facts["entries"]:
                manifest = archive.read("META-INF/MANIFEST.MF").decode(
                    "utf-8", "replace"
                )
                for line in manifest.splitlines():
                    if line.startswith("Implementation-Title:"):
                        facts["title"] = line.split(":", 1)[1].strip()
                    elif line.startswith("Main-Class:"):
                        facts["main"] = line.split(":", 1)[1].strip()
    except (OSError, zipfile.BadZipFile, ValueError):
        pass
    return facts


def _server_jars(root):
    return [
        path
        for path in sorted(root.glob("*.jar"))
        if path.is_file()
        and not path.name.startswith(".")
        and not any(marker in path.name.lower() for marker in JAR_EXCLUDES)
    ]


def _detect_loader(root, jars):
    libraries = root / "libraries"
    if libraries.is_dir():
        if (libraries / "net/neoforged").is_dir():
            return "neoforge"
        if (libraries / "net/minecraftforge").is_dir():
            return "forge"
        if next(libraries.glob("**/unix_args.txt"), None) or next(
            libraries.glob("**/win_args.txt"), None
        ):
            return "forge"
    for jar in jars:
        name = jar.name.lower()
        if name.startswith("paper"):
            return "paper"
        if name.startswith("purpur"):
            return "purpur"
        if name.startswith("neoforge"):
            return "neoforge"
        if name.startswith("forge"):
            return "forge"
        if "fabric" in name:
            return "fabric"
    for jar in jars:
        facts = _jar_facts(jar)
        entries = facts["entries"]
        if "META-INF/neoforge.mods.toml" in entries:
            return "neoforge"
        if "META-INF/mods.toml" in entries:
            return "forge"
        title = facts.get("title", "").lower()
        for marker, loader in IMPLEMENTATION_TITLES.items():
            if marker in title:
                return loader
        main = facts.get("main", "").lower()
        if "paperclip" in main:
            return "paper"
        if "purpur" in main:
            return "purpur"
        if "fabric" in main:
            return "fabric"
        if main.startswith("net.minecraft.server"):
            return "vanilla"
    return "unknown"


def _detect_version(root, jar):
    if jar is not None:
        version = _jar_facts(jar).get("version")
        if version:
            return version
        match = VERSION_RE.search(jar.name)
        if match:
            return match.group(0)
    for script in sorted(
        list(root.glob("*.sh")) + list(root.glob("*.bat")) + list(root.glob("*.ps1"))
    ):
        match = VERSION_RE.search(script.name)
        if match:
            return match.group(0)
        try:
            match = VERSION_RE.search(
                script.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            continue
        if match:
            return match.group(0)
    return ""


def _launch_facts(root):
    memory = ""
    for script in sorted(
        list(root.glob("*.sh")) + list(root.glob("*.bat")) + list(root.glob("*.ps1"))
    ):
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = XMX_RE.search(text)
        if match:
            amount, unit = int(match[1]), match[2].upper()
            if unit == "K":
                continue
            if unit == "M" and amount >= 1024 and amount % 1024 == 0:
                memory = f"{amount // 1024}G"
            else:
                memory = f"{amount}{unit}"
            break
    return memory


def _worlds(root):
    result = []
    try:
        children = sorted(root.iterdir())
    except OSError:
        return result
    for item in children:
        if (
            item.is_dir()
            and not item.is_symlink()
            and not item.name.startswith(".")
            and item.name not in SKIP_NAMES
            and (item / "level.dat").exists()
        ):
            result.append(item.name)
    return result


def _count_jars(folder):
    try:
        return len([p for p in folder.glob("*.jar") if p.is_file()])
    except OSError:
        return 0


def scan(directory):
    """Recognize an existing server directory from real on-disk evidence."""
    root = Path(directory).expanduser()
    if not root.exists():
        raise Error("Каталог не существует: " + str(directory))
    if not root.is_dir():
        raise Error("Это не каталог: " + str(directory))
    root = root.resolve()
    jars = _server_jars(root)
    jar = (
        root / "server.jar"
        if (root / "server.jar").is_file()
        else (jars[0] if jars else None)
    )
    run_script = (root / "run.sh").exists() or (root / "run.bat").exists()
    if jar is None and not run_script and not (root / "libraries").is_dir():
        raise Error(
            "Сервер не распознан: нет server.jar, другого серверного .jar, run.sh/run.bat."
        )
    detected = Detected(directory=root, jar=jar)
    detected.loader = _detect_loader(root, jars)
    detected.version = _detect_version(root, jar)
    detected.memory = _launch_facts(root)
    properties = read_properties(root / "server.properties")
    port = properties.get("server-port", "")
    if port.isdecimal() and 1 <= int(port) <= 65535:
        detected.port = int(port)
    detected.online_mode = properties.get("online-mode", "true").lower() != "false"
    detected.motd = properties.get("motd", "")
    players = properties.get("max-players", "")
    if players.isdecimal() and 1 <= int(players) <= 10000:
        detected.max_players = int(players)
    eula = read_properties(root / "eula.txt")
    if "eula" in eula:
        detected.eula = eula["eula"].lower() == "true"
    detected.worlds = _worlds(root)
    detected.mods = _count_jars(root / "mods")
    detected.plugins = _count_jars(root / "plugins")
    detected.has_config = (root / "config").is_dir()
    detected.resourcepacks = _count_jars(root / "resourcepacks")
    for world in detected.worlds + [""]:
        folder = root / world / "datapacks" if world else root / "datapacks"
        detected.datapacks += _count_jars(folder)
    scripts = [
        name
        for name in ("run.sh", "start.sh", "run.bat", "start.bat")
        if (root / name).exists()
    ]
    detected.launch = ", ".join(scripts)
    return detected


def summarize(detected):
    worlds = ", ".join(detected.worlds) or "не найдены"
    content = []
    if detected.mods:
        content.append(f"mods: {detected.mods}")
    if detected.plugins:
        content.append(f"plugins: {detected.plugins}")
    if detected.datapacks:
        content.append(f"datapacks: {detected.datapacks}")
    if detected.has_config:
        content.append("config/")
    if detected.resourcepacks:
        content.append(f"resourcepacks: {detected.resourcepacks}")
    lines = [
        f"Каталог: {detected.directory}",
        f"JAR: {detected.jar.name if detected.jar else 'не найден (run-скрипт / libraries)'}",
        f"Ядро: {detected.loader} · Minecraft: {detected.version or 'не определена'}",
        f"Порт: {detected.port} · online-mode: {str(detected.online_mode).lower()}"
        + (f" · RAM: {detected.memory}" if detected.memory else ""),
    ]
    if detected.motd:
        lines.append("MOTD: " + detected.motd[:80])
    if detected.eula is not None:
        lines.append("EULA: " + ("принята" if detected.eula else "не принята"))
    lines.append(f"Миры: {worlds}")
    if content:
        lines.append("Контент: " + ", ".join(content))
    if detected.launch:
        lines.append("Скрипты запуска: " + detected.launch)
    return "\n".join(lines)


def _looks_like_server(directory):
    try:
        if not directory.is_dir() or directory.is_symlink():
            return False
        if (directory / "server.jar").is_file() or (directory / "run.sh").is_file():
            return True
        if (directory / "run.bat").is_file() and (directory / "libraries").is_dir():
            return True
        jars = _server_jars(directory)
        return bool(jars) and (
            (directory / "server.properties").exists()
            or (directory / "eula.txt").exists()
            or (directory / "world/level.dat").exists()
        )
    except OSError:
        return False


def _inside(directory, parent):
    try:
        directory.relative_to(parent)
        return True
    except ValueError:
        return False


def discover():
    """Bounded search of typical user locations; never walks the whole disk."""
    home = Path.home()
    roots = [home]
    roots += [
        home / name
        for name in (
            "Desktop",
            "Downloads",
            "Documents",
            "Games",
            "games",
            "minecraft",
            "servers",
            "Серверы",
        )
    ]
    roots += [Path("/opt"), Path("/srv")]
    try:
        managed = Paths(profile="default").root_data.resolve()
    except Error:
        managed = None
    found = []
    scanned = 0

    def consider(directory):
        nonlocal scanned
        if scanned >= 400 or len(found) >= 20:
            return
        scanned += 1
        if managed is not None and _inside(directory.resolve(), managed):
            return
        if _looks_like_server(directory):
            try:
                found.append(scan(directory))
            except Error:
                pass

    for root in roots:
        if not root.is_dir():
            continue
        consider(root)
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children[:100]:
            if child.name.startswith(".") or child.name in SKIP_NAMES:
                continue
            if child.is_dir():
                consider(child)
    return found


def running_in(directory):
    """True when a JVM already runs inside the directory (a shell merely
    sitting there must not block the first backup)."""
    target = str(Path(directory).resolve())
    for process in psutil.process_iter(["name"]):
        try:
            if "java" not in (process.info.get("name") or "").lower():
                continue
            cwd = process.cwd()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except OSError:
            continue
        if cwd and str(Path(cwd).resolve()) == target:
            return True
    return False


def _already_managed(directory):
    base = Paths(profile="default")
    managed_root = base.root_data.resolve()
    if _inside(directory, managed_root):
        return "Этот каталог уже внутри данных uwucraft и управляется им."
    for existing in profiles.listing():
        marker = existing.data / "server-location"
        if marker.exists() and not marker.is_symlink():
            try:
                target = Path(marker.read_text(encoding="utf-8").strip()).resolve()
            except (OSError, ValueError):
                target = None
            if target == directory:
                return (
                    "Этот сервер уже импортирован как профиль " + existing.profile + "."
                )
        try:
            config = existing.load()
        except Error:
            continue
        if config.imported_from:
            try:
                if Path(config.imported_from).resolve() == directory:
                    return (
                        "Этот сервер уже импортирован как профиль "
                        + existing.profile
                        + "."
                    )
            except OSError:
                continue
    return ""


def register(
    detected,
    *,
    profile=None,
    copy=False,
    name=None,
    loader=None,
    version=None,
    memory=None,
    max_players=None,
    configure_network=True,
    enable_autostart=True,
    progress=None,
):
    """Attach a detected server to uwucraft: metadata only, files untouched.

    By default the server stays in its directory (managed via a
    server-location marker); with copy=True it is duplicated into the uwucraft
    data root and the original directory is left alone.
    """
    progress = progress or (lambda message: None)
    directory = Path(detected.directory).resolve()
    loader = loader or detected.loader
    if loader not in LOADERS:
        raise Error(
            "Ядро не распознано или не поддерживается. Укажите явно: "
            + ", ".join(LOADERS)
        )
    version = detected.version if version is None else version.strip()
    if version and (
        len(version) > 64
        or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._"
            for c in version
        )
    ):
        raise Error("Недопустимая версия Minecraft.")
    name = name or detected.directory.name or "Imported server"
    validate_name(name)
    memory = memory or detected.memory or "2G"
    memory_mb(memory)
    players = max_players or detected.max_players
    if not 1 <= players <= 10000:
        raise Error("Количество игроков: 1–10000.")
    problem = _already_managed(directory)
    if problem:
        raise Error(problem)
    paths = Paths(profile=profile) if profile else profiles.next_profile()
    if paths.file.exists():
        raise Error(
            "Профиль " + paths.profile + " уже занят. Выберите другой --profile."
        )
    report = {
        "profile": paths.profile,
        "loader": loader,
        "version": version,
        "directory": str(directory),
        "address": "",
        "verified": False,
        "errors": [],
        "warnings": [],
    }
    if detected.eula is False:
        report["warnings"].append(
            "eula.txt не принята: сервер не запустится, пока в нём не будет eula=true."
        )
    if not shutil.which("java"):
        report["warnings"].append(
            "Команда java не найдена в PATH: укажите путь к Java в config.toml до запуска."
        )
    progress("Регистрация сервера в uwucraft")
    paths.data.mkdir(parents=True, exist_ok=True, mode=0o700)
    if copy:
        progress("Копирование сервера в каталог uwucraft")
        shutil.copytree(directory, paths.server, symlinks=True)
    else:
        atomic(paths.data / "server-location", str(directory) + "\n")
    paths = Paths(profile=paths.profile)
    import secrets

    paths.save(
        Config(
            loader=loader,
            version=version,
            memory=memory,
            port=detected.port,
            java="java",
            name=name,
            max_players=players,
            online_mode=detected.online_mode,
            server_id=secrets.token_hex(6),
            imported_from=str(directory),
        )
    )
    if running_in(directory):
        report["warnings"].append(
            "Похоже, сервер сейчас запущен вручную из этого каталога. Остановите прежний процесс перед start через uwucraft."
        )
    try:
        report["backup"] = str(backups.create(paths))
    except (Error, OSError) as exc:
        report["warnings"].append("Первая резервная копия не создана: " + str(exc))
    if enable_autostart:
        progress("Настройка автозапуска")
        try:
            warning = deployment.enable_server(paths)
            if warning:
                report["warnings"].append(warning)
        except (Error, OSError) as exc:
            report["warnings"].append(
                "Автозапуск не настроен (сервер доступен через uwucraft start): "
                + str(exc)
            )
    if configure_network:
        progress("Определение публичного адреса")
        try:
            network_report = deployment.configure(paths, progress)
        except (Error, OSError, ValueError) as exc:
            report["errors"].append("Сеть не настроена: " + str(exc))
        else:
            report["address"] = network_report.get("address", "")
            report["verified"] = network_report.get("verified", False)
            report["errors"].extend(network_report.get("errors", []))
            report["warnings"].extend(network_report.get("warnings", []))
    else:
        atomic(
            paths.data / "deployment.json",
            json.dumps(report, ensure_ascii=False, indent=2),
        )
    return report
