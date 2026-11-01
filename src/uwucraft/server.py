"""A private Unix-socket console; Java never exposes remote administration."""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

from .config import Error, Paths, atomic, memory_mb


def status(paths=None):
    paths = paths or Paths()
    if not paths.state.exists():
        return {"status": "Stopped"}
    state = {}
    try:
        state = json.loads(paths.state.read_text())
        process = psutil.Process(state["pid"])
        if (
            abs(process.create_time() - state["created"]) > 0.1
            or not process.is_running()
            or process.status() == psutil.STATUS_ZOMBIE
        ):
            return {
                "status": "Crashed"
                if state["status"] not in {"Stopped", "Stopping"}
                else "Stopped"
            }
        state["ram_mb"] = round(process.memory_info().rss / 1024**2)
        state["uptime"] = int(time.time() - state["created"])
        return state
    except psutil.NoSuchProcess:
        return {
            "status": "Stopped"
            if state.get("status") in {"Stopped", "Stopping"}
            else "Crashed"
        }
    except (OSError, ValueError, KeyError, psutil.Error):
        return {"status": "Stopped"}


def command(text, paths=None):
    paths = paths or Paths()
    if "\n" in text or "\r" in text or not text.strip() or len(text.encode()) > 4096:
        raise Error("Ожидается одна команда длиной до 4096 байт.")
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(5)
            client.connect(str(paths.socket))
            client.sendall(text.strip().encode() + b"\n")
            reply = client.recv(256)
            if reply != b"OK":
                raise Error("Сервер не принял команду.")
    except OSError:
        raise Error(
            "Консоль недоступна. Запустите сервер и проверьте status/logs."
        ) from None


def start(paths=None):
    paths = paths or Paths()
    with paths.lock():
        if status(paths)["status"] in {"Running", "Starting", "Stopping"}:
            raise Error("Сервер уже запущен или останавливается.")
        if (
            not (paths.server / "eula.txt").exists()
            or "eula=true" not in (paths.server / "eula.txt").read_text()
        ):
            raise Error("Сначала uwucraft auto с явным принятием EULA.")
        if (
            not (paths.server / "server.jar").exists()
            and not (paths.server / "run.sh").exists()
        ):
            raise Error("Сервер не установлен.")
        paths.data.mkdir(parents=True, exist_ok=True)
        with (paths.data / "supervisor.log").open("ab") as log:
            subprocess.Popen(
                [sys.executable, "-m", "uwucraft.runner"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        for _ in range(100):
            if status(paths)["status"] in {"Starting", "Running"}:
                return
            time.sleep(0.1)
        raise Error("Не удалось запустить supervisor; проверьте supervisor.log.")


def stop(paths=None):
    paths = paths or Paths()
    if status(paths)["status"] == "Stopped":
        return
    command("stop", paths)
    for _ in range(1200):
        if status(paths)["status"] in {"Stopped", "Crashed"}:
            return
        time.sleep(0.1)
    raise Error(
        "Остановка не завершена за 120 секунд; проверьте логи. Принудительный kill не выполнен."
    )


def logs(paths=None, lines=160):
    paths = paths or Paths()
    file = paths.server / "logs/latest.log"
    if not file.exists():
        return "Логов ещё нет. Установите и запустите сервер."
    # Bound I/O on every TUI refresh even when a server has a very large log.
    with file.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 512 * 1024))
        return "\n".join(
            stream.read()
            .decode(errors="replace")
            .splitlines()[-max(1, min(lines, 5000)) :]
        )


def supervise():
    paths = Paths()
    config = paths.load()
    paths.data.mkdir(parents=True, exist_ok=True, mode=0o700)
    import fcntl

    guard = (paths.data / "runner.lock").open("a")
    try:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise Error("Supervisor уже работает.") from None
    paths.socket.unlink(missing_ok=True)
    ram = memory_mb(config.memory)
    args = [
        config.java,
        f"-Xms{min(1024, ram)}M",
        f"-Xmx{ram}M",
        "-XX:+UseG1GC",
        "-XX:+ParallelRefProcEnabled",
        "-XX:+ExitOnOutOfMemoryError",
    ]
    if config.loader in {"forge", "neoforge"}:
        argument_files = list((paths.server / "libraries").glob("**/unix_args.txt"))
        if len(argument_files) != 1:
            raise Error("Не найден однозначный Forge/NeoForge unix_args.txt.")
        args += ["@" + str(argument_files[0]), "nogui"]
    else:
        args += ["-jar", "server.jar", "nogui"]
    process = subprocess.Popen(
        args,
        cwd=paths.server,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    state = {
        "pid": process.pid,
        "created": psutil.Process(process.pid).create_time(),
        "status": "Starting",
    }
    mutex = threading.Lock()

    def persist(value):
        state["status"] = value
        atomic(paths.state, json.dumps(state))

    persist("Starting")

    def send(text):
        with mutex:
            if process.poll() is None:
                process.stdin.write(text + "\n")
                process.stdin.flush()
                if text == "stop":
                    persist("Stopping")

    def handle_signal(*_):
        send("stop")

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(paths.socket))
    os.chmod(paths.socket, 0o600)
    listener.listen(8)
    listener.settimeout(1)

    def serve():
        while process.poll() is None:
            try:
                client, _ = listener.accept()
                with client:
                    client.settimeout(2)
                    data = b""
                    while b"\n" not in data and len(data) <= 4096:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    text = data.decode().strip()
                    if (
                        text
                        and "\n" not in text
                        and "\r" not in text
                        and len(data) <= 4097
                    ):
                        send(text)
                        client.sendall(b"OK")
            except (OSError, UnicodeError):
                continue

    threading.Thread(target=serve, daemon=True).start()
    try:
        with (paths.data / "console.log").open("a") as log:
            for line in process.stdout:
                log.write(line)
                log.flush()
                if "Done (" in line and "For help" in line:
                    persist("Running")
        code = process.wait()
        persist("Stopped" if code == 0 else "Crashed")
    finally:
        listener.close()
        paths.socket.unlink(missing_ok=True)
    return code


def unit(paths=None, system=False):
    paths = paths or Paths()
    executable = str(Path(sys.executable).absolute())
    # System unit uses the owning account; an isolated service user is deliberately
    # not inferred from private home-directory installations.
    if system:
        raise Error(
            "Системная установка требует отдельного service-user и /var/lib. Используйте user service; см. README."
        )

    def quote(value):
        return (
            '"'
            + str(value).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
            + '"'
        )

    content = f"""[Unit]
Description=uwucraft Minecraft server
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5
[Service]
Type=simple
ExecStart={quote(executable)} -m uwucraft.runner
Environment="XDG_CONFIG_HOME={str(paths.config.parent).replace("%", "%%")}"
Environment="XDG_DATA_HOME={str(paths.data.parent).replace("%", "%%")}"
WorkingDirectory={str(paths.server).replace("%", "%%")}
Restart=on-failure
RestartSec=10
TimeoutStopSec=150
KillSignal=SIGTERM
KillMode=mixed
SendSIGKILL=no
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths={quote(paths.data)}
UMask=0077
[Install]
WantedBy=default.target
"""
    file = Path.home() / ".config/systemd/user/uwucraft.service"
    atomic(file, content)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    return file


def wait_ready(paths=None, timeout=240):
    paths = paths or Paths()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = status(paths)["status"]
        if state == "Running":
            return
        if state in {"Stopped", "Crashed"}:
            raise Error("Сервер завершился при запуске. Проверьте uwucraft logs.")
        time.sleep(1)
    raise Error("Не получено подтверждение готовности за 240 секунд; проверьте логи.")
