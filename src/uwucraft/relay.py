"""Opt-in public TCP relay for computers behind CGNAT.

The verified official bore client forwards bytes to the local Minecraft port.
No relay address is an invitation until an independent Minecraft probe matches
this server's MOTD identity. Minecraft and the user's world stay on this PC.
"""

import ipaddress
import json
import os
import platform
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


def provision(paths=None):
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
    if executable.is_file():
        return executable
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
    return executable


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
    with paths.lock():
        config = paths.load()
        config.public_access = "relay"
        config.relay_host = host
        paths.save(config)
    (paths.data / "relay-stop").unlink(missing_ok=True)
    if windows():
        from .windows import task

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
            ["systemctl", "--user", "enable", "--now", unit.name],
            check=True,
            timeout=15,
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = status(paths)
        if value:
            return value
        time.sleep(0.25)
    raise Error(
        "TCP relay не выдал адрес за отведённое время. Проверьте relay.log и доступ к relay-host:7835."
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


def _session(paths, executable, stopped):
    config = paths.load()
    host = config.relay_host
    # Resolve to a public IPv4 once, so the address and tested relay are identical.
    addresses = socket.getaddrinfo(host, 7835, socket.AF_INET, socket.SOCK_STREAM)
    ip = str(ipaddress.IPv4Address(addresses[0][4][0]))
    from .network import public_ipv4

    public_ipv4(ip)
    args = [
        str(executable),
        "local",
        str(config.port),
        "--local-host",
        "127.0.0.1",
        "--to",
        ip,
    ]
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if windows() else {}
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "NO_COLOR": "1"},
        **flags,
    )
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
        return process.wait(), published
    finally:
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
