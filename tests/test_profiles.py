import json
import subprocess
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from uwucraft import backups, compat, profiles, server, sources
from uwucraft.cli import app
from uwucraft.config import Config, Error, Paths, atomic


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("UWUCRAFT_PROFILE", raising=False)
    return Paths(profile="default")


def seed(paths, version="1.20.1", port=25565):
    paths.save(Config(name=paths.profile, version=version, port=port, java="java"))
    atomic(paths.server / "world/level.dat", version)
    atomic(paths.server / "server.jar", b"fixture")
    return paths


def test_profiles_preserve_legacy_and_isolate_selection(root):
    default = seed(root)
    second = seed(Paths(profile="creative"), "1.21.4", 25566)
    assert default.config == default.root_config
    assert second.server != default.server
    assert second.backups != default.backups
    assert second.socket != default.socket
    profiles.select("creative")
    assert Paths().profile == "creative"
    assert default.load().version == "1.20.1"
    assert {p.profile for p in profiles.listing()} == {"default", "creative"}


@pytest.mark.parametrize(
    "identifier",
    ["../world", "../../", "UPPER", "x y", "$(id)", "x/../../world", "x" * 41],
)
def test_bad_profile_ids_do_not_write(root, identifier):
    with pytest.raises(Error):
        Paths(profile=identifier)
    assert not root.file.exists()


def test_profile_cli_override_does_not_leak(root):
    seed(root)
    seed(Paths(profile="modern"), "1.21.4", 25566)
    runner = CliRunner()
    result = runner.invoke(app, ["--profile", "modern", "status"])
    assert result.exit_code == 0
    assert Paths().profile == "default"
    assert runner.invoke(app, ["profile", "use", "modern"]).exit_code == 0
    assert Paths().profile == "modern"
    assert "1.21.4" in runner.invoke(app, ["profile", "list"]).output


def test_names_cannot_collide_with_other_profiles_workers(root):
    items = [
        Paths(profile=p)
        for p in ["default", "test", "test-network", "network", "backup"]
    ]
    names = [p.unit_name for p in items] + [p.network_unit_name for p in items]
    assert len(names) == len(set(names))
    for paths in items:
        assert paths.environment()["XDG_DATA_HOME"] == str(root.root_data.parent)
        assert paths.environment()["UWUCRAFT_PROFILE"] == paths.profile


def test_profile_unit_pins_environment(root, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(server, "windows", lambda: False)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0)
    )
    path = server.unit(Paths(profile="survival"))
    text = path.read_text(encoding="utf-8")
    assert path.name == "uwucraft-server@survival.service"
    assert 'Environment="UWUCRAFT_PROFILE=survival"' in text
    assert str(root.root_data.parent) in text


def test_select_pins_all_legacy_workers_without_restart(root, tmp_path, monkeypatch):
    seed(Paths(profile="modern"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "windows", lambda: False)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda args, **k: calls.append(args))
    folder = tmp_path / ".config/systemd/user"
    for name in [
        "uwucraft.service",
        "uwucraft-network.service",
        "uwucraft-backup.service",
    ]:
        atomic(
            folder / name,
            "[Unit]\nDescription=uwucraft test\n[Service]\nExecStart=/bin/true\n",
        )
    profiles.select("modern")
    for path in folder.iterdir():
        assert 'Environment="UWUCRAFT_PROFILE=default"' in path.read_text(
            encoding="utf-8"
        )
    assert calls == [["systemctl", "--user", "daemon-reload"]]


def test_default_backup_restore_never_reverts_another_profile(root):
    seed(root)
    second = seed(Paths(profile="other"), "1.21.4", 25566)
    backup = backups.create(root)
    with tarfile.open(backup) as archive:
        assert not any(i.name.startswith("config/profiles") for i in archive)
    second.save(Config(version="26.3", port=25567))
    atomic(root.root_config / "active-profile", "other\n")
    atomic(root.server / "world/level.dat", "changed")
    backups.restore(backup.name, root)
    assert (root.server / "world/level.dat").read_text(encoding="utf-8") == "1.20.1"
    assert second.load().version == "26.3"
    assert Paths().profile == "other"
    assert (second.server / "world/level.dat").read_text(encoding="utf-8") == "1.21.4"


def test_suggested_port_skips_stopped_profiles(root, monkeypatch):
    seed(root, port=25565)
    seed(Paths(profile="other"), port=25566)
    assert profiles.suggested_port() not in {25565, 25566}


def test_official_version_catalog_intersects_loader(root, monkeypatch):
    def metadata(url):
        if url == sources.MANIFEST:
            return {
                "versions": [
                    {"id": "26.3", "type": "release"},
                    {"id": "26w01a", "type": "snapshot"},
                    {"id": "1.20.1", "type": "release"},
                ]
            }
        return {"versions": {"1.20": ["1.20.1"]}}

    monkeypatch.setattr(sources, "metadata", metadata)
    assert sources.available_versions("paper") == ["1.20.1"]
    assert sources.available_versions("vanilla") == ["26.3", "1.20.1"]
    assert "26w01a" in sources.available_versions("vanilla", snapshots=True)
    assert sources.neoforge_prefix("1.21") == "21.0."
    assert sources.neoforge_prefix("1.20.2") == "20.2."
    assert sources.neoforge_prefix("26.1") == "26.1."


def test_compat_downloads_all_before_install_and_preserves_world(root, monkeypatch):
    seed(root)
    monkeypatch.setattr(compat, "validate", lambda p: None)

    def metadata(url):
        project = url.split("/")[-3]
        return {
            "tag_name": "1",
            "assets": [
                {
                    "name": project + "-1.jar",
                    "digest": "sha256:" + "a" * 64,
                    "browser_download_url": "https://example.test/" + project,
                }
            ],
        }

    monkeypatch.setattr(compat, "metadata", metadata)

    def download(url, path, hashes):
        with zipfile.ZipFile(path, "w") as file:
            file.writestr("plugin.yml", "name: Test\n")

    monkeypatch.setattr(compat, "download", download)
    compat.install(root, legacy=True)
    assert len(list((root.server / "plugins").glob("*.jar"))) == 3
    assert (root.server / "world/level.dat").read_text(encoding="utf-8") == "1.20.1"
    assert json.loads((root.config / "compat.json").read_text(encoding="utf-8"))[
        "legacy_clients"
    ]

    def fail(url, path, hashes):
        raise Error("network interrupted")

    monkeypatch.setattr(compat, "download", fail)
    before = {p.name: p.read_bytes() for p in (root.server / "plugins").iterdir()}
    with pytest.raises(Error):
        compat.install(root)
    assert before == {
        p.name: p.read_bytes() for p in (root.server / "plugins").iterdir()
    }


async def test_tui_profile_switch_and_version_picker(root, monkeypatch):
    from textual.widgets import Input, Select

    import uwucraft.version_ui as ui
    from uwucraft.tui import Uwucraft

    seed(root)
    seed(Paths(profile="creative"), "1.21.4", 25566)
    monkeypatch.setattr(ui, "available_versions", lambda loader: ["1.21.4", "1.20.1"])
    app = Uwucraft()
    async with app.run_test(size=(140, 60)) as pilot:
        app.query_one("#profile-select", Select).value = "creative"
        await pilot.pause(0.2)
        assert app.paths.profile == "creative"
        await pilot.click("#create-server")
        await pilot.pause()
        assert app.screen.paths.profile == "server-2"
        await pilot.click("#create-versions")
        await pilot.pause(0.2)
        assert app.screen.versions == ["1.21.4", "1.20.1"]
        app.screen.dismiss("1.20.1")
        await pilot.pause()
        assert app.screen.query_one("#create-version", Input).value == "1.20.1"
        await pilot.click("#create-close")
