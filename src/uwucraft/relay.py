"""Opt-in public TCP relay for computers behind CGNAT.

The verified official bore client forwards bytes to the local Minecraft port.
No relay address is an invitation until an independent Minecraft probe matches
this server's MOTD identity. Minecraft and the user's world stay on this PC.
"""

import hashlib
import ipaddress
import json
import os
import platform
import queue
import re
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import psutil

from .config import Error, Paths, atomic
from .platforms import file_lock, windows
from .sources import download, metadata


def provision(paths=None, refresh=False):
    paths = paths or Paths()
    architecture = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }.get(platform.machine().lower())
    if windows():
        if architecture != "x86_64":
            raise Error("Автоустановка TCP relay на Windows поддерживает x64.")
        suffix = "x86_64-pc-windows-msvc.zip"
    else:
        suffix = str(architecture) + "-unknown-linux-musl.tar.gz"
    manifest = paths.data / "tools/bore-cache.json"
    if not refresh:
        try:
            cached = json.loads(manifest.read_text(encoding="utf-8"))
            if cached["platform"] == suffix and re.fullmatch(
                r"bore-v[0-9.]+(?:\.exe)?", cached["name"]
            ):
                binary = manifest.parent / cached["name"]
                with binary.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if digest == cached["sha256"]:
                    return binary
        except (OSError, ValueError, KeyError, TypeError):
            pass
    release = metadata("https://api.github.com/repos/ekzhang/bore/releases/latest")
    asset = next((a for a in release["assets"] if a["name"].endswith(suffix)), None)
    if not asset or not re.fullmatch(
        r"sha256:[0-9a-fA-F]{64}", asset.get("digest") or ""
    ):
        raise Error("Официальный bore binary с SHA-256 не найден для этой платформы.")
    tag = release["tag_name"]
    if not re.fullmatch(r"v[0-9.]+", tag):
        raise Error("Некорректная версия bore.")
    executable = paths.data / "tools" / ("bore-" + tag + (".exe" if windows() else ""))
    paths.data.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=paths.data) as directory:
        archive = Path(directory) / "bore.archive"
        download(
            asset["browser_download_url"],
            archive,
            {"sha256": asset["digest"].split(":", 1)[1]},
        )
        name = "bore.exe" if windows() else "bore"
        if windows():
            with zipfile.ZipFile(archive) as file:
                items = [
                    i
                    for i in file.infolist()
                    if Path(i.filename).name == name
                    and not i.is_dir()
                    and i.file_size <= 30 * 1024**2
                ]
                if len(items) != 1:
                    raise Error("Некорректный архив bore.")
                data = file.read(items[0])
        else:
            with tarfile.open(archive) as file:
                items = [
                    i
                    for i in file.getmembers()
                    if Path(i.name).name == name
                    and i.isfile()
                    and i.size <= 30 * 1024**2
                ]
                if len(items) != 1:
                    raise Error("Некорректный архив bore.")
                data = file.extractfile(items[0]).read()
        atomic(executable, data, 0o700)
        atomic(
            manifest,
            json.dumps(
                {
                    "name": executable.name,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "platform": suffix,
                }
            ),
        )
    return executable


def resolve_host(host, paths):
    cache = paths.data / "relay-endpoint.json"
    cached = ""
    try:
        value = json.loads(cache.read_text(encoding="utf-8"))
        if value["host"] == host:
            from .network import public_ipv4

            cached = public_ipv4(value["ip"])
            if 0 <= time.time() - value["checked_at"] < 3600:
                return cached
    except (OSError, ValueError, KeyError, TypeError, Error):
        pass
    result = queue.Queue(maxsize=1)

    def lookup():
        try:
            value = socket.getaddrinfo(host, 7835, socket.AF_INET, socket.SOCK_STREAM)[
                0
            ][4][0]
            result.put(value)
        except OSError as exc:
            result.put(exc)

    threading.Thread(target=lookup, daemon=True).start()
    try:
        value = result.get(timeout=4)
        if isinstance(value, Exception):
            raise value
        from .network import public_ipv4

        ip = public_ipv4(value)
        atomic(cache, json.dumps({"host": host, "ip": ip, "checked_at": time.time()}))
        return ip
    except (OSError, queue.Empty, Error) as exc:
        if cached:
            return cached
        raise Error(
            "DNS relay не ответил; сохранённого проверяемого IP ещё нет."
        ) from exc


def status(paths=None):
    paths = paths or Paths()
    try:
        value = json.loads((paths.data / "relay.json").read_text(encoding="utf-8"))
        process = psutil.Process(value["pid"])
        if (
            abs(process.create_time() - value["created"]) > 0.1
            or not process.is_running()
            or process.status() == psutil.STATUS_ZOMBIE
        ):
            return {}
        return value
    except (OSError, ValueError, KeyError, psutil.Error):
        return {}


def setup(paths=None, host="bore.pub", timeout=45):
    paths = paths or Paths()
    if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?", host):
        raise Error("Некорректное имя TCP relay.")
    from . import server

    if server.status(paths)["status"] != "Running":
        raise Error("Сначала запустите Minecraft и дождитесь готовности.")
    provision(paths)
    config = paths.load()
    desired = "system"
    preparation_error = ""
    if config.relay_transport not in {"auto", "system", "physical"}:
        raise Error("relay_transport: auto, system или physical.")
    if config.relay_transport != "system":
        from .direct_bridge import prepare

        ip = resolve_host(host, paths)
        try:
            prepare(ip)
            desired = "physical"
        except (Error, OSError, ValueError, subprocess.SubprocessError) as exc:
            if config.relay_transport == "physical":
                raise Error("Прямой канал relay без VPN не готов: " + str(exc)) from exc
            preparation_error = str(exc)
            print(
                "TCP relay: физический канал недоступен, используется системный маршрут: "
                + str(exc),
                flush=True,
            )
    existing = status(paths)
    restart = not existing or existing.get("transport", "system") != desired
    with paths.lock():
        config = paths.load()
        config.public_access = "relay"
        config.relay_host = host
        paths.save(config)
    (paths.data / "relay-stop").unlink(missing_ok=True)
    if windows():
        from .windows import task

        if restart:
            from .windows import literal, powershell, task_name

            powershell(
                "Stop-ScheduledTask -TaskName "
                + literal(task_name(paths, "relay"))
                + " -ErrorAction SilentlyContinue"
            )
        task(paths, "relay", start=True)
    else:
        from .server import unit_quote

        unit = (
            Path.home()
            / ".config/systemd/user"
            / f"uwucraft-relay@{paths.profile}.service"
        )
        environment = "".join(
            "Environment=" + unit_quote(k + "=" + v) + "\n"
            for k, v in paths.environment().items()
        )
        atomic(
            unit,
            f"""[Unit]
Description=uwucraft public TCP relay
After=network-online.target {paths.unit_name}
Wants=network-online.target {paths.unit_name}
[Service]
ExecStart={unit_quote(sys.executable)} -m uwucraft.relay
{environment}Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths={unit_quote(paths.data)} {unit_quote(paths.config)}
UMask=0077
[Install]
WantedBy=default.target
""",
        )
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=15)
        subprocess.run(
            ["systemctl", "--user", "enable", unit.name],
            check=True,
            timeout=15,
        )
        # A live service can still hold deleted files after a server directory
        # was recreated. enable --now does not restart that stale process.
        subprocess.run(
            ["systemctl", "--user", "restart" if restart else "start", unit.name],
            check=True,
            timeout=20,
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = status(paths)
        if value:
            return (
                {**value, "transport_error": preparation_error}
                if preparation_error
                else value
            )
        time.sleep(0.25)
    raise Error(
        "Не удалось подключиться к TCP relay после автоматического перезапуска. Повторные попытки продолжаются в фоне; проверьте исходящий TCP 7835 и доступность bore.pub."
    )


def stop(paths=None):
    paths = paths or Paths()
    atomic(paths.data / "relay-stop", "stop")
    value = status(paths)
    if value:
        try:
            psutil.Process(value["pid"]).terminate()
        except psutil.NoSuchProcess:
            pass
    if not windows():
        unit = (
            Path.home()
            / ".config/systemd/user"
            / f"uwucraft-relay@{paths.profile}.service"
        )
        if unit.exists():
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", unit.name],
                check=True,
                timeout=30,
            )
    with paths.lock():
        config = paths.load()
        config.public_access = "direct"
        paths.save(config)
    (paths.data / "relay.json").unlink(missing_ok=True)


def preferred_port(paths):
    try:
        value = int((paths.data / "relay-port").read_text(encoding="ascii"))
        return value if 1024 <= value <= 65535 else 0
    except (OSError, ValueError):
        return 0


def _session(paths, executable, stopped):
    config = paths.load()
    host = config.relay_host
    # Resolve to a public IPv4 once, so the address and tested relay are identical.
    ip = str(ipaddress.IPv4Address(resolve_host(host, paths)))
    from .network import public_ipv4

    public_ipv4(ip)
    bridge = None
    transport_error = ""
    if config.relay_transport != "system":
        from .direct_bridge import DirectBridge

        try:
            bridge = DirectBridge(ip)
        except (Error, OSError, ValueError, subprocess.SubprocessError) as exc:
            if config.relay_transport == "physical":
                raise Error("Физический канал relay недоступен: " + str(exc)) from exc
            transport_error = str(exc)
    args = [
        str(executable),
        "local",
        str(config.port),
        "--local-host",
        "127.0.0.1",
        "--to",
        bridge.listen_ip if bridge else ip,
        "--port",
        str(preferred_port(paths)),
    ]
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if windows() else {}
    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "NO_COLOR": "1", "RUST_LOG": "info"},
            **flags,
        )
    except BaseException:
        if bridge:
            bridge.close()
        raise
    created = psutil.Process(process.pid).create_time()

    def shutdown(*_):
        stopped[0] = True
        if process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    published = False
    finished = threading.Event()

    def verify_periodically():
        while not finished.wait(180):
            value = status(paths)
            if value and value.get("pid") == process.pid:
                try:
                    from .network import probe

                    probe(value["address"], paths)
                except (Error, OSError, ValueError):
                    pass

    verifier = threading.Thread(target=verify_periodically, daemon=True)
    verifier.start()

    def connection_timeout():
        if not published and process.poll() is None:
            process.kill()

    startup_timer = threading.Timer(20, connection_timeout)
    startup_timer.daemon = True
    startup_timer.start()
    try:
        with (paths.data / "relay.log").open("a", encoding="utf-8") as log:
            for line in process.stdout:
                log.write(line)
                log.flush()
                match = re.search(r"remote_port[= :]+(\d+)", line) or re.search(
                    r"listening at [^\s:]+:(\d+)", line
                )
                if match:
                    port = int(match[1])
                    if not 1024 <= port <= 65535:
                        raise Error("Relay выдал недопустимый порт.")
                    atomic(paths.data / "relay-port", str(port))
                    address = f"{ip}:{port}"
                    (paths.data / "network-probe.json").unlink(missing_ok=True)
                    atomic(
                        paths.data / "relay.json",
                        json.dumps(
                            {
                                "address": address,
                                "host": host,
                                "pid": process.pid,
                                "created": created,
                                "provider": "bore",
                                "transport": "physical" if bridge else "system",
                                "interface": bridge.interface["device"]
                                if bridge
                                else "",
                                "transport_error": transport_error,
                                "checked_at": time.time(),
                            }
                        ),
                    )
                    published = True
                    # Bounded independent verification; failure never enables copy.
                    try:
                        from .network import probe

                        probe(address, paths)
                    except (Error, OSError, ValueError):
                        pass
                if (paths.data / "relay-stop").exists():
                    shutdown()
        code = process.wait()
        if not published and code:
            (paths.data / "relay-port").unlink(missing_ok=True)
        return code, published
    finally:
        startup_timer.cancel()
        if bridge:
            bridge.close()
        finished.set()
        verifier.join(timeout=10)
        (paths.data / "relay.json").unlink(missing_ok=True)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def run(paths=None):
    paths = paths or Paths()
    executable = provision(paths)
    with (paths.data / "relay.lock").open("a+") as guard, file_lock(guard):
        stopped = [False]
        while not stopped[0] and not (paths.data / "relay-stop").exists():
            try:
                _session(paths, executable, stopped)
            except (Error, OSError, ValueError) as exc:
                print("TCP relay: " + str(exc), flush=True)
            for _ in range(100):
                if stopped[0] or (paths.data / "relay-stop").exists():
                    return
                time.sleep(0.1)


if __name__ == "__main__":
    run()
