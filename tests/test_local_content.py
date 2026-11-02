import json
import os
import zipfile
from pathlib import Path

import pytest
from textual.events import Paste
from textual.widgets import Checkbox, Static, TextArea
from typer.testing import CliRunner

from uwucraft import backups, content, local_content
from uwucraft.cli import app
from uwucraft.config import Config, Error, Paths, atomic


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("UWUCRAFT_PROFILE", "default")
    result = Paths()
    result.save(Config(loader="fabric", version="1.21.1"))
    result.server.mkdir(parents=True)
    return result


def archive(path, entries):
    with zipfile.ZipFile(path, "w") as file:
        for name, data in entries.items():
            file.writestr(name, data)
    return path


def mod(path, **extra):
    return archive(path, {"fabric.mod.json": json.dumps({"id": "example", **extra})})


def test_drop_paths_spaces_unicode_quotes_uris(tmp_path):
    first = mod(tmp_path / "первый мод.jar")
    second = mod(tmp_path / "second.jar")
    assert local_content.dropped_paths(f'"{first}" "{second}"') == [first, second]
    assert local_content.dropped_paths(f"{first}\r\n{second}") == [first, second]
    assert local_content.dropped_paths(first.as_uri()) == [first]
    escaped = str(first).replace(" ", "\\ ")
    if os.name != "nt":
        assert local_content.dropped_paths(escaped) == [first]
    assert local_content.dropped_paths(f"'{first}' '{first}'") == [first]
    with pytest.raises(Error):
        local_content.dropped_paths("file://remote-host/file.jar")


def test_batch_mods_and_data_and_resources(paths, tmp_path):
    first, second = mod(tmp_path / "a.jar"), mod(tmp_path / "b.jar")
    data = archive(
        tmp_path / "data.zip", {"pack.mcmeta": "{}", "data/x/test.json": "{}"}
    )
    resource = archive(
        tmp_path / "res.zip", {"pack.mcmeta": "{}", "assets/x/test.json": "{}"}
    )
    atomic(paths.server / "server.properties", "level-name=custom-world\n")
    result = local_content.install_many([first, second, data, resource], True, paths)
    assert len(result) == 4
    assert (paths.server / "mods/a.jar").read_bytes() == first.read_bytes()
    assert (paths.server / "custom-world/datapacks/data.zip").exists()
    assert (paths.server / "resourcepacks/res.zip").exists()
    assert len(content.inventory(paths)) == 4
    content.remove("local:a.jar", paths)
    assert not (paths.server / "mods/a.jar").exists()


@pytest.mark.parametrize(
    "problem", ["client", "loader", "corrupt", "traversal", "existing"]
)
def test_entire_batch_is_rejected_before_writes(paths, tmp_path, problem):
    first = mod(tmp_path / "first.jar")
    second = tmp_path / "bad.jar"
    if problem == "client":
        mod(second, environment="client")
    elif problem == "loader":
        archive(second, {"META-INF/mods.toml": "modLoader='javafml'"})
    elif problem == "corrupt":
        second.write_bytes(b"not a ZIP")
    elif problem == "traversal":
        archive(second, {"fabric.mod.json": "{}", "../outside": "no"})
    else:
        mod(second)
        atomic(paths.server / "mods/bad.jar", b"existing")
    with pytest.raises(Error):
        local_content.install_many([first, second], True, paths)
    assert not (paths.server / "mods/first.jar").exists()
    assert not (paths.config / "content.json").exists()
    if problem == "existing":
        assert (paths.server / "mods/bad.jar").read_bytes() == b"existing"


def test_running_server_and_untrusted_files_are_untouched(paths, tmp_path, monkeypatch):
    file = mod(tmp_path / "a.jar")
    with pytest.raises(Error, match="trust-files"):
        local_content.install_many([file], paths=paths)
    monkeypatch.setattr(backups, "status", lambda p: {"status": "Running"})
    with pytest.raises(Error, match="остановите"):
        local_content.install_many([file], True, paths)
    assert not (paths.server / "mods").exists()


def test_transaction_rolls_back_files_on_inventory_failure(
    paths, tmp_path, monkeypatch
):
    files = [mod(tmp_path / "a.jar"), mod(tmp_path / "b.jar")]
    real_atomic = local_content.atomic

    def fail(path, *args, **kwargs):
        if path == paths.config / "content.json":
            raise OSError("disk full")
        return real_atomic(path, *args, **kwargs)

    monkeypatch.setattr(local_content, "atomic", fail)
    with pytest.raises(OSError, match="disk full"):
        local_content.install_many(files, True, paths)
    assert not list((paths.server / "mods").glob("*.jar"))


def test_serverpack_and_world_and_modified_config_preserved(paths, tmp_path):
    jar = mod(tmp_path / "mod.jar")
    pack = archive(
        tmp_path / "server.zip",
        {"mods/a.jar": jar.read_bytes(), "config/a.toml": "original"},
    )
    world = archive(
        tmp_path / "My World.zip", {"level.dat": "world", "region/test.mca": "region"}
    )
    local_content.install_many([pack, world], True, paths)
    assert (paths.server / "worlds/My World/level.dat").read_text() == "world"
    atomic(paths.server / "config/a.toml", "edited")
    with pytest.raises(Error, match="изменён"):
        content.remove("local:server.zip", paths)
    assert (paths.server / "mods/a.jar").exists()
    with pytest.raises(Error, match="мира"):
        content.remove("local:My World.zip", paths)


def test_mrpack_uses_existing_verified_installer(paths, tmp_path):
    pack = archive(
        tmp_path / "mods.mrpack",
        {
            "modrinth.index.json": json.dumps(
                {
                    "name": "Example",
                    "versionId": "1",
                    "dependencies": {"minecraft": "1.21.1", "fabric-loader": "0.16"},
                    "files": [],
                }
            ),
            "server-overrides/config/a.toml": "value=true",
        },
    )
    local_content.install_many([pack], True, paths)
    assert (paths.server / "config/a.toml").read_text() == "value=true"
    assert content.inventory(paths)["local:mods.mrpack"]["paths"] == ["config/a.toml"]


def test_plugin_loader_and_symlink_escape(paths, tmp_path):
    plugin = archive(tmp_path / "plugin.jar", {"plugin.yml": "name: Example"})
    with pytest.raises(Error, match="Paper"):
        local_content.install_many([plugin], True, paths)
    paths.save(Config(loader="paper", version="1.21.1"))
    local_content.install_many([plugin], True, paths)
    paths.save(Config(loader="fabric", version="1.21.1"))
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (paths.server / "mods").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(Error, match="ссылку"):
        local_content.install_many([mod(tmp_path / "a.jar")], True, paths)
    assert not list(outside.iterdir())


def test_cli_multiple_local_files(paths, tmp_path):
    files = [mod(tmp_path / "a.jar"), mod(tmp_path / "b.jar")]
    result = CliRunner().invoke(app, ["add-files", *map(str, files), "--trust-files"])
    assert result.exit_code == 0, result.output
    assert len(content.inventory(paths)) == 2


async def test_real_tui_drop_paste_and_batch_button(paths, tmp_path):
    from uwucraft.tui import Uwucraft

    files = [mod(tmp_path / "a mod.jar"), mod(tmp_path / "b.jar")]
    application = Uwucraft()
    async with application.run_test(size=(140, 60)) as pilot:
        application.page = "Mods"
        await application.render_page()
        await pilot.pause()
        await pilot.click("#drop-files")
        await pilot.pause()
        field = application.screen.query_one("#drop-paths", TextArea)
        field.focus()
        field.post_message(Paste("\n".join(f'"{f}"' for f in files)))
        await pilot.pause()
        assert str(files[1]) in field.text
        await pilot.click("#drop-preview")
        await application.workers.wait_for_complete()
        await pilot.pause()
        assert "a mod.jar" in str(
            application.screen.query_one("#drop-result", Static).render()
        )
        application.screen.query_one("#drop-trust", Checkbox).value = True
        await pilot.click("#drop-install")
        await application.workers.wait_for_complete()
        await pilot.pause()
        assert (paths.server / "mods/a mod.jar").exists()
        assert (paths.server / "mods/b.jar").exists()


@pytest.mark.parametrize("kind", ["world", "datapack", "resourcepack", "serverpack"])
def test_single_folder_wrapper_is_normalized(paths, tmp_path, kind):
    entries = {
        "world": {"folder/level.dat": "world"},
        "datapack": {"folder/pack.mcmeta": "{}", "folder/data/a/test.json": "{}"},
        "resourcepack": {"folder/pack.mcmeta": "{}", "folder/assets/a/test.json": "{}"},
        "serverpack": {"folder/config/a.toml": "test"},
    }[kind]
    source = archive(tmp_path / "Wrapped.zip", entries)
    assert local_content.inspect_file(source, paths)["kind"] == kind
    local_content.install_many([source], True, paths)
    if kind == "world":
        assert (paths.server / "worlds/Wrapped/level.dat").read_text() == "world"
    elif kind == "serverpack":
        assert (paths.server / "config/a.toml").read_text() == "test"
    else:
        target = paths.server / content.KINDS[kind] / "Wrapped.zip"
        with zipfile.ZipFile(target) as file:
            assert "pack.mcmeta" in file.namelist()
            assert not any(n.startswith("folder/") for n in file.namelist())


def test_windows_drop_quotes_do_not_eat_backslashes(monkeypatch):
    monkeypatch.setattr(Path, "is_file", lambda p: p.suffix.lower() == ".jar")
    # Newlines from Explorer/clipboard and quoted Windows Terminal drops.
    text = '"C:\\Users\\Player Name\\mods\\a.jar" "C:\\mods\\b.jar"'
    result = local_content.dropped_paths(text)
    assert str(result[0]) == r"C:\Users\Player Name\mods\a.jar"
    assert str(result[1]) == r"C:\mods\b.jar"
