import json
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from uwucraft import server
from uwucraft.config import Config, Paths, atomic


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("UWUCRAFT_PROFILE", "default")
    p = Paths()
    p.save(Config())
    return p


def test_failed_unit_stop_still_stops_detached_jvm(paths, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "windows", lambda: False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    unit = tmp_path / ".config/systemd/user" / paths.unit_name
    unit.parent.mkdir(parents=True)
    unit.touch()
    calls = []
    monkeypatch.setattr(server.subprocess, "run", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(server, "stop", lambda p: calls.append("stop-real-jvm"))
    server.manage("stop", paths)
    assert calls == [["systemctl", "--user", "stop", paths.unit_name], "stop-real-jvm"]


def test_restart_stops_jvm_before_starting_unit_and_clears_limit(
    paths, tmp_path, monkeypatch
):
    monkeypatch.setattr(server, "windows", lambda: False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    unit = tmp_path / ".config/systemd/user" / paths.unit_name
    unit.parent.mkdir(parents=True)
    unit.touch()
    calls = []
    states = iter([{"status": "Stopped"}, {"status": "Starting"}])
    monkeypatch.setattr(server, "status", lambda p: next(states))
    monkeypatch.setattr(
        server.subprocess, "run", lambda args, **kw: calls.append(args[2])
    )
    monkeypatch.setattr(server, "stop", lambda p: calls.append("stop-real-jvm"))
    server.manage("restart", paths)
    assert calls == ["stop", "stop-real-jvm", "reset-failed", "start"]


def test_repeated_start_does_not_spawn_second_supervisor(paths, monkeypatch):
    monkeypatch.setattr(server, "status", lambda p: {"status": "Running"})
    monkeypatch.setattr(server, "start", lambda p: pytest.fail("duplicate start"))
    monkeypatch.setattr(
        server.subprocess, "run", lambda *a, **k: pytest.fail("duplicate service start")
    )
    server.manage("start", paths)


def test_live_pid_overrides_incorrect_stopped_record(paths):
    process = psutil.Process()
    atomic(
        paths.state,
        json.dumps(
            {"pid": process.pid, "created": process.create_time(), "status": "Stopped"}
        ),
    )
    assert server.status(paths)["status"] == "Running"


def test_stop_waits_for_actual_exit_and_save(paths, tmp_path, monkeypatch):
    saved = tmp_path / "saved"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time,pathlib; sys.stdin.readline(); time.sleep(.3); pathlib.Path(sys.argv[1]).write_text('saved')",
            str(saved),
        ],
        stdin=subprocess.PIPE,
        text=True,
    )
    atomic(
        paths.state,
        json.dumps(
            {
                "pid": child.pid,
                "created": psutil.Process(child.pid).create_time(),
                "status": "Running",
            }
        ),
    )
    monkeypatch.setattr(server, "windows", lambda: False)

    def command(text, p):
        assert text == "stop"
        child.stdin.write("stop\n")
        child.stdin.flush()

    monkeypatch.setattr(server, "command", command)
    try:
        server.stop(paths)
        assert saved.read_text() == "saved"
        assert child.wait(timeout=1) == 0
        assert server.status(paths)["status"] == "Stopped"
        server.stop(paths)  # idempotent
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)


@pytest.mark.asyncio
async def test_drop_dialog_can_stop_then_install(paths, monkeypatch):
    from textual.widgets import Button, Static

    import uwucraft.drop_ui as drop
    from uwucraft.tui import Uwucraft

    current = {"status": "Running"}
    calls = []
    monkeypatch.setattr(drop.server, "status", lambda *a: dict(current))

    def manage(action, p):
        assert action == "stop"
        calls.append(p.profile)
        current["status"] = "Stopped"

    monkeypatch.setattr(drop.server, "manage", manage)
    application = Uwucraft()
    async with application.run_test(size=(150, 60)) as pilot:
        application.push_screen(drop.DropFiles(paths))
        await pilot.pause()
        assert application.screen.query_one("#drop-install", Button).disabled
        await pilot.click("#drop-stop")
        await pilot.pause()
        await application.workers.wait_for_complete()
        assert not application.screen.query_one("#drop-install", Button).disabled
        assert application.screen.query_one("#drop-stop", Button).disabled
        assert "Stopped" in str(
            application.screen.query_one("#drop-server-status", Static).render()
        )
    assert calls == [paths.profile]


def test_corrupt_process_state_blocks_install_and_start(paths):
    from uwucraft import backups
    from uwucraft.config import Error

    atomic(paths.state, "not valid JSON")
    assert server.status(paths)["status"] == "Unknown"
    with pytest.raises(Error):
        backups.require_stopped(paths)
    with pytest.raises(Error):
        server.start(paths)
    with pytest.raises(Error, match="PID"):
        server.manage("start", paths)
