import hashlib
import json
import re
import uuid

from .config import Error, Paths, atomic
from .sources import metadata


def seed_admin(name, offline=False, paths=None):
    paths = paths or Paths()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", name):
        raise Error("Имя Minecraft: 1–16 латинских букв, цифр и _.")
    if offline:
        digest = hashlib.md5(("OfflinePlayer:" + name).encode()).digest()
        identifier = str(uuid.UUID(bytes=digest, version=3))
    else:
        result = metadata("https://api.mojang.com/users/profiles/minecraft/" + name)
        identifier = str(uuid.UUID(result["id"]))
        name = result["name"]
    record = {"uuid": identifier, "name": name}
    for filename in ["whitelist.json", "ops.json"]:
        target = paths.server / filename
        records = json.loads(target.read_text()) if target.exists() else []
        if not any(p["uuid"] == identifier for p in records):
            records.append(
                record
                | (
                    {"level": 4, "bypassesPlayerLimit": False}
                    if filename == "ops.json"
                    else {}
                )
            )
            atomic(target, json.dumps(records, indent=2))
