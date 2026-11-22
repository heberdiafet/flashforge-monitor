#!/usr/bin/env python3
"""Construye el wheel puro de FlashForge Monitor sin dependencias externas.

Uso:
    python3 build_wheel.py            # desde plugin/
    python3 build_wheel.py --check    # construye y valida el RECORD

El artefacto sale en dist/ y se copia a release/ manualmente (o con --release).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import pathlib
import re
import shutil
import sys
import zipfile

NAME = "FlashForgeMonitor"
VERSION = "0.8.2"
NORMALIZED = "flashforgemonitor"
DIST_INFO = f"flashforge_monitor-{VERSION}.dist-info"
WHEEL_NAME = f"{NORMALIZED}-{VERSION}-py3-none-any.whl"

ROOT = pathlib.Path(__file__).resolve().parent
DIST = ROOT / "dist"
PACKAGE = DIST / "flashforge_monitor" / "__init__.py"
OUTPUT = DIST / WHEEL_NAME
RELEASE = ROOT / "release" / WHEEL_NAME

SUMMARY = (
    "Monitor y control local para impresoras FlashForge (AD5X / Adventurer 5M / "
    "5M Pro) dentro de OrcaSlicer — estado, temperaturas, IFS, archivos, ejes, "
    "calibración, filamento y multi-impresora, sin nube."
)

METADATA = (
    "Metadata-Version: 2.1\n"
    f"Name: {NAME}\n"
    f"Version: {VERSION}\n"
    f"Summary: {SUMMARY}\n"
    "Author: Heberdiafet\n"
    "License: MIT\n"
    "\n"
).encode("utf-8")

WHEEL_META = (
    "Wheel-Version: 1.0\n"
    "Generator: flashforge-monitor-build (1.0)\n"
    "Root-Is-Purelib: true\n"
    "Tag: py3-none-any\n"
).encode("utf-8")

TOP_LEVEL = b"flashforge_monitor\n"


def record_hash(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def add_file(files: dict[str, bytes], path: str, data: bytes) -> None:
    files[path] = data


def build() -> pathlib.Path:
    if not PACKAGE.is_file():
        raise SystemExit(f"No existe {PACKAGE}. Copia primero el plugin actual.")

    source = PACKAGE.read_bytes()
    match = re.search(rb'PLUGIN_VERSION\s*=\s*"([^"]+)"', source)
    found = match.group(1).decode("ascii") if match else "(sin versión)"
    if found != VERSION:
        raise SystemExit(f"PLUGIN_VERSION={found!r} no coincide con {VERSION!r}")

    files: dict[str, bytes] = {}
    add_file(files, "flashforge_monitor/__init__.py", source)
    add_file(files, f"{DIST_INFO}/METADATA", METADATA)
    add_file(files, f"{DIST_INFO}/WHEEL", WHEEL_META)
    add_file(files, f"{DIST_INFO}/top_level.txt", TOP_LEVEL)

    record_lines: list[str] = []
    for path in sorted(files):
        data = files[path]
        record_lines.append(f"{path},{record_hash(data)},{len(data)}")
    record_lines.append(f"{DIST_INFO}/RECORD,,")
    files[f"{DIST_INFO}/RECORD"] = ("\n".join(record_lines) + "\n").encode("utf-8")

    DIST.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w") as wheel:
        for path in sorted(files):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o644 & 0xFFFF) << 16
            wheel.writestr(info, files[path], compress_type=zipfile.ZIP_DEFLATED,
                           compresslevel=9)

    digest = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    print(f"Wheel: {OUTPUT}")
    print(f"SHA-256: {digest}")
    print(f"Tamaño: {OUTPUT.stat().st_size} bytes")
    return OUTPUT


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="valida el RECORD/hashes tras construir")
    parser.add_argument("--release", action="store_true",
                        help="copia también a release/")
    args = parser.parse_args()

    wheel = build()
    if args.check:
        import validate_wheel
        result = validate_wheel.validate(wheel)
        if result != 0:
            return result
    if args.release:
        RELEASE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(wheel, RELEASE)
        print(f"Release: {RELEASE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
