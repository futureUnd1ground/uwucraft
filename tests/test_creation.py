import json
import socket
import struct
import subprocess

import pytest
from typer.testing import CliRunner

from uwucraft import creation, deployment, firewall, installers, network, portmap
from uwucraft.cli import app
from uwucraft.config import Config, Error, Paths, atomic


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return Paths()


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "192.168.1.1",
        "100.64.2.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "example.test",
        "203.0.113.1",
    ],
)
def test_nonpublic_addresses_never_become_invitation(address):
    with pytest.raises(Error):
        network.public_ipv4(address)


def test_create_eula_precedes_any_side_effect(paths, monkeypatch):
    def forbidden():
        raise AssertionError("must not mutate or install dependencies before EULA")

    monkeypatch.setattr(creation, "dependencies", forbidden)
    with pytest.raises(Error, match="EULA"):
        creation.create(creation.CreateOptions(), paths)
    assert not paths.data.exists()


def test_create_existing_world_preserved(paths, monkeypatch):
    paths.save(Config())
    atomic(paths.server / "world/level.dat", b"important")
    monkeypatch.setattr(
        creation,
        "dependencies",
        lambda: pytest.fail("existing install must not be changed"),
    )
    with pytest.raises(Error, match="уже существует"):
        creation.create(creation.CreateOptions(accept_eula=True), paths)
    assert (paths.server / "world/level.dat").read_bytes() == b"important"


def test_quick_create_never_prompts(paths, monkeypatch):
    received = []

    def backend(options, **kwargs):
        received.append(options)
        return {
            "address": "8.8.8.8:25566",
            "verified": True,
            "errors": [],
            "warnings": [],
        }

    monkeypatch.setattr(creation, "create", backend)
    result = CliRunner().invoke(
        app,
        [
            "create",
            "--quick",
            "--loader",
            "paper",
            "--port",
            "25566",
            "--accept-eula",
            "--start",
            "--mode",
            "pirate",
            "--max-players",
            "12",
        ],
        input="",
    )
    assert result.exit_code == 0, result.output
    assert len(received) == 1
    assert received[0].online_mode is False
    assert received[0].max_players == 12
    assert received[0].start and received[0].accept_eula
    assert "8.8.8.8:25566" in result.output
    assert "127.0.0.1" not in result.output


def test_cli_does_not_call_failed_network_success(paths, monkeypatch):
    monkeypatch.setattr(
        creation,
        "create",
        lambda *a, **k: {
            "address": "8.8.8.8:25565",
            "verified": False,
            "errors": ["CGNAT: нужен белый IPv4"],
            "warnings": [],
        },
    )
    result = CliRunner().invoke(app, ["create", "--quick", "--accept-eula"])
    assert result.exit_code == 1
    assert "НЕ подтверждена" in result.output
    assert "CGNAT" in result.output


def test_backend_runs_all_steps_in_order(paths, monkeypatch):
    from uwucraft import auth

    calls = []
    monkeypatch.setattr(creation, "dependencies", lambda: calls.append("dependencies"))
    monkeypatch.setattr(creation, "latest_compatible", lambda loader: "1.21.1")

    def install(loader, version, memory, port, accept, **kwargs):
        calls.append("install")
        assert accept and kwargs["local_runtime"]
        assert kwargs["offline_mode"] and kwargs["name"] == "Friends"
        assert kwargs["max_players"] == 9 and kwargs["whitelist"] is False
        paths.save(Config(version=version, memory=memory, port=port))
        paths.server.mkdir(parents=True)

    monkeypatch.setattr(installers, "install", install)
    monkeypatch.setattr(auth, "install_authme", lambda p: calls.append("auth"))
    monkeypatch.setattr(
        deployment, "enable_server", lambda p: calls.append("service") or ""
    )
    monkeypatch.setattr(creation.backups, "create", lambda p: calls.append("backup"))
    monkeypatch.setattr(
        deployment, "disable_legacy_service", lambda: calls.append("migration")
    )
    monkeypatch.setattr(deployment, "start_server", lambda p: calls.append("start"))
    monkeypatch.setattr(
        deployment,
        "configure",
        lambda p, progress: (
            calls.append("network")
            or {
                "address": "8.8.8.8:25565",
                "verified": True,
                "errors": [],
                "warnings": [],
            }
        ),
    )
    report = creation.create(
        creation.CreateOptions(
            name="Friends",
            loader="paper",
            memory="1G",
            max_players=9,
            online_mode=False,
            accept_eula=True,
        ),
        paths,
    )
    assert calls == [
        "dependencies",
        "install",
        "auth",
        "service",
        "backup",
        "migration",
        "start",
        "network",
    ]
    assert report["verified"]
    assert json.loads((paths.data / "deployment.json").read_text())["name"] == "Friends"


def test_installer_writes_selected_fields(paths, monkeypatch):
    monkeypatch.setattr(installers, "preflight", lambda *args: None)
    monkeypatch.setattr(
        installers, "minecraft", lambda v: (v, {"javaVersion": {"majorVersion": 21}})
    )
    monkeypatch.setattr(installers, "java_for", lambda *args: "/usr/bin/java")
    monkeypatch.setattr(
        installers, "resolve", lambda *args: ("https://example.test/server.jar", {}, 21)
    )
    monkeypatch.setattr(
        installers,
        "download",
        lambda url, target, hashes, *args, **kwargs: target.write_bytes(b"jar"),
    )
    config = installers.install(
        "paper",
        "1.21.1",
        "1G",
        25566,
        True,
        paths=paths,
        name="Friends",
        max_players=8,
        offline_mode=True,
        whitelist=False,
    )
    text = (paths.server / "server.properties").read_text()
    for value in [
        "server-ip=0.0.0.0",
        "server-port=25566",
        "online-mode=false",
        "max-players=8",
        "white-list=false",
    ]:
        assert value in text
    assert config.name == "Friends" and config.max_players == 8
    assert config.server_id in text


def test_firewall_inserts_into_every_ipv4_input_hook():
    rules = {
        "nftables": [
            {
                "chain": {
                    "family": "inet",
                    "table": "filter",
                    "name": "input",
                    "hook": "input",
                    "policy": "drop",
                }
            },
            {
                "chain": {
                    "family": "ip",
                    "table": "other",
                    "name": "incoming",
                    "hook": "input",
                    "policy": "drop",
                }
            },
            {
                "chain": {
                    "family": "ip6",
                    "table": "v6",
                    "name": "input",
                    "hook": "input",
                }
            },
            {
                "chain": {
                    "family": "inet",
                    "table": "filter",
                    "name": "forward",
                    "hook": "forward",
                }
            },
            {"chain": {"family": "inet", "table": "filter", "name": "subchain"}},
        ]
    }
    document, count = firewall.exception_plan(rules, 25566)
    assert count == 2
    assert [item["insert"]["rule"]["chain"] for item in document["nftables"]] == [
        "input",
        "incoming",
    ]
    for item in document["nftables"]:
        rule = item["insert"]["rule"]
        assert rule["expr"][-1] == {"accept": None}
        assert any(expr.get("match", {}).get("right") == 25566 for expr in rule["expr"])
    assert "flush" not in json.dumps(document) and "policy" not in json.dumps(document)


def test_firewall_idempotent_when_owned_rule_is_first():
    chain = {"family": "inet", "table": "filter", "name": "input", "hook": "input"}
    rule = {
        "family": "inet",
        "table": "filter",
        "chain": "input",
        "comment": "uwucraft-tcp-25565",
        "handle": 12,
    }
    document, count = firewall.exception_plan(
        {"nftables": [{"chain": chain}, {"rule": rule}]}, 25565
    )
    assert document == {"nftables": []}
    assert count == 1


def test_firewall_does_not_claim_success_without_privilege(monkeypatch):
    monkeypatch.setattr(firewall.shutil, "which", lambda name: "/usr/bin/nft")
    monkeypatch.setattr(firewall.os, "geteuid", lambda: 1000)
    commands = []

    def execute(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 1, "", "sudo: a password is required"
        )

    monkeypatch.setattr(firewall, "run", execute)
    with pytest.raises(firewall.FirewallError, match="sudo -v"):
        firewall.configure(25565)
    assert commands[0][:3] == ["sudo", "-n", "--"]


def test_natpmp_packet_layout_and_source_pinning(monkeypatch):
    sent = []
    connected = []
    responses = [
        struct.pack("!BBHI4s", 0, 128, 0, 10, socket.inet_aton("8.8.8.8")),
        struct.pack("!BBHIHHI", 0, 130, 0, 11, 25565, 25565, 3600),
    ]

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, address):
            assert address == ("192.168.1.10", 0)

        def connect(self, address):
            connected.append(address)

        def settimeout(self, value):
            pass

        def send(self, value):
            sent.append(value)

        def recv(self, size):
            return responses.pop(0)

    monkeypatch.setattr(portmap.socket, "socket", lambda *args: Socket())
    mapping = portmap.natpmp("192.168.1.1", "192.168.1.10", 25565)
    assert connected == [("192.168.1.1", 5351)]
    assert sent[0] == b"\x00\x00"
    assert struct.unpack("!BBHHHI", sent[1]) == (0, 2, 0, 25565, 25565, 3600)
    assert mapping.wan == "8.8.8.8" and mapping.port == 25565


def test_mapping_falls_back_to_natpmp(monkeypatch):
    monkeypatch.setattr(portmap, "route", lambda: ("192.168.1.1", "192.168.1.10"))

    def unavailable(*args):
        raise portmap.MappingError("No UPnP")

    monkeypatch.setattr(portmap, "upnp", unavailable)
    monkeypatch.setattr(
        portmap,
        "natpmp",
        lambda *args: portmap.Mapping("NAT-PMP", "8.8.8.8", 25565, 3600),
    )
    assert portmap.map_port(25565).protocol == "NAT-PMP"


def test_upnp_control_endpoint_never_leaves_gateway():
    for url in [
        "http://attacker.test/control",
        "http://127.0.0.1/control",
        "https://192.168.1.1@attacker.test/",
        "file:///etc/passwd",
    ]:
        with pytest.raises(portmap.MappingError):
            portmap._gateway_url(url, "192.168.1.1")
    assert portmap._gateway_url("http://192.168.1.1:5000/control", "192.168.1.1")


def test_upnp_soap_uses_advertised_endpoint(monkeypatch):
    calls = []

    def request(method, url, gateway, **kwargs):
        calls.append((method, url, kwargs))
        return (
            200,
            b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><u:GetExternalIPAddressResponse xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1"><NewExternalIPAddress>8.8.8.8</NewExternalIPAddress></u:GetExternalIPAddressResponse></s:Body></s:Envelope>',
        )

    monkeypatch.setattr(portmap, "_request", request)
    result = portmap._soap(
        "http://192.168.1.1/advertised",
        "urn:schemas-upnp-org:service:WANIPConnection:1",
        "GetExternalIPAddress",
        {},
        "192.168.1.1",
    )
    assert result["NewExternalIPAddress"] == "8.8.8.8"
    assert calls[0][1].endswith("/advertised")
    assert "#GetExternalIPAddress" in calls[0][2]["headers"]["SOAPAction"]


def test_xml_entities_rejected():
    with pytest.raises(portmap.MappingError):
        portmap._xml(b'<!DOCTYPE test [<!ENTITY x "secret">]><test>&x;</test>')


def test_cgnat_and_pc_failure_still_run_external_check(paths, monkeypatch):
    paths.save(Config())

    def denied(port):
        raise firewall.FirewallError("Нужен sudo -v")

    def no_mapping(port):
        raise portmap.MappingError("Нет автопроброса", "100.64.1.2")

    monkeypatch.setattr(firewall, "configure", denied)
    monkeypatch.setattr(portmap, "map_port", no_mapping)
    monkeypatch.setattr(network, "external_ipv4", lambda p: "8.8.8.8")
    monkeypatch.setattr(deployment.server, "status", lambda p: {"status": "Running"})
    calls = []
    monkeypatch.setattr(
        network,
        "probe",
        lambda address, p: (
            calls.append(address) or {"online": False, "identity_verified": False}
        ),
    )
    report = deployment.configure(paths)
    assert not report["verified"]
    assert calls == ["8.8.8.8:25565"]
    assert "CGNAT" in "\n".join(report["errors"])
    assert "sudo -v" in "\n".join(report["errors"])
    assert report["address"] == "8.8.8.8:25565"
    assert json.loads((paths.data / "deployment.json").read_text())["verified"] is False


def test_external_online_without_identity_is_not_success(paths, monkeypatch):
    from uwucraft import sources

    paths.save(Config(server_id="this-server-unique"))
    monkeypatch.setattr(
        sources,
        "metadata",
        lambda *a, **k: {"online": True, "motd": {"clean": ["another server"]}},
    )
    report = network.probe("8.8.8.8:25565", paths)
    assert report["online"] and not report["identity_verified"]
    assert network.reachability("8.8.8.8:25565", paths) != "Доступен извне"


@pytest.mark.parametrize(
    "identifier,motd,expected",
    [
        ("", "", False),
        ("unique-server-id", "My server unique-server-id", True),
    ],
)
def test_external_identity_requires_own_identifier(
    paths, monkeypatch, identifier, motd, expected
):
    from uwucraft import sources

    paths.save(Config(server_id=identifier))
    monkeypatch.setattr(
        sources,
        "metadata",
        lambda *a, **k: {
            "online": True,
            "motd": {"clean": [motd]},
        },
    )
    assert network.probe("8.8.8.8:25565", paths)["identity_verified"] is expected


async def test_tui_create_button_and_shared_backend(paths, monkeypatch):
    from textual.widgets import Checkbox, Input

    import uwucraft.create_ui as ui
    from uwucraft.tui import Uwucraft

    calls = []

    def backend(options, p, progress):
        progress("Проверяем шаг")
        calls.append(options)
        return {
            "address": "8.8.8.8:25565",
            "verified": False,
            "errors": ["CGNAT: нужен белый IPv4"],
            "warnings": [],
        }

    monkeypatch.setattr(ui, "create", backend)
    application = Uwucraft()
    async with application.run_test(size=(140, 60)) as pilot:
        await pilot.click("#create-server")
        await pilot.pause()
        await pilot.click("#create-submit")
        await pilot.pause()
        assert not calls  # no accepted EULA, no side effects
        application.screen.query_one("#create-name", Input).value = "Friends"
        application.screen.query_one("#create-eula", Checkbox).value = True
        await pilot.pause(0.3)
        await pilot.click("#create-submit")
        await pilot.pause(0.5)
        assert calls, str(application.screen.query_one("#create-errors").render())
        assert calls[0].name == "Friends"
        assert application.screen.query_one("#create-copy").display
        assert application.screen.query_one("#create-copy").disabled
        assert not application.screen.query("#create-fields")
        await pilot.click("#create-close")


def test_upnp_discovers_and_maps_only_advertised_gateway(monkeypatch):
    packets = [
        (
            b"HTTP/1.1 200 OK\r\nLOCATION: http://192.168.1.1/root.xml\r\n\r\n",
            ("192.168.1.1", 1900),
        )
    ]

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, address):
            pass

        def setsockopt(self, *args):
            pass

        def settimeout(self, *args):
            pass

        def sendto(self, *args):
            pass

        def recvfrom(self, *args):
            if packets:
                return packets.pop()
            raise TimeoutError

    monkeypatch.setattr(portmap.socket, "socket", lambda *args: Socket())
    calls = []
    service = "urn:schemas-upnp-org:service:WANIPConnection:1"

    def request(method, url, gateway, **kwargs):
        if method == "GET":
            return (
                200,
                f"<root><device><serviceList><service><serviceType>{service}</serviceType><controlURL>/device-advertised-control</controlURL></service></serviceList></device></root>".encode(),
            )
        assert url == "http://192.168.1.1/device-advertised-control"
        action = kwargs["headers"]["SOAPAction"].split("#")[-1].strip('"')
        calls.append((action, kwargs["content"]))
        if action == "GetSpecificPortMappingEntry":
            return 500, b"<Fault><detail><errorCode>714</errorCode></detail></Fault>"
        value = (
            "<NewExternalIPAddress>8.8.8.8</NewExternalIPAddress>"
            if action == "GetExternalIPAddress"
            else ""
        )
        return (
            200,
            f"<Envelope><{action}Response>{value}</{action}Response></Envelope>".encode(),
        )

    monkeypatch.setattr(portmap, "_request", request)
    mapping = portmap.upnp("192.168.1.1", "192.168.1.10", 25566)
    assert mapping.protocol == "UPnP" and mapping.port == 25566
    action, body = calls[-1]
    assert action == "AddPortMapping"
    for value in [
        b"<NewProtocol>TCP</NewProtocol>",
        b"<NewExternalPort>25566</NewExternalPort>",
        b"<NewInternalPort>25566</NewInternalPort>",
        b"<NewLeaseDuration>3600</NewLeaseDuration>",
    ]:
        assert value in body
