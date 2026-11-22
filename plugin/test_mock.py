#!/usr/bin/env python3
"""Pruebas del esqueleto de FlashForge Monitor, fuera de OrcaSlicer.

Uso:
    python3 test_mock.py            # todas las pruebas
    python3 test_mock.py -v         # con detalle de cada paso

El script carga el archivo del plugin con ``importlib`` en dos modos:

1. Sin el módulo ``orca`` (como un import normal de Python) para probar
   ``FlashForgeClient`` en modo demo y sus validaciones.
2. Con un ``orca`` falso en ``sys.modules`` para probar la capability
   ``FlashForgePage``: ``get_ui()``, configuración, ``on_message()`` y el hilo
   de sondeo, sin necesidad de OrcaSlicer ni de la impresora.

No necesita dependencias externas.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import socketserver
import struct
import sys
import tempfile
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_DEFAULT_PLUGIN_PATH = (
    pathlib.Path(__file__).resolve().parent
    / "orca_plugins"
    / "flashforge_monitor"
    / "flashforge_monitor.py"
)
# Permite validar el paquete extraído del wheel:
#   FLASHFORGE_PLUGIN_PATH=/tmp/x/flashforge_monitor/__init__.py python3 test_mock.py
PLUGIN_PATH = pathlib.Path(
    os.environ.get("FLASHFORGE_PLUGIN_PATH", str(_DEFAULT_PLUGIN_PATH))
).resolve()

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

_failures: list[str] = []
_passes = 0


# ---------------------------------------------------------------------------
# Mini framework de pruebas
# ---------------------------------------------------------------------------


def check(name: str, fn) -> None:
    """Ejecuta una prueba; registra el fallo en vez de abortar."""
    global _passes
    try:
        fn()
    except AssertionError as exc:
        _failures.append(f"{name}: {exc}")
        print(f"  FALLO  {name}: {exc}")
    except Exception as exc:  # noqa: BLE001 - queremos ver cualquier error
        _failures.append(f"{name}: {type(exc).__name__}: {exc}")
        print(f"  ERROR  {name}: {type(exc).__name__}: {exc}")
    else:
        _passes += 1
        if VERBOSE:
            print(f"  ok     {name}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def wait_for(predicate, timeout: float = 6.0, interval: float = 0.05) -> bool:
    """Espera hasta que ``predicate()`` sea verdadero (o se agote el tiempo)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def wait_new_message(page, kind: str, since: int, timeout: float = 6.0) -> dict | None:
    """Espera un mensaje de ``kind`` que llegue después de los ``since`` previos."""
    found: dict | None = None

    def predicate() -> bool:
        nonlocal found
        items = page.messages(kind)
        if len(items) > since:
            found = items[-1]
            return True
        return False

    return found if wait_for(predicate, timeout) else None


def load_plugin(module_name: str):
    """Carga el archivo del plugin como módulo de Python."""
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"No se pudo cargar {PLUGIN_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Falso host `orca`
# ---------------------------------------------------------------------------


class FakeCapabilityBase:
    """Base mínima que imita lo que la página usa de una capability."""

    def __init__(self) -> None:
        self._stored_config = "{}"
        self.sent: list[dict] = []
        self._sent_event = threading.Event()

    # Puente con la página
    def post_message(self, data: dict) -> None:
        if not isinstance(data, dict):
            raise TypeError("post_message espera un dict")
        json.dumps(data)  # valida que sea JSON-compatible
        self.sent.append(data)
        self._sent_event.set()

    # Persistencia de config
    def get_config(self) -> str:
        return self._stored_config

    def save_config(self, value: str) -> bool:
        json.loads(value)  # valida el JSON, como el host real
        self._stored_config = value
        return True

    # Helpers del test
    def messages(self, kind: str) -> list[dict]:
        return [m for m in self.sent if m.get("type") == kind]

    def wait_message(self, kind: str, timeout: float = 6.0) -> dict | None:
        found: dict | None = None

        def predicate() -> bool:
            nonlocal found
            items = self.messages(kind)
            if items:
                found = items[-1]
                return True
            return False

        return found if wait_for(predicate, timeout) else None


def build_fake_orca() -> tuple[types.ModuleType, list]:
    """Crea un módulo `orca` en memoria con la API de plugins documentada."""
    registered: list = []

    pages = types.SimpleNamespace(PagesPluginCapabilityBase=FakeCapabilityBase)

    class FakePluginBase:
        """Equivalente a `orca.base`."""

    class FakeHostUi:
        """`orca.host.ui` (notificaciones del host)."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def push_notification(self, level: str, text: str,
                              hyper_text: str = "", on_click=None) -> None:
            self.calls.append((level, text))

    module = types.ModuleType("orca")
    module.base = FakePluginBase
    module.pages = pages
    module.plugin = lambda cls: cls  # decorador identidad
    module.register_capability = lambda cls: registered.append(cls)
    module.host = types.SimpleNamespace(ui=FakeHostUi())
    return module, registered


# ---------------------------------------------------------------------------
# Pruebas del cliente (sin host)
# ---------------------------------------------------------------------------


def test_client_without_orca() -> None:
    section("Cliente FlashForgeClient (sin orca)")

    module = load_plugin("flashforge_monitor_no_orca")
    expect(module.orca is None, "el módulo se cargó con orca=None")
    expect(module.FlashForgePage is None, "sin host no se define la capability")

    client = module.FlashForgeClient(mock=True)

    info = client.test_connection()
    expect(info.get("ok") is True and info.get("mock") is True, "test_connection en demo")

    status = client.get_status()
    expect(status["state"] == "printing", f"estado inicial: {status['state']!r}")
    expect(0.0 <= float(status["job"]["progress"]) <= 100.0, "progreso entre 0 y 100")
    expect(float(status["nozzle"]["target"]) > 0, "objetivo de boquilla > 0")
    expect(float(status["bed"]["current"]) >= 0, "temperatura de cama >= 0")
    json.dumps(status)  # JSON-compatible

    temps = client.get_temperatures()
    expect({"nozzle", "bed"} <= set(temps), "temperaturas de boquilla y cama")

    job = client.get_job_info()
    expect(job["job"]["name"], "el trabajo tiene nombre")
    expect(int(job["job"]["total_layers"]) > 0, "capas totales > 0")

    ifs = client.get_ifs_slots()
    expect(len(ifs["slots"]) == 4, "IFS expone 4 slots")
    expect(ifs["slots"][0]["material"] == "PLA", "slot 0 arranca en PLA")

    result = client.set_slot_material(3, "ABS", "#112233")
    expect(result.get("ok") is True, "set_slot_material devuelve ok")
    slots = client.get_ifs_slots()["slots"]
    expect(slots[3]["material"] == "ABS" and slots[3]["present"] is True,
           "el slot 3 quedó actualizado")
    expect(client.get_ifs_slots()["slots"][0]["material"] == "PLA",
           "editar un slot no toca los demás")

    client.send_control("pause")
    expect(client.get_job_info()["state"] == "paused", "pause detiene la impresión demo")
    client.send_control("resume")
    expect(client.get_job_info()["state"] == "printing", "resume la reanudan")
    client.send_control("cancel")
    expect(client.get_job_info()["state"] == "cancelled", "cancel deja el trabajo cancelado")
    expect(client.get_status()["job"]["progress"] == 0.0, "cancel reinicia el progreso")

    # --- módulo 1 en modo demo ---
    demo = module.FlashForgeClient(mock=True)
    demo.set_temperatures(nozzle=235, bed=70)
    snapshot = demo.get_status()
    expect(snapshot["nozzle"]["target"] == 235.0, "set_temperatures (mock) cambia el objetivo")
    expect(snapshot["bed"]["target"] == 70.0, "set_temperatures (mock) cambia la cama")
    demo.set_print_speed(130)
    demo.set_z_offset(-0.2)
    demo.set_fans(part=60, aux=15)
    demo.set_led(True)
    live = demo.get_status()
    expect(live["speed"] == 130 and live["z_offset"] == -0.2, "speed/z-offset en el estado")
    expect(live["fan_part"] == 60 and live["fan_aux"] == 15, "ventiladores en el estado")
    expect(live["light"] == "open", "LED en el estado")
    expect(demo.set_print_speed(0)["speed"] == module.SPEED_MIN, "velocidad acotada (mock)")

    # --- módulo 2 en modo demo ---
    demo_files = demo.list_files()
    expect(len(demo_files["files"]) == 3, "3 archivos en modo demo")
    expect(demo_files["files"][1]["use_station"] is True, "archivo IFS en demo")
    started = demo.start_file("Benchy_0.20mm_PLA.gx", leveling=True)
    expect(started["ok"] is True, "start_file (mock)")
    expect(demo.get_status()["job"]["name"] == "Benchy_0.20mm_PLA.gx",
           "start_file cambia el trabajo en demo")
    with tempfile.NamedTemporaryFile("wb", suffix=".gcode", delete=False) as tmp:
        tmp.write(b"G28\n")
        mock_upload_path = tmp.name
    try:
        uploaded = demo.upload_file(mock_upload_path, print_now=False)
        expect(uploaded["ok"] is True and uploaded["mock"] is True, "upload (mock)")
    finally:
        Path(mock_upload_path).unlink(missing_ok=True)

    # --- módulo 3 en modo demo ---
    expect(demo.home_axes()["ok"] is True, "home (mock)")
    expect(demo.move_axis("x", 10)["axis"] == "x", "move (mock)")
    expect(demo.extrude_filament(5)["delta"] == 5.0, "extrude (mock)")
    expect(demo.start_calibration()["ok"] is True, "calibration (mock)")
    expect(demo.set_auto_shutdown(45)["minutes"] == 45, "auto-apagado (mock)")
    demo._mock_error = "E0100"
    expect(demo.set_auto_shutdown(0)["minutes"] == 0, "auto-apagado off (mock)")
    try:
        module.FlashForgeClient(mock=True).clear_errors()
    except module.FlashForgeError as exc:
        expect(exc.kind == "invalid", "clear_errors sin error (mock)")

    # El progreso debe avanzar con el tiempo (modo demo "vivo").
    live = module.FlashForgeClient(mock=True)
    first = live.get_status()["job"]["progress"]
    time.sleep(0.6)
    second = live.get_status()["job"]["progress"]
    expect(second >= first, f"el progreso no retrocede ({first} -> {second})")

    # Validaciones
    for bad_command in ("", "explode", None):
        try:
            client.send_control(bad_command)  # type: ignore[arg-type]
        except module.FlashForgeError as exc:
            expect(exc.kind == "invalid", f"orden inválida reportada como {exc.kind!r}")
        else:
            raise AssertionError(f"send_control({bad_command!r}) debió fallar")

    try:
        client.set_slot_material(9, "PLA", "#fff")
    except module.FlashForgeError as exc:
        expect(exc.kind == "invalid", "slot fuera de rango reportado como invalid")
    else:
        raise AssertionError("set_slot_material(9, ...) debió fallar")

    # Modo real sin credenciales: error claro ANTES de abrir un socket.
    real = module.FlashForgeClient(host="192.168.1.50")
    expect(real.mock is False, "el modo real no es mock")
    expect(real.get_camera_url() == "http://192.168.1.50:8080/?action=stream",
           f"URL de cámara inesperada: {real.get_camera_url()}")
    try:
        real.get_status()
    except module.FlashForgeError as exc:
        expect(exc.kind == "auth-serial", f"sin SN debe ser auth-serial ({exc.kind!r})")
    else:
        raise AssertionError("get_status sin serial debía fallar limpiamente")

    try:
        module.FlashForgeClient(host="192.168.1.50", serial="SN123").get_status()
    except module.FlashForgeError as exc:
        expect(exc.kind == "auth-code", f"sin check code debe ser auth-code ({exc.kind!r})")
        expect("Printer ID" in str(exc), "el mensaje explica dónde está el Printer ID")
    else:
        raise AssertionError("get_status sin check code debía fallar limpiamente")

    # Colores: normalización a #RRGGBB mayúsculas (paleta del firmware).
    expect(module.normalize_color("#ff0000") == "#FF0000", "normalize_color sube a mayúsculas")
    expect(module.normalize_color("fef043") == "#FEF043", "normalize_color añade '#'")
    expect(module.normalize_color("", strict=False) == "", "color vacío tolerado al leer")
    try:
        module.normalize_color("rojo")
    except module.FlashForgeError as exc:
        expect(exc.kind == "invalid", "color inválido rechazado")
    else:
        raise AssertionError("normalize_color('rojo') debía fallar")
    expect(module._parse_pid("0026") == 38, "pid hex '0026' = 38 (AD5X)")
    expect(len(module.PALETTE) == 24, "la paleta tiene 24 colores")
    expect("#FFFFFF" in module._PALETTE_HEX, "la paleta incluye blanco")

    # _request sin host: error controlado, no un URLError crudo.
    try:
        real_no_host = module.FlashForgeClient(host="")
        real_no_host._request("/cualquiera")
    except module.FlashForgeError as exc:
        expect(exc.kind == "no-host", f"se esperaba no-host, llegó {exc.kind!r}")
    else:
        raise AssertionError("_request sin host debía fallar limpiamente")

    # Normalización de lo que escribe el usuario.
    expect(module.normalize_host(" http://192.168.1.50/ ") == "192.168.1.50", "normalize_host")
    expect(module.normalize_host("192.168.1.50:9000") == "192.168.1.50:9000",
           "normalize_host conserva el puerto")
    expect(module.split_host_port("192.168.1.50:9000") == ("192.168.1.50", 9000),
           "split_host_port separa el puerto")
    expect(module.clamp_interval(0) == module.MIN_INTERVAL, "intervalo mínimo")
    expect(module.clamp_interval(9999) == module.MAX_INTERVAL, "intervalo máximo")
    expect(module.clamp_interval("basura") == module.DEFAULT_INTERVAL,
           "intervalo inválido -> default")

    # Cadencia adaptativa (función pura).
    expect(module.next_delay("ready", 0, 5) == 5.0, "idle usa poll_interval")
    expect(module.next_delay("ready", 0, 1) == 1.0, "idle respeta el mínimo")
    expect(module.next_delay("ready", 0, 999) == module.MAX_INTERVAL,
           "idle respeta el máximo")
    for state in ("printing", "pausing", "pause", "canceling", "calibrate_doing",
                  "heating", "loading", "busy", "downloading"):
        expect(module.next_delay(state, 0, 30) == 2.0,
               f"{state}: 2 s durante actividad")
    for failures, expected in ((1, 5.0), (2, 10.0), (3, 20.0), (4, 40.0),
                               (5, 60.0), (9, 60.0)):
        expect(module.next_delay("ready", failures, 5) == expected,
               f"backoff {failures} → {expected}s")
    expect(module.next_delay("printing", 2, 5) == 10.0,
           "el backoff manda sobre el estado")
    expect(module.next_delay("ready", 0, 5) == 5.0, "reset al éxito vuelve al idle")

    # El HTML del dashboard debe ser autocontenido y usar la tematización del host.
    html = module._DASHBOARD_HTML
    for needle in ("--orca-bg", "--orca-fg", "FlashForge Monitor", "postMessage",
                   "onMessage", "IFS", "Configura tu impresora"):
        expect(needle in html, f"el dashboard no contiene {needle!r}")


# ---------------------------------------------------------------------------
# Impresora falsa por HTTP local (misma API que la AD5X)
# ---------------------------------------------------------------------------

FAKE_SERIAL = "SNAD5X000001"
FAKE_CODE = "3f9a0c1e"

_FAKE_DETAIL: dict = {
    "status": "printing",
    "printProgress": 0.423,
    "printLayer": 118,
    "targetPrintLayer": 280,
    "printDuration": 900,
    "estimatedTime": 3480,
    "printFileName": "AD5X_benchy_0.20mm_PLA_1h02m.gx",
    "rightTemp": 215.2,
    "rightTargetTemp": 220,
    "platTemp": 60.1,
    "platTargetTemp": 60,
    "errorCode": "",
    "pid": "0026",
    "name": "AD5X",
    "firmwareVersion": "v3.1.8",
    "cameraStreamUrl": "",
    "camera": 0,
    "hasMatlStation": True,
    "matlStationInfo": {
        "slotCnt": 4,
        "currentSlot": 1,
        "stateAction": 0,
        "slotInfos": [
            {"slotId": 1, "hasFilament": True, "materialName": "PLA", "materialColor": "#ff0000"},
            {"slotId": 2, "hasFilament": True, "materialName": "PETG", "materialColor": "#2750E0"},
            {"slotId": 3, "hasFilament": False, "materialName": "", "materialColor": ""},
            {"slotId": 4, "hasFilament": False, "materialName": "", "materialColor": ""},
        ],
    },
}


_FAKE_FILES = {
    "gcodeList": ["Benchy_0.20mm_PLA.gx", "Multicolor_llavero.3mf"],
    "gcodeListDetail": [
        {
            "gcodeFileName": "Benchy_0.20mm_PLA.gx",
            "printingTime": 3720,
            "totalFilamentWeight": 12.4,
            "useMatlStation": False,
            "gcodeToolCnt": 1,
            "gcodeToolDatas": [
                {"toolId": 0, "materialName": "PLA", "materialColor": "#F72224",
                 "filamentWeight": 12.4, "slotId": 0},
            ],
        },
        {
            "gcodeFileName": "Multicolor_llavero.3mf",
            "printingTime": 5400,
            "totalFilamentWeight": 24.8,
            "useMatlStation": True,
            "gcodeToolCnt": 2,
            "gcodeToolDatas": [
                {"toolId": 0, "materialName": "PLA", "materialColor": "#FFFFFF",
                 "filamentWeight": 12.0, "slotId": 1},
                {"toolId": 1, "materialName": "PETG", "materialColor": "#2750E0",
                 "filamentWeight": 12.8, "slotId": 2},
            ],
        },
    ],
}

#: PNG 1x1 transparente (base64) para simular /gcodeThumb.
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
_TINY_BMP = b"BM" + b"\x00" * 60


class FakePrinterHandler(BaseHTTPRequestHandler):
    """Imita lo medido en la AD5X: HTTP 200 siempre + envelope ``{code, message}``."""

    protocol_version = "HTTP/1.1"
    requests: list = []
    control_calls: list = []
    print_calls: list = []
    uploads: list = []
    camera_url = ""
    force_envelope: dict | None = None
    no_temp_effect = False
    bmp_thumb = b""
    targets = {"nozzle": 220.0, "bed": 60.0}
    # Secuencia de estados transitorios: [{"state": ..., "duration": s}, ...].
    state_sequence: list = []
    sequence_started = 0.0
    sequence_error = ""

    @classmethod
    def set_state_sequence(cls, entries: list, error: str = "") -> None:
        cls.state_sequence = entries
        cls.sequence_started = 0.0
        cls.sequence_error = error

    @classmethod
    def current_state(cls) -> str | None:
        if not cls.state_sequence:
            return None
        if not cls.sequence_started:
            cls.sequence_started = time.monotonic()
        elapsed = time.monotonic() - cls.sequence_started
        for entry in cls.state_sequence:
            duration = float(entry.get("duration") or 0.0)
            if elapsed < duration:
                return str(entry.get("state") or "busy")
            elapsed -= duration
        return str(cls.state_sequence[-1].get("state") or "busy")

    def log_message(self, *args):  # silencia el log del servidor
        pass

    def _send(self, payload: dict | None, status: int = 200,
              content_type: str = "application/json") -> None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _handle_upload(self, length: int) -> None:
        body = self.rfile.read(length) if length else b""
        headers = {key.lower(): value for key, value in self.headers.items()}
        type(self).uploads.append({"headers": headers, "body": body,
                                   "length": length})
        if (headers.get("serialnumber") != FAKE_SERIAL
                or headers.get("checkcode") != FAKE_CODE):
            return self._send({"code": 1, "message": "Access code is different"})
        return self._send({"code": 0, "message": "Success"})

    def do_POST(self):  # noqa: N802 - nombre impuesto por BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length") or 0)
        if self.path == "/uploadGcode":
            return self._handle_upload(length)

        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            return self._send(None)
        if not isinstance(body, dict):
            body = {}
        type(self).requests.append({"path": self.path, "body": body})

        if self.path == "/empty":
            return self._send(None)
        if type(self).force_envelope is not None:
            return self._send(type(self).force_envelope)
        routes = ("/checkCode", "/detail", "/control", "/gcodeList", "/gcodeThumb",
                  "/printGcode")
        if self.path not in routes:
            return self._send({"code": -1, "message": "Not found"}, status=404)

        # El firmware comprueba el serial y luego el check code (§3.5), con la
        # excepción documentada de /checkCode: un código válido pasa aunque el
        # serial no coincida (F4, confirmado en hardware).
        if self.path == "/checkCode":
            if body.get("checkCode") != FAKE_CODE:
                return self._send({"code": 1, "message": "Access code is different"})
            return self._send({"code": 0, "message": "Success"})

        if body.get("serialNumber") != FAKE_SERIAL:
            return self._send({"code": 1, "message": "SN is different"})
        if body.get("checkCode") != FAKE_CODE:
            return self._send({"code": 1, "message": "Access code is different"})

        if self.path == "/detail":
            detail = dict(_FAKE_DETAIL)
            sequence_state = type(self).current_state()
            if sequence_state:
                detail["status"] = sequence_state
                detail["errorCode"] = (type(self).sequence_error
                                       if sequence_state == "error" else "")
            detail["matlStationInfo"] = dict(_FAKE_DETAIL["matlStationInfo"])
            detail["rightTargetTemp"] = type(self).targets["nozzle"]
            detail["platTargetTemp"] = type(self).targets["bed"]
            if type(self).camera_url:
                detail["cameraStreamUrl"] = type(self).camera_url
                detail["camera"] = 1
            return self._send({"code": 0, "message": "Success", "detail": detail})
        if self.path == "/gcodeList":
            payload = dict(_FAKE_FILES)
            payload.update({"code": 0, "message": "Success"})
            return self._send(payload)
        if self.path == "/gcodeThumb":
            name = str(body.get("fileName") or "")
            if name == "Benchy_0.20mm_PLA.gx":
                return self._send({"code": 0, "message": "Success",
                                   "imageData": _TINY_PNG_B64})
            return self._send({"code": -1, "message": "thumbnail error"})
        if self.path == "/printGcode":
            type(self).print_calls.append(body)
            if not body.get("fileName"):
                return self._send({"code": -1, "message": "Parameters is error"})
            return self._send({"code": 0, "message": "Success"})

        payload = body.get("payload") or {}
        type(self).control_calls.append(payload)
        if payload.get("cmd") == "temperatureCtl_cmd" and not type(self).no_temp_effect:
            args = payload.get("args") or {}
            if "rightNozzle" in args:
                type(self).targets["nozzle"] = float(args["rightNozzle"])
            if "platform" in args:
                type(self).targets["bed"] = float(args["platform"])
        return self._send({"code": 0, "message": "Success"})

    def do_GET(self):  # noqa: N802
        if self.path == "/getThum" and type(self).bmp_thumb:
            body = type(self).bmp_thumb
            self.send_response(200)
            self.send_header("Content-Type", "image/bmp")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return self._send({"code": -1, "message": "thumbnail error"})


class FakeTcpHandler(socketserver.StreamRequestHandler):
    """Servidor TCP 8899 falso: responde 'ok' a cada comando ``~M…``."""

    #: Línea de endstops configurable para probar el parser de ~M119.
    m119_line = "Endstop: X-max: 110 Y-max: 110 Z-min: 0"

    def handle(self) -> None:
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("ascii", "replace").strip()
            if not line:
                return
            self.server.commands.append(line)  # type: ignore[attr-defined]
            command = line.lstrip("~")
            if command.startswith("M601"):
                self.wfile.write(b"CMD M601 Received.\nControl Success V2.1.\nok\n")
            elif command.startswith("M602"):
                self.wfile.write(b"CMD M602 Received.\nControl Release.\nok\n")
            elif command.startswith("M119"):
                self.wfile.write(
                    b"CMD M119 Received.\n" + type(self).m119_line.encode("ascii")
                    + b"\nMachineStatus: READY\nMoveMode: READY\nok\n"
                )
            else:
                self.wfile.write(b"CMD Received.\nok\n")


class FakeTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.commands: list[str] = []


def _reset_fake_printer() -> None:
    FakePrinterHandler.requests = []
    FakePrinterHandler.control_calls = []
    FakePrinterHandler.print_calls = []
    FakePrinterHandler.uploads = []
    FakePrinterHandler.camera_url = ""
    FakePrinterHandler.force_envelope = None
    FakePrinterHandler.no_temp_effect = False
    FakePrinterHandler.bmp_thumb = b""
    FakePrinterHandler.targets = {"nozzle": 220.0, "bed": 60.0}
    FakePrinterHandler.set_state_sequence([], "")
    FakeTcpHandler.m119_line = "Endstop: X-max: 110 Y-max: 110 Z-min: 0"


def test_client_real_api() -> None:
    section("Cliente contra la API real (impresora HTTP simulada)")

    module = load_plugin("flashforge_monitor_real_api")
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakePrinterHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _reset_fake_printer()

    def make_client(**kwargs):
        params = {"host": "127.0.0.1", "port": port, "serial": FAKE_SERIAL,
                  "check_code": FAKE_CODE, "timeout": 3.0}
        params.update(kwargs)
        return module.FlashForgeClient(**params)

    try:
        client = make_client()

        connection = client.test_connection()
        expect(connection["ok"] is True and connection["mock"] is False,
               "test_connection con credenciales buenas")
        expect(FakePrinterHandler.requests[-1]["path"] == "/checkCode",
               "test_connection usa POST /checkCode")

        # --- mapeo de /detail ---
        status = client.get_status()
        expect(status["state"] == "printing", f"estado: {status['state']!r}")
        expect(status["nozzle"] == {"current": 215.2, "target": 220.0},
               f"boquilla: {status['nozzle']!r}")
        expect(status["bed"] == {"current": 60.1, "target": 60.0}, f"cama: {status['bed']!r}")
        expect(status["job"]["name"].startswith("AD5X_benchy"), "nombre del trabajo")
        expect(status["job"]["progress"] == 42.3, "printProgress 0-1 → porcentaje")
        expect(status["job"]["remaining_min"] == 58, "estimatedTime (s) → minutos")
        expect(status["job"]["layer"] == 118 and status["job"]["total_layers"] == 280,
               "capas actual/total")
        expect(status["pid"] == 38, "pid hex '0026' → 38 (AD5X)")
        expect(status["firmware"] == "v3.1.8", "firmware")
        expect(status["camera_available"] is False, "sin cámara")
        expect(status["error_code"] == "", "sin error de firmware")

        temps = client.get_temperatures()
        expect(temps["nozzle"]["current"] == 215.2 and temps["mock"] is False,
               "get_temperatures mapea rightTemp")
        job = client.get_job_info()
        expect(job["state"] == "printing" and job["job"]["remaining_min"] == 58,
               "get_job_info")

        # --- IFS: slotId 1-4 del firmware → index 0-3 de la API del plugin ---
        ifs = client.get_ifs_slots()
        expect(len(ifs["slots"]) == 4, "4 slots")
        first = ifs["slots"][0]
        expect(first["index"] == 0 and first["slot_id"] == 1, "slot 0-based / slot_id 1-based")
        expect(first["material"] == "PLA" and first["color"] == "#FF0000",
               "material y color normalizado a mayúsculas")
        expect(ifs["slots"][1]["color"] == "#2750E0", "segundo slot")
        expect(ifs["slots"][2]["present"] is False, "slot vacío marcado")
        expect(ifs["active_slot"] == 1, "slot activo")

        # --- get_snapshot: una sola petición a /detail ---
        before = len([r for r in FakePrinterHandler.requests if r["path"] == "/detail"])
        snapshot = client.get_snapshot()
        after = len([r for r in FakePrinterHandler.requests if r["path"] == "/detail"])
        expect(after - before == 1, f"get_snapshot pidió {after - before} veces /detail")
        expect(snapshot["status"]["state"] == "printing", "snapshot trae estado")
        expect(snapshot["ifs"]["slots"][1]["material"] == "PETG", "snapshot trae IFS")
        expect(snapshot["mock"] is False, "snapshot real")

        # --- msConfig_cmd: slot 0-based → 1-based, rgb #RRGGBB de la paleta ---
        FakePrinterHandler.control_calls = []
        result = client.set_slot_material(0, "PETG", "#f72224")
        expect(result["ok"] is True and result["slot"]["slot_id"] == 1, "slot guardado")
        expect(result["in_palette"] is True, "color de la paleta de 24")
        call = FakePrinterHandler.control_calls[-1]
        expect(call["cmd"] == "msConfig_cmd", f"cmd: {call.get('cmd')!r}")
        expect(call["args"] == {"slot": 1, "mt": "PETG", "rgb": "#F72224"},
               f"args: {call['args']!r}")

        # Un color válido fuera de la paleta se envía igual, pero se avisa.
        off = client.set_slot_material(1, "PLA", "#FF0000")
        expect(off["in_palette"] is False, "#FF0000 no está en la paleta")
        expect(FakePrinterHandler.control_calls[-1]["args"]["rgb"] == "#FF0000",
               "el color fuera de paleta se envía en mayúsculas")

        # --- jobCtl_cmd: pause/resume/cancel ---
        expected = {"pause": "pause", "resume": "continue", "cancel": "cancel"}
        for command, action in expected.items():
            FakePrinterHandler.control_calls = []
            client.send_control(command)
            call = FakePrinterHandler.control_calls[-1]
            expect(call["cmd"] == "jobCtl_cmd", f"{command}: cmd")
            expect(call["args"] == {"jobID": "", "action": action},
                   f"{command}: {call['args']!r}")

        # --- módulo 1: printerCtl_cmd / lightControl_cmd ---
        FakePrinterHandler.control_calls = []
        client.set_print_speed(120)
        expect(FakePrinterHandler.control_calls[-1] ==
               {"cmd": "printerCtl_cmd", "args": {"speed": 120}}, "velocidad")
        client.set_z_offset(-0.35)
        expect(FakePrinterHandler.control_calls[-1]["args"] == {"zAxisCompensation": -0.35},
               "z-offset")
        client.set_fans(part=80, aux=25)
        expect(FakePrinterHandler.control_calls[-1]["args"] ==
               {"coolingFan": 80, "coolingLeftFan": 25}, "ventiladores")
        client.set_led(True)
        expect(FakePrinterHandler.control_calls[-1] ==
               {"cmd": "lightControl_cmd", "args": {"status": "open"}}, "LED on")
        client.set_led(False)
        expect(FakePrinterHandler.control_calls[-1]["args"] == {"status": "close"}, "LED off")

        # Clamps de seguridad.
        client.set_print_speed(9999)
        expect(FakePrinterHandler.control_calls[-1]["args"]["speed"] == 500, "velocidad acotada")
        client.set_z_offset(-99)
        expect(FakePrinterHandler.control_calls[-1]["args"]["zAxisCompensation"] == -5.0,
               "z-offset acotado")

        # --- temperatureCtl_cmd por HTTP (el /detail falso refleja el cambio) ---
        FakePrinterHandler.control_calls = []
        result = client.set_temperatures(nozzle=230, bed=65)
        expect(result["via"] == "http", f"temperaturas por HTTP ({result['via']})")
        expect(FakePrinterHandler.control_calls[-1] == {
            "cmd": "temperatureCtl_cmd",
            "args": {"rightNozzle": 230, "platform": 65},
        }, f"args temp: {FakePrinterHandler.control_calls[-1]}")
        expect(FakePrinterHandler.targets == {"nozzle": 230.0, "bed": 65.0},
               "la impresora falsa aplicó los objetivos")

        # --- fallback TCP 8899 si el HTTP no tiene efecto (§6.5) ---
        tcp_server = FakeTcpServer(("127.0.0.1", 0), FakeTcpHandler)
        tcp_port = tcp_server.server_address[1]
        threading.Thread(target=tcp_server.serve_forever, daemon=True).start()
        try:
            FakePrinterHandler.no_temp_effect = True
            tcp_client = make_client(tcp_port=tcp_port)
            result = tcp_client.set_temperatures(nozzle=240, bed=70)
            expect(result["via"] == "tcp", f"fallback TCP ({result['via']})")
            expect(tcp_server.commands == ["~M104 S240", "~M140 S70"],
                   f"comandos TCP: {tcp_server.commands}")
        finally:
            FakePrinterHandler.no_temp_effect = False
            tcp_server.shutdown()
            tcp_server.server_close()

        # --- ~M119: homing real (handshake + parser + degradación) ---
        tcp_server = FakeTcpServer(("127.0.0.1", 0), FakeTcpHandler)
        tcp_port = tcp_server.server_address[1]
        threading.Thread(target=tcp_server.serve_forever, daemon=True).start()
        try:
            endstop_client = make_client(tcp_port=tcp_port)
            endstops = endstop_client.get_endstops()
            expect(endstops["ok"] and endstops["homed"] is True,
                   f"M119 moderno: {endstops}")
            expect((endstops["x"], endstops["y"], endstops["z"]) == (True, True, True),
                   "M119 110/110/0 → X/Y/Z homed")
            expect(tcp_server.commands == ["~M601 S1", "~M119", "~M602"],
                   f"handshake M119: {tcp_server.commands}")

            FakeTcpHandler.m119_line = "Endstop: X-max: 0 Y-max: 0 Z-min: 1"
            legacy = endstop_client.get_endstops()
            expect((legacy["x"], legacy["y"], legacy["z"]) == (False, False, True),
                   f"M119 legacy 0/0/1: {legacy}")
            expect(legacy["homed"] is False, "legacy 0/0/1 no es homed completo")

            FakeTcpHandler.m119_line = "Endstop: X-max: 1 Y-max: 1 Z-min: 1"
            legacy_ok = endstop_client.get_endstops()
            expect(legacy_ok["homed"] is True, "legacy 1/1/1 es homed")
        finally:
            tcp_server.shutdown()
            tcp_server.server_close()

        dead = make_client(tcp_port=1)  # puerto cerrado → degradación silenciosa
        dead_result = dead.get_endstops(timeout=0.4)
        expect(dead_result["ok"] is False and dead_result["homed"] is None,
               f"TCP muerto → homed null ({dead_result.get('error')})")

        # --- ms_cmd load/unload (slot 0-based → 1-based) ---
        FakePrinterHandler.control_calls = []
        client.load_filament(0)
        expect(FakePrinterHandler.control_calls[-1] ==
               {"cmd": "ms_cmd", "args": {"slot": 1, "action": 0}},
               f"ms_cmd load: {FakePrinterHandler.control_calls[-1]}")
        client.unload_filament(2)
        expect(FakePrinterHandler.control_calls[-1] ==
               {"cmd": "ms_cmd", "args": {"slot": 3, "action": 1}},
               f"ms_cmd unload: {FakePrinterHandler.control_calls[-1]}")
        try:
            client.load_filament(4)
        except module.FlashForgeError as exc:
            expect(exc.kind == "invalid", "ms_cmd slot fuera de rango")
        else:
            raise AssertionError("load_filament(4) debía fallar")

        # --- secuencia de estados transitorios en /detail ---
        FakePrinterHandler.set_state_sequence([
            {"state": "calibrate_doing", "duration": 0.5},
            {"state": "ready", "duration": 0.0},
        ])
        snap = client.get_snapshot()
        expect(snap["status"]["state"] == "calibrate_doing",
               f"secuencia: calibrate_doing ({snap['status']['state']})")
        time.sleep(0.6)
        snap = client.get_snapshot()
        expect(snap["status"]["state"] == "ready",
               f"secuencia: termina en ready ({snap['status']['state']})")

        FakePrinterHandler.set_state_sequence([
            {"state": "calibrate_doing", "duration": 0.3},
            {"state": "error", "duration": 0.0},
        ], error="E0100")
        client.get_snapshot()
        time.sleep(0.35)
        snap = client.get_snapshot()
        expect(snap["status"]["state"] == "error", "secuencia: calibrate_doing → error")
        expect(snap["status"]["error_code"] == "E0100",
               f"secuencia: errorCode ({snap['status'].get('error_code')})")

        for transient in ("heating", "loading"):
            FakePrinterHandler.set_state_sequence([
                {"state": transient, "duration": 0.25},
                {"state": "ready", "duration": 0.0},
            ])
            snap = client.get_snapshot()
            expect(snap["status"]["state"] == transient,
                   f"secuencia: {transient}")
            time.sleep(0.3)
            snap = client.get_snapshot()
            expect(snap["status"]["state"] == "ready",
                   f"secuencia: {transient} → ready")
        FakePrinterHandler.set_state_sequence([], "")

        demo = module.FlashForgeClient(host="", mock=True)
        demo.unload_filament(0)
        expect(demo._mock_slots[0]["present"] is False, "mock unload marca el slot sin filamento")
        demo.load_filament(0)
        expect(demo._mock_slots[0]["present"] is True, "mock load vuelve a marcarlo presente")

        # --- módulo 2: gcodeList / gcodeThumb ---
        listing = client.list_files(with_thumbs=True)
        names = [entry["name"] for entry in listing["files"]]
        expect(names == ["Benchy_0.20mm_PLA.gx", "Multicolor_llavero.3mf"],
               f"lista de archivos: {names}")
        benchy = listing["files"][0]
        expect(benchy["printing_time_s"] == 3720 and benchy["filament_g"] == 12.4,
               "metadatos del archivo")
        expect(benchy["thumb"].startswith("data:image/png;base64,"), "miniatura PNG")
        expect(listing["files"][1]["use_station"] is True, "archivo IFS detectado")
        expect(listing["files"][1]["thumb"] == "", "sin miniatura: se deja vacía")
        # Ronda 3: mapping tool→slot también en la lista (para la UI).
        tools = listing["files"][1]["tools"]
        expect([tool["tool_id"] for tool in tools] == [0, 1], "tools 0-based en la lista")
        expect([tool["slot_id"] for tool in tools] == [1, 2], "slots 1-based en la lista")
        expect(tools[0]["color"] == "#FFFFFF", "color del tool en la lista")

        FakePrinterHandler.bmp_thumb = _TINY_BMP
        thumb = client.get_current_thumb()
        expect(thumb.startswith("data:image/bmp;base64,"), "getThum devuelve BMP")
        FakePrinterHandler.bmp_thumb = ""

        # --- /printGcode ---
        FakePrinterHandler.print_calls = []
        started = client.start_file("Benchy_0.20mm_PLA.gx", leveling=True)
        expect(started["ok"] is True and started["use_station"] is False, "start_file simple")
        payload = FakePrinterHandler.print_calls[-1]
        expect(payload["fileName"] == "Benchy_0.20mm_PLA.gx", "fileName")
        expect(payload["levelingBeforePrint"] is True, "levelingBeforePrint")
        expect(payload["useMatlStation"] is False, "sin estación para archivo simple")

        # Archivo multi-material: mapeo tool 0-based → slot 1-based.
        client.get_status()  # cachea los slots de /detail
        multi = client.start_file("Multicolor_llavero.3mf")
        expect(multi["use_station"] is True and multi["tool_count"] == 2, "archivo IFS")
        payload = FakePrinterHandler.print_calls[-1]
        mappings = payload["materialMappings"]
        expect([m["toolId"] for m in mappings] == [0, 1], "toolId 0-based")
        expect(len({m["slotId"] for m in mappings}) == 2, "slots distintos por tool")
        expect(all(1 <= m["slotId"] <= 4 for m in mappings), "slotId 1-based")
        expect(all(m["slotMaterialColor"].startswith("#") for m in mappings),
               "colores #RRGGBB")

        try:
            client.start_file("no_existe.gcode")
        except module.FlashForgeError as exc:
            expect(exc.kind == "file", f"archivo inexistente → {exc.kind!r}")
        else:
            raise AssertionError("un archivo inexistente debía fallar")

        # --- /uploadGcode: auth por cabeceras + multipart ---
        with tempfile.NamedTemporaryFile("wb", suffix=".gcode", delete=False) as tmp:
            tmp.write(b"; test gcode\nG28\n")
            upload_path = tmp.name
        try:
            FakePrinterHandler.uploads = []
            uploaded = client.upload_file(upload_path, print_now=False, leveling=True)
            expect(uploaded["ok"] is True, "upload ok")
            record = FakePrinterHandler.uploads[-1]
            headers = record["headers"]
            expect(headers.get("serialnumber") == FAKE_SERIAL, "serialNumber en cabecera")
            expect(headers.get("checkcode") == FAKE_CODE, "checkCode en cabecera")
            expect(headers.get("printnow") == "false", "printNow en cabecera")
            expect(headers.get("content-type", "").startswith("multipart/form-data; boundary="),
                   "Content-Type multipart")
            expect(b"; test gcode" in record["body"], "el cuerpo incluye el archivo")
            expect(b'name="gcodeFile"' in record["body"], "campo gcodeFile")

            bad_ext = Path(upload_path).with_suffix(".txt")
            bad_ext.write_bytes(b"x")
            try:
                client.upload_file(str(bad_ext))
            except module.FlashForgeError as exc:
                expect(exc.kind == "invalid", f"extensión inválida → {exc.kind!r}")
            else:
                raise AssertionError("una extensión .txt debía rechazarse")
            finally:
                bad_ext.unlink(missing_ok=True)

            try:
                client.upload_file(str(Path(upload_path).with_suffix(".gcode")) + ".nope")
            except module.FlashForgeError as exc:
                expect(exc.kind == "file", f"archivo inexistente → {exc.kind!r}")
            else:
                raise AssertionError("un archivo inexistente debía rechazarse")
        finally:
            Path(upload_path).unlink(missing_ok=True)

        # --- módulo 3: movimiento y mantenimiento ---
        FakePrinterHandler.control_calls = []
        client.home_axes()
        expect(FakePrinterHandler.control_calls[-1] == {"cmd": "homingCtrl_cmd"},
               "homing")
        client.move_axis("z", 10.5)
        expect(FakePrinterHandler.control_calls[-1] == {
            "cmd": "moveCtrl_cmd", "args": {"axis": "z", "delta": 10.5},
        }, "mover Z")
        client.extrude_filament(-5)
        expect(FakePrinterHandler.control_calls[-1] == {
            "cmd": "extrudeCtrl_cmd", "args": {"axis": "e", "delta": -5.0},
        }, "retraer")
        client.start_calibration(leveling=True, vibration=True)
        expect(FakePrinterHandler.control_calls[-1] == {
            "cmd": "calibration_cmd",
            "args": {"levelingDetection": "open", "vibrationCompensation": "open"},
        }, "calibración")
        client.set_auto_shutdown(30)
        expect(FakePrinterHandler.control_calls[-1] == {
            "cmd": "delayClose_cmd",
            "args": {"automaticShutdown": "open", "shutdownAfterTime": 30},
        }, "auto-apagado")
        client.set_auto_shutdown(0)
        expect(FakePrinterHandler.control_calls[-1]["args"]["automaticShutdown"] == "close",
               "auto-apagado off")

        # clear_errors usa el errorCode cacheado de /detail.
        FakePrinterHandler.targets = {"nozzle": 220.0, "bed": 60.0}
        try:
            client.clear_errors()
        except module.FlashForgeError as exc:
            expect(exc.kind == "invalid", f"sin error activo → {exc.kind!r}")
        else:
            raise AssertionError("clear_errors sin error activo debía fallar")
        client.clear_errors("E0100")
        expect(FakePrinterHandler.control_calls[-1] == {
            "cmd": "errorCodeCtrl_cmd",
            "args": {"action": "clearErrorCode", "errorCode": "E0100"},
        }, "limpiar error")

        # Validaciones de movimiento.
        try:
            client.move_axis("w", 10)
        except module.FlashForgeError as exc:
            expect(exc.kind == "invalid", "eje inválido")
        else:
            raise AssertionError("un eje 'w' debía rechazarse")

        # --- cámara anunciada por /detail ---
        FakePrinterHandler.camera_url = "http://127.0.0.1:8080/?action=stream"
        cam_client = make_client()
        cam_status = cam_client.get_status()
        expect(cam_status["camera_available"] is True, "cámara detectada por /detail")
        expect(cam_client.get_camera_url() == FakePrinterHandler.camera_url,
               "get_camera_url usa cameraStreamUrl")
        FakePrinterHandler.camera_url = ""

        # --- diagnósticos de credenciales (mensajes exactos del firmware) ---
        # F4 (confirmado en hardware): /checkCode acepta un check code válido
        # aunque el serial no coincida. El serial lo valida /detail.
        bad_serial = make_client(serial="SNFALSO000000")
        expect(bad_serial.test_connection()["ok"] is True,
               "/checkCode no valida el serial (comportamiento F4)")
        try:
            bad_serial.get_status()
        except module.FlashForgeError as exc:
            expect(exc.kind == "auth-serial", f"serial malo en /detail → {exc.kind!r}")
            expect(exc.raw_message == "SN is different", "mensaje crudo del firmware")
            expect("Serial incorrecto" in str(exc), "diagnóstico en español")
        else:
            raise AssertionError("un serial incorrecto debía fallar en /detail")

        bad_code = make_client(check_code="88888888")
        try:
            bad_code.test_connection()
        except module.FlashForgeError as exc:
            expect(exc.kind == "auth-code", f"check code malo → {exc.kind!r}")
            expect(exc.raw_message == "Access code is different", "mensaje crudo")
            expect("Check code incorrecto" in str(exc), "diagnóstico en español")
        else:
            raise AssertionError("un check code incorrecto debía fallar")

        # --- LAN-only apagado ---
        FakePrinterHandler.force_envelope = {"code": -2, "message": "Lan mode error"}
        try:
            client.test_connection()
        except module.FlashForgeError as exc:
            expect(exc.kind == "lan-mode", f"LAN mode → {exc.kind!r}")
            expect("LAN-only" in str(exc), "el mensaje menciona LAN-only")
        else:
            raise AssertionError("code -2 debía traducirse a lan-mode")
        finally:
            FakePrinterHandler.force_envelope = None

        # --- cuerpo vacío → error de parámetros, no excepción rara ---
        try:
            client._call("/empty")
        except module.FlashForgeError as exc:
            expect(exc.kind == "parameters", f"cuerpo vacío → {exc.kind!r}")
        else:
            raise AssertionError("un cuerpo vacío debía fallar como parameters")

        # --- ruta inexistente → HTTP 404 controlado ---
        try:
            client._call("/noExiste")
        except module.FlashForgeError as exc:
            expect(exc.kind == "http", f"404 → {exc.kind!r}")
        else:
            raise AssertionError("una ruta inexistente debía dar HTTP 404")

        # --- impresora apagada / puerto cerrado → error de red legible ---
        dead = module.FlashForgeClient(host="127.0.0.1", port=1, serial=FAKE_SERIAL,
                                       check_code=FAKE_CODE, timeout=1.0)
        try:
            dead.test_connection()
        except module.FlashForgeError as exc:
            expect(exc.kind in ("network", "timeout", "http"), f"red → {exc.kind!r}")
        else:
            raise AssertionError("un puerto cerrado debía fallar")
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Descubrimiento UDP (servidor local)
# ---------------------------------------------------------------------------


def _make_discovery_packet(name: str, pid: int, serial: str,
                           http_port: int = 8898) -> bytes:
    """Paquete de respuesta de 276 B con los offsets de §9.2."""
    data = bytearray(276)
    name_bytes = name.encode("utf-8")[:120]
    data[0:len(name_bytes)] = name_bytes
    struct.pack_into(">H", data, 0x84, 8899)
    struct.pack_into(">H", data, 0x86, 0x2B71)
    struct.pack_into(">H", data, 0x88, pid)
    struct.pack_into(">H", data, 0x8A, 0)
    struct.pack_into(">H", data, 0x8C, 0x5A02)
    struct.pack_into(">H", data, 0x8E, http_port)
    data[0x90] = 0
    data[0x91] = 0
    serial_bytes = serial.encode("utf-8")[:120]
    data[0x92:0x92 + len(serial_bytes)] = serial_bytes
    return bytes(data)


class FakeDiscoveryHandler(socketserver.BaseRequestHandler):
    """Responde al broadcast del plugin con un anuncio de AD5X."""

    def handle(self) -> None:
        _data, sock = self.request
        sock.sendto(_make_discovery_packet("AD5X", 0x0026, FAKE_SERIAL),
                    self.client_address)


def test_discovery_udp() -> None:
    section("Descubrimiento UDP (servidor local)")

    module = load_plugin("flashforge_monitor_udp")
    server = socketserver.UDPServer(("127.0.0.1", 0), FakeDiscoveryHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        found = module.discover_printers(timeout=1.5, targets=[("127.0.0.1", port)])
        expect(len(found) == 1, f"respuestas: {found}")
        entry = found[0]
        expect(entry["ip"] == "127.0.0.1", "IP de origen")
        expect(entry["serial"] == FAKE_SERIAL, "serial del anuncio")
        expect(entry["pid"] == 0x0026 and entry["model"] == "AD5X", "PID/modelo")
        expect(entry["http_port"] == 8898, "puerto HTTP del anuncio")
        expect(entry["name"] == "AD5X", "nombre del anuncio")

        # Tolerancia a paquetes cortos y a otros PIDs.
        expect(module.parse_discovery_packet(b"corto", "1.2.3.4") is None,
               "paquete corto ignorado")
        expect(module._pid_model(0x0023) == "Adventurer 5M", "PID 5M")
        expect(module._pid_model(0x9999).startswith("PID 0x"), "PID desconocido")
    finally:
        server.shutdown()
        server.server_close()

    # Permiso denegado (audit hook / sandbox) → error claro y accionable.
    import socket as socket_module

    original_socket = socket_module.socket

    class DeniedSocket:
        def __init__(self, *args, **kwargs):
            raise PermissionError("bloqueado por el host")

    socket_module.socket = DeniedSocket  # type: ignore[assignment]
    try:
        module.discover_printers(timeout=0.2, targets=[("127.0.0.1", 9)])
    except module.FlashForgeError as exc:
        expect(exc.kind == "permission", f"tipo: {exc.kind!r}")
        expect("manualmente" in str(exc), "el mensaje ofrece el flujo manual")
    else:
        raise AssertionError("un socket denegado debía lanzar FlashForgeError")
    finally:
        socket_module.socket = original_socket  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Pruebas de la capability con un host falso
# ---------------------------------------------------------------------------


def test_page_with_fake_orca() -> None:
    section("Capability FlashForgePage (host simulado)")

    fake_orca, registered = build_fake_orca()
    sys.modules["orca"] = fake_orca
    try:
        module = load_plugin("flashforge_monitor_with_orca")
    finally:
        sys.modules.pop("orca", None)

    expect(module.orca is fake_orca, "el plugin ve el orca inyectado")
    page_cls = module.FlashForgePage
    plugin_cls = module.FlashForgeMonitorPlugin
    expect(page_cls is not None and plugin_cls is not None, "se definen las clases del plugin")

    # La clase paquete debe ser instanciable sin argumentos y registrar la pestaña.
    plugin = plugin_cls()
    plugin.register_capabilities()
    expect(registered == [page_cls], f"register_capabilities registró {registered!r}")

    page = page_cls()

    name = page.get_name()
    expect(name == "FlashForge", f"get_name() = {name!r}")
    expect(";" not in name, "get_name() no contiene ';'")

    ui = page.get_ui()
    expect(ui.startswith("<!DOCTYPE html>"), "get_ui() devuelve un documento HTML")
    expect(module.PLUGIN_VERSION in ui, "el pie muestra la versión del plugin")

    # Config por defecto: schema v2, mock activado, sin impresoras.
    default_cfg = json.loads(page.get_config())
    expect(default_cfg["printers"] == [] and default_cfg["mock"] is True,
           f"config por defecto inesperada: {default_cfg}")
    expect(default_cfg["poll_interval"] == module.DEFAULT_INTERVAL, "intervalo por defecto")
    expect(default_cfg["notify_on_finish"] is True and default_cfg["notify_on_error"] is True,
           "notificaciones activadas por defecto")
    expect(page.get_config_version() == module.CONFIG_VERSION == "2", "versión de config v2")

    # --- Migración v1 → v2 sin perder credenciales ---
    page._stored_config = json.dumps({
        "host": "192.168.1.50", "serial": "SNAD5X000001",
        "check_code": "a1b2c3d4", "interval": 3, "mock": False,
    })
    migrated = json.loads(page.get_config())
    expect(len(migrated["printers"]) == 1, "la migración creó una impresora")
    expect(migrated["printers"][0]["ip"] == "192.168.1.50", "IP migrada")
    expect(migrated["printers"][0]["serial"] == "SNAD5X000001", "serial migrado")
    expect(migrated["printers"][0]["check_code"] == "a1b2c3d4",
           "check code migrado (no se pierde el Printer ID)")
    expect(migrated["poll_interval"] == 3 and migrated["mock"] is False,
           "intervalo y mock migrados")
    persisted = json.loads(page._stored_config)
    expect("printers" in persisted, "la migración se persiste en el host")
    expect(persisted["printers"][0]["check_code"] == "a1b2c3d4", "persistida con el código")

    # Restaura un config limpio para el resto de la prueba.
    page._stored_config = "{}"
    expect(json.loads(page.get_config())["printers"] == [], "config reiniciada")

    # La UI de config existe y usa el puente getConfig/saveConfig.
    expect(page.has_config_ui() is True, "has_config_ui()")
    config_ui = page.get_config_ui()
    for needle in ("getConfig", "saveConfig"):
        expect(needle in config_ui, f"la UI de config no usa {needle}")

    # Flujo real: la página avisa que está lista.
    page.on_message({"type": "ready"})

    cfg_msg = page.wait_message("config", timeout=3.0)
    expect(cfg_msg is not None, "responde con la config al 'ready'")
    expect(cfg_msg["config"]["mock"] is True, "la config enviada es la de demo")

    status = page.wait_message("status", timeout=6.0)
    expect(status is not None, "el hilo de sondeo envía 'status'")
    assert status is not None
    expect(status["mock"] is True, "el status llega en modo demo")
    expect(status["state"] in ("printing", "paused"), f"estado inesperado {status['state']!r}")
    expect("nozzle" in status and "bed" in status and "job" in status,
           "el status trae boquilla, cama y trabajo")
    expect(status["job"]["name"], "el status trae nombre de trabajo")
    expect("job_thumb" in status, "el status incluye la miniatura del trabajo")
    expect(isinstance(status.get("job_tools"), list), "el status incluye job_tools (UI AMS)")

    ifs = page.wait_message("ifs", timeout=6.0)
    expect(ifs is not None, "el hilo de sondeo envía 'ifs'")
    assert ifs is not None
    expect(len(ifs["slots"]) == 4, "se envían 4 slots IFS")

    # Homing real: en modo demo ~M119 responde homed sin abrir sockets.
    homing = page.wait_message("homing", timeout=6.0)
    expect(homing is not None and homing.get("homed") is True,
           f"el worker envía 'homing' ({homing})")
    expect(homing.get("source") == "mock", "homing de demo marcado como mock")

    # Guardar config v2 desde la página (drawer multi-impresora).
    page.on_message({
        "type": "save-config",
        "config": {
            "printers": [
                {"name": "AD5X taller", "ip": "192.168.1.50",
                 "serial": "SNAD5X000001", "check_code": "a1b2c3d4"},
                {"name": "Sala", "ip": "192.168.8.31", "serial": "SNOTRA1234567"},
            ],
            "active_printer": 0,
            "poll_interval": 2,
            "mock": True,
            "notify_on_finish": False,
            "notify_on_error": True,
        },
    })
    saved = page.wait_message("config-saved", timeout=3.0)
    expect(saved is not None and saved["ok"] is True, "save-config confirma el guardado")
    stored = json.loads(page.get_config())
    expect(len(stored["printers"]) == 2, f"se guardaron {len(stored['printers'])} impresoras")
    expect(stored["printers"][0]["name"] == "AD5X taller", "nombre guardado")
    expect(stored["poll_interval"] == 2, f"intervalo guardado: {stored['poll_interval']!r}")
    expect(stored["notify_on_finish"] is False, "flag de notificación guardado")
    expect(stored["printers"][0]["check_code"] == "a1b2c3d4", "check code guardado")
    expect("check_code" not in saved["config"]["printers"][0],
           "el check code no se reenvía a la página")

    # Editar sin reenviar el código: se conserva el guardado (por IP/serial).
    page.on_message({"type": "save-config", "config": {
        "printers": [
            {"name": "AD5X renombrada", "ip": "192.168.1.50", "serial": "SNAD5X000001"},
            {"name": "Sala", "ip": "192.168.8.31", "serial": "SNOTRA1234567"},
        ],
        "active_printer": 1,
        "poll_interval": 5,
        "mock": True,
        "notify_on_finish": True,
        "notify_on_error": True,
    }})
    stored = json.loads(page.get_config())
    expect(stored["printers"][0]["check_code"] == "a1b2c3d4",
           "el check code se conserva al editar nombre/IP")
    expect(stored["active_printer"] == 1, "impresora activa guardada")

    # Compatibilidad con el payload v1 (formulario antiguo) → printers[0].
    page.on_message({"type": "save-config", "config": {
        "host": "192.168.1.77", "serial": "SN123", "check_code": "abcd1234",
        "interval": 4, "mock": True}})
    stored = json.loads(page.get_config())
    expect(stored["printers"][0]["ip"] == "192.168.1.77", "guardado v1 compatible")
    expect(stored["printers"][0]["check_code"] == "abcd1234", "código v1 conservado")
    expect(stored["poll_interval"] == 4, "intervalo v1 migrado")

    # Basura: no debe romper nada ni aceptarse.
    expect(page.save_config("esto no es json") is False, "save_config rechaza JSON inválido")
    page.on_message("tampoco es json")  # no debe lanzar
    page.on_message({"type": "desconocido"})  # tampoco

    # La config se sanea: IP con esquema, intervalo fuera de rango.
    page.on_message({"type": "save-config", "config": {
        "printers": [{"name": "x", "ip": "http://10.0.0.9/", "serial": "S1"}],
        "active_printer": 0, "poll_interval": 9999, "mock": True}})
    stored = json.loads(page.get_config())
    expect(stored["printers"][0]["ip"] == "10.0.0.9",
           f"IP saneada: {stored['printers'][0]['ip']!r}")
    expect(stored["poll_interval"] == module.MAX_INTERVAL,
           f"intervalo saneado: {stored['poll_interval']}")

    # Cambio de impresora activa desde el dropdown del topbar.
    before_active = len(page.messages("active-printer"))
    page.on_message({"type": "set-active-printer", "index": 0})
    ack = wait_new_message(page, "active-printer", before_active, timeout=5.0)
    expect(ack is not None and ack.get("ok") is True, "cambio de impresora acusado")
    expect(json.loads(page.get_config())["active_printer"] == 0, "índice activo guardado")

    # Controles: se encolan y el worker responde con acuse.
    page.on_message({"type": "control", "action": "pause"})
    ack = page.wait_message("control-ack", timeout=6.0)
    expect(ack is not None and ack["ok"] is True, "control 'pause' acusado")
    expect(ack["action"] == "pause", "el acuse identifica la orden")

    acks_before = len(page.messages("control-ack"))
    page.on_message({"type": "control", "action": "hackear"})
    bad_ack = wait_new_message(page, "control-ack", acks_before, timeout=2.0)
    expect(bad_ack is not None and bad_ack["ok"] is False, "orden inválida rechazada")

    # Edición de un slot IFS.
    ifs_before = len(page.messages("ifs"))
    page.on_message({"type": "set-slot", "slot": 2, "material": "TPU", "color": "#00ff88"})
    slot_ack = page.wait_message("slot-saved", timeout=6.0)
    expect(slot_ack is not None and slot_ack["ok"] is True, "slot guardado")
    latest_ifs = wait_new_message(page, "ifs", ifs_before, timeout=6.0)
    expect(latest_ifs is not None, "llega un nuevo estado de IFS tras editar el slot")
    assert latest_ifs is not None
    materials = [s["material"] for s in latest_ifs["slots"]]
    expect("TPU" in materials, f"el slot editado aparece en el sondeo: {materials}")

    # --- módulos 1–3: mensajes nuevos de la página ---
    actions = [
        ({"type": "set-temps", "nozzle": 235}, "set-temps"),
        ({"type": "set-speed", "percent": 110}, "set-speed"),
        ({"type": "set-zoffset", "offset": -0.1}, "set-zoffset"),
        ({"type": "set-fan", "part": 50}, "set-fan"),
        ({"type": "set-led", "on": True}, "set-led"),
        ({"type": "home"}, "home"),
        ({"type": "move", "axis": "y", "delta": 5}, "move"),
        ({"type": "extrude", "delta": -2}, "extrude"),
        ({"type": "calibrate", "leveling": True}, "calibrate"),
        ({"type": "set-shutdown", "minutes": 20}, "set-shutdown"),
        ({"type": "ms-load", "slot": 0}, "ms-load"),
        ({"type": "ms-unload", "slot": 0}, "ms-unload"),
    ]
    for message, command in actions:
        before = len(page.messages("command-ack"))
        page.on_message(message)
        ack = wait_new_message(page, "command-ack", before, timeout=8.0)
        expect(ack is not None, f"{command}: sin acuse")
        assert ack is not None
        expect(ack.get("ok") is True, f"{command}: {ack.get('message')!r}")
        expect(ack.get("command") == command, f"{command}: comando {ack.get('command')!r}")

    # Payload inválido: acuse de error, sin excepción.
    before = len(page.messages("command-ack"))
    page.on_message({"type": "move", "axis": "w", "delta": 5})
    ack = wait_new_message(page, "command-ack", before, timeout=3.0)
    expect(ack is not None and ack.get("ok") is False, "eje inválido rechazado")

    before = len(page.messages("command-ack"))
    page.on_message({"type": "set-temps"})
    ack = wait_new_message(page, "command-ack", before, timeout=3.0)
    expect(ack is not None and ack.get("ok") is False, "set-temps vacío rechazado")

    # Archivos y subida (mock).
    before = len(page.messages("files"))
    page.on_message({"type": "list-files"})
    listing = wait_new_message(page, "files", before, timeout=8.0)
    expect(listing is not None and listing.get("ok") is True, "list-files responde")
    assert listing is not None
    expect(len(listing["files"]) == 3, "list-files trae los archivos mock")

    with tempfile.NamedTemporaryFile("wb", suffix=".gcode", delete=False) as tmp:
        tmp.write(b"G28\n")
        page_upload_path = tmp.name
    try:
        before = len(page.messages("upload"))
        page.on_message({"type": "upload", "path": page_upload_path})
        uploaded = wait_new_message(page, "upload", before, timeout=10.0)
        expect(uploaded is not None and uploaded.get("ok") is True, "upload responde")
    finally:
        Path(page_upload_path).unlink(missing_ok=True)

    # Ciclo de vida: on_unload detiene el hilo.
    page.on_unload()
    expect(page._worker is None, "on_unload detiene el hilo de sondeo")
    before = len(page.sent)
    time.sleep(0.8)
    expect(len(page.sent) == before, "tras on_unload no llegan más mensajes")

    # --- notificaciones (worker detenido: secuencia controlada) ---
    page._cfg = {"notify_on_finish": True, "notify_on_error": True}
    page._prev_state = ""
    page._prev_error = ""
    page._maybe_notify({"state": "printing", "job": {"name": "Pieza.gx"}, "error_code": ""})
    expect(not page.messages("notification"), "el primer estado no notifica")

    page._maybe_notify({"state": "ready", "job": {"name": "Pieza.gx"}, "error_code": ""})
    notes = page.messages("notification")
    expect(len(notes) == 1, f"aviso de fin: {notes}")
    expect("Terminó: Pieza.gx" in notes[-1]["message"], "texto de fin correcto")
    expect(notes[-1]["level"] == "info", "nivel info")
    host_calls = fake_orca.host.ui.calls
    expect(host_calls and "Terminó" in host_calls[-1][1],
           f"push_notification del host: {host_calls}")

    page._maybe_notify({"state": "ready", "job": {"name": "Pieza.gx"}, "error_code": "E0100"})
    expect(len(page.messages("notification")) == 2, "aviso de error nuevo")
    expect(page.messages("notification")[-1]["level"] == "warning", "nivel warning")
    page._maybe_notify({"state": "ready", "job": {"name": "Pieza.gx"}, "error_code": "E0100"})
    expect(len(page.messages("notification")) == 2, "el mismo error no se repite")

    # --- transitorios: normalización y micro-refresco ---
    expect(module.normalize_state("weird_state") == "busy",
           "estado desconocido → busy")
    expect(module.normalize_state("calibrate_doing") == "calibrate_doing",
           "estado conocido intacto")
    page._interval = 5.0
    page._current_state = "ready"
    expect(page._poll_delay() == 5.0, f"cadencia normal ({page._poll_delay()})")
    page._current_state = "calibrate_doing"
    expect(page._poll_delay() == 2.0, f"cadencia transitoria ({page._poll_delay()})")
    page._current_state = "loading"
    expect(page._poll_delay() == 2.0, "loading también a 2 s")
    page._current_state = "ready"
    expect(page._poll_delay() == 5.0, "cadencia restaurada")

    # --- notificaciones de calibración/filamento (siempre activas) ---
    page._cfg = {"notify_on_finish": False, "notify_on_error": False}
    page._prev_error = ""

    def transient_note(previous, current, *, calibration="", filament="",
                        error="", label=""):
        page._prev_state = previous
        page._prev_error = ""
        page._last_calibration = calibration
        page._last_filament_action = filament
        before = len(page.messages("notification"))
        page._maybe_notify({"state": current, "job": {"name": ""},
                            "error_code": error})
        notes = page.messages("notification")
        expect(len(notes) > before, f"{label}: sin notificación")
        return notes[-1]

    note = transient_note("calibrate_doing", "ready", calibration="level",
                          label="nivelado")
    expect("Nivelado completado" in note["message"] and note["level"] == "info",
           f"notificación nivelado: {note}")
    note = transient_note("calibrate_doing", "ready", calibration="vibr",
                          label="input shaper")
    expect("Input shaper completado" in note["message"], "notificación input shaper")
    note = transient_note("calibrate_doing", "ready", label="calibración externa")
    expect("Calibración completada" in note["message"], "calibración sin disparador local")

    before = len(page.messages("notification"))
    page._prev_state = "calibrate_doing"
    page._prev_error = ""
    page._last_calibration = "level"
    page._maybe_notify({"state": "error", "job": {"name": ""},
                        "error_code": "E0100"})
    notes = page.messages("notification")[before:]
    expect(len(notes) == 1 and notes[0]["level"] == "warning"
           and "Calibración terminó con error" in notes[0]["message"]
           and "E0100" in notes[0]["message"],
           f"error de calibración sin duplicar: {notes}")

    note = transient_note("heating", "ready", label="calentamiento")
    expect(note["message"] == "Temperatura lista", f"temperatura: {note}")

    note = transient_note("loading", "ready", filament="load", label="carga")
    expect("Filamento cargado" in note["message"], f"carga: {note}")
    note = transient_note("loading", "ready", filament="unload", label="descarga")
    expect("Filamento descargado" in note["message"], f"descarga: {note}")
    note = transient_note("loading", "ready", label="carga externa")
    expect("Carga/descarga completada" in note["message"], "carga sin disparador local")

    page._cfg = {"notify_on_finish": False, "notify_on_error": False}
    page._prev_state = "printing"
    page._prev_error = ""
    before_flags = len(page.messages("notification"))
    page._maybe_notify({"state": "ready", "job": {"name": "Otra.gx"}, "error_code": ""})
    page._maybe_notify({"state": "ready", "job": {"name": "Otra.gx"}, "error_code": "E0101"})
    expect(len(page.messages("notification")) == before_flags,
           "los flags notify_* desactivan los avisos de trabajo")

    # --- intención local: éxito, error, timeout y M119 ---
    intent_page = page_cls()
    intent_page._ensure_state()

    for kind, slot, expected in (
        ("leveling", None, "Nivelado completado"),
        ("input_shaper", None, "Input shaper completado"),
        ("load", 2, "Filamento cargado (slot 2)"),
        ("unload", 3, "Filamento descargado (slot 3)"),
        ("homing", None, "Homing completado"),
    ):
        intent_page._set_intent(kind, slot)
        sent_intent = intent_page.messages("intent")[-1]
        expect(sent_intent["intent"]["kind"] == kind, f"intent emitido: {kind}")
        intent_page._local_intent["started"] -= 3.0
        before = len(intent_page.messages("notification"))
        intent_page._update_intent_from_status("ready", "")
        notes = intent_page.messages("notification")
        expect(len(notes) == before + 1 and expected in notes[-1]["message"],
               f"cierre de {kind}: {notes[-1] if len(notes) > before else None}")
        expect(intent_page._local_intent is None, f"{kind}: intención limpiada")

    intent_page._set_intent("leveling")
    before = len(intent_page.messages("notification"))
    intent_page._update_intent_from_status("error", "E0100")
    notes = intent_page.messages("notification")[before:]
    expect(len(notes) == 1 and notes[0]["level"] == "warning"
           and "Calibración terminó con error" in notes[0]["message"]
           and "E0100" in notes[0]["message"],
           f"intención con error: {notes}")

    intent_page._set_intent("leveling")
    intent_page._local_intent["started"] -= 13 * 60
    before = len(intent_page.messages("notification"))
    intent_page._maybe_check_homing(types.SimpleNamespace(get_endstops=lambda: {"ok": False}))
    notes = intent_page.messages("notification")[before:]
    expect(len(notes) == 1 and "No se confirmó el fin" in notes[0]["message"],
           f"timeout de intención: {notes}")
    expect(intent_page._local_intent is None, "timeout limpia la intención")

    # M119 confirma actividad y luego el fin (antes que /detail).
    intent_page._set_intent("leveling")
    intent_page._homed_next_at = 0
    intent_page._maybe_check_homing(types.SimpleNamespace(
        get_endstops=lambda: {"ok": True, "homed": True, "machine_status": "BUSY",
                              "move_mode": "MOVING"}))
    expect(intent_page._local_intent is not None
           and intent_page._local_intent["seen_active"] is True,
           "M119 BUSY/MOVING marca actividad")
    intent_page._homed_next_at = 0
    before = len(intent_page.messages("notification"))
    intent_page._maybe_check_homing(types.SimpleNamespace(
        get_endstops=lambda: {"ok": True, "homed": True, "machine_status": "READY",
                              "move_mode": "READY"}))
    notes = intent_page.messages("notification")[before:]
    expect(intent_page._local_intent is None
           and len(notes) == 1 and "Nivelado completado" in notes[0]["message"],
           f"M119 cierra el intento: {notes}")

    # Desconexión: limpia sin notificar.
    intent_page._set_intent("homing")
    before = len(intent_page.messages("notification"))
    intent_page._finish_intent("disconnect")
    expect(intent_page._local_intent is None
           and len(intent_page.messages("notification")) == before,
           "desconexión limpia la intención sin aviso")

    # --- descubrimiento vía handler del worker (función UDP sustituida) ---
    original_discover = module.discover_printers
    module.discover_printers = lambda timeout=0.0, targets=None: [
        {"ip": "192.168.1.50", "name": "AD5X", "model": "AD5X",
         "serial": FAKE_SERIAL, "pid": 0x0026, "http_port": 8898},
    ]
    try:
        before = len(page.messages("discovered"))
        page._run_discovery()
        found = wait_new_message(page, "discovered", before, timeout=2.0)
        expect(found is not None and found.get("ok") is True, "descubrimiento responde")
        assert found is not None
        expect(found["printers"][0]["ip"] == "192.168.1.50", "IP descubierta")
    finally:
        module.discover_printers = original_discover

    module.discover_printers = lambda timeout=0.0, targets=None: []
    try:
        before = len(page.messages("discovered"))
        page._run_discovery()
        empty = wait_new_message(page, "discovered", before, timeout=2.0)
        expect(empty is not None and empty.get("printers") == [], "sin resultados")
        expect("manualmente" in empty.get("message", ""),
               "sin resultados sugiere el flujo manual")
    finally:
        module.discover_printers = original_discover

    # Robustez: un cliente roto no debe tumbar el hilo ni lanzar.
    broken = page_cls()
    broken.get_ui()  # inicializa estado
    broken._client = object()  # type: ignore[assignment]
    broken._poll_once()  # no debe lanzar
    expect(broken.messages("error"), "un cliente roto se reporta como error interno")
    broken.on_message(b'{"type": "get-config"}')  # bytes: tampoco debe lanzar

    # El plugin también debe arrancar el hilo si el host llama on_load.
    page2 = page_cls()
    page2.on_load()
    expect(page2._worker is not None and page2._worker.is_alive(),
           "on_load arranca el hilo de sondeo")
    expect(wait_for(lambda: page2.messages("status"), 6.0),
           "on_load configura el worker y llegan datos")
    page2.on_unload()

    # --- integración: calibration_cmd → busy → ready (notificación por intención) ---
    http_server = ThreadingHTTPServer(("127.0.0.1", 0), FakePrinterHandler)
    http_port = http_server.server_address[1]
    threading.Thread(target=http_server.serve_forever, daemon=True).start()
    _reset_fake_printer()
    intent_live = page_cls()
    intent_live._stored_config = json.dumps({
        "printers": [{"name": "fake", "ip": f"127.0.0.1:{http_port}",
                      "serial": FAKE_SERIAL, "check_code": FAKE_CODE}],
        "active_printer": 0,
        "poll_interval": 5,
        "mock": False,
    })
    try:
        intent_live.on_load()
        expect(wait_for(lambda: intent_live.messages("status"), 8.0),
               "intención: primer status del fake")

        FakePrinterHandler.set_state_sequence([
            {"state": "busy", "duration": 1.2},
            {"state": "ready", "duration": 0.0},
        ])
        before_intents = len(intent_live.messages("intent"))
        before_notes = len(intent_live.messages("notification"))
        intent_live.on_message({"type": "calibrate", "leveling": True})
        intent_msg = wait_new_message(intent_live, "intent", before_intents, timeout=3.0)
        expect(intent_msg is not None and intent_msg["intent"]["kind"] == "leveling",
               f"calibration_cmd crea intent leveling ({intent_msg})")
        expect(any(m.get("intent") for m in intent_live.messages("status")),
               "el status incluye la intención para el hero")
        note = wait_new_message(intent_live, "notification", before_notes, timeout=8.0)
        expect(note is not None and "Nivelado completado" in note["message"],
               f"notificación de fin por intención: {note}")
        expect(intent_live._local_intent is None, "la intención se cierra al volver ready")

        FakePrinterHandler.set_state_sequence([
            {"state": "busy", "duration": 0.4},
            {"state": "error", "duration": 0.0},
        ], error="E0100")
        before_notes = len(intent_live.messages("notification"))
        intent_live.on_message({"type": "calibrate", "leveling": False,
                                "vibration": True})
        note = wait_new_message(intent_live, "notification", before_notes, timeout=8.0)
        expect(note is not None and note["level"] == "warning"
               and "Calibración terminó con error" in note["message"]
               and "E0100" in note["message"],
               f"error de intención sin duplicar: {note}")
    finally:
        intent_live.on_unload()
        FakePrinterHandler.set_state_sequence([], "")
        http_server.shutdown()
        http_server.server_close()

    # 'refresh' interrumpe un backoff largo y fuerza un intento inmediato.
    page3 = page_cls()
    page3._stored_config = json.dumps({
        "printers": [{"name": "muerta", "ip": "127.0.0.1:1",
                      "serial": "SN123", "check_code": "abcd"}],
        "active_printer": 0,
        "poll_interval": 60,
        "mock": False,
    })
    page3.on_load()
    try:
        def network_errors() -> list:
            return [m for m in page3.messages("error")
                    if m.get("kind") in ("network", "timeout", "tcp")]

        expect(wait_for(lambda: bool(network_errors()), 8.0),
               "la impresora muerta reporta error de red")
        first = network_errors()[-1]
        expect(first.get("retry_in") == 5,
               f"primer backoff de 5 s ({first.get('retry_in')})")
        page3._next_poll = time.monotonic() + 60  # simula backoff largo en curso
        before = len(network_errors())
        page3.on_message({"type": "refresh"})
        expect(wait_for(lambda: len(network_errors()) > before, 2.0),
               "refresh interrumpe el backoff y sondea de inmediato")
    finally:
        page3.on_unload()


# ---------------------------------------------------------------------------


def main() -> int:
    print(f"Plugin: {PLUGIN_PATH}")
    if not PLUGIN_PATH.exists():
        print(f"No existe el archivo del plugin: {PLUGIN_PATH}")
        return 2

    check("cliente FlashForgeClient (sin orca)", test_client_without_orca)
    check("cliente contra API real simulada", test_client_real_api)
    check("descubrimiento UDP (servidor local)", test_discovery_udp)
    check("capability FlashForgePage (host simulado)", test_page_with_fake_orca)

    total = _passes + len(_failures)
    print(f"\nResultado: {_passes}/{total} secciones correctas")
    if _failures:
        print("\nFallos:")
        for item in _failures:
            print(f"  - {item}")
        return 1
    print("Todo OK (modo demo, sin OrcaSlicer y sin impresora).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
