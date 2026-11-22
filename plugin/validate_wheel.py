#!/usr/bin/env python3
"""Valida un wheel de plugin OrcaSlicer según el formato oficial.

Comprueba estructura, metadata, tag puro, top_level, RECORD/hashes, import
sin `orca`, clases del plugin con un `orca` falso y compilación de __init__.py.

Uso:
    python3 validate_wheel.py [ruta.whl]
"""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib
import io
import pathlib
import py_compile
import sys
import tempfile
import types
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT = ROOT / "dist" / "flashforgemonitor-0.9.0-py3-none-any.whl"
VERSION = "0.9.0"
STABLE = "flashforge_monitor_any.whl"
PACKAGE = "flashforge_monitor"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("  OK   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))


def record_hash(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def metadata_headers(data: bytes) -> dict[str, str]:
    text = data.decode("utf-8")
    headers: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            break
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return headers


def validate(path: pathlib.Path) -> int:
    print(f"Wheel: {path}")
    if not path.is_file():
        check("el archivo existe", False, str(path))
        return 1
    check("el archivo existe", True, f"{path.stat().st_size} bytes")

    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        infos = {name: wheel.getinfo(name) for name in names}

        dist_infos = sorted({name.split("/")[0] for name in names
                             if name.split("/")[0].endswith(".dist-info")})
        check("exactamente un directorio *.dist-info", len(dist_infos) == 1,
              ", ".join(dist_infos) or "ninguno")
        if len(dist_infos) != 1:
            return 1
        dist_info = dist_infos[0]

        required = ["METADATA", "WHEEL", "RECORD"]
        for filename in required:
            member = f"{dist_info}/{filename}"
            check(f"{filename} presente", member in names, member)
        if any(f"{dist_info}/{filename}" not in names for filename in required):
            return 1

        metadata = metadata_headers(wheel.read(f"{dist_info}/METADATA"))
        check("METADATA Name = FlashForgeMonitor",
              metadata.get("name") == "FlashForgeMonitor", metadata.get("name", ""))
        check("METADATA Version = 0.9.0",
              metadata.get("version") == VERSION, metadata.get("version", ""))
        check("METADATA Author = Heberdiafet",
              metadata.get("author") == "Heberdiafet", metadata.get("author", ""))
        check("METADATA License = MIT",
              metadata.get("license") == "MIT", metadata.get("license", ""))

        wheel_text = wheel.read(f"{dist_info}/WHEEL").decode("utf-8")
        wheel_headers: dict[str, str] = {}
        for line in wheel_text.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                wheel_headers[key.strip().lower()] = value.strip()
        check("WHEEL Tag = py3-none-any",
              wheel_headers.get("tag") == "py3-none-any", wheel_headers.get("tag", ""))
        check("WHEEL Root-Is-Purelib = true",
              wheel_headers.get("root-is-purelib") == "true",
              wheel_headers.get("root-is-purelib", ""))
        check("nombre de archivo canónico o estable",
              path.name in ("flashforgemonitor-0.9.0-py3-none-any.whl", STABLE),
              path.name)

        top_level = [line.strip() for line in
                     wheel.read(f"{dist_info}/top_level.txt").decode("utf-8").splitlines()
                     if line.strip()]
        check("top_level.txt nombra un único paquete",
              top_level == [PACKAGE], repr(top_level))
        check("el paquete importable existe en el wheel",
              f"{PACKAGE}/__init__.py" in names)

        record_text = wheel.read(f"{dist_info}/RECORD").decode("utf-8")
        rows = list(csv.reader(io.StringIO(record_text)))
        record_ok = True
        record_detail = ""
        listed: set[str] = set()
        for row in rows:
            if not row:
                continue
            if len(row) != 3:
                record_ok = False
                record_detail = f"fila inválida: {row!r}"
                break
            name, digest, size = row
            if name.endswith("/RECORD"):
                continue
            listed.add(name)
            if name not in names:
                record_ok = False
                record_detail = f"falta en el zip: {name}"
                break
            data = wheel.read(name)
            if digest != record_hash(data):
                record_ok = False
                record_detail = f"hash distinto: {name}"
                break
            if str(len(data)) != size:
                record_ok = False
                record_detail = f"tamaño distinto: {name}"
                break
        expected = set(names) - {f"{dist_info}/RECORD"}
        if listed != expected:
            record_ok = False
            missing = sorted(expected - listed)
            extra = sorted(listed - expected)
            record_detail = f"faltan {missing} sobran {extra}"
        check("RECORD completo y hashes sha256 correctos", record_ok, record_detail)

        check("sin __pycache__ ni archivos temporales",
              not any("__pycache__" in name or name.endswith(".pyc") for name in names))

        with tempfile.TemporaryDirectory(prefix="ffm-wheel-") as temp:
            target = pathlib.Path(temp)
            wheel.extractall(target)
            entry = target / PACKAGE / "__init__.py"
            try:
                py_compile.compile(str(entry), doraise=True)
                compiled = True
                compile_detail = ""
            except py_compile.PyCompileError as exc:
                compiled = False
                compile_detail = str(exc)
            check("py_compile de flashforge_monitor/__init__.py", compiled, compile_detail)

            sys.path.insert(0, str(target))
            try:
                for module_name in list(sys.modules):
                    if module_name == PACKAGE or module_name.startswith(PACKAGE + "."):
                        del sys.modules[module_name]
                pkg = importlib.import_module(PACKAGE)
                check("import con orca=None", True)
                check("PLUGIN_VERSION = 0.9.0",
                      getattr(pkg, "PLUGIN_VERSION", None) == VERSION,
                      str(getattr(pkg, "PLUGIN_VERSION", None)))
                check("FlashForgeClient disponible sin Orca",
                      hasattr(pkg, "FlashForgeClient"))
                check("atributos de clase paquete presentes",
                      hasattr(pkg, "FlashForgePage")
                      and hasattr(pkg, "FlashForgeMonitorPlugin"))
                check("sin Orca, las clases de capability son None (import seguro)",
                      pkg.FlashForgePage is None and pkg.FlashForgeMonitorPlugin is None)

                # Con un orca falso se comprueba que el registro real se define.
                fake = types.ModuleType("orca")
                fake.base = type("PluginBase", (), {})
                fake.pages = types.SimpleNamespace(
                    PagesPluginCapabilityBase=type("PagesBase", (), {}))
                fake.plugin = lambda cls: cls
                fake.register_capability = lambda cls: None
                fake.host = types.SimpleNamespace(
                    ui=types.SimpleNamespace(push_notification=lambda *a, **k: None))
                sys.modules["orca"] = fake
                for module_name in list(sys.modules):
                    if module_name == PACKAGE or module_name.startswith(PACKAGE + "."):
                        del sys.modules[module_name]
                pkg2 = importlib.import_module(PACKAGE)
                check("con orca falso: FlashForgePage definida",
                      isinstance(getattr(pkg2, "FlashForgePage", None), type))
                check("con orca falso: FlashForgeMonitorPlugin definida",
                      isinstance(getattr(pkg2, "FlashForgeMonitorPlugin", None), type))
            finally:
                sys.modules.pop("orca", None)
                for module_name in list(sys.modules):
                    if module_name == PACKAGE or module_name.startswith(PACKAGE + "."):
                        del sys.modules[module_name]
                sys.path.remove(str(target))

    print()
    failed = [item for item in results if not item[1]]
    print(f"Resultado: {len(results) - len(failed)}/{len(results)} comprobaciones OK")
    if failed:
        print("Fallos:")
        for name, _, detail in failed:
            print(f"  - {name} {detail}")
        return 1
    return 0


def main() -> int:
    target = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT
    return validate(target)


if __name__ == "__main__":
    raise SystemExit(main())
