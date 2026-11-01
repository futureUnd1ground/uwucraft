import base64
import ipaddress
import os
import shutil
import socket
import subprocess
import sys

from .config import Error, Paths
from .sources import get


def addresses(paths=None, public=False):
    config = (paths or Paths()).load()
    local = "127.0.0.1"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 80))
            local = sock.getsockname()[0]
    except OSError:
        pass
    # Select the physical default-route interface instead of a split-tunnel VPN.
    try:
        import json

        import psutil

        routes = json.loads(
            subprocess.check_output(["ip", "-j", "route", "show", "default"], timeout=3)
        )
        for route in routes:
            interface = route.get("dev", "")
            if interface.startswith(("tun", "tap", "wg", "amn")):
                continue
            candidates = psutil.net_if_addrs().get(interface, [])
            local = next(a.address for a in candidates if a.family == socket.AF_INET)
            break
    except (OSError, subprocess.SubprocessError, ValueError, StopIteration):
        pass
    from .tunnel import status as tunnel_status

    tunnel = tunnel_status(paths)
    ip = config.public_address
    if public and not ip:
        try:
            ip = str(ipaddress.ip_address(get(config.public_ip_url).text.strip()))
            if ":" in ip:
                ip = "[" + ip + "]"
        except (Error, ValueError):
            ip = ""
    return {
        "public": tunnel.get("address")
        or (f"{ip}:{config.port}" if ip else "Не определён"),
        "local": f"{local}:{config.port}",
        "playit": config.playit_address or "Не настроен",
        "port": config.port,
        "reachability": "Не удалось проверить извне",
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

    if not address or "/" in address or len(address) > 300:
        raise Error("Укажите hostname:port или IP:port.")
    result = metadata("https://api.mcsrvstat.us/3/" + quote(address, safe=":[]"))
    import json
    import time

    from .config import atomic

    paths = paths or Paths()
    atomic(
        paths.data / "network-probe.json",
        json.dumps(
            {
                "address": address,
                "online": result.get("online", False),
                "checked_at": time.time(),
                "cached": result.get("debug", {}).get("cachehit", False),
            }
        ),
    )
    return {
        "online": result.get("online", False),
        "version": result.get("version", "—"),
        "players": result.get("players", {}),
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
        record = json.loads((paths.data / "network-probe.json").read_text())
        if record["address"] != address or time.time() - record["checked_at"] > 300:
            return "Не проверена / проверка устарела"
        return (
            "Доступен извне" if record["online"] else "Внешняя проверка: не отвечает"
        ) + (" (кэш API)" if record.get("cached") else "")
    except (OSError, ValueError, KeyError):
        return "Не проверена извне"
