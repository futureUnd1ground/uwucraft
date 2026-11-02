"""Linux owner-only public-address shortcut; root helper uses only stdlib.

External players still use the relay. This is never an external latency probe.
"""

import argparse
import ipaddress
import json
import math
import os
import re
import stat
import subprocess
import time
from pathlib import Path


def identity(uid, local_port):
    if not 0 < uid < 2**31 or not 1024 <= local_port <= 65535:
        raise ValueError("Invalid owner UID/local port")
    return f"uwucraft_owner_{uid}_{local_port}"


def read_endpoint(path, uid, local_port=None):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_size > 16384:
            raise ValueError("Unsafe relay state")
        raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("Oversized relay state")
        data = json.loads(raw)
    if local_port is not None and data.get("local_port", local_port) != local_port:
        raise ValueError("Server port changed; repeat owner-route enable")
    host, port = data["address"].rsplit(":", 1)
    address = ipaddress.IPv4Address(host)
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise ValueError("Relay address is not public IPv4")
    port = int(port)
    if not 1024 <= port <= 65535:
        raise ValueError("Invalid relay port")
    pid = int(data["pid"])
    if pid <= 1 or Path(f"/proc/{pid}").stat().st_uid != uid:
        raise ValueError("Relay process owner mismatch")
    # Check process birth, not only a reusable PID. No user code runs as root.
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    boot = re.search(r"^btime (\d+)$", Path("/proc/stat").read_text(), re.M)
    created = int(boot[1]) + int(fields[19]) / os.sysconf("SC_CLK_TCK")
    recorded = float(data["created"])
    if not math.isfinite(recorded) or abs(created - recorded) > 0.2:
        raise ValueError("Stale relay process identity")
    return str(address), port


def plan(document, uid, local_port, endpoint):
    name = identity(uid, local_port)
    entries = document.get("nftables", [])
    exists = any(
        item.get("table", {}).get("family") == "ip"
        and item["table"].get("name") == name
        for item in entries
    )
    commands = []
    if exists:
        commands.append({"delete": {"table": {"family": "ip", "name": name}}})
    if endpoint is None:
        return {"nftables": commands}
    host, port = endpoint
    address = ipaddress.IPv4Address(host)
    if not address.is_global or address.is_multicast or not 1024 <= port <= 65535:
        raise ValueError("Invalid public endpoint")
    commands.extend(
        [
            {"add": {"table": {"family": "ip", "name": name}}},
            {
                "add": {
                    "chain": {
                        "family": "ip",
                        "table": name,
                        "name": "output",
                        "type": "nat",
                        "hook": "output",
                        "prio": -110,
                        "policy": "accept",
                    }
                }
            },
            {
                "add": {
                    "rule": {
                        "family": "ip",
                        "table": name,
                        "chain": "output",
                        "comment": "uwucraft owner shortcut; not external reachability",
                        "expr": [
                            {
                                "match": {
                                    "op": "==",
                                    "left": {"meta": {"key": "skuid"}},
                                    "right": uid,
                                }
                            },
                            {
                                "match": {
                                    "op": "==",
                                    "left": {
                                        "payload": {"protocol": "ip", "field": "daddr"}
                                    },
                                    "right": host,
                                }
                            },
                            {
                                "match": {
                                    "op": "==",
                                    "left": {
                                        "payload": {"protocol": "tcp", "field": "dport"}
                                    },
                                    "right": port,
                                }
                            },
                            {"dnat": {"addr": "127.0.0.1", "port": local_port}},
                        ],
                    }
                }
            },
        ]
    )
    return {"nftables": commands}


def execute(args, **kwargs):
    return subprocess.run(
        args, capture_output=True, text=True, check=True, timeout=30, **kwargs
    )


def apply(state, uid, local_port, disable=False):
    name = identity(uid, local_port)
    endpoint = None
    error = ""
    if not disable:
        try:
            endpoint = read_endpoint(state, uid, local_port)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            error = str(exc)
    document = json.loads(execute(["nft", "-j", "list", "ruleset"]).stdout)
    transaction = plan(document, uid, local_port, endpoint)
    # Avoid replacing an unchanged NAT table (and leave conntrack alone).
    current = Path(f"/run/{name}.json")
    value = {
        "endpoint": list(endpoint) if endpoint else None,
        "state": str(Path(state).absolute()),
        "uid": uid,
        "local_port": local_port,
        "error": error,
    }
    old = None
    try:
        old = json.loads(current.read_text())
        old.pop("checked_at", None)
    except (OSError, ValueError):
        pass
    table_present = any(
        i.get("table", {}).get("name") == name for i in document.get("nftables", [])
    )
    if transaction["nftables"] and (
        value != old or not table_present or endpoint is None
    ):
        execute(["nft", "-j", "-f", "-"], input=json.dumps(transaction))
    value["checked_at"] = time.time()
    temporary = current.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.chmod(0o644)
    temporary.replace(current)
    return value


def install(state, uid, local_port, disable=False):
    if os.geteuid() != 0:
        raise PermissionError("Administrator rights required for owner-only NAT")
    name = identity(uid, local_port)
    if disable:
        if (Path("/etc/systemd/system") / (name + ".timer")).exists():
            execute(["systemctl", "disable", "--now", name + ".timer"])
        return apply(state, uid, local_port, disable=True)
    state = Path(state).absolute()
    if any(c in str(state) for c in "\n\r\0"):
        raise ValueError("Invalid state path")
    read_endpoint(state, uid, local_port)
    folder = Path("/etc/uwucraft")
    folder.mkdir(mode=0o755, exist_ok=True)
    if (
        folder.is_symlink()
        or folder.stat().st_uid != 0
        or folder.stat().st_mode & 0o022
    ):
        raise PermissionError("Unsafe root helper directory")
    target = folder / (name + ".py")
    if target.is_symlink():
        raise PermissionError("Unsafe root helper path")
    target.write_bytes(Path(__file__).read_bytes())
    target.chmod(0o644)
    quoted = json.dumps(str(state), ensure_ascii=False).replace("%", "%%")
    units = {
        name + ".service": f"""[Unit]
Description=uwucraft same-PC public-address shortcut (owner only)
After=network-online.target nftables.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {target} --apply --state {quoted} --uid {uid} --local-port {local_port}
""",
        name + ".timer": f"""[Unit]
Description=uwucraft refresh owner shortcut when relay endpoint changes
[Timer]
OnBootSec=15
OnUnitActiveSec=5
Unit={name}.service
[Install]
WantedBy=timers.target
""",
    }
    for filename, text in units.items():
        destination = Path("/etc/systemd/system") / filename
        if destination.is_symlink():
            raise PermissionError("Unsafe unit path")
        destination.write_text(text)
        destination.chmod(0o644)
    value = apply(state, uid, local_port)
    execute(["systemctl", "daemon-reload"])
    execute(["systemctl", "enable", "--now", name + ".timer"])
    return value


def configure(paths=None, disable=False):
    from .config import Error, Paths
    from .platforms import windows
    from .privilege import elevated

    if windows():
        raise Error("Owner shortcut через nftables доступен только на Linux.")
    paths = paths or Paths()
    command = elevated(
        [
            "/usr/bin/python3",
            __file__,
            "--state",
            str(paths.data / "relay.json"),
            "--uid",
            str(os.getuid()),
            "--local-port",
            str(paths.load().port),
            *(["--disable"] if disable else []),
        ]
    )
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise Error("Не удалось настроить owner shortcut: " + result.stderr[-700:])
    return json.loads(result.stdout)


def status(paths=None):
    from .config import Paths
    from .platforms import windows

    if windows():
        return {}
    paths = paths or Paths()
    try:
        value = json.loads(
            Path(f"/run/{identity(os.getuid(), paths.load().port)}.json").read_text()
        )
        if value.get("state") != str((paths.data / "relay.json").absolute()):
            return {}
        value["stale"] = not 0 <= time.time() - value.get("checked_at", 0) < 20
        value["scope"] = "same-PC owner only; not an external latency result"
        return value
    except (OSError, ValueError):
        return {}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--uid", required=True, type=int)
    parser.add_argument("--local-port", required=True, type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--disable", action="store_true")
    options = parser.parse_args()
    function = apply if options.apply else install
    print(
        json.dumps(
            function(
                options.state, options.uid, options.local_port, disable=options.disable
            )
        )
    )
