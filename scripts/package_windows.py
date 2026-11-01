"""Build the Windows release ZIP with the exact official Python installer.

Run from the checkout: python scripts/package_windows.py --python-installer FILE
The executable is a release asset, never committed or copied into the wheel.
"""

import argparse
import hashlib
import json
import subprocess
import tomllib
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python-installer", type=Path, required=True)
    options = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    metadata = json.loads(
        (root / "vendor/python-runtime.json").read_text(encoding="utf-8")
    )
    if (
        hashlib.sha256(options.python_installer.read_bytes()).hexdigest()
        != metadata["sha256"]
    ):
        parser.error("Python installer SHA-256 mismatch")
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    target = root / "dist" / f"uwucraft-{version}-windows.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "archive",
            "--format=zip",
            "--prefix=uwucraft/",
            "--output=" + str(target),
            "HEAD",
        ],
        cwd=root,
        check=True,
    )
    with zipfile.ZipFile(target, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(options.python_installer, "uwucraft/vendor/python-installer.exe")
    target.with_suffix(".zip.sha256").write_text(
        hashlib.sha256(target.read_bytes()).hexdigest() + "  " + target.name + "\n",
        encoding="utf-8",
    )
    print(target)


if __name__ == "__main__":
    main()
