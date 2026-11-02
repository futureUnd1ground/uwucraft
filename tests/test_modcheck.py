import json
import zipfile

import pytest

from uwucraft import modcheck
from uwucraft.config import Config, Paths


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("UWUCRAFT_PROFILE", "default")
    p = Paths()
    p.save(Config(loader="forge", version="1.16.5"))
    (p.server / "mods").mkdir(parents=True)
    return p


def forge(p, name, identity, version="1.0", dependency="", side="BOTH"):
    text = f'modLoader="javafml"\nloaderVersion="[36,)"\n[[mods]]\nmodId="{identity}"\nversion="{version}"\n'
    if dependency:
        text += f'[[dependencies.{identity}]]\nmodId="{dependency}"\nmandatory=true\nversionRange="[1.0,)"\nside="{side}"\n'
    with zipfile.ZipFile(p.server / "mods" / name, "w") as z:
        z.writestr("META-INF/mods.toml", text)


@pytest.mark.parametrize(
    "version,required,expected",
    [
        ("1.16.5", "[1.16.5]", True),
        ("1.20.1", "[1.16.5]", False),
        ("3.8.2+1.16.5-forge", "[3.8.2,)", True),
        ("2.0", "[1,2)", False),
        ("1.21.1", ">=1.20 <1.22", True),
        ("1.21.1", "1.21.x", True),
        ("1.0-beta", "[1,2)", None),
        ("1.0", "^1.0", None),
    ],
)
def test_conservative_version_ranges(version, required, expected):
    assert modcheck.matches(version, required) is expected


def test_dependency_install_batch_and_duplicate_mod_ids(paths):
    forge(paths, "feature.jar", "feature", dependency="library")
    assert "library" in "\n".join(modcheck.audit(paths)["errors"])
    forge(paths, "library.jar", "library")
    assert not modcheck.audit(paths)["errors"]
    forge(paths, "duplicate.jar", "feature")
    assert "Дублируется" in "\n".join(modcheck.audit(paths)["errors"])


def test_client_only_dependency_is_not_required_on_server(paths):
    forge(paths, "feature.jar", "feature", dependency="clientlib", side="CLIENT")
    assert not modcheck.audit(paths)["errors"]


def test_wrong_minecraft_version_and_fabric_nested_library(paths):
    paths.save(Config(loader="fabric", version="1.21.1"))
    import io

    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as z:
        z.writestr("fabric.mod.json", json.dumps({"id": "library", "version": "1.0"}))
    for n in ["a", "b"]:
        with zipfile.ZipFile(paths.server / "mods" / f"{n}.jar", "w") as z:
            z.writestr(
                "fabric.mod.json",
                json.dumps(
                    {
                        "id": n,
                        "version": "1.0",
                        "depends": {"library": ">=1.0", "minecraft": "1.21.1"},
                        "jars": [{"file": "META-INF/jars/library.jar"}],
                    }
                ),
            )
            z.writestr("META-INF/jars/library.jar", nested.getvalue())
    assert not modcheck.audit(paths)["errors"]
    paths.save(Config(loader="fabric", version="1.20.1"))
    assert "minecraft" in "\n".join(modcheck.audit(paths)["errors"])


def test_debug_compares_ids_not_different_server_client_jar_hashes(
    paths, tmp_path, monkeypatch
):
    from uwucraft import network
    from uwucraft.config import atomic
    from uwucraft.diagnostics import debug_report

    forge(paths, "feature-server.jar", "feature")
    client = tmp_path / "client"
    (client / "mods").mkdir(parents=True)
    with zipfile.ZipFile(client / "mods/feature-client.jar", "w") as z:
        z.writestr("META-INF/mods.toml", '[[mods]]\nmodId="feature"\nversion="1.0"\n')
    atomic(client / "logs/latest.log", "Connection reset token=secret-value\n")
    monkeypatch.setattr(
        network,
        "minecraft_status",
        lambda **kw: {"version": {"name": "1.16.5"}, "players": {}},
    )
    report = debug_report(paths, client)
    assert not report["client_missing_candidates"]
    assert not report["client_version_mismatch"]
    assert "secret-value" not in json.dumps(report)
    assert report["findings"][0]["code"] == "connection_reset"


async def test_tui_debug_button_and_structured_report(paths, monkeypatch):
    from textual.widgets import TextArea

    import uwucraft.debug_ui as debug_ui
    from uwucraft.tui import Uwucraft

    monkeypatch.setattr(
        debug_ui,
        "debug_report",
        lambda *a: {"findings": [{"code": "fixture_diagnosis"}]},
    )
    application = Uwucraft()
    async with application.run_test(size=(140, 60)) as pilot:
        application.page = "Network"
        await application.render_page()
        await pilot.pause()
        await pilot.click("#debug-server")
        await pilot.pause()
        await application.workers.wait_for_complete()
        assert (
            "fixture_diagnosis"
            in application.screen.query_one("#debug-report", TextArea).text
        )
        await pilot.click("#debug-close")
        await pilot.pause()
        assert application.screen.id == "_default"


def test_local_batch_dependencies_checked_before_commit(paths, tmp_path):
    from uwucraft import local_content
    from uwucraft.config import Error

    forge(paths, "feature.jar", "feature", dependency="library")
    feature = tmp_path / "feature.jar"
    (paths.server / "mods/feature.jar").replace(feature)
    with pytest.raises(Error, match="library"):
        local_content.install_many([feature], True, paths)
    assert not list((paths.server / "mods").iterdir())
    forge(paths, "library.jar", "library")
    library = tmp_path / "library.jar"
    (paths.server / "mods/library.jar").replace(library)
    local_content.install_many([feature, library], True, paths)
    assert len(list((paths.server / "mods").glob("*.jar"))) == 2


def test_transport_cli_positional_mode_is_saved(paths, monkeypatch):
    from typer.testing import CliRunner

    import uwucraft.cli as cli

    monkeypatch.setattr(cli, "network_configure", lambda: None)
    result = CliRunner().invoke(cli.app, ["network", "transport", "physical"])
    assert result.exit_code == 0, result.output
    assert paths.load().relay_transport == "physical"
