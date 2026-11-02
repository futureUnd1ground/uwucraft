import json
import socket
import socketserver
import struct
import threading

from uwucraft import direct_bridge, relay_egress


def test_output_exception_is_exact_scoped_and_idempotent():
    chains = [
        {
            "chain": {
                "family": "inet",
                "table": "filter",
                "name": "output",
                "hook": "output",
            }
        },
        {"chain": {"family": "ip", "table": "vpn", "name": "output", "hook": "output"}},
    ]
    document = {"nftables": chains}
    plan = relay_egress.output_plan(document, "159.223.110.159", "ethernet0")
    assert len(plan["nftables"]) == 2
    for index, command in enumerate(plan["nftables"]):
        rule = command["insert"]["rule"]
        assert rule["expr"][0]["match"]["right"] == "ethernet0"
        assert rule["expr"][1]["match"]["right"] == "159.223.110.159"
        assert rule["expr"][2]["match"]["right"] == 7835
        document["nftables"].append({"rule": {**rule, "handle": index + 1}})
    assert (
        relay_egress.output_plan(document, "159.223.110.159", "ethernet0")["nftables"]
        == []
    )
    assert "input" not in json.dumps(plan)


def test_windows_socket_is_bound_to_physical_adapter(monkeypatch):
    calls = []

    class Socket:
        def settimeout(self, value):
            pass

        def setsockopt(self, *args):
            calls.append(args)

        def bind(self, value):
            calls.append(("bind", value))

        def connect(self, value):
            calls.append(("connect", value))

        def close(self):
            pass

    monkeypatch.setattr(direct_bridge, "windows", lambda: True)
    monkeypatch.setattr(direct_bridge.socket, "socket", lambda *a: Socket())
    direct_bridge.connect(
        "159.223.110.159", {"index": 7, "device": "Ethernet", "address": "192.168.1.2"}
    )
    assert (socket.IPPROTO_IP, 31, struct.pack("!I", 7)) in calls
    assert ("bind", ("192.168.1.2", 0)) in calls
    assert ("connect", ("159.223.110.159", 7835)) in calls


def test_bridge_preserves_large_stream_and_half_close(monkeypatch):
    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            data = []
            while chunk := self.request.recv(65536):
                data.append(chunk)
            self.request.sendall(b"".join(data))

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    endpoint = Server(("127.0.0.1", 0), Echo)
    thread = threading.Thread(target=endpoint.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        direct_bridge,
        "physical_interface",
        lambda: {"device": "test", "index": 1, "address": "127.0.0.1"},
    )
    monkeypatch.setattr(
        direct_bridge,
        "connect",
        lambda *a: socket.create_connection(endpoint.server_address, timeout=3),
    )
    bridge = direct_bridge.DirectBridge("159.223.110.159")
    try:
        with socket.create_connection((bridge.listen_ip, 7835), timeout=3) as client:
            data = b"chunk-packet" * 32768
            client.sendall(data)
            client.shutdown(socket.SHUT_WR)
            result = []
            while chunk := client.recv(65536):
                result.append(chunk)
            assert b"".join(result) == data
    finally:
        bridge.close()
        endpoint.shutdown()
        endpoint.server_close()


def test_verified_bore_cache_does_not_need_github_at_boot(tmp_path, monkeypatch):
    import hashlib

    from uwucraft import relay
    from uwucraft.config import Paths, atomic

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    p = Paths()
    binary = p.data / "tools/bore-v0.6.0"
    atomic(binary, b"verified executable fixture")
    monkeypatch.setattr(relay, "windows", lambda: False)
    monkeypatch.setattr(relay.platform, "machine", lambda: "x86_64")
    atomic(
        binary.parent / "bore-cache.json",
        json.dumps(
            {
                "platform": "x86_64-unknown-linux-musl.tar.gz",
                "name": binary.name,
                "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            }
        ),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Verified local binary must not require GitHub")

    monkeypatch.setattr(relay, "metadata", forbidden)
    assert relay.provision(p) == binary


def test_relay_cached_endpoint_survives_dns_outage(tmp_path, monkeypatch):
    import pytest

    from uwucraft import relay
    from uwucraft.config import Error, Paths, atomic

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    p = Paths()
    cache = p.data / "relay-endpoint.json"
    atomic(
        cache,
        json.dumps({"host": "bore.pub", "ip": "159.223.110.159", "checked_at": 0}),
    )

    def unavailable(*a, **kw):
        raise socket.gaierror("VPN DNS unavailable")

    monkeypatch.setattr(relay.socket, "getaddrinfo", unavailable)
    assert relay.resolve_host("bore.pub", p) == "159.223.110.159"
    with pytest.raises(Error, match="DNS"):
        relay.resolve_host("different-relay.example", p)
