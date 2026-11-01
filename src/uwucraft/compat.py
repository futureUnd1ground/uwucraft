"""Optional official ViaVersion + ViaBackwards distribution."""

from .backups import require_stopped
from .config import Error, Paths
from .sources import download, metadata


def install(paths=None):
    paths = paths or Paths()
    if paths.load().loader not in {"paper", "purpur"}:
        raise Error("ViaVersion/ViaBackwards требуют Paper/Purpur.")
    require_stopped(paths)
    with paths.lock():
        require_stopped(paths)
        for project in ["ViaVersion", "ViaBackwards"]:
            release = metadata(
                f"https://api.github.com/repos/ViaVersion/{project}/releases/latest"
            )
            item = next(
                a
                for a in release["assets"]
                if a["name"].endswith(".jar")
                and not a["name"].endswith(("-sources.jar", "-javadoc.jar"))
            )
            if not (item.get("digest") or "").startswith("sha256:"):
                raise Error("Нет SHA-256 для официального релиза " + project)
            destination = paths.server / "plugins" / item["name"]
            download(
                item["browser_download_url"],
                destination,
                {"sha256": item["digest"].split(":", 1)[1]},
            )
            for old in destination.parent.glob(project + "-*.jar"):
                if old != destination:
                    old.unlink()
    return "ViaVersion + ViaBackwards установлены. Список поддерживаемых протоколов смотрите в логе после запуска."
