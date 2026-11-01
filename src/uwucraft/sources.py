"""Official source resolution and bounded, verified downloads."""

import hashlib
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import __version__
from .config import Error

UA = f"uwucraft/{__version__} (https://github.com/futureUnd1ground/uwucraft)"


def get(url, **kwargs):
    if urlparse(url).scheme != "https":
        raise Error("Разрешены только HTTPS-источники.")
    headers = {"User-Agent": UA, **kwargs.pop("headers", {})}
    for attempt in range(3):
        try:

            def validate_request(request):
                if request.url.scheme != "https":
                    raise Error("Небезопасное перенаправление: требуется HTTPS.")
                if (
                    "x-api-key" in request.headers
                    and request.url.host != urlparse(url).hostname
                ):
                    raise Error("API key не передаётся на другой хост при redirect.")

            with httpx.Client(
                timeout=30,
                follow_redirects=True,
                headers=headers,
                event_hooks={"request": [validate_request]},
            ) as client:
                response = client.get(url, **kwargs)
            response.raise_for_status()
            if response.url.scheme != "https":
                raise Error("Небезопасное перенаправление.")
            return response
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == 2:
                raise Error(
                    f"Источник {urlparse(url).hostname} недоступен; проверьте сеть."
                ) from None
            time.sleep(attempt + 1)
        except httpx.HTTPStatusError as exc:
            raise Error(
                f"Источник {urlparse(url).hostname}: HTTP {exc.response.status_code}."
            ) from None


def metadata(url, **kwargs):
    return get(url, **kwargs).json()


def download(url, destination: Path, hashes=None, progress=None, min_speed=0):
    """Stream-download with verified hashes and transparent resume.

    A .part file survives failures, so a retried download (or the next
    mirror) continues from the last byte instead of starting over. When
    min_speed (bytes/second) is given, a download slower than that during
    the first windows raises Error so callers can switch the source.
    progress(message) is throttled to ~4 updates per second.
    """
    if urlparse(url).scheme != "https":
        raise Error("Загрузка разрешена только по HTTPS.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    label = Path(urlparse(url).path).name or "файла"

    def fresh_digests():
        return {
            key: hashlib.new(key)
            for key in (hashes or {})
            if key in {"sha1", "sha256", "sha512", "md5"}
        }

    digests = fresh_digests()
    existing = temporary.stat().st_size if temporary.exists() else 0
    if existing:
        # Resume is only sound if the hash covers the whole file: replay the
        # already-downloaded prefix into the digests before appending.
        with temporary.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                for digest in digests.values():
                    digest.update(chunk)
    try:
        for _ in range(6):
            headers = {"User-Agent": UA}
            if existing:
                headers["Range"] = f"bytes={existing}-"
            with httpx.stream(
                "GET",
                url,
                timeout=60,
                follow_redirects=False,
                headers=headers,
            ) as response:
                if response.is_redirect:
                    redirect = response.headers.get("location")
                    if not redirect:
                        raise Error("Redirect без Location.")
                    next_url = response.url.join(redirect)
                    if next_url.scheme != "https":
                        raise Error("Небезопасное перенаправление: требуется HTTPS.")
                    url = str(next_url)
                    continue
                response.raise_for_status()
                if existing and response.status_code != 206:
                    # The server ignored Range: restart from a clean file.
                    existing = 0
                    digests = fresh_digests()
                total = (
                    existing + int(response.headers.get("content-length") or 0)
                    if existing
                    else int(response.headers.get("content-length") or 0)
                )
                mode = "ab" if existing else "wb"
                size = existing
                last_report = 0.0
                window_start = time.monotonic()
                window_bytes = 0
                with temporary.open(mode) as stream:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        window_bytes += len(chunk)
                        if size > 2 * 1024**3:
                            raise Error("Файл превышает лимит 2 GiB.")
                        stream.write(chunk)
                        for digest in digests.values():
                            digest.update(chunk)
                        now = time.monotonic()
                        if progress is not None and now - last_report > 0.25:
                            last_report = now
                            elapsed = max(now - window_start, 0.001)
                            speed = window_bytes / elapsed
                            done = size
                            if total:
                                percent = done * 100 // total
                                progress(
                                    f"Скачивание {label}: "
                                    f"{done / 1024**2:.0f}/{total / 1024**2:.0f} МиБ "
                                    f"({percent}%) · {speed / 1024:.0f} КБ/с"
                                )
                            else:
                                progress(
                                    f"Скачивание {label}: {done / 1024**2:.0f} МиБ "
                                    f"· {speed / 1024:.0f} КБ/с"
                                )
                        if min_speed and now - window_start > 20:
                            speed = window_bytes / (now - window_start)
                            if speed < min_speed:
                                raise Error(
                                    f"Скорость загрузки {label} слишком низкая "
                                    f"({speed / 1024:.0f} КБ/с); источник пропущен."
                                )
                            window_start, window_bytes = now, 0
                break
        else:
            raise Error("Слишком много перенаправлений при загрузке.")
        for key, digest in digests.items():
            if digest.hexdigest().lower() != hashes[key].lower():
                # Corrupt content, not a network blip: a resumed retry would
                # replay the same bad bytes, so drop the partial file.
                temporary.unlink(missing_ok=True)
                raise Error("Хеш не совпадает: файл отклонён.")
        temporary.replace(destination)
    except httpx.HTTPError:
        raise Error(
            "Загрузка прервана; проверьте доступ к источнику и повторите."
        ) from None
    finally:
        # Keep a non-empty .part file so the next attempt (or mirror) resumes
        # instead of restarting; drop only a never-started empty part.
        if (
            not destination.exists()
            and temporary.exists()
            and temporary.stat().st_size == 0
        ):
            temporary.unlink(missing_ok=True)


MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"


def minecraft(version=None):
    manifest = metadata(MANIFEST)
    version = version or manifest["latest"]["release"]
    record = next((v for v in manifest["versions"] if v["id"] == version), None)
    if not record:
        raise Error(f"Minecraft {version} отсутствует в официальном манифесте.")
    return version, metadata(record["url"])


def resolve(loader, version):
    version, info = minecraft(version)
    if loader == "vanilla":
        item = info.get("downloads", {}).get("server")
        if not item:
            raise Error("Для этой версии нет серверного дистрибутива.")
        return (
            item["url"],
            {"sha1": item["sha1"]},
            info.get("javaVersion", {}).get("majorVersion", 8),
        )
    if loader == "paper":
        builds = metadata(
            f"https://fill.papermc.io/v3/projects/paper/versions/{version}/builds"
        )
        stable = [
            b for b in builds if b["channel"].upper() in {"STABLE", "RECOMMENDED"}
        ]
        if not stable:
            raise Error("Для версии нет стабильного Paper; выберите другую версию.")
        item = stable[0]["downloads"]["server:default"]
        return (
            item["url"],
            item.get("checksums", {}),
            info.get("javaVersion", {}).get("majorVersion", 8),
        )
    if loader == "purpur":
        item = metadata(f"https://api.purpurmc.org/v2/purpur/{version}/latest")
        return (
            f"https://api.purpurmc.org/v2/purpur/{version}/{item['build']}/download",
            {"md5": item["md5"]},
            info.get("javaVersion", {}).get("majorVersion", 8),
        )
    if loader == "fabric":
        loaders = metadata(f"https://meta.fabricmc.net/v2/versions/loader/{version}")
        stable = next((x for x in loaders if x["loader"]["stable"]), None)
        installer = next(
            x
            for x in metadata("https://meta.fabricmc.net/v2/versions/installer")
            if x["stable"]
        )
        if not stable:
            raise Error("Нет стабильного Fabric для выбранной версии.")
        return (
            f"https://meta.fabricmc.net/v2/versions/loader/{version}/{stable['loader']['version']}/{installer['version']}/server/jar",
            {},
            info.get("javaVersion", {}).get("majorVersion", 8),
        )
    raise Error("Неизвестный загрузчик.")


def latest_compatible(loader):
    """Select releases only; never suggest a snapshot just because it sorts first."""
    manifest = metadata(MANIFEST)
    releases = [
        item["id"] for item in manifest["versions"] if item["type"] == "release"
    ]
    if loader == "paper":
        project = metadata("https://fill.papermc.io/v3/projects/paper")
        listed = project["versions"]
        if isinstance(listed, dict):
            listed = [v for group in listed.values() for v in group]
        for version in [v for v in releases if v in listed][:12]:
            builds = metadata(
                f"https://fill.papermc.io/v3/projects/paper/versions/{version}/builds"
            )
            if any(b["channel"].upper() in {"STABLE", "RECOMMENDED"} for b in builds):
                return version
        raise Error("В API Paper не найден стабильный совместимый release.")
    if loader == "purpur":
        listed = metadata("https://api.purpurmc.org/v2/purpur")["versions"]
        return next(v for v in releases if v in listed)
    if loader == "fabric":
        listed = [
            v["version"]
            for v in metadata("https://meta.fabricmc.net/v2/versions/game")
            if v["stable"]
        ]
        for version in [v for v in releases if v in listed][:12]:
            if any(
                v["loader"]["stable"]
                for v in metadata(
                    f"https://meta.fabricmc.net/v2/versions/loader/{version}"
                )
            ):
                return version
        raise Error("В API Fabric нет совместимого stable loader.")
    if loader == "forge":
        promos = metadata(
            "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
        )["promos"]
        return next(
            v
            for v in releases
            if v + "-recommended" in promos or v + "-latest" in promos
        )
    if loader == "neoforge":
        from xml.etree import ElementTree

        root = ElementTree.fromstring(
            get(
                "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
            ).text
        )
        listed = [
            v.text
            for v in root.findall("./versioning/versions/version")
            if "beta" not in v.text
        ]
        for version in releases:
            prefix = neoforge_prefix(version)
            if any(v.startswith(prefix) for v in listed):
                return version
        raise Error("Не найдена совместимая стабильная версия NeoForge.")
    return manifest["latest"]["release"]


def available_versions(loader, snapshots=False):
    """Published Minecraft versions from official manifests, newest first.

    A listed core version can still lack a stable build; resolve validates the
    actual artifact before installation. Snapshot opt-in never becomes default.
    """
    if loader not in {"vanilla", "paper", "purpur", "fabric", "forge", "neoforge"}:
        raise Error("Неизвестное ядро.")
    manifest = metadata(MANIFEST)
    versions = [
        v["id"]
        for v in manifest["versions"]
        if v["type"] == "release" or snapshots and v["type"] == "snapshot"
    ]
    if loader == "vanilla":
        return versions
    if loader == "paper":
        listed = metadata("https://fill.papermc.io/v3/projects/paper")["versions"]
        if isinstance(listed, dict):
            listed = [v for group in listed.values() for v in group]
    elif loader == "purpur":
        listed = metadata("https://api.purpurmc.org/v2/purpur")["versions"]
    elif loader == "fabric":
        listed = [
            v["version"]
            for v in metadata("https://meta.fabricmc.net/v2/versions/game")
            if v["stable"] or snapshots
        ]
    elif loader == "forge":
        promos = metadata(
            "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
        )["promos"]
        listed = {v.rsplit("-", 1)[0] for v in promos}
    else:
        from xml.etree import ElementTree

        root = ElementTree.fromstring(
            get(
                "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
            ).text
        )
        builds = [
            v.text
            for v in root.findall("./versioning/versions/version")
            if "beta" not in v.text
        ]
        listed = [
            v for v in versions if any(b.startswith(neoforge_prefix(v)) for b in builds)
        ]
    return [version for version in versions if version in listed]


def neoforge_prefix(version):
    parts = version.split(".")
    if version.startswith("1."):
        return parts[1] + "." + (parts[2] if len(parts) > 2 else "0") + "."
    return version + "."
