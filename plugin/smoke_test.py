#!/usr/bin/env python3
"""Smoke test de la API HTTP real de una FlashForge AD5X (solo lectura).

Valida la plomería del cliente del plugin contra una impresora física **sin
necesitar el check code correcto**: comprueba que el puerto 8898 habla el
envelope documentado ``{code, message}`` y que los mensajes de error de
credenciales son los esperados.

Uso:
    python3 smoke_test.py
    python3 smoke_test.py --host 192.168.1.50 --serial SNAD5X000001
    python3 smoke_test.py --code 3f9a0c1e        # con el Printer ID real

Hace **4 peticiones** (5 si el check code es válido). No envía `/control`, no
imprime y no modifica nada en la impresora.

Códigos de salida: 0 = plomería correcta, 1 = algo no coincide, 2 = sin red.
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys

PLUGIN_PATH = (
    pathlib.Path(__file__).resolve().parent
    / "orca_plugins"
    / "flashforge_monitor"
    / "flashforge_monitor.py"
)

#: Valor que OrcaSlicer tiene guardado en esta unidad y que la impresora rechaza
#: (docs/api-reference.md §2.2a). Solo se usa si no se pasa --code.
KNOWN_BAD_CODE = "88888888"


def load_plugin():
    spec = importlib.util.spec_from_file_location("flashforge_monitor_smoke", PLUGIN_PATH)
    if spec is None or spec.loader is None:
        raise SystemExit(f"No se pudo cargar {PLUGIN_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def probe(module, host: str, serial: str, code: str, path: str,
          extra: dict | None = None) -> dict:
    """POST crudo (sin validar el envelope) para ver la respuesta tal cual."""
    client = module.FlashForgeClient(host=host, serial=serial, check_code=code, timeout=5.0)
    body = {"serialNumber": serial, "checkCode": code}
    if extra:
        body.update(extra)
    try:
        return client._request(path, body)
    except module.FlashForgeError as exc:
        return {"code": None, "message": f"{exc}", "network_error": exc.kind}


def show(label: str, data: dict) -> None:
    code = data.get("code")
    message = str(data.get("message") or "")
    print(f"  {label}: code={code!r} message={message!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="192.168.1.50", help="IP de la impresora")
    parser.add_argument("--serial", default="SNAD5X000001", help="número de serie real")
    parser.add_argument("--code", default="", help="Printer ID real (8 caracteres)")
    parser.add_argument("--fake-serial", default="SNFALSO00000X",
                        help="serial inventado para probar el diagnóstico")
    args = parser.parse_args()

    module = load_plugin()
    code = args.code or KNOWN_BAD_CODE
    using_real_code = bool(args.code)
    port = module.API_PORT

    print("FlashForge Monitor · smoke test")
    print(f"  impresora : http://{args.host}:{port}")
    print(f"  serial    : {args.serial}")
    print(f"  check code: {'(el indicado)' if using_real_code else KNOWN_BAD_CODE + ' (se espera que falle)'}")
    print()

    checks: list[tuple[str, bool, str]] = []

    # 0) Descubrimiento UDP (sin credenciales, un intento de 2 s).
    try:
        found = module.discover_printers(timeout=2.0)
        print(f"  UDP discovery: {len(found)} respuesta(s)")
        for entry in found:
            print(f"     {entry['model']} · {entry['ip']} · {entry['serial']} "
                  f"· pid 0x{entry['pid']:04X} · http {entry['http_port']}")
        checks.append((
            "Descubrimiento UDP responde",
            True,
            "" if found else "sin respuestas (¿broadcast bloqueado?)",
        ))
    except module.FlashForgeError as exc:
        print(f"  UDP discovery: error {exc} ({exc.kind})")
        checks.append(("Descubrimiento UDP responde", False, str(exc)))
    print()

    # 1) /checkCode con el serial bueno y el check code indicado.
    data = probe(module, args.host, args.serial, code, "/checkCode")
    if data.get("network_error"):
        print(f"✗ Sin conexión con la impresora: {data.get('message')}")
        return 2

    show("POST /checkCode", data)
    ok = data.get("code") == 0 if using_real_code else data.get("message") == "Access code is different"
    checks.append(("El envelope de /checkCode llega y el formato del cuerpo es correcto", ok,
                   "" if ok else "respuesta inesperada"))

    # 2) Serial inventado. Ojo (§3.4/F4, confirmado en hardware): /checkCode
    #    acepta un check code válido aunque el serial no coincida; el serial lo
    #    valida /detail. Con un código incorrecto, /checkCode sí responde
    #    "SN is different" porque comprueba el serial primero.
    data = probe(module, args.host, args.fake_serial, code, "/checkCode")
    show("POST /checkCode (SN falso)", data)
    if using_real_code:
        fake_detail = probe(module, args.host, args.fake_serial, code, "/detail")
        print(f"  POST /detail (SN falso): code={fake_detail.get('code')!r} "
              f"message={fake_detail.get('message')!r}")
        ok = fake_detail.get("message") == "SN is different"
        checks.append(("Con serial falso, /detail responde 'SN is different'", ok,
                       "" if ok else f"llegó {fake_detail.get('message')!r}"))
    else:
        ok = data.get("message") == "SN is different"
        checks.append(("Con serial falso responde 'SN is different'", ok,
                       "" if ok else f"llegó {data.get('message')!r}"))

    # 3) /detail con el check code actual: debe ser error de credenciales, no 404.
    data = probe(module, args.host, args.serial, code, "/detail")
    show("POST /detail", data)
    valid_session = bool(using_real_code and data.get("code") == 0)
    if valid_session:
        ok = True
        detail = data.get("detail") or {}
        print(f"     estado={detail.get('status')!r} pid={detail.get('pid')!r} "
              f"fw={detail.get('firmwareVersion')!r}")
        print(f"     nozzle={detail.get('rightTemp')}/{detail.get('rightTargetTemp')} "
              f"cama={detail.get('platTemp')}/{detail.get('platTargetTemp')} "
              f"speed={detail.get('printSpeedAdjust')} z={detail.get('zAxisCompensation')} "
              f"led={detail.get('lightStatus')!r}")
        slots = ((detail.get("matlStationInfo") or {}).get("slotInfos") or [])
        print("     IFS: " + ", ".join(
            f"slot{s.get('slotId')}={s.get('materialName') or 'vacío'}" for s in slots) if slots
            else "     IFS: sin estación")
    else:
        ok = data.get("message") == "Access code is different"
    checks.append(("La ruta /detail existe y autentica (no es 404)", ok,
                   "" if ok else f"llegó {data.get('message')!r}"))

    # 4) /product: existe y devuelve envelope (con credenciales malas, code 1).
    data = probe(module, args.host, args.serial, code, "/product")
    show("POST /product", data)
    ok = "code" in data
    checks.append(("La ruta /product existe y devuelve el envelope {code, message}", ok,
                   "" if ok else "respuesta sin 'code'"))

    # 5-7) Solo lectura de archivos, únicamente con credenciales válidas.
    if valid_session:
        listing = probe(module, args.host, args.serial, code, "/gcodeList")
        files = listing.get("gcodeList") or []
        print(f"  POST /gcodeList: code={listing.get('code')!r} · {len(files)} archivo(s)")
        for name in files[:8]:
            print(f"     - {name}")
        checks.append(("gcodeList devuelve la lista de archivos", listing.get("code") == 0,
                       "" if listing.get("code") == 0 else str(listing.get("message"))))

        if files:
            thumb = probe(module, args.host, args.serial, code, "/gcodeThumb",
                          {"fileName": files[0]})
            has_image = bool(thumb.get("imageData"))
            print(f"  POST /gcodeThumb: code={thumb.get('code')!r} · "
                  f"{len(thumb.get('imageData') or '')} bytes base64")
            checks.append(("gcodeThumb responde para un archivo guardado",
                           thumb.get("code") == 0 or thumb.get("message") == "thumbnail error",
                           "" if has_image else "el archivo no tiene miniatura"))

        client = module.FlashForgeClient(host=args.host, serial=args.serial, check_code=code)
        try:
            status, content_type, body = client._get_raw("/getThum")
            is_bmp = body[:2] == b"BM"
            print(f"  GET /getThum: HTTP {status} · {content_type or 'sin content-type'} · "
                  f"{len(body)} bytes" + (" (BMP)" if is_bmp else ""))
            checks.append(("GET /getThum responde (BMP o error JSON controlado)",
                           status == 200, "" if body else "cuerpo vacío"))
        except module.FlashForgeError as exc:
            print(f"  GET /getThum: error {exc}")
            checks.append(("GET /getThum responde", False, str(exc)))

    print()
    failures = 0
    for label, passed, detail in checks:
        print(f"  {'✓' if passed else '✗'} {label}" + (f"  ({detail})" if detail else ""))
        failures += 0 if passed else 1

    print()
    if failures:
        print(f"Resultado: {len(checks) - failures}/{len(checks)} comprobaciones OK")
        return 1

    if valid_session:
        print("Credenciales válidas: la API real responde datos de la impresora.")
    else:
        print(f"Plomería correcta. Falta el check code real (Printer ID) para leer datos:")
        print(f"  vuelve a ejecutar: python3 smoke_test.py --code <Printer ID de la pantalla>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
