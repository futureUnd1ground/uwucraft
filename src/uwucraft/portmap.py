"""Direct gateway mapping: UPnP IGD discovery and RFC 6886 NAT-PMP."""

import ipaddress
import json
import socket
import struct
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx

from .config import Error


@dataclass
class Mapping:
    protocol: str
    wan: str
    port: int
    lifetime: int


class MappingError(Error):
    def __init__(self, message, wan=""):
        super().__init__(message)
        self.wan = wan


def route():
    import subprocess

    try:
        routes = json.loads(
            subprocess.check_output(
                ["ip", "-j", "-4", "route", "show", "default"], timeout=5
            )
        )
        routes = sorted(
            (r for r in routes if r.get("gateway")), key=lambda r: r.get("metric", 0)
        )
        if not routes:
            raise Error("Нет IPv4-шлюза. Проверьте подключение к маршрутизатору.")
        item = routes[0]
        addresses = json.loads(
            subprocess.check_output(
                ["ip", "-j", "-4", "addr", "show", "dev", item["dev"]], timeout=5
            )
        )
        local = next(
            a["local"]
            for interface in addresses
            for a in interface["addr_info"]
            if a["family"] == "inet"
        )
        ipaddress.IPv4Address(local)
        ipaddress.IPv4Address(item["gateway"])
        return item["gateway"], local
    except (
        OSError,
        ValueError,
        KeyError,
        StopIteration,
        subprocess.SubprocessError,
    ) as exc:
        raise Error(
            "Не удалось определить IPv4-шлюз. Установите iproute2 и проверьте default route."
        ) from exc


def natpmp(gateway, local, port, lifetime=3600):
    """A connected UDP socket accepts responses only from this gateway."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((local, 0))
        sock.connect((gateway, 5351))

        def exchange(request, opcode, length):
            for delay in (0.5, 1, 2):
                sock.settimeout(delay)
                sock.send(request)
                try:
                    reply = sock.recv(64)
                except TimeoutError:
                    continue
                except OSError as exc:
                    raise MappingError(
                        "NAT-PMP: шлюз не принимает запросы UDP 5351. Включите NAT-PMP в маршрутизаторе или настройте проброс TCP вручную."
                    ) from exc
                if len(reply) != length:
                    continue
                version, response, code = struct.unpack("!BBH", reply[:4])
                if version != 0 or response != opcode + 128:
                    continue
                if code:
                    raise MappingError(
                        f"NAT-PMP: код {code}; разрешите автоматический проброс в маршрутизаторе."
                    )
                return reply
            raise MappingError("NAT-PMP: маршрутизатор не ответил на UDP 5351.")

        reply = exchange(b"\x00\x00", 0, 12)
        wan = socket.inet_ntoa(reply[8:12])
        request = struct.pack("!BBHHHI", 0, 2, 0, port, port, lifetime)
        try:
            reply = exchange(request, 2, 16)
        except MappingError as exc:
            exc.wan = wan
            raise
        _, _, _, _, internal, external, granted = struct.unpack("!BBHIHHI", reply)
        if internal == port and external != port:
            # Roll back only the mapping just assigned to this client/port.
            try:
                exchange(struct.pack("!BBHHHI", 0, 2, 0, port, external, 0), 2, 16)
            except (MappingError, OSError):
                pass
        if internal != port or external != port or (lifetime and not granted):
            raise MappingError(
                "NAT-PMP не выделил выбранный TCP-порт; проверьте занятость порта на маршрутизаторе.",
                wan,
            )
        return Mapping("NAT-PMP", wan, external, granted)


def _gateway_url(url, gateway):
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname != gateway
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise MappingError(
            "UPnP отклонён: устройство предлагает control URL вне IPv4-шлюза."
        )
    return url


def _xml(data):
    if (
        len(data) > 1024 * 1024
        or b"<!DOCTYPE" in data.upper()
        or b"<!ENTITY" in data.upper()
    ):
        raise MappingError("UPnP: небезопасное XML-описание устройства.")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise MappingError("UPnP: повреждённое XML-описание устройства.") from exc


def _request(method, url, gateway, **kwargs):
    _gateway_url(url, gateway)
    # Never use an HTTP proxy or follow a gateway's redirects.
    try:
        with httpx.Client(timeout=4, trust_env=False, follow_redirects=False) as client:
            with client.stream(method, url, **kwargs) as response:
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 1024 * 1024:
                        raise MappingError("UPnP: ответ превышает 1 MiB.")
                if response.is_redirect:
                    raise MappingError("UPnP: redirect шлюза запрещён.")
                return response.status_code, bytes(data)
    except httpx.HTTPError as exc:
        raise MappingError("UPnP: не удалось обратиться к маршрутизатору.") from exc


class SoapFault(MappingError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"UPnP: SOAP fault {code}.")


def _soap(control, service, action, arguments, gateway):
    values = "".join(
        f"<{key}>{escape(str(value))}</{key}>" for key, value in arguments.items()
    )
    body = (
        f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{service}">{values}</u:{action}></s:Body></s:Envelope>'
    )
    status, data = _request(
        "POST",
        control,
        gateway,
        content=body.encode(),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{service}#{action}"',
        },
    )
    root = _xml(data)
    values = {node.tag.split("}")[-1]: node.text or "" for node in root.iter()}
    if "errorCode" in values:
        raise SoapFault(values["errorCode"])
    if status != 200 or action + "Response" not in values:
        raise MappingError(f"UPnP: действие {action} не подтверждено (HTTP {status}).")
    return values


def upnp(gateway, local, port, lifetime=3600):
    message = (
        "M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\nMX: 2\r\n'
        "ST: ssdp:all\r\n\r\n"
    )
    locations = set()
    import time

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.bind((local, 0))
        sock.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local)
        )
        sock.sendto(message.encode(), ("239.255.255.250", 1900))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            try:
                data, peer = sock.recvfrom(8192)
            except TimeoutError:
                break
            if peer[0] != gateway:
                continue
            headers = dict(
                line.split(":", 1)
                for line in data.decode(errors="replace").split("\r\n")
                if ":" in line
            )
            location = next(
                (v.strip() for k, v in headers.items() if k.lower() == "location"), ""
            )
            if location and len(locations) < 4:
                locations.add(_gateway_url(location, gateway))
    if not locations:
        raise MappingError(
            "UPnP IGD не найден. Включите UPnP в маршрутизаторе или настройте TCP port forwarding вручную."
        )
    failures = []
    for location in sorted(locations):
        status, data = _request("GET", location, gateway)
        if status != 200:
            continue
        root = _xml(data)
        base = root.findtext("{*}URLBase") or location
        for item in root.findall(".//{*}service"):
            service = item.findtext("{*}serviceType") or ""
            if service not in {
                "urn:schemas-upnp-org:service:WANIPConnection:1",
                "urn:schemas-upnp-org:service:WANIPConnection:2",
                "urn:schemas-upnp-org:service:WANPPPConnection:1",
            }:
                continue
            control = _gateway_url(
                urljoin(base, item.findtext("{*}controlURL") or ""), gateway
            )
            wan = ""
            try:
                wan = _soap(control, service, "GetExternalIPAddress", {}, gateway)[
                    "NewExternalIPAddress"
                ]
                keys = {
                    "NewRemoteHost": "",
                    "NewExternalPort": port,
                    "NewProtocol": "TCP",
                }
                try:
                    existing = _soap(
                        control, service, "GetSpecificPortMappingEntry", keys, gateway
                    )
                    if existing.get("NewInternalClient") != local or existing.get(
                        "NewInternalPort"
                    ) != str(port):
                        raise MappingError(
                            "Выбранный TCP-порт уже проброшен на другое устройство; правило не изменено.",
                            wan,
                        )
                except SoapFault as fault:
                    if fault.code != "714":
                        raise
                arguments = keys | {
                    "NewInternalPort": port,
                    "NewInternalClient": local,
                    "NewEnabled": 1,
                    "NewPortMappingDescription": "uwucraft",
                    "NewLeaseDuration": lifetime,
                }
                try:
                    _soap(control, service, "AddPortMapping", arguments, gateway)
                except SoapFault as fault:
                    if fault.code != "725":
                        raise
                    arguments["NewLeaseDuration"] = 0
                    _soap(control, service, "AddPortMapping", arguments, gateway)
                return Mapping("UPnP", wan, port, arguments["NewLeaseDuration"])
            except MappingError as exc:
                if wan and not exc.wan:
                    exc.wan = wan
                failures.append(exc)
    if failures:
        raise failures[-1]
    raise MappingError(
        "Маршрутизатор не предоставляет WANIPConnection/WANPPPConnection для UPnP."
    )


def map_port(port, lifetime=3600):
    gateway, local = route()
    failures = []
    for method in (upnp, natpmp):
        try:
            return method(gateway, local, port, lifetime)
        except (MappingError, OSError, ValueError, KeyError) as exc:
            failures.append(exc)
    wan = next((e.wan for e in failures if isinstance(e, MappingError) and e.wan), "")
    raise MappingError(
        "Автопроброс TCP-порта не выполнен. " + " ".join(str(e) for e in failures), wan
    )


def maintain():
    """Renew a gateway lease; a successful mapping is not an external probe."""
    import time
    from dataclasses import asdict

    from . import network
    from .config import Paths, atomic

    paths = Paths()
    while True:
        try:
            mapping = map_port(paths.load().port)
            atomic(
                paths.data / "port-mapping.json",
                json.dumps(asdict(mapping) | {"checked_at": time.time()}),
            )
            ip = network.external_ipv4(paths)
            with paths.lock():
                config = paths.load()
                if config.public_address != ip:
                    config.public_address = ip
                    paths.save(config)
                    # Old network evidence must not survive a change in WAN.
                    (paths.data / "network-probe.json").unlink(missing_ok=True)
            delay = (
                max(15, min(600, mapping.lifetime // 2)) if mapping.lifetime else 600
            )
        except (Error, OSError, ValueError):
            print(
                "Не удалось продлить проброс порта; проверьте маршрутизатор.",
                flush=True,
            )
            delay = 30
        time.sleep(delay)


if __name__ == "__main__":
    maintain()
