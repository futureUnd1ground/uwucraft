import base64
import ipaddress
import os
import shutil
import socket
import subprocess
import sys

from .config import Error, Paths
from .platforms import windows


def public_ipv4(value):
    try:
        address = ipaddress.IPv4Address(value.strip())
    except (ValueError, AttributeError) as exc:
        raise Error(
            "HTTPS-сервис не вернул корректный IPv4. Проверьте public_ip_url."
        ) from exc
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise Error(
            "Полученный IPv4 не является публичным. Нужен белый IPv4 от провайдера."
        )
    return str(address)


def external_ipv4(paths=None):
    paths = paths or Paths()
    # One bounded HTTPS attempt; public discovery must not stall the wizard.
    import httpx

    url = paths.load().public_ip_url
    if not url.startswith("https://"):
        raise Error("public_ip_url должен использовать HTTPS.")
    try:
        with httpx.Client(timeout=5, follow_redirects=False) as client:
            response = client.get(url)
            response.raise_for_status()
            return public_ipv4(response.text)
    except httpx.HTTPError as exc:
        raise Error(
            "Не удалось определить публичный IPv4 через HTTPS. Проверьте сеть и public_ip_url."
        ) from exc


def addresses(paths=None, public=False):
    paths = paths or Paths()
    config = paths.load()
    if config.public_access == "relay":
        from .relay import status

        value = status(paths)
        if (
            config.relay_transport == "physical"
            and value.get("transport") != "physical"
        ):
            value = {}
        address = value.get("address", "Не определён")
        return {
            "public": address,
            "port": int(address.rsplit(":", 1)[1]) if value else config.port,
            "reachability": reachability(address, paths),
        }
    ip = external_ipv4(paths) if public else config.public_address
    try:
        ip = public_ipv4(ip)
    except Error:
        ip = ""
    address = f"{ip}:{config.port}" if ip else "Не определён"
    return {
        "public": address,
        "port": config.port,
        "reachability": reachability(address, paths),
    }


def check(paths=None):
    config = (paths or Paths()).load()
    try:
        with socket.create_connection(("127.0.0.1", config.port), timeout=3):
            local = True
    except OSError:
        local = False
    return {
        "local_listening": local,
        "public_reachability": "Не проверено: нужен независимый внешний probe. Локальный listen не доказывает публичную доступность.",
    }


def copy(text):
    if windows():
        from .windows import literal, powershell

        powershell("Set-Clipboard -Value " + literal(text))
        return "Адрес скопирован"
    choices = []
    if os.environ.get("WAYLAND_DISPLAY"):
        choices.append(["wl-copy"])
    if os.environ.get("DISPLAY"):
        choices += [
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ]
    for args in choices:
        if shutil.which(args[0]):
            try:
                subprocess.run(
                    args,
                    input=text.encode(),
                    check=True,
                    timeout=3,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return "Адрес скопирован"
            except (subprocess.SubprocessError, OSError):
                continue
    # OSC52 has no acknowledgement: never claim verified clipboard success.
    if sys.stdout.isatty() and os.environ.get("TERM", "dumb") != "dumb":
        sys.stdout.write("\033]52;c;" + base64.b64encode(text.encode()).decode() + "\a")
        sys.stdout.flush()
        return "OSC 52 отправлен; терминал может потребовать разрешение. Проверьте буфер обмена."
    raise Error("Буфер недоступен. Скопируйте адрес вручную: " + text)


def probe(address, paths=None):
    """Independent Minecraft SLP check; address is shared with mcsrvstat.us."""
    from urllib.parse import quote

    from .sources import metadata

    try:
        host, port = address.rsplit(":", 1)
        public_ipv4(host)
        if not 1024 <= int(port) <= 65535:
            raise ValueError
    except (ValueError, Error) as exc:
        raise Error("Укажите публичный IPv4:порт (1024–65535).") from exc
    result = metadata(
        "https://api.mcsrvstat.us/3/" + quote(address, safe=":[]"), timeout=8
    )
    import json
    import time

    from .config import atomic

    paths = paths or Paths()
    identifier = paths.load().server_id
    identity_verified = bool(
        result.get("online")
        and identifier
        and identifier in " ".join(result.get("motd", {}).get("clean", []))
    )
    atomic(
        paths.data / "network-probe.json",
        json.dumps(
            {
                "address": address,
                "online": result.get("online", False),
                "identity_verified": identity_verified,
                "server_id": identifier,
                "checked_at": time.time(),
                "cached": result.get("debug", {}).get("cachehit", False),
            }
        ),
    )
    return {
        "online": result.get("online", False),
        "version": result.get("version", "—"),
        "players": result.get("players", {}),
        "identity_verified": identity_verified,
        "source": "mcsrvstat.us",
        "cached": result.get("debug", {}).get("cachehit", False),
        "cache_expires": result.get("debug", {}).get("cacheexpire"),
        "note": "Внешняя Minecraft SLP-проверка; API кэширует ответ до 5 минут.",
    }


def minecraft_status(host="127.0.0.1", port=25565):
    """Minecraft Server List Ping, not merely a TCP connect."""
    import json
    import struct

    def varint(number):
        data = bytearray()
        number &= 0xFFFFFFFF
        while True:
            byte = number & 0x7F
            number >>= 7
            data.append(byte | (0x80 if number else 0))
            if not number:
                return bytes(data)

    def read_varint(sock):
        result = 0
        for offset in range(5):
            part = sock.recv(1)
            if not part:
                raise Error("Minecraft закрыл соединение.")
            result |= (part[0] & 0x7F) << (offset * 7)
            if not part[0] & 0x80:
                return result
        raise Error("Некорректный VarInt.")

    def read_exact(sock, size):
        data = b""
        if size > 2**20:
            raise Error("Ответ SLP слишком большой.")
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise Error("Неполный ответ SLP.")
            data += chunk
        return data

    with socket.create_connection((host, port), timeout=5) as sock:
        encoded = host.encode()
        handshake = (
            b"\x00"
            + varint(-1)
            + varint(len(encoded))
            + encoded
            + struct.pack(">H", port)
            + b"\x01"
        )
        sock.sendall(varint(len(handshake)) + handshake + b"\x01\x00")
        length = read_varint(sock)
        if length > 2**20:
            raise Error("Ответ SLP слишком большой.")
        if read_varint(sock) != 0:
            raise Error("Неожиданный пакет SLP.")
        size = read_varint(sock)
        result = json.loads(read_exact(sock, size))
        return {
            key: result[key]
            for key in ["version", "players", "description"]
            if key in result
        }


def reachability(address, paths=None):
    import json
    import time

    paths = paths or Paths()
    try:
        record = json.loads(
            (paths.data / "network-probe.json").read_text(encoding="utf-8")
        )
        if record["address"] != address or time.time() - record["checked_at"] > 300:
            return "Не проверена / проверка устарела"
        if not record.get("identity_verified", False) and record["online"]:
            return "Порт отвечает, но принадлежность этому серверу не подтверждена"
        return (
            "Доступен извне" if record["online"] else "Внешняя проверка: не отвечает"
        ) + (" (кэш API)" if record.get("cached") else "")
    except (OSError, ValueError, KeyError):
        return "Не проверена извне"


def connection_address(paths=None, refresh=False):
    """Only a fresh independently verified OWN server can be invited/copied."""
    import json
    import time

    from . import server

    paths = paths or Paths()
    address = addresses(paths, public=refresh)["public"]
    try:
        record = json.loads(
            (paths.data / "network-probe.json").read_text(encoding="utf-8")
        )
        identifier = paths.load().server_id
        age = time.time() - record["checked_at"]
        verified = (
            address != "Не определён"
            and record.get("address") == address
            and record.get("online") is True
            and record.get("identity_verified") is True
            and bool(identifier)
            and record.get("server_id") == identifier
            and 0 <= age <= 300
            and server.status(paths)["status"] == "Running"
        )
    except (OSError, ValueError, KeyError, TypeError):
        verified = False
    if not verified:
        raise Error(
            "Публичный вход не подтверждён: адрес для приглашения недоступен. Выполните uwucraft network configure и исправьте указанные причины. Исходящий IP из HTTPS-сервиса сам по себе не является адресом работающего сервера."
        )
    return address


def outbound_route(paths=None):
    """Detect the actual route to the HTTPS address-discovery host, not only /0.

    Full-tunnel VPNs often install two /1 routes while leaving the physical
    default route intact. Their exit IP must never be presented as a ready
    address of a server behind the home router.
    """
    import json
    import platform
    from urllib.parse import urlparse

    paths = paths or Paths()
    try:
        host = urlparse(paths.load().public_ip_url).hostname
        destination = socket.gethostbyname(host)
        if windows():
            from .windows import literal, powershell

            value = json.loads(
                powershell(
                    "$r=Find-NetRoute -RemoteIPAddress " + literal(destination) + "; "
                    "$a=Get-NetAdapter -InterfaceIndex $r.InterfaceIndex -ErrorAction SilentlyContinue; "
                    "@{interface=[string]$r.InterfaceAlias;description=[string]$a.InterfaceDescription} | ConvertTo-Json -Compress"
                )
            )
            description = value.get("description", "").lower()
            tunnel = any(
                name in description
                for name in [
                    "wireguard",
                    "wintun",
                    "amnezia",
                    "openvpn",
                    "tap-windows",
                    "tailscale",
                    "zerotier",
                ]
            )
            interface = value.get("interface", "")
        elif platform.system() == "Linux":
            routes = json.loads(
                subprocess.check_output(
                    ["ip", "-j", "-4", "route", "get", destination], timeout=5
                )
            )
            interface = routes[0]["dev"]
            links = json.loads(
                subprocess.check_output(
                    ["ip", "-d", "-j", "link", "show", "dev", interface], timeout=5
                )
            )
            kind = links[0].get("linkinfo", {}).get("info_kind", "")
            tunnel = kind in {"tun", "wireguard", "ipip", "gre", "vti"}
        else:
            return {"state": "unknown"}
        return {"state": "tunnel" if tunnel else "direct", "interface": interface}
    except (
        Error,
        OSError,
        ValueError,
        KeyError,
        IndexError,
        subprocess.SubprocessError,
    ):
        return {"state": "unknown"}
