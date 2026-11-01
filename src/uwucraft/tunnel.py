"""Optional account-free TCP relay. The Minecraft process stays local."""

import json
import os
import platform
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import psutil

from .config import Error, Paths, atomic
from .sources import download, metadata


def provision(paths=None):
    paths = paths or Paths()
    executable = paths.data / "tools/bore"
    if executable.exists():
        return executable
    architecture = {"x86_64": "x86_64", "aarch64": "aarch64"}.get(platform.machine())
    if not architecture:
        raise Error("Bore binary не поддерживает эту архитектуру.")
    release = metadata("https://api.github.com/repos/ekzhang/bore/releases/latest")
    asset = next(
        (
            a
            for a in release["assets"]
            if a["name"].endswith(architecture + "-unknown-linux-musl.tar.gz")
        ),
        None,
    )
    if not asset or not (asset.get("digest") or "").startswith("sha256:"):
        raise Error("Нет бинарного bore с SHA-256 от GitHub.")
    paths.data.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=paths.data) as directory:
        archive = Path(directory) / "bore.tar.gz"
        download(
            asset["browser_download_url"],
            archive,
            {"sha256": asset["digest"].split(":", 1)[1]},
        )
        with tarfile.open(archive) as file:
            member = next(
                (
                    m
                    for m in file.getmembers()
                    if Path(m.name).name == "bore"
                    and m.isfile()
                    and m.size < 30 * 1024**2
                ),
                None,
            )
            if not member:
                raise Error("Некорректный архив bore.")
            atomic(executable, file.extractfile(member).read(), 0o700)
    return executable


def status(paths=None):
    paths = paths or Paths()
    file = paths.data / "tunnel.json"
    try:
        state = json.loads(file.read_text())
        process = psutil.Process(state["pid"])
        if (
            abs(process.create_time() - state["created"]) > 0.1
            or process.status() == psutil.STATUS_ZOMBIE
        ):
            return {}
        return state
    except (OSError, ValueError, KeyError, psutil.Error):
        return {}


def setup(host="bore.pub", paths=None):
    paths = paths or Paths()
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
        raise Error("Некорректное имя TCP relay.")
    provision(paths)
    atomic(paths.config / "tunnel-host.txt", host)
    unit = Path.home() / ".config/systemd/user/uwucraft-tunnel.service"

    def q(value):
        return (
            '"'
            + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
            + '"'
        )

    atomic(
        unit,
        f"""[Unit]
Description=uwucraft optional bore TCP tunnel
After=network-online.target uwucraft.service
Wants=network-online.target
[Service]
Type=simple
ExecStart={q(sys.executable)} -m uwucraft.tunnel
Environment={q("XDG_CONFIG_HOME=" + str(paths.config.parent))}
Environment={q("XDG_DATA_HOME=" + str(paths.data.parent))}
Restart=always
RestartSec=15
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths={q(paths.data)}
UMask=0077
[Install]
WantedBy=default.target
""",
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", "uwucraft-tunnel.service"],
        check=True,
    )
    return "TCP-туннель включён. Адрес: uwucraft network address. При перезапуске адрес может измениться; relay не даёт SLA."


def run():
    paths = Paths()
    executable = paths.data / "tools/bore"
    host = (paths.config / "tunnel-host.txt").read_text().strip()
    config = paths.load()
    process = subprocess.Popen(
        [
            str(executable),
            "local",
            str(config.port),
            "--local-host",
            "127.0.0.1",
            "--to",
            host,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env={**os.environ, "NO_COLOR": "1"},
    )
    created = psutil.Process(process.pid).create_time()

    def stop(*_):
        process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for line in process.stdout:
            print(line, end="", flush=True)
            # bore's structured tracing field is stable across supported releases.
            clean = re.sub(r"\x1b\[[0-9;]*m", "", line)
            match = re.search(r"remote_port[= :]+(\d+)", clean) or re.search(
                r"listening at [^\s:]+:(\d+)", clean
            )
            if match:
                port = int(match[1])
                atomic(
                    paths.data / "tunnel.json",
                    json.dumps(
                        {
                            "address": f"{host}:{port}",
                            "pid": process.pid,
                            "created": created,
                            "provider": "bore",
                            "host": host,
                            "port": port,
                        }
                    ),
                )
        return process.wait()
    finally:
        paths.data.joinpath("tunnel.json").unlink(missing_ok=True)
        if process.poll() is None:
            process.terminate()


if __name__ == "__main__":
    sys.exit(run())
