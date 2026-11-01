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
        "note": "Публичный доступ требует белого IPv4, входящего TCP-порта и корректного обратного маршрута.",
    }
