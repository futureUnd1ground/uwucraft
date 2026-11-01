import json
import stat
import zipfile

import pytest
from typer.testing import CliRunner

from uwucraft import backups, content, network, server
from uwucraft.cli import app
from uwucraft.config import Config, Error, Paths, atomic, memory_mb


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return Paths()


@pytest.mark.parametrize("value", ["0G", "-1G", "1.5G", "2GB", "$(id)", ""])
def test_invalid_memory(value):
    with pytest.raises(Error):
        memory_mb(value)


def test_config_atomic_permissions(paths):
    paths.save(Config())
    assert paths.load().port == 25565
    assert stat.S_IMODE(paths.file.stat().st_mode) == 0o600
    with paths.lock(), pytest.raises(Error), paths.lock():
        pass


@pytest.mark.parametrize(
    "name", ["../escape", "/etc/passwd", "a/../../escape", "a\\..\\escape", "C:/escape"]
)
def test_archive_paths(name, tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as file:
        file.writestr(name, "evil")
    with pytest.raises(Error):
        backups.extract_zip(archive, tmp_path / "out")


def test_zip_symlink(tmp_path):
    archive = tmp_path / "bad.zip"
    info = zipfile.ZipInfo("link")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as file:
        file.writestr(info, "/etc/passwd")
    with pytest.raises(Error):
        backups.extract_zip(archive, tmp_path / "out")


def test_backup_restore(paths):
    paths.save(Config(version="1.21.1"))
    atomic(paths.server / "world/level.dat", b"first-world")
    original = backups.create(paths)
    atomic(paths.server / "world/level.dat", b"changed-world")
    rescue = backups.restore(original.name, paths)
    assert (paths.server / "world/level.dat").read_bytes() == b"first-world"
    assert rescue.exists()
    assert original.exists()


def test_backup_running_refused(paths, monkeypatch):
    paths.save(Config())
    monkeypatch.setattr(backups, "status", lambda p: {"status": "Running"})
    with pytest.raises(Error):
        backups.create(paths)


def test_dead_pid_not_running(paths):
    atomic(
        paths.state, json.dumps({"pid": 99999999, "created": 0, "status": "Running"})
    )
    assert server.status(paths)["status"] != "Running"


def test_command_injection(paths):
    with pytest.raises(Error):
        server.command("say hello\nstop", paths)


def test_eula_required(paths):
    from uwucraft.installers import install

    with pytest.raises(Error, match="EULA"):
        install("paper", "1.21.1", "1G", 25565, False, paths=paths)
    assert not paths.file.exists()


def test_repeated_auto_preserves(paths):
    paths.save(Config(version="1.21.1"))
    atomic(paths.server / "world/level.dat", b"world")
    result = CliRunner().invoke(app, ["auto", "--loader", "paper", "--accept-eula"])
    assert result.exit_code == 0
    assert (paths.server / "world/level.dat").read_bytes() == b"world"


def test_no_false_clipboard_success(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(network.sys.stdout, "isatty", lambda: False)
    with pytest.raises(Error, match="вручную"):
        network.copy("example.test:25565")


def test_client_only_mod_rejected(paths, monkeypatch):
    paths.save(Config(loader="fabric", version="1.21.1"))
    monkeypatch.setattr(
        content,
        "metadata",
        lambda *a, **k: {"title": "Client mod", "server_side": "unsupported"},
    )
    with pytest.raises(Error, match="клиентский"):
        content.install("sodium", paths=paths)


async def test_tui_pages_and_theme(paths, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    from textual.widgets import Select

    from uwucraft.tui import NAV, Uwucraft

    application = Uwucraft()
    async with application.run_test(size=(140, 48)) as pilot:
        await pilot.pause()
        for name in NAV:
            application.page = name
            await application.render_page()
            await pilot.pause(0.05)
        application.query_one("#theme-select", Select).value = "tokyo-night"
        await pilot.pause()
        assert application.theme == "tokyo-night"


def test_restore_preserves_selected_copy_with_keep_one(paths):
    paths.save(Config(version="1.21.1", backup_keep=1))
    atomic(paths.server / "world/level.dat", b"original")
    atomic(paths.config / "content.json", '{"old":true}')
    saved = backups.create(paths)
    atomic(paths.config / "content.json", '{"new":true}')
    atomic(paths.server / "world/level.dat", b"changed")
    backups.restore(saved.name, paths)
    assert (paths.config / "content.json").read_text() == '{"old":true}'
    assert (paths.server / "world/level.dat").read_bytes() == b"original"


def test_dead_running_process_is_crashed(paths):
    atomic(
        paths.state, json.dumps({"pid": 99999999, "created": 0, "status": "Running"})
    )
    assert server.status(paths)["status"] == "Crashed"


def test_whitelist_positional_player(paths, monkeypatch):
    received = []
    monkeypatch.setattr(server, "command", lambda text: received.append(text))
    result = CliRunner().invoke(app, ["whitelist", "add", "ExamplePlayer"])
    assert result.exit_code == 0
    assert received == ["whitelist add ExamplePlayer"]


def test_protected_settings(paths):
    from uwucraft.settings import properties, set_property

    paths.save(Config(version="1.21.1"))
    atomic(paths.server / "server.properties", "online-mode=true\nserver-port=25565\n")
    with pytest.raises(Error):
        set_property("online-mode", "false", paths=paths)
    with pytest.raises(Error):
        set_property("motd", "hello\nstop", paths=paths)
    set_property("server-port", "25566", paths=paths)
    assert paths.load().port == 25566
    assert properties(paths)["online-mode"] == "true"


def test_public_probe_is_not_local_listen(paths, monkeypatch):
    from uwucraft import sources

    monkeypatch.setattr(sources, "metadata", lambda *a, **k: {"online": False})
    assert network.probe("8.8.8.8:25565")["online"] is False


def test_download_hash_mismatch_keeps_original(tmp_path, monkeypatch):
    from contextlib import contextmanager

    import httpx

    from uwucraft.sources import download

    @contextmanager
    def fake(*args, **kwargs):
        response = httpx.Response(
            200,
            request=httpx.Request("GET", "https://example.test/file"),
            content=b"corrupt",
        )
        yield response

    monkeypatch.setattr(httpx, "stream", fake)
    target = tmp_path / "server.jar"
    target.write_bytes(b"original")
    with pytest.raises(Error, match="Хеш"):
        download("https://example.test/file", target, {"sha256": "0" * 64})
    assert target.read_bytes() == b"original"
    assert not target.with_suffix(".jar.part").exists()


def test_https_redirect_cannot_downgrade(tmp_path, monkeypatch):
    from contextlib import contextmanager

    import httpx

    from uwucraft.sources import download

    requests = []

    @contextmanager
    def fake(method, url, **kwargs):
        requests.append(url)
        yield httpx.Response(
            302,
            headers={"location": "http://insecure.test/file"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "stream", fake)
    with pytest.raises(Error, match="HTTPS"):
        download("https://example.test/file", tmp_path / "jar")
    assert requests == ["https://example.test/file"]


async def test_no_color_does_not_overwrite_saved_theme(paths, monkeypatch):
    from uwucraft.tui import Uwucraft

    paths.save(Config(theme="uwu-dark"))
    monkeypatch.setenv("NO_COLOR", "1")
    application = Uwucraft()
    async with application.run_test(size=(120, 42)) as pilot:
        application.page = "Themes"
        await application.render_page()
        await pilot.pause()
        assert application.theme == "monochrome"
        assert paths.load().theme == "uwu-dark"


def test_metadata_redirect_does_not_send_key_to_other_host(monkeypatch):
    import httpx

    from uwucraft.sources import get

    original = httpx.Client
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://other.test/steal"})

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    with pytest.raises(Error, match="API key"):
        get("https://api.test/data", headers={"x-api-key": "dummy-test-value"})
    assert calls == ["api.test"]


def test_import_registered_and_dependency_removal_refused(paths, tmp_path):
    paths.save(Config(loader="fabric", version="1.21.1"))
    paths.server.mkdir(parents=True)
    jar = tmp_path / "example.jar"
    jar.write_bytes(b"a trusted local test file")
    content.import_file(str(jar), "mod", True, paths)
    assert content.inventory(paths)["local:example.jar"]["source"] == "import"
    atomic(
        paths.config / "content.json",
        json.dumps(
            {
                "dependency": {"project_id": "a", "path": "mods/a.jar"},
                "parent": {
                    "project_id": "b",
                    "path": "mods/b.jar",
                    "dependencies": ["a"],
                },
            }
        ),
    )
    with pytest.raises(Error, match="требуется"):
        content.remove("dependency", paths)


def test_auto_existing_dry_run_never_starts(paths, monkeypatch):
    from uwucraft import cli

    paths.save(Config(version="1.21.1"))

    def forbidden(*args):
        raise AssertionError("dry-run must not start service")

    monkeypatch.setattr(cli, "_lifecycle", forbidden)
    result = CliRunner().invoke(app, ["create", "--quick", "--start", "--dry-run"])
    assert result.exit_code == 0
    assert "max_players" in result.output


def test_latest_paper_skips_rc_and_unstable(monkeypatch):
    from uwucraft import sources

    def fake(url, **kwargs):
        if url == sources.MANIFEST:
            return {
                "latest": {"release": "26.3"},
                "versions": [
                    {"id": "26.4-rc1", "type": "snapshot"},
                    {"id": "26.3", "type": "release"},
                    {"id": "26.2", "type": "release"},
                ],
            }
        if url.endswith("/paper"):
            return {
                "versions": {"26.4": ["26.4-rc1"], "26.3": ["26.3"], "26.2": ["26.2"]}
            }
        if "/26.3/" in url:
            return [{"channel": "BETA"}]
        if "/26.2/" in url:
            return [{"channel": "STABLE"}]
        raise AssertionError(url)

    monkeypatch.setattr(sources, "metadata", fake)
    assert sources.latest_compatible("paper") == "26.2"


def test_addresses_shows_saved_external_result(paths):
    import time

    paths.save(Config(public_address="8.8.8.8"))
    atomic(
        paths.data / "network-probe.json",
        json.dumps(
            {
                "address": "8.8.8.8:25565",
                "online": True,
                "checked_at": time.time(),
                "cached": False,
                "identity_verified": True,
            }
        ),
    )
    assert network.addresses(paths)["reachability"] == "Доступен извне"
