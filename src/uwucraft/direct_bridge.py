"""Loopback-only bore transport with sockets bound to a physical adapter.

No default/host route is changed, and the VPN used by other applications stays
untouched. Windows filtering drivers can still veto traffic; report failure.
"""

import json
import secrets
import select
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

from .config import Error
from .platforms import windows


def physical_interface():
    if windows():
        from .windows import powershell

        document = powershell("""
$r = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' | Where-Object { (Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue).HardwareInterface } | Sort-Object RouteMetric | Select-Object -First 1
if (!$r) { throw 'No physical IPv4 default gateway' }
$a = Get-NetIPAddress -InterfaceIndex $r.InterfaceIndex -AddressFamily IPv4 | Where-Object { $_.AddressState -eq 'Preferred' } | Select-Object -First 1
@{device=$r.InterfaceAlias; index=$r.InterfaceIndex; address=$a.IPAddress} | ConvertTo-Json -Compress
""")
        return json.loads(document)
    routes = json.loads(
        subprocess.run(
            ["ip", "-j", "route", "show", "default"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout
    )
    for route in sorted(routes, key=lambda r: r.get("metric", 0)):
        device = route.get("dev", "")
        if not (Path("/sys/class/net") / device / "device").exists():
            continue
        details = json.loads(
            subprocess.run(
                ["ip", "-j", "address", "show", "dev", device],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout
        )
        for item in details[0].get("addr_info", []):
            if item.get("family") == "inet" and item.get("scope") == "global":
                return {
                    "device": device,
                    "index": details[0]["ifindex"],
                    "address": item["local"],
                }
    raise Error("Не найден физический IPv4-интерфейс с маршрутом по умолчанию.")


def connect(address, interface=None):
    interface = interface or physical_interface()
    stream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        stream.settimeout(8)
        stream.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        stream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if windows():
            stream.setsockopt(
                socket.IPPROTO_IP, 31, struct.pack("!I", interface["index"])
            )
        else:
            stream.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_BINDTODEVICE,
                interface["device"].encode() + b"\0",
            )
        stream.bind((interface["address"], 0))
        stream.connect((address, 7835))
        return stream
    except BaseException:
        stream.close()
        raise


def prepare(address):
    """Interactive configuration only; background relay never prompts for root."""
    from .network import public_ipv4

    public_ipv4(address)
    interface = physical_interface()
    try:
        with connect(address, interface):
            return interface
    except OSError as initial:
        if windows():
            raise Error(
                "Windows/VPN заблокировал прямой исходящий TCP 7835. Добавьте relay в исключения split tunneling VPN; маршрут сам по себе не отменяет блокировку WFP."
            ) from initial
        from . import privilege, relay_egress

        command = privilege.elevated(
            [
                "/usr/bin/python3",
                relay_egress.__file__,
                "--ip",
                address,
                "--device",
                interface["device"],
            ]
        )
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise Error(
                "Не удалось разрешить прямой TCP relay через физический интерфейс: "
                + result.stderr[-600:]
            ) from initial
        try:
            with connect(address, interface):
                return interface
        except OSError as exc:
            raise Error(
                "Физический TCP 7835 недоступен даже после узкого firewall-исключения. Проверьте провайдера или доступность relay."
            ) from exc


class DirectBridge:
    def __init__(self, address):
        self.address = address
        self.interface = physical_interface()
        # Prove a physical connection before bore can publish an address.
        with connect(address, self.interface):
            pass
        self.stopped = threading.Event()
        self.guard = threading.Lock()
        self.streams = set()
        self.slots = threading.BoundedSemaphore(128)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # bore fixes its control port at 7835; isolate profiles by loopback IP.
        self.listen_ip = (
            f"127.64.{secrets.randbelow(254) + 1}.{secrets.randbelow(254) + 1}"
        )
        self.listener.bind((self.listen_ip, 7835))
        self.listener.listen(64)
        self.listener.settimeout(1)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stopped.is_set():
            try:
                incoming, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self.slots.acquire(blocking=False):
                incoming.close()
                continue
            threading.Thread(target=self.forward, args=(incoming,), daemon=True).start()

    def forward(self, incoming):
        streams = [incoming]
        try:
            outgoing = connect(self.address)
            streams.append(outgoing)
            for stream in streams:
                stream.settimeout(10)
            with self.guard:
                self.streams.update(streams)
            readable = list(streams)
            while readable and not self.stopped.is_set():
                ready, _, _ = select.select(readable, [], [], 1)
                for stream in ready:
                    other = outgoing if stream is incoming else incoming
                    data = stream.recv(65536)
                    if data:
                        other.sendall(data)
                    else:
                        readable.remove(stream)
                        try:
                            other.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
        except OSError as exc:
            print("Physical relay stream: " + str(exc), file=sys.stderr, flush=True)
        finally:
            with self.guard:
                for stream in streams:
                    self.streams.discard(stream)
                    stream.close()
            self.slots.release()

    def close(self):
        self.stopped.set()
        self.listener.close()
        with self.guard:
            for stream in self.streams:
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.thread.join(timeout=2)
