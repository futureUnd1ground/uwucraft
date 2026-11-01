"""Timer entry point, maintaining prior running state even if backup fails."""

from . import backups, server
from .cli import _lifecycle


def main():
    running = server.status()["status"] in {"Running", "Starting"}
    if running:
        _lifecycle("stop")
    try:
        print(backups.create())
    finally:
        if running:
            _lifecycle("start")


if __name__ == "__main__":
    main()
