import json
import time

import pytest
from typer.testing import CliRunner

from uwucraft import deployment, network, server
from uwucraft.cli import app, show_creation_report
from uwucraft.config import Config, Error, Paths, atomic


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("UWUCRAFT_PROFILE", "default")
    result = Paths()
    result.save(Config(public_address="8.8.8.8", server_id="our-server"))
    monkeypatch.setattr(server, "status", lambda *a: {"status": "Running"})
    return result


def evidence(paths, **changes):
    value = {
        "address": "8.8.8.8:25565",
        "server_id": "our-server",
        "online": True,
        "identity_verified": True,
        "checked_at": time.time(),
    }
    value.update(changes)
    atomic(paths.data / "network-probe.json", json.dumps(value))


def test_invite_requires_fresh_own_external_server(paths):
    with pytest.raises(Error):
        network.connection_address(paths)
    evidence(paths)
    assert network.connection_address(paths) == "8.8.8.8:25565"


@pytest.mark.parametrize(
    "change",
    [
        {"online": False},
        {"identity_verified": False},
        {"server_id": "someone-else"},
        {"server_id": ""},
        {"address": "9.9.9.9:25565"},
        {"checked_at": 0},
        {"checked_at": time.time() + 3600},
    ],
)
def test_stale_or_wrong_endpoint_is_not_an_invite(paths, change):
    evidence(paths, **change)
    with pytest.raises(Error):
        network.connection_address(paths)


def test_stopped_server_cannot_be_invited(paths, monkeypatch):
    evidence(paths)
    monkeypatch.setattr(server, "status", lambda *a: {"status": "Stopped"})
    with pytest.raises(Error):
        network.connection_address(paths)


def test_copy_command_never_copies_a_vpn_exit_without_proof(paths, monkeypatch):
    monkeypatch.setattr(network, "external_ipv4", lambda *a: "8.8.8.8")
    copied = []
    monkeypatch.setattr(network, "copy", lambda text: copied.append(text))
    result = CliRunner().invoke(app, ["copy-address"])
    assert result.exit_code != 0 and not copied


def test_report_does_not_advertise_failed_outbound_ip(paths, capsys):
    show_creation_report(
        {
            "address": "8.8.8.8:25565",
            "verified": False,
            "errors": ["CGNAT"],
            "warnings": [],
        }
    )
    text = capsys.readouterr().out
    assert "8.8.8.8:25565" not in text
    assert "CGNAT" in text


def test_actual_route_detects_vpn_even_with_physical_default(paths, monkeypatch):
    import platform

    monkeypatch.setattr(network, "windows", lambda: False)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(network.socket, "gethostbyname", lambda host: "1.1.1.1")
    calls = []

    def output(args, **kwargs):
        calls.append(args)
        return json.dumps(
            [{"dev": "amn0"}]
            if "route" in args
            else [{"linkinfo": {"info_kind": "tun"}}]
        ).encode()

    monkeypatch.setattr(network.subprocess, "check_output", output)
    assert network.outbound_route(paths) == {"state": "tunnel", "interface": "amn0"}
    assert ["route", "get", "1.1.1.1"] == calls[0][-3:]


def test_known_cgnat_and_vpn_remain_errors_after_local_setup(paths, monkeypatch):
    from uwucraft import firewall, portmap

    monkeypatch.setattr(
        network, "outbound_route", lambda *a: {"state": "tunnel", "interface": "amn0"}
    )
    monkeypatch.setattr(firewall, "configure", lambda port: {"state": "configured"})
    monkeypatch.setattr(
        portmap, "map_port", lambda port: (_ for _ in ()).throw(Error("UPnP disabled"))
    )
    monkeypatch.setattr(network, "external_ipv4", lambda *a: "8.8.8.8")
    monkeypatch.setattr(
        network, "probe", lambda *a: {"online": False, "identity_verified": False}
    )
    report = deployment.configure(paths, wan_ip="100.105.99.48")
    assert report["firewall"]["state"] == "configured"
    assert not report["verified"]
    assert any("CGNAT" in text for text in report["errors"])
    assert any("туннель" in text for text in report["errors"])


def test_relay_endpoint_uses_remote_port_and_never_vpn_exit(paths, monkeypatch):
    from uwucraft import relay

    config = paths.load()
    config.public_access = "relay"
    paths.save(config)
    monkeypatch.setattr(relay, "status", lambda p: {"address": "9.9.9.9:45678"})
    evidence(paths, address="9.9.9.9:45678")
    assert network.connection_address(paths) == "9.9.9.9:45678"
    monkeypatch.setattr(relay, "status", lambda p: {})
    with pytest.raises(Error):
        network.connection_address(paths)


def test_relay_rejects_shell_host_before_changes(paths):
    from uwucraft import relay

    with pytest.raises(Error, match="имя"):
        relay.setup(paths, host="host;command")
    assert paths.load().public_access == "direct"


def test_relay_status_rejects_reused_pid(paths):
    import os

    from uwucraft import relay

    atomic(paths.data / "relay.json", json.dumps({"pid": os.getpid(), "created": 0}))
    assert relay.status(paths) == {}


@pytest.mark.parametrize(
    "value, expected", [("8374", 8374), ("0", 0), ("70000", 0), ("--secret x", 0)]
)
def test_relay_preferred_port_is_validated(paths, value, expected):
    from uwucraft import relay

    atomic(paths.data / "relay-port", value)
    assert relay.preferred_port(paths) == expected


@pytest.mark.parametrize("verified", [True, False])
def test_relay_deployment_never_opens_router_or_firewall(paths, monkeypatch, verified):
    from uwucraft import firewall, portmap, relay

    config = paths.load()
    config.public_access = "relay"
    paths.save(config)

    def forbidden(*a, **kw):
        pytest.fail("Relay must not configure inbound firewall/NAT or use VPN exit")

    monkeypatch.setattr(firewall, "configure", forbidden)
    monkeypatch.setattr(portmap, "map_port", forbidden)
    monkeypatch.setattr(network, "external_ipv4", forbidden)
    monkeypatch.setattr(relay, "setup", lambda *a, **kw: {"address": "9.9.9.9:45678"})
    monkeypatch.setattr(
        network, "probe", lambda *a: {"online": True, "identity_verified": verified}
    )
    monkeypatch.setattr(network, "connection_address", lambda *a: "9.9.9.9:45678")
    report = deployment.configure(paths)
    assert report["verified"] is verified
    assert report["address"] == ("9.9.9.9:45678" if verified else "")
    assert bool(report["errors"]) is not verified


def test_relay_failure_is_actionable_and_never_falls_back(paths, monkeypatch):
    from uwucraft import relay

    config = paths.load()
    config.public_access = "relay"
    paths.save(config)

    def fail(*a, **kw):
        raise Error("relay-host:7835 недоступен")

    monkeypatch.setattr(relay, "setup", fail)
    report = deployment.configure(paths)
    assert not report["verified"] and not report["address"]
    assert "7835" in report["errors"][0]


def test_creation_default_and_explicit_direct():
    from uwucraft.creation import CreateOptions

    assert CreateOptions().public_access == "relay"
    CreateOptions(public_access="direct", accept_eula=True).validate()
    with pytest.raises(Error, match="relay или direct"):
        CreateOptions(public_access="invalid", accept_eula=True).validate()


def test_cli_explicit_direct_reaches_shared_creation_backend(paths, monkeypatch):
    from uwucraft import creation

    paths.file.unlink()
    received = []

    def create(options, **kwargs):
        received.append(options.public_access)
        return {
            "verified": True,
            "address": "9.9.9.9:25565",
            "errors": [],
            "warnings": [],
        }

    monkeypatch.setattr(creation, "create", create)
    result = CliRunner().invoke(
        app,
        [
            "create",
            "--quick",
            "--public-access",
            "direct",
            "--port",
            "25565",
            "--accept-eula",
        ],
    )
    assert result.exit_code == 0, result.output
    assert received == ["direct"]
