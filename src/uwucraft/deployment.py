"""Public deployment with verifiable postconditions."""

import json
import sqlite3
import time

from . import network, server
from .config import Error, Paths, atomic
from .settings import properties


def expose(method, paths=None, host="bore.pub"):
    paths = paths or Paths()
    if method != "bore":
        raise Error("Доступные режимы: none, bore.")
    server.wait_ready(paths)
    config = properties(paths)
    if config.get("online-mode") == "false":
        database = paths.server / "plugins/AuthMe/authme.db"
        if not database.exists():
            raise Error(
                "Offline-публичный сервер требует настроенный AuthMe. Используйте Paper/Purpur + auth-setup."
            )
        with sqlite3.connect(
            f"file:{database}?mode=ro", uri=True, timeout=3
        ) as connection:
            ops = paths.server / "ops.json"
            for player in json.loads(ops.read_text()) if ops.exists() else []:
                if not connection.execute(
                    "SELECT 1 FROM authme WHERE username=? AND password<>?",
                    (player["name"].lower(), ""),
                ).fetchone():
                    raise Error(
                        "Сначала зарегистрируйте offline-администратора: uwucraft auth-reserve-admin "
                        + player["name"]
                    )
    from .tunnel import setup, status

    setup(host=host, paths=paths)
    for _ in range(45):
        state = status(paths)
        if state:
            # A successful SLP reply proves that the published address reaches
            # Minecraft, rather than just proving the relay process is alive.
            reply = network.minecraft_status(state["host"], state["port"])
            report = {
                "address": state["address"],
                "version": reply.get("version"),
                "checked_at": time.time(),
                "kind": "Minecraft SLP via public relay",
                "independent_probe": False,
            }
            atomic(paths.data / "deployment.json", json.dumps(report))
            return (
                "Публичное Minecraft-подключение проверено: "
                + state["address"]
                + "; версия "
                + reply.get("version", {}).get("name", "—")
                + ". Вход игрока требует отдельной проверки. Адрес может измениться при рестарте relay."
            )
        time.sleep(1)
    raise Error(
        "Relay не выдал адрес за 45 секунд. journalctl --user -u uwucraft-tunnel.service"
    )
