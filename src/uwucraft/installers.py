import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from xml.etree import ElementTree

import psutil

from .config import Config, Error, Paths, atomic, memory_mb
from .sources import download, get, metadata, minecraft, resolve

LOADERS = ["vanilla", "paper", "purpur", "fabric", "forge", "neoforge"]


def preflight(paths, memory, port):
    if platform.system() != "Linux":
        raise Error("Требуется Linux.")
    if memory_mb(memory) < 512:
        raise Error("Минимум 512M памяти.")
    if memory_mb(memory) * 1024**2 > psutil.virtual_memory().available - 1024**3:
        raise Error("Недостаточно свободной RAM: оставьте минимум 1 GiB системе.")
    paths.data.mkdir(parents=True, exist_ok=True, mode=0o700)
    if shutil.disk_usage(paths.data).free < 3 * 1024**3:
        raise Error("Требуется минимум 3 GiB свободного места.")
    import socket

    try:
        with socket.socket() as test:
            test.bind(("0.0.0.0", port))
    except OSError:
        raise Error(f"Порт {port} занят или недоступен.") from None
    return {
        "os": platform.freedesktop_os_release().get("ID", "unknown"),
        "arch": platform.machine(),
    }


def java_for(required, install=False, local_runtime=False):
    candidates = [
        f"/usr/lib/jvm/java-{required}-openjdk/bin/java",
        shutil.which("java") or "java",
    ]
    for path in candidates:
        try:
            result = subprocess.run(
                [path, "-version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            match = re.search(r'version "(?:1\.)?(\d+)', result.stderr + result.stdout)
            if result.returncode == 0 and match and int(match[1]) == required:
                return str(Path(shutil.which(path) or path).resolve())
        except OSError:
            pass
    if local_runtime:
        from .runtime import provision

        return provision(required)
    package = (
        f"jre{required}-openjdk-headless" if required != 8 else "jre8-openjdk-headless"
    )
    if install and shutil.which("pacman"):
        subprocess.run(["sudo", "pacman", "-S", "--needed", package], check=True)
        return java_for(required)
    raise Error(
        f"Нужна Java {required}. Arch: sudo pacman -S {package}; либо auto --install-deps."
    )


def forge_artifact(loader, version):
    if loader == "forge":
        promotions = metadata(
            "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
        )["promos"]
        build = promotions.get(version + "-recommended") or promotions.get(
            version + "-latest"
        )
        if not build:
            raise Error("Нет Forge для этой версии Minecraft.")
        full = f"{version}-{build}"
        return f"https://maven.minecraftforge.net/net/minecraftforge/forge/{full}/forge-{full}-installer.jar"
    # NeoForge publishes version families tied to the Minecraft release.
    # Select only stable builds actually present in official Maven metadata.
    base = "https://maven.neoforged.net/releases/net/neoforged/neoforge"
    root = ElementTree.fromstring(get(base + "/maven-metadata.xml").text)
    candidates = [
        x.text
        for x in root.findall("./versioning/versions/version")
        if "beta" not in x.text
    ]
    for build in reversed(candidates):
        if version.startswith("1.20."):
            prefix = "20." + version.split(".")[2] + "."
        elif version.startswith("1.21"):
            prefix = (
                "21."
                + (version.split(".")[2] if len(version.split(".")) > 2 else "0")
                + "."
            )
        else:
            prefix = version + "."
        if build.startswith(prefix):
            return f"{base}/{build}/neoforge-{build}-installer.jar"
    raise Error(
        "Нет стабильного NeoForge для этой версии; задайте поддерживаемую версию."
    )


def install(
    loader,
    version,
    memory,
    port,
    accept_eula,
    install_deps=False,
    paths=None,
    local_runtime=False,
    offline_mode=False,
    name="My uwucraft server",
    max_players=20,
    whitelist=True,
):
    paths = paths or Paths()
    if not accept_eula:
        raise Error(
            "Требуется явное принятие https://aka.ms/MinecraftEULA (--accept-eula)."
        )
    if loader not in LOADERS:
        raise Error("Неизвестный загрузчик.")
    import secrets

    from .creation import validate_name

    validate_name(name)
    if not 1024 <= port <= 65535 or not 1 <= max_players <= 10000:
        raise Error("Проверьте порт (1024–65535) и количество игроков (1–10000).")
    server_id = secrets.token_hex(6)
    motd_name = name.encode("unicode_escape").decode("ascii").replace("=", "\\=")
    with paths.lock():
        if paths.file.exists() or (
            paths.server.exists() and any(paths.server.iterdir())
        ):
            raise Error(
                "Существующая установка сохранена. Используйте status, start или update."
            )
        preflight(paths, memory, port)
        version, info = minecraft(version)
        required = info.get("javaVersion", {}).get("majorVersion", 8)
        java = java_for(required, install_deps, local_runtime)
        with tempfile.TemporaryDirectory(dir=paths.data) as directory:
            staging = Path(directory) / "server"
            staging.mkdir()
            if loader in {"forge", "neoforge"}:
                url = forge_artifact(loader, version)
                hashes = {}
                checksum = get(url + ".sha1").text.strip().split()[0]
                if re.fullmatch("[0-9a-fA-F]{40}", checksum):
                    hashes = {"sha1": checksum}
                download(url, staging / "installer.jar", hashes)
                with (staging / "install.log").open("w") as log:
                    result = subprocess.run(
                        [java, "-jar", "installer.jar", "--installServer"],
                        cwd=staging,
                        check=False,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=900,
                    )
                if result.returncode or not (staging / "run.sh").exists():
                    raise Error(
                        "Installer Forge/NeoForge завершился с ошибкой; установка не применена."
                    )
            else:
                url, hashes, _ = resolve(loader, version)
                download(url, staging / "server.jar", hashes)
            atomic(staging / "eula.txt", "eula=true\n")
            atomic(
                staging / "server.properties",
                f"""# Managed initial defaults; later changes are preserved.
server-port={port}
server-ip=0.0.0.0
online-mode={str(not offline_mode).lower()}
enable-rcon=false
white-list={str(whitelist).lower()}
enforce-whitelist={str(whitelist).lower()}
enforce-secure-profile={str(not offline_mode).lower()}
max-players={max_players}
difficulty=normal
gamemode=survival
pvp=true
motd={motd_name} | uwucraft {server_id}
view-distance=8
simulation-distance=6
spawn-protection=16
""",
            )
            if paths.server.exists():
                paths.server.rmdir()
            staging.rename(paths.server)
            paths.save(
                Config(
                    loader=loader,
                    version=version,
                    memory=memory,
                    port=port,
                    java=java,
                    name=name,
                    max_players=max_players,
                    online_mode=not offline_mode,
                    server_id=server_id,
                )
            )
    return paths.load()


def update(paths=None):
    from . import backups

    paths = paths or Paths()
    config = paths.load()
    backups.require_stopped(paths)
    rescue = backups.create(paths)
    if config.loader in {"forge", "neoforge"}:
        raise Error(
            "Автообновление Forge/NeoForge пока не поддерживается; резервная копия создана."
        )
    with paths.lock():
        backups.require_stopped(paths)
        url, hashes, _ = resolve(config.loader, config.version)
        download(url, paths.server / "server.jar", hashes)
    return rescue
