"""Verified per-user Temurin runtime, without changing the system Java."""

import platform
import tarfile
import tempfile
from pathlib import Path

from .backups import extract_zip, safe_name
from .config import Error, Paths
from .platforms import windows
from .sources import download, metadata


def provision(major, paths=None):
    paths = paths or Paths()
    architecture = {
        "x86_64": "x64",
        "amd64": "x64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }.get(platform.machine().lower())
    if not architecture:
        raise Error("Автоустановка Temurin не поддерживает эту архитектуру.")
    target = paths.data / "runtimes" / str(major)
    executable = "bin/java.exe" if windows() else "bin/java"

    def result():
        if windows():
            from .windows import java_path

            return java_path(target / executable)
        return str(target / executable)

    if (target / executable).exists():
        return result()
    assets = metadata(
        f"https://api.adoptium.net/v3/assets/latest/{major}/hotspot",
        params={
            "architecture": architecture,
            "image_type": "jre",
            "os": "windows" if windows() else "linux",
            "vendor": "eclipse",
        },
    )
    if not assets:
        raise Error("Temurin JRE для этой платформы не найден.")
    package = assets[0]["binary"]["package"]
    paths.data.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=paths.data) as directory:
        directory = Path(directory)
        archive = directory / "runtime.tar.gz"
        download(package["link"], archive, {"sha256": package["checksum"]})
        if windows():
            extract_zip(archive, directory / "unpacked")
        else:
            with tarfile.open(archive) as file:
                members = file.getmembers()
                if sum(i.size for i in members) > 1024**3:
                    raise Error("Runtime превышает лимит распаковки.")
                for item in members:
                    safe_name(item.name)
                    # Temurin includes relative library symlinks. Python's data filter
                    # rejects links escaping the extraction root.
                file.extractall(directory / "unpacked", filter="data")
        roots = list((directory / "unpacked").iterdir())
        if len(roots) != 1 or not (roots[0] / executable).exists():
            raise Error("Неожиданная структура Temurin runtime.")
        target.parent.mkdir(parents=True, exist_ok=True)
        roots[0].rename(target)
    return result()
