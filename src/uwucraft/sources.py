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


def download(url, destination: Path, hashes=None):
    if urlparse(url).scheme != "https":
        raise Error("Загрузка разрешена только по HTTPS.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    digests = {
        key: hashlib.new(key)
        for key in (hashes or {})
        if key in {"sha1", "sha256", "sha512", "md5"}
    }
    try:
        for _ in range(6):
            with httpx.stream(
                "GET",
                url,
                timeout=60,
                follow_redirects=False,
                headers={"User-Agent": UA},
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
                with temporary.open("wb") as stream:
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 2 * 1024**3:
                            raise Error("Файл превышает лимит 2 GiB.")
                        stream.write(chunk)
                        for digest in digests.values():
                            digest.update(chunk)
                break
        else:
            raise Error("Слишком много перенаправлений при загрузке.")
        for key, digest in digests.items():
            if digest.hexdigest().lower() != hashes[key].lower():
                raise Error("Хеш не совпадает: файл отклонён.")
        temporary.replace(destination)
    except httpx.HTTPError:
        raise Error(
            "Загрузка прервана; проверьте доступ к источнику и повторите."
        ) from None
    finally:
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
        return item["url"], {"sha1": item["sha1"]}, info["javaVersion"]["majorVersion"]
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
            info["javaVersion"]["majorVersion"],
        )
    if loader == "purpur":
        item = metadata(f"https://api.purpurmc.org/v2/purpur/{version}/latest")
        return (
            f"https://api.purpurmc.org/v2/purpur/{version}/{item['build']}/download",
            {"md5": item["md5"]},
            info["javaVersion"]["majorVersion"],
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
            info["javaVersion"]["majorVersion"],
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
            parts = version.split(".")
            prefix = (
                ".".join(parts[1:]) if version.startswith("1.") else version
            ) + "."
            if len(parts) == 2 and version.startswith("1."):
                prefix = parts[1] + ".0."
            if any(v.startswith(prefix) for v in listed):
                return version
        raise Error("Не найдена совместимая стабильная версия NeoForge.")
    return manifest["latest"]["release"]
