"""Windows 10/11 native firewall, routing and per-user scheduled tasks."""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .config import Error, Paths
from .platforms import file_lock


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def powershell(script, timeout=30):
    prefix = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
    encoded = base64.b64encode((prefix + script).encode("utf-16-le")).decode()
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Error(
            "Не удалось выполнить Windows PowerShell. Проверьте Windows 10/11 и доступ к PowerShell."
        ) from exc
    if result.returncode:
        raise Error("Windows: " + result.stderr.strip()[-1500:])
    return result.stdout.strip()


def route():
    document = powershell("""
$r = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' |
    Where-Object {$_.NextHop -ne '0.0.0.0'} |
    Sort-Object @{Expression={$_.RouteMetric + (Get-NetIPInterface -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4).InterfaceMetric}} |
    Select-Object -First 1
if (!$r) {throw 'No default IPv4 route'}
$a = Get-NetIPAddress -InterfaceIndex $r.InterfaceIndex -AddressFamily IPv4 |
    Where-Object {$_.AddressState -eq 'Preferred' -and !$_.SkipAsSource} | Select-Object -First 1
if (!$a) {throw 'No preferred IPv4 address'}
@{gateway=$r.NextHop; local=$a.IPAddress} | ConvertTo-Json -Compress
""")
    import ipaddress

    try:
        value = json.loads(document)
        return str(ipaddress.IPv4Address(value["gateway"])), str(
            ipaddress.IPv4Address(value["local"])
        )
    except (ValueError, KeyError) as exc:
        raise Error("Windows не сообщил корректный IPv4-маршрут.") from exc


def firewall(port):
    if not 1024 <= port <= 65535:
        raise Error("Недопустимый TCP-порт.")
    # Run once elevated. No elevated persistent task ever executes user code.
    rule = f"uwucraft-TCP-{port}"
    operation = f"""
if (!(Get-NetFirewallProfile | Where-Object Enabled)) {{exit 0}}
Get-NetFirewallRule -Name {literal(rule)} -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -Name {literal(rule)} -DisplayName {literal(rule)} -Direction Inbound -Action Allow -Protocol TCP -LocalPort {port} -Profile Any -Enabled True | Out-Null
"""
    encoded = base64.b64encode(
        (
            "$ErrorActionPreference='Stop'; try {" + operation + "} catch {exit 1}"
        ).encode("utf-16-le")
    ).decode()
    output = powershell(
        f"""
if (!(Get-NetFirewallProfile | Where-Object Enabled)) {{Write-Output 'inactive'; exit 0}}
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($admin) {{
    {operation}
}} else {{
    $p = Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -ArgumentList @('-NoProfile','-NonInteractive','-EncodedCommand',{literal(encoded)}) -Wait -PassThru
    if ($p.ExitCode -ne 0) {{throw 'Firewall elevation failed or was cancelled'}}
}}
$r = Get-NetFirewallRule -Name {literal(rule)}
if ($r.Enabled -ne 'True' -or $r.Action -ne 'Allow') {{throw 'Firewall rule verification failed'}}
Write-Output 'configured'
""",
        timeout=180,
    )
    if output not in {"configured", "inactive"}:
        raise Error("Не удалось подтвердить правило Windows Defender Firewall.")
    return {"state": output, "persistent": output == "configured"}


def task_name(paths, kind="server"):
    digest = hashlib.sha256(str(paths.data.resolve()).casefold().encode()).hexdigest()[
        :12
    ]
    return f"uwucraft-{kind}-{digest}"


def task(paths, kind="server", start=False, daily=False):
    python = Path(sys.executable)
    hidden = python.with_name("pythonw.exe")
    executable = str(hidden if hidden.exists() else python)
    args = subprocess.list2cmdline(
        [
            "-m",
            "uwucraft.windows",
            kind,
            "--config",
            str(paths.config.parent),
            "--data",
            str(paths.data.parent),
        ]
    )
    name = task_name(paths, kind)
    trigger = (
        "New-ScheduledTaskTrigger -Daily -At '04:00'"
        if daily
        else "New-ScheduledTaskTrigger -AtLogOn -User $user"
    )
    powershell(
        f"""
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute {literal(executable)} -Argument {literal(args)} -WorkingDirectory {literal(paths.data)}
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$trigger = {trigger}
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName {literal(name)} -Action $action -Principal $principal -Trigger $trigger -Settings $settings -Description 'uwucraft managed per-user task' -Force | Out-Null
"""
        + (f"Start-ScheduledTask -TaskName {literal(name)}" if start else "")
    )
    return name


def java_path(path):
    """The Windows JRE launcher uses ANSI paths for DLL discovery on some builds.

    8.3 paths keep Java usable under a Unicode Windows username. If 8.3 names
    are disabled, never return an executable that will silently fail to load.
    """
    import ctypes

    value = str(Path(path).absolute())
    if value.isascii():
        return value
    buffer = ctypes.create_unicode_buffer(32768)
    function = ctypes.windll.kernel32.GetShortPathNameW
    function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    count = function(value, buffer, len(buffer))
    if count and count < len(buffer) and buffer.value.isascii():
        return buffer.value
    raise Error(
        "Java Windows не поддерживает этот Unicode-путь при отключённых коротких именах 8.3. Задайте XDG_DATA_HOME на каталог с ASCII-путём (например C:\\uwucraft-data) и повторите установку."
    )


def pipe_address(paths):
    return "\\\\.\\pipe\\" + task_name(paths, "console")


def pipe_key(paths):
    file = paths.data / "console.key"
    paths.data.mkdir(parents=True, exist_ok=True)
    try:
        with file.open("xb") as stream:
            stream.write(os.urandom(32))
    except FileExistsError:
        pass
    key = file.read_bytes()
    if len(key) != 32:
        raise Error("Повреждён ключ локальной консоли.")
    return key


def wait_watchdog(paths, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        try:
            with (paths.data / "watchdog.lock").open("a+") as file, file_lock(file):
                return
        except (BlockingIOError, PermissionError):
            if time.monotonic() >= deadline:
                raise Error(
                    "Windows supervisor уже работает или перезапускает сервер. Сначала выполните uwucraft stop."
                ) from None
            time.sleep(0.1)


def run(paths):
    from . import server

    # One watchdog per data directory, shared by logon task and manual CLI start.
    with (paths.data / "watchdog.lock").open("a+") as file:
        try:
            with file_lock(file):
                stop_file = paths.data / "watchdog-stop"
                stop_file.unlink(missing_ok=True)
                failures = []
                while True:
                    started = time.monotonic()
                    code = server.supervise()
                    if code == 0 or stop_file.exists():
                        return
                    failures = [t for t in failures if started - t < 300] + [started]
                    if len(failures) >= 5:
                        raise Error(
                            "Пять падений за 5 минут. Исправьте ошибку в logs перед новым запуском."
                        )
                    for _ in range(100):
                        if stop_file.exists():
                            return
                        time.sleep(0.1)
        except (BlockingIOError, PermissionError):
            return  # Another watchdog already owns this installation.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["server", "network", "backup"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--data", required=True)
    options = parser.parse_args()
    os.environ["XDG_CONFIG_HOME"] = options.config
    os.environ["XDG_DATA_HOME"] = options.data
    paths = Paths()
    paths.data.mkdir(parents=True, exist_ok=True)
    with (paths.data / f"{options.kind}-task.log").open("a", encoding="utf-8") as log:
        sys.stdout = sys.stderr = log
        if options.kind == "server":
            run(paths)
        elif options.kind == "network":
            from .portmap import maintain

            maintain()
        else:
            from .scheduled import main as backup

            backup()


if __name__ == "__main__":
    main()
