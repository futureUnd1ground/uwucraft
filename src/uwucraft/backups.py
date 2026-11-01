import shutil
import stat
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from .config import Error, Paths
from .server import status

MAX_UNPACKED = 8 * 1024**3


def safe_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise Error(f"Небезопасный путь в архиве: {name!r}")
    return path


def extract_zip(archive, target):
    with zipfile.ZipFile(archive) as file:
        if (
            sum(i.file_size for i in file.infolist()) > MAX_UNPACKED
            or len(file.infolist()) > 100000
        ):
            raise Error("Архив превышает допустимый размер/число файлов.")
        for item in file.infolist():
            safe_name(item.filename)
            if stat.S_ISLNK(item.external_attr >> 16):
                raise Error("Символические ссылки в ZIP запрещены.")
        file.extractall(target)


def require_stopped(paths):
    if status(paths)["status"] not in {"Stopped", "Crashed"}:
        raise Error(
            "Для целостной копии/изменения файлов остановите сервер: uwucraft stop."
        )


def create(paths=None, protected=None):
    paths = paths or Paths()
    with paths.lock():
        require_stopped(paths)
        if not paths.file.exists():
            raise Error("Сервер ещё не настроен.")
        paths.backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1000000:06}.tar.gz"
        target = paths.backups / name
        temporary = target.with_suffix(".part")
        try:
            with tarfile.open(temporary, "w:gz") as archive:
                archive.add(
                    paths.config,
                    arcname="config",
                    filter=lambda i: None if i.issym() or i.islnk() else i,
                )
                archive.add(
                    paths.server,
                    arcname="server",
                    filter=lambda i: None if i.issym() or i.islnk() else i,
                )
            temporary.chmod(0o600)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        for old in sorted(paths.backups.glob("*.tar.gz"), reverse=True)[
            max(1, paths.load().backup_keep) :
        ]:
            if old != protected:
                old.unlink()
        return target


def listing(paths=None):
    paths = paths or Paths()
    return [
        {"name": p.name, "bytes": p.stat().st_size}
        for p in sorted(paths.backups.glob("*.tar.gz"), reverse=True)
    ]


def restore(name, paths=None):
    paths = paths or Paths()
    if Path(name).name != name:
        raise Error("Укажите имя из list-backups, без пути.")
    source = paths.backups / name
    if not source.is_file():
        raise Error("Копия не найдена.")
    # Preserve the current installation before any replacement.
    rescue = create(paths, protected=source)
    with paths.lock():
        require_stopped(paths)
        with tempfile.TemporaryDirectory(dir=paths.data) as staging:
            staging = Path(staging)
            with tarfile.open(source) as archive:
                members = archive.getmembers()
                if sum(i.size for i in members) > MAX_UNPACKED:
                    raise Error("Копия превышает лимит распаковки 8 GiB.")
                for item in members:
                    path = safe_name(item.name)
                    if (
                        not path.parts
                        or path.parts[0] not in {"config", "server"}
                        or not (item.isfile() or item.isdir())
                    ):
                        raise Error("Небезопасное содержимое копии.")
                archive.extractall(staging, filter="data")
            if (
                not (staging / "config/config.toml").exists()
                or not (staging / "server").is_dir()
            ):
                raise Error("Неполная резервная копия.")
            import tomllib
            from dataclasses import fields

            from .config import Config, memory_mb

            values = tomllib.loads(
                (staging / "config/config.toml").read_text(encoding="utf-8")
            )
            known = {field.name for field in fields(Config)}
            restored_config = Config(
                **{key: value for key, value in values.items() if key in known}
            )
            memory_mb(restored_config.memory)
            if not 1024 <= restored_config.port <= 65535:
                raise Error("Некорректный порт в резервной копии.")
            old = paths.data / "restore-previous"
            old_config = paths.config.with_name(paths.config.name + ".restore-previous")
            if old.exists() or old_config.exists():
                raise Error("Осталась restore-previous; проверьте её вручную.")
            with tempfile.TemporaryDirectory(
                prefix=".uwucraft-restore-", dir=paths.config.parent
            ) as config_stage:
                new_config = Path(config_stage) / "config"
                shutil.copytree(staging / "config", new_config)
                paths.server.rename(old)
                try:
                    paths.config.rename(old_config)
                    (staging / "server").rename(paths.server)
                    new_config.rename(paths.config)
                except Exception:
                    if old_config.exists():
                        if paths.config.exists():
                            shutil.rmtree(paths.config)
                        old_config.rename(paths.config)
                    if paths.server.exists():
                        shutil.rmtree(paths.server)
                    old.rename(paths.server)
                    raise
                shutil.rmtree(old)
                shutil.rmtree(old_config)
    return rescue
