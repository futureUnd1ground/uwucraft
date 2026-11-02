import json
import os
import sys

import psutil
import pytest
from typer.testing import CliRunner

from uwucraft import owner_route
from uwucraft.cli import app
from uwucraft.config import Paths


def test_only_owner_and_exact_endpoint_are_redirected():
    transaction = owner_route.plan({}, 1000, 25565, ("159.223.110.159", 8801))
    chain = transaction["nftables"][1]["add"]["chain"]
    assert chain["hook"] == "output" and chain["type"] == "nat"
    rule = transaction["nftables"][2]["add"]["rule"]
    expr = rule["expr"]
    assert expr[0]["match"] == {
        "op": "==",
        "left": {"meta": {"key": "skuid"}},
        "right": 1000,
    }
    assert expr[1]["match"]["right"] == "159.223.110.159"
    assert expr[2]["match"]["right"] == 8801
    assert expr[3] == {"dnat": {"addr": "127.0.0.1", "port": 25565}}
    assert "prerouting" not in json.dumps(transaction)
    assert "route" not in chain


def test_removal_only_deletes_own_table():
    document = {
        "nftables": [
            {"table": {"family": "ip", "name": "vpn"}},
            {"table": {"family": "ip", "name": "uwucraft_owner_1000_25565"}},
            {"table": {"family": "ip", "name": "uwucraft_owner_1000_25566"}},
        ]
    }
    assert owner_route.plan(document, 1000, 25565, None) == {
        "nftables": [
            {"delete": {"table": {"family": "ip", "name": "uwucraft_owner_1000_25565"}}}
        ]
    }
    assert owner_route.plan({}, 1000, 25565, None) == {"nftables": []}


@pytest.mark.parametrize(
    "endpoint",
    [
        ("127.0.0.1", 8801),
        ("100.64.0.1", 8801),
        ("224.1.1.1", 8801),
        ("159.223.110.159", 0),
    ],
)
def test_nonpublic_or_invalid_endpoint_is_rejected(endpoint):
    with pytest.raises(ValueError):
        owner_route.plan({}, 1000, 25565, endpoint)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc and nftables helper")
def test_state_requires_live_process_identity_and_regular_owned_file(tmp_path):
    state = tmp_path / "relay.json"
    data = {
        "address": "159.223.110.159:8801",
        "pid": os.getpid(),
        "created": psutil.Process().create_time(),
    }
    state.write_text(json.dumps(data))
    assert owner_route.read_endpoint(state, os.getuid()) == ("159.223.110.159", 8801)
    link = tmp_path / "link"
    link.symlink_to(state)
    with pytest.raises(OSError):
        owner_route.read_endpoint(link, os.getuid())
    for created in [0, float("nan")]:
        state.write_text(json.dumps({**data, "created": created}))
        with pytest.raises(ValueError, match="Stale"):
            owner_route.read_endpoint(state, os.getuid())


def test_cli_enable_disable_and_status_use_shared_backend(monkeypatch):
    calls = []
    monkeypatch.setattr(
        owner_route,
        "configure",
        lambda **kw: calls.append(kw) or {"endpoint": ["159.223.110.159", 8801]},
    )
    monkeypatch.setattr(owner_route, "status", lambda: {"scope": "owner only"})
    for action in ["enable", "disable", "status"]:
        result = CliRunner().invoke(app, ["network", "owner-route", action])
        assert result.exit_code == 0, result.output
        assert "внешнего пинга" in result.output
    assert calls == [{"disable": False}, {"disable": True}]
    assert CliRunner().invoke(app, ["network", "owner-route", "unknown"]).exit_code != 0


@pytest.mark.asyncio
async def test_tui_button_uses_same_backend(tmp_path, monkeypatch):
    import uwucraft.tui as tui

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(tui, "windows", lambda: False)
    monkeypatch.setattr(owner_route, "status", lambda *args: {})
    calls = []
    monkeypatch.setattr(
        owner_route,
        "configure",
        lambda paths, disable=False: calls.append((paths.profile, disable)),
    )
    application = tui.Uwucraft()
    async with application.run_test(size=(150, 60)) as pilot:
        application.page = "Network"
        await application.render_page()
        await pilot.pause()
        await pilot.click("#owner-route")
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause()
        await application.workers.wait_for_complete()
    assert calls == [(Paths().profile, False)]


def test_invalid_relay_state_removes_redirect_without_touching_vpn(
    tmp_path, monkeypatch
):
    from pathlib import Path
    from types import SimpleNamespace

    name = owner_route.identity(1000, 25565)
    document = {
        "nftables": [
            {"table": {"family": "ip", "name": name}},
            {"table": {"family": "inet", "name": "vpn"}},
        ]
    }
    writes = []

    def execute(args, **kwargs):
        if args == ["nft", "-j", "list", "ruleset"]:
            return SimpleNamespace(stdout=json.dumps(document))
        writes.append(json.loads(kwargs["input"]))
        return SimpleNamespace(stdout="")

    def invalid(*args):
        raise ValueError("Stale relay process identity")

    monkeypatch.setattr(owner_route, "execute", execute)
    monkeypatch.setattr(owner_route, "read_endpoint", invalid)
    monkeypatch.setattr(
        owner_route,
        "Path",
        lambda p: tmp_path / Path(p).name if str(p).startswith("/run/") else Path(p),
    )
    result = owner_route.apply(tmp_path / "relay.json", 1000, 25565)
    assert result["endpoint"] is None
    assert "Stale" in result["error"]
    assert writes == [
        {"nftables": [{"delete": {"table": {"family": "ip", "name": name}}}]}
    ]


def test_unchanged_timer_tick_does_not_replace_nat_table(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    name = owner_route.identity(1000, 25565)
    writes = []

    def execute(args, **kwargs):
        if args == ["nft", "-j", "list", "ruleset"]:
            return SimpleNamespace(
                stdout=json.dumps(
                    {"nftables": [{"table": {"family": "ip", "name": name}}]}
                )
            )
        writes.append(kwargs["input"])
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(owner_route, "execute", execute)
    monkeypatch.setattr(
        owner_route, "read_endpoint", lambda *a: ("159.223.110.159", 8801)
    )
    monkeypatch.setattr(
        owner_route,
        "Path",
        lambda p: tmp_path / Path(p).name if str(p).startswith("/run/") else Path(p),
    )
    for _ in range(2):
        owner_route.apply(tmp_path / "relay.json", 1000, 25565)
    assert len(writes) == 1
