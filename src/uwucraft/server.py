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
from .platforms import file_lock, windows


def status(paths=None):
    paths = paths or Paths()
    if not paths.state.exists():
        return {"status": "Stopped"}
    state = {}
    try:
        state = json.loads(paths.state.read_text(encoding="utf-8"))
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
    if windows():
        from multiprocessing.connection import Client

        from .windows import pipe_address, pipe_key

        try:
            with Client(
                pipe_address(paths), family="AF_PIPE", authkey=pipe_key(paths)
            ) as client:
                client.send_bytes(text.strip().encode())
                if not client.poll(5) or client.recv_bytes(256) != b"OK":
                    raise Error("Сервер не принял команду.")
            return
        except (OSError, EOFError) as exc:
            raise Error(
                "Локальная консоль Windows недоступна. Проверьте status/logs."
            ) from exc
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
        if not (paths.server / "eula.txt").exists() or "eula=true" not in (
            paths.server / "eula.txt"
        ).read_text(encoding="utf-8"):
            raise Error("Сначала uwucraft auto с явным принятием EULA.")
        if (
            not (paths.server / "server.jar").exists()
            and not (paths.server / ("run.bat" if windows() else "run.sh")).exists()
        ):
            raise Error("Сервер не установлен.")
        paths.data.mkdir(parents=True, exist_ok=True)
        if windows():
            from .windows import wait_watchdog

            wait_watchdog(paths)
        with (paths.data / "supervisor.log").open("ab") as log:
            args = [sys.executable, "-m", "uwucraft.runner"]
            options = {"start_new_session": True}
            if windows():
                args = [
                    sys.executable,
                    "-m",
                    "uwucraft.windows",
                    "server",
                    "--config",
                    str(paths.root_config.parent),
                    "--data",
                    str(paths.root_data.parent),
                    "--profile",
                    paths.profile,
                ]
                options = {
                    "creationflags": subprocess.CREATE_NO_WINDOW
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                }
            env = os.environ.copy()
            env.update(paths.environment())
            subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                env=env,
                **options,
            )
        for _ in range(100):
            if status(paths)["status"] in {"Starting", "Running"}:
                return
            time.sleep(0.1)
        raise Error("Не удалось запустить supervisor; проверьте supervisor.log.")


def stop(paths=None):
    paths = paths or Paths()
    if windows():
        atomic(paths.data / "watchdog-stop", "stop")
    if status(paths)["status"] in {"Stopped", "Crashed"}:
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
    with (paths.data / "runner.lock").open("a+") as guard:
        try:
            with file_lock(guard):
                return _supervise(paths, config)
        except (BlockingIOError, PermissionError):
            raise Error("Supervisor уже работает.") from None


def _supervise(paths, config):
    paths.socket.unlink(missing_ok=True)
    ram = memory_mb(config.memory)
    args = [
        config.java,
        f"-Xms{min(1024, ram)}M",
        f"-Xmx{ram}M",
        "-XX:+UseG1GC",
        "-XX:+ParallelRefProcEnabled",
        "-XX:+ExitOnOutOfMemoryError",
        "-Dfile.encoding=UTF-8",
    ]
    if config.loader in {"forge", "neoforge"}:
        argument_name = "win_args.txt" if windows() else "unix_args.txt"
        argument_files = list((paths.server / "libraries").glob("**/" + argument_name))
        if len(argument_files) == 1:
            args += ["@" + str(argument_files[0].relative_to(paths.server)), "nogui"]
        elif (
            not argument_files
            and config.loader == "forge"
            and (paths.server / "server.jar").exists()
        ):
            args += ["-jar", "server.jar", "nogui"]
        else:
            raise Error(f"Не найден однозначный Forge/NeoForge {argument_name}.")
    else:
        args += ["-jar", "server.jar", "nogui"]
    process = subprocess.Popen(
        args,
        cwd=paths.server,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
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
    if windows():
        from multiprocessing.connection import Listener

        from .windows import pipe_address, pipe_key

        listener = Listener(
            pipe_address(paths), family="AF_PIPE", authkey=pipe_key(paths)
        )

        pipe_shutdown = threading.Event()

        def serve_pipe():
            from multiprocessing import AuthenticationError

            while True:
                try:
                    with listener.accept() as client:
                        if pipe_shutdown.is_set():
                            return
                        if not client.poll(2):
                            continue
                        text = client.recv_bytes(4096).decode()
                        if text and "\n" not in text and "\r" not in text:
                            send(text)
                            client.send_bytes(b"OK")
                except (OSError, EOFError, UnicodeError, AuthenticationError):
                    continue

        pipe_thread = threading.Thread(target=serve_pipe, daemon=True)
        pipe_thread.start()
    else:
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
        with (paths.data / "console.log").open("a", encoding="utf-8") as log:
            for line in process.stdout:
                log.write(line)
                log.flush()
                if "Done (" in line and "For help" in line:
                    persist("Running")
        code = process.wait()
        persist("Stopped" if code == 0 else "Crashed")
    finally:
        if windows():
            # PipeListener.close only closes queued handles, not an active
            # ConnectNamedPipe in another thread. Wake and join that accept
            # before reusing the pipe name for a restarted JVM.
            from multiprocessing.connection import Client

            pipe_shutdown.set()
            try:
                with Client(
                    pipe_address(paths), family="AF_PIPE", authkey=pipe_key(paths)
                ):
                    pass
            finally:
                pipe_thread.join(5)
        listener.close()
        paths.socket.unlink(missing_ok=True)
    return code


def unit(paths=None, system=False):
    paths = paths or Paths()
    if windows():
        from .windows import task

        return task(paths)
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
Environment="XDG_CONFIG_HOME={str(paths.root_config.parent).replace("%", "%%")}"
Environment="XDG_DATA_HOME={str(paths.root_data.parent).replace("%", "%%")}"
Environment="UWUCRAFT_PROFILE={paths.profile}"
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
    # An imported server can live outside paths.data (see importer); the unit
    # must allow writing there or the JVM could not save the world.
    writable = {str(paths.data), str(paths.server)}
    if len(writable) > 1:
        content = content.replace(
            f"ReadWritePaths={quote(paths.data)}",
            "ReadWritePaths=" + " ".join(quote(path) for path in sorted(writable)),
        )
    file = Path.home() / ".config/systemd/user" / paths.unit_name
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


def manage(action, paths=None):
    """One lifecycle authority for CLI, TUI and plugin setup."""
    paths = paths or Paths()
    if action not in {"start", "stop", "restart"}:
        raise Error("Действие: start/stop/restart.")
    unit_file = Path.home() / ".config/systemd/user" / paths.unit_name
    if not windows() and unit_file.exists():
        subprocess.run(
            ["systemctl", "--user", action, paths.unit_name], check=True, timeout=180
        )
        if action in {"start", "restart"}:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if status(paths)["status"] in {"Starting", "Running"}:
                    return
                time.sleep(0.1)
            raise Error(
                f"Supervisor не запустился. Проверьте journalctl --user -u {paths.unit_name}."
            )
    elif action == "restart":
        stop(paths)
        start(paths)
    else:
        {"start": start, "stop": stop}[action](paths)


def unit_quote(value):
    return (
        '"'
        + str(value).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
        + '"'
    )
