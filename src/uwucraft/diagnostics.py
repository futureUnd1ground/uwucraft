"""Read-only host diagnostics, reusable on any Linux machine."""

import platform
import shutil

import psutil

from .config import Paths
from .platforms import os_name


def doctor():
    paths = Paths()
    ancestor = paths.data
    while not ancestor.exists():
        ancestor = ancestor.parent
    return {
        "os": os_name(),
        "architecture": platform.machine(),
        "available_ram_mb": psutil.virtual_memory().available // 1024**2,
        "disk_free_gb": round(shutil.disk_usage(ancestor).free / 1024**3, 1),
        "java": shutil.which("java"),
        "systemd": shutil.which("systemctl"),
        "config": str(paths.file),
        "data": str(paths.data),
        "note": "Для прямого входа нужен публичный IPv4; relay обходит CGNAT. Используйте debug для проверки модов, JVM и публичного Minecraft.",
    }


def debug_report(paths=None, client_dir=None, external=False):
    """Structured diagnosis; never exports launcher arguments or complete logs."""
    import re

    from . import modcheck, network, owner_route, relay, server

    paths = paths or Paths()
    config = paths.load()
    report = {
        "server": {
            "loader": config.loader,
            "version": config.version,
            "status": server.status(paths),
        },
        "mods": modcheck.audit(paths),
        "relay": relay.status(paths),
        "requested_transport": config.relay_transport,
        "owner_route": owner_route.status(paths),
        "owner_route_note": "При включённом owner shortcut измерение публичного адреса с этого ПК идёт напрямую; внешняя доступность проверяется только независимым сервисом.",
        "findings": [],
    }
    try:
        response = network.minecraft_status(port=config.port)
        report["local_minecraft"] = {
            "version": response.get("version"),
            "players": response.get("players"),
        }
    except Exception as exc:
        # No arbitrary log body or process arguments are included.
        report["local_minecraft"] = {"error": str(exc)[:300]}
    patterns = {
        "missing_dependency": r"Missing (?:mandatory|required)|requires .*currently.*not installed|Mod resolution encountered",
        "wrong_side": r"Attempted to load class .* for invalid dist DEDICATED_SERVER|only supported on the client",
        "java_mismatch": r"UnsupportedClassVersionError|class file version",
        "mod_mismatch": r"server is likely to require channel|Server has additional mods|mismatched mod channel|Incompatible FML",
        "memory": r"OutOfMemoryError",
        "connection_reset": r"Connection reset|Соединение разорвано|Broken pipe",
    }
    logs = [
        ("server", paths.server / "logs/latest.log"),
        ("relay", paths.data / "relay.log"),
    ]
    if client_dir:
        from pathlib import Path

        client = Path(client_dir)
        mods = []
        for file in (client / "mods").glob("*.jar"):
            try:
                mods.extend(modcheck.read_mods(file, config.loader))
            except Exception as exc:
                report["findings"].append(
                    {
                        "code": "client_metadata",
                        "file": file.name,
                        "message": str(exc)[:300],
                    }
                )
        versions = {m["id"]: m["version"] for m in mods}
        report["client_mods"] = versions
        report["client_missing_candidates"] = [
            m["id"]
            for m in report["mods"]["mods"]
            if m["id"] not in versions and not m.get("nested")
        ]
        report["client_version_mismatch"] = [
            {"id": m["id"], "server": m["version"], "client": versions[m["id"]]}
            for m in report["mods"]["mods"]
            if m["id"] in versions and versions[m["id"]] != m["version"]
        ]
        report["client_note"] = (
            "Клиентские и серверные JAR могут различаться; сравниваются mod ID/версии. Не все серверные моды нужны клиенту. Перезапустите клиент после изменения mods."
        )
        logs.append(("client", client / "logs/latest.log"))
    for origin, file in logs:
        if not file.is_file():
            continue
        with file.open("rb") as stream:
            stream.seek(max(0, file.stat().st_size - 512 * 1024))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
        found = {}
        for line in lines:
            for code, pattern in patterns.items():
                if re.search(pattern, line, re.I):
                    # Findings are narrow matching lines, never auth/launch lines.
                    safe = re.sub(
                        r"(?i)(token|password|secret|api[_-]?key)[=:\s]+[^\s,;]+",
                        r"\1=<redacted>",
                        line,
                    )
                    found[code] = {
                        "origin": origin,
                        "code": code,
                        "message": safe[:600],
                    }
        report["findings"].extend(found.values())
    if report["mods"]["errors"]:
        report["findings"].append(
            {
                "code": "mod_preflight_failed",
                "message": "Исправьте указанные зависимости/версии до запуска.",
            }
        )
    if external and report["relay"].get("address"):
        try:
            report["external_minecraft"] = network.probe(
                report["relay"]["address"], paths
            )
        except Exception as exc:
            report["external_minecraft"] = {"error": str(exc)[:300]}
    report["limits"] = (
        "SLP-проверка подтверждает ответ Minecraft, а не полный игровой вход. Статический анализ не доказывает отсутствие ошибок кода модов."
    )
    return report
