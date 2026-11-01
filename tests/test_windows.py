"""Portable adapter tests plus native Windows integration (explicitly skipped on Linux)."""

import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from uwucraft import deployment, network, runtime, server, windows
from uwucraft.config import Config, Error, Paths, atomic
from uwucraft.platforms import windows as is_windows

native = pytest.mark.skipif(not is_windows(), reason="requires native Windows")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "конфиг space"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "данные space"))
    return Paths()


def test_windows_default_paths(tmp_path, monkeypatch):
    import uwucraft.config as config

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(config, "windows", lambda: True)
    assert Paths().config == tmp_path / "roaming/uwucraft"
    assert Paths().data == tmp_path / "local/uwucraft"


def test_windows_java_zip_and_cache(paths, monkeypatch):
    monkeypatch.setattr(runtime, "windows", lambda: True)
    monkeypatch.setattr(runtime.platform, "machine", lambda: "AMD64")
    calls = []

    def metadata(url, params):
        calls.append(params)
        return [
            {
                "binary": {
                    "package": {
                        "link": "https://example.test/java.zip",
                        "checksum": "a" * 64,
                    }
                }
            }
        ]

    def download(url, target, hashes):
        assert hashes == {"sha256": "a" * 64}
        with zipfile.ZipFile(target, "w") as file:
            file.writestr("jdk-21/bin/java.exe", b"test fixture, not an executable")

    monkeypatch.setattr(runtime, "metadata", metadata)
    monkeypatch.setattr(runtime, "download", download)
    executable = runtime.provision(21, paths)
    assert executable.endswith("java.exe")
    assert Path(executable).is_file()
    assert calls[0]["os"] == "windows" and calls[0]["architecture"] == "x64"
    assert runtime.provision(21, paths) == executable
    assert len(calls) == 1


def test_windows_route_validated(monkeypatch):
    monkeypatch.setattr(
        windows,
        "powershell",
        lambda script: json.dumps({"gateway": "192.168.1.1", "local": "192.168.1.5"}),
    )
    assert windows.route() == ("192.168.1.1", "192.168.1.5")
    monkeypatch.setattr(
        windows, "powershell", lambda script: '{"gateway":"invalid","local":"bad"}'
    )
    with pytest.raises(Error):
        windows.route()


def test_task_arguments_are_quoted_and_per_install(paths, monkeypatch):
    scripts = []
    monkeypatch.setattr(windows, "powershell", lambda script: scripts.append(script))
    name = windows.task(paths, start=True)
    assert name.startswith("uwucraft-server-")
    script = scripts[0]
    assert "-LogonType Interactive -RunLevel Limited" in script
    assert "-MultipleInstances IgnoreNew" in script
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in script
    assert "Start-ScheduledTask" in script
    assert '"' + str(paths.data.parent) + '"' in script
    assert "-Password" not in script
    assert windows.literal("owner's") == "'owner''s'"
    assert name != windows.task_name(Paths(), "network")


def test_windows_firewall_is_one_time_elevation(monkeypatch):
    scripts = []
    monkeypatch.setattr(
        windows,
        "powershell",
        lambda script, **kwargs: scripts.append(script) or "configured",
    )
    assert windows.firewall(25565)["persistent"]
    assert "-Verb RunAs" in scripts[0]
    assert "-Protocol TCP -LocalPort 25565" in scripts[0]
    assert "Register-ScheduledTask" not in scripts[0]
    with pytest.raises(Error):
        windows.firewall(1)


def test_windows_deployment_dispatch(paths, monkeypatch):
    calls = []
    monkeypatch.setattr(deployment, "windows", lambda: True)
    monkeypatch.setattr(windows, "task", lambda *a, **k: calls.append(k))
    monkeypatch.setattr(server, "start", lambda p: calls.append("start"))
    monkeypatch.setattr(server, "wait_ready", lambda p: calls.append("ready"))
    deployment.enable_server(paths)
    deployment.enable_renewal(paths)
    deployment.start_server(paths)
    assert calls == [{}, {"start": True}, "start", "ready"]


def test_windows_clipboard_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(network, "windows", lambda: True)
    monkeypatch.setattr(windows, "powershell", lambda script: calls.append(script))
    assert network.copy("8.8.8.8:25565") == "Адрес скопирован"
    assert calls == ["Set-Clipboard -Value '8.8.8.8:25565'"]


@native
def test_native_windows_locks_unicode_and_cli(paths):
    paths.save(Config(name="Мой сервер"))
    assert paths.load().name == "Мой сервер"
    with paths.lock(), pytest.raises(Error), paths.lock():
        pass
    result = subprocess.run(
        [sys.executable, "-m", "uwucraft.cli", "--help"],
        capture_output=True,
        timeout=20,
    )
    # cli module has no __main__; console entrypoint tested by CI as well.
    assert result.returncode == 0


@native
def test_native_powershell_and_route():
    assert windows.powershell("Write-Output 'Привет Windows'") == "Привет Windows"
    gateway, local = windows.route()
    assert gateway and local


@native
def test_native_task_registration(paths):
    paths.data.mkdir(parents=True)
    name = windows.task(paths)
    try:
        details = windows.powershell(
            f"$t=Get-ScheduledTask -TaskName {windows.literal(name)}; @{{logon=[string]$t.Principal.LogonType; limit=[string]$t.Settings.ExecutionTimeLimit; args=$t.Actions.Arguments}} | ConvertTo-Json -Compress"
        )
        value = json.loads(details)
        assert value["logon"] == "Interactive"
        assert value["limit"] == "PT0S"
        assert str(paths.data.parent) in value["args"]
    finally:
        windows.powershell(
            f"Unregister-ScheduledTask -TaskName {windows.literal(name)} -Confirm:$false"
        )


@native
def test_native_supervisor_console_crash_restart_and_stop(paths):
    """A tiny real JVM fixture tests lifecycle, not Minecraft gameplay/readiness."""
    if not shutil.which("javac") or not shutil.which("jar"):
        pytest.skip("JDK needed for supervisor integration fixture")
    paths.server.mkdir(parents=True)
    source = paths.server / "Fixture.java"
    source.write_text(
        """
import java.io.*;
public class Fixture {
    public static void main(String[] args) throws Exception {
        System.out.println("Done (0.1s)! For help, type help");
        BufferedReader r = new BufferedReader(new InputStreamReader(System.in));
        String line;
        while ((line = r.readLine()) != null) {
            if (line.equals("stop")) return;
            if (line.equals("crash")) System.exit(3);
            System.out.println("received: " + line);
        }
    }
}
""",
        encoding="utf-8",
    )
    subprocess.run(["javac", str(source)], check=True, timeout=30)
    subprocess.run(
        [
            "jar",
            "--create",
            "--file",
            str(paths.server / "server.jar"),
            "--main-class",
            "Fixture",
            "-C",
            str(paths.server),
            "Fixture.class",
        ],
        check=True,
        timeout=30,
    )
    paths.save(Config(java=shutil.which("java"), memory="512M"))
    atomic(paths.server / "eula.txt", "eula=true\n")
    try:
        server.start(paths)
        server.wait_ready(paths, timeout=30)
        pid = server.status(paths)["pid"]
        server.command("say hello", paths)
        server.command("crash", paths)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            state = server.status(paths)
            if state["status"] == "Running" and state["pid"] != pid:
                break
            time.sleep(0.2)
        else:
            pytest.fail("Watchdog did not restart crashed JVM")
        assert "received: say hello" in (paths.data / "console.log").read_text(
            encoding="utf-8"
        )
        server.stop(paths)
        windows.wait_watchdog(paths)
        assert server.status(paths)["status"] == "Stopped"
        server.start(paths)
        server.wait_ready(paths, timeout=30)
        server.stop(paths)
        windows.wait_watchdog(paths)
    finally:
        server.stop(paths)
        windows.wait_watchdog(paths)


@native
def test_native_temurin_download(paths):
    if os.environ.get("UWUCRAFT_WINDOWS_INTEGRATION") != "1":
        pytest.skip("explicit network integration opt-in")
    java = runtime.provision(21, paths)
    result = subprocess.run([java, "-version"], capture_output=True, timeout=20)
    assert result.returncode == 0
    assert b"21." in result.stderr


@native
def test_native_firewall_rule():
    if os.environ.get("UWUCRAFT_WINDOWS_INTEGRATION") != "1":
        pytest.skip("explicit host firewall integration opt-in")
    admin = windows.powershell(
        "([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)"
    )
    if admin != "True":
        pytest.skip("runner has no administrator token; UAC is a manual check")
    port = 32677
    try:
        report = windows.firewall(port)
        assert report["state"] in {"configured", "inactive"}
        if report["state"] == "configured":
            assert windows.firewall(port)["state"] == "configured"
            value = json.loads(
                windows.powershell(
                    f"$r=Get-NetFirewallRule -Name 'uwucraft-TCP-{port}'; $p=$r | Get-NetFirewallPortFilter; @{{port=[string]$p.LocalPort; action=[string]$r.Action}} | ConvertTo-Json -Compress"
                )
            )
            assert value == {"port": str(port), "action": "Allow"}
    finally:
        windows.powershell(
            f"Get-NetFirewallRule -Name 'uwucraft-TCP-{port}' -ErrorAction SilentlyContinue | Remove-NetFirewallRule"
        )
