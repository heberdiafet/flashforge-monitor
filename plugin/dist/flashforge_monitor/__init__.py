# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "FlashForge Monitor"
# description = "Dashboard para monitorear y controlar una FlashForge AD5X por red local"
# author = "jfet"
# version = "0.9.0"
# ///

"""FlashForge Monitor — plugin de OrcaSlicer.

Agrega una pestaña "FlashForge" con un dashboard HTML autocontenido que muestra
estado, temperaturas, trabajo en curso, cámara y slots IFS de una impresora
FlashForge AD5X accesible por HTTP en la red local.

Diseño general
--------------
* Un único `@orca.plugin` (`FlashForgeMonitorPlugin`) registra una única
  capability de tipo ``pages`` (`FlashForgePage`).
* Todo el trabajo de red ocurre en un hilo daemon propio: ``on_message()`` corre
  en el hilo de UI y solo valida, guarda configuración y encola peticiones.
* Los datos viajan a la página con ``post_message()`` y la página los pide con
  ``{type:'ready'}`` al cargar (un post_message previo a la existencia de la
  página se descarta).
* Cero dependencias: solo stdlib (``urllib.request``, ``json``, ``threading``,
  ``queue``, ``time``).

Modo demo
---------
Si la configuración tiene ``mock = true`` (valor por defecto), el cliente
devuelve datos simulados y el dashboard funciona sin impresora. Es útil para
desarrollar la UI y para probar el plugin sin credenciales.

API real (verificada en hardware, ver ``docs/api-reference.md``)
---------------------------------------------------------------
``http://<ip>:8898`` con POST JSON en todos los endpoints. Cada cuerpo lleva
``serialNumber`` + ``checkCode`` (el "Printer ID" de 8 caracteres que muestra la
pantalla en LAN-only mode). El envelope de respuesta es ``{code, message}`` y
``code != 0`` significa error: la impresora responde **siempre HTTP 200**.
El puerto 8899 es el protocolo TCP crudo de texto (``~M115``…), no esta API.

Endpoints usados: ``/checkCode`` (validar credenciales), ``/detail`` (estado,
temperaturas, trabajo y estación IFS), ``/control`` (``jobCtl_cmd``,
``msConfig_cmd``, ``temperatureCtl_cmd``, ``printerCtl_cmd``, ``lightControl_cmd``,
``homingCtrl_cmd``, ``moveCtrl_cmd``, ``extrudeCtrl_cmd``, ``calibration_cmd``,
``errorCodeCtrl_cmd``, ``delayClose_cmd``), ``/gcodeList``, ``/gcodeThumb``,
``/printGcode`` y ``/uploadGcode`` (este último autentica por **cabeceras** y es
multipart, §3.3 — la excepción a la regla del body JSON). La cámara MJPEG va
por ``http://<ip>:8080/?action=stream`` y es opcional: el AD5X no la trae de
fábrica.

Módulos del dashboard (v0.2)
----------------------------
* **Estado**: tarjetas de temperaturas, trabajo, cámara e IFS.
* **Control**: setpoints de temperatura, velocidad, Z-offset, ventiladores y LED.
* **Archivos**: ``/gcodeList`` + miniaturas ``/gcodeThumb``, imprimir
  (``/printGcode``) y subir (``/uploadGcode`` con ruta local). La API **no**
  permite borrar archivos: no hay botón de borrado.
* **Mantenimiento**: homing, mover ejes, extruir/retraer, calibración, limpiar
  error y auto-apagado.

Temperaturas en AD5X (§6.5): se envía ``temperatureCtl_cmd`` por HTTP y, tal
como documenta la referencia, si ``/detail`` no refleja el cambio en ~2 s se
hace fallback por TCP ``~M104 S<temp>`` / ``~M140 S<temp>`` (puerto 8899).

Nota sobre permisos de red
--------------------------
OrcaSlicer audita las operaciones de red de los plugins y muestra un diálogo
Sí/No la primera vez que un plugin abre una conexión hacia un destino nuevo; un
"Sí" queda recordado para ese plugin. Como la IP de la impresora la configura el
usuario (y cambia entre redes), aquí NO se declara ``orca.request_permissions``:
se deja que el audit hook pregunte en el primer uso real. Si en el futuro se
quisiera pre-declarar, sería en ``register_capabilities()``:
``orca.request_permissions(network_http=["http://192.168.x.y:8898"])``.
"""

from __future__ import annotations

import base64
import http.client
import json
import math
import os
import pathlib
import queue
import re
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

# El módulo `orca` solo existe dentro de OrcaSlicer. Al importar este archivo
# desde un test externo (test_mock.py), `orca` es None y las clases que heredan
# de la API del host simplemente no se definen.
try:  # pragma: no cover - depende del host
    import orca  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    orca = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

PLUGIN_VERSION = "0.9.0"

#: Puerto de la API HTTP local (JSON). El 8899 es TCP crudo de texto (~M…): no
#: se usa en este plugin. Ver docs/api-reference.md §1–2.
API_PORT = 8898

#: Cámara MJPEG (misma IP, sin autenticación). El AD5X no la trae de fábrica:
#: el usuario debe activarla en la pantalla de la impresora (§8).
CAMERA_PORT = 8080
CAMERA_PATH = "/?action=stream"

#: Protocolo TCP crudo de texto (`~M105`, `~M104`…). No es la API principal,
#: pero §6.5 lo usa como fallback de temperatura en AD5X.
TCP_PORT = 8899

#: Subida de archivos (§5.9). El firmware lista .g/.gx/.gcode/.3mf.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
UPLOAD_EXTENSIONS = (".gcode", ".gx", ".g", ".3mf")

#: Rangos de los controles (§6.4–6.5, valores de AD5X).
SPEED_MIN, SPEED_MAX = 50, 500
Z_OFFSET_MIN, Z_OFFSET_MAX = -5.0, 5.0
NOZZLE_MAX_TEMP, BED_MAX_TEMP = 260, 100
FAN_MAX_PERCENT = 100

#: Por debajo de esta temperatura la UI avisa antes de extruir.
MIN_EXTRUDE_TEMP = 170.0

DEFAULT_INTERVAL = 5.0
MIN_INTERVAL = 1.0
MAX_INTERVAL = 120.0

#: Estados transitorios documentados (§11.2). Durante ellos se sondea cada 2 s.
TRANSIENT_STATES: frozenset[str] = frozenset({
    "calibrate_doing", "heating", "loading", "busy",
    "downloading", "sending", "unzipping", "cloud_slicing",
})

#: Estados conocidos del firmware; cualquier otro se trata como ``busy``.
KNOWN_STATES: frozenset[str] = frozenset({
    "ready", "busy", "calibrate_doing", "heating", "printing", "pausing", "pause",
    "canceling", "cancel", "completed", "error", "downloading", "loading",
    "cloud_slicing", "sending", "unzipping", "resuming", "paused", "paused_p",
    "cancelled", "finish", "idle",
})


def normalize_state(value: Any) -> str:
    """Cualquier estado desconocido se trata como ``busy`` (regla del firmware)."""
    state = str(value or "").strip()
    return state if state in KNOWN_STATES else "busy"


#: Estados de impresión: también a 2 s para progreso/avisos fluidos.
PRINTING_STATES: frozenset[str] = frozenset({
    "printing", "pausing", "pause", "paused", "paused_p", "resuming", "canceling",
})

#: Tope del backoff cuando la impresora no responde.
MAX_BACKOFF = 60.0

#: Tiempos máximos de una intención local (segundos) antes de rendirse.
INTENT_TIMEOUTS: dict[str, int] = {
    "leveling": 12 * 60,
    "input_shaper": 8 * 60,
    "load": 5 * 60,
    "unload": 5 * 60,
    "homing": 3 * 60,
}
#: Margen mínimo antes de aceptar un /detail "ready" como fin de intención.
INTENT_GRACE = 2.0


def next_delay(state: str, consecutive_failures: int, base_interval: float) -> float:
    """Cadencia del próximo sondeo (función pura y testeable).

    - Fallos de red: backoff exponencial 5/10/20/40/60 s (tope 60).
    - Transitorios y impresión: 2 s.
    - Idle: ``poll_interval`` del usuario (clamp 1–120).
    """
    try:
        base = float(base_interval)
    except (TypeError, ValueError):
        base = DEFAULT_INTERVAL
    base = max(MIN_INTERVAL, min(MAX_INTERVAL, base))
    try:
        failures = int(consecutive_failures)
    except (TypeError, ValueError):
        failures = 0
    if failures > 0:
        return min(MAX_BACKOFF, 5.0 * (2 ** (min(failures, 8) - 1)))
    if state in TRANSIENT_STATES or state in PRINTING_STATES:
        return max(MIN_INTERVAL, min(base, 2.0))
    return base

#: Timeout de red por petición (la referencia usa 5 s).
REQUEST_TIMEOUT = 5.0

#: Paleta de 24 colores que la UI de la impresora reconoce (§7.4). Cualquier otro
#: valor se guarda pero la pantalla lo dibuja como blanco.
PALETTE: tuple[tuple[str, str], ...] = (
    ("White", "#FFFFFF"),
    ("Yellow", "#FEF043"),
    ("Light Green", "#DCF478"),
    ("Green", "#0ACC38"),
    ("Dark Green", "#067749"),
    ("Teal", "#0C6283"),
    ("Cyan", "#0DE2A0"),
    ("Light Blue", "#75D9F3"),
    ("Blue", "#45A8F9"),
    ("Dark Blue", "#2750E0"),
    ("Purple", "#46328E"),
    ("Violet", "#A03CF7"),
    ("Magenta", "#F330F9"),
    ("Pink", "#D4B0DC"),
    ("Coral", "#F95D73"),
    ("Red", "#F72224"),
    ("Brown", "#7C4B00"),
    ("Orange", "#F98D33"),
    ("Cream", "#FDEBD5"),
    ("Tan", "#D3C4A3"),
    ("Dark Brown", "#AF7836"),
    ("Gray", "#898989"),
    ("Light Gray", "#BCBCBC"),
    ("Black", "#161616"),
)

_PALETTE_HEX = frozenset(hex_value for _name, hex_value in PALETTE)

#: Materiales que la UI de la impresora dibuja con icono (§7.4). El firmware no
#: valida `mt`: acepta cualquier texto, así que la lista es solo una ayuda.
MATERIALS: tuple[str, ...] = (
    "PLA", "PLA-CF", "ABS", "PETG", "PETG-CF", "TPU", "SILK",
    "PA-CF", "PC", "PC-ABS", "PET-CF", "PPS-CF", "PA", "PAHT-CF",
)

#: Versión del esquema de config (v1: ip/serial/check_code en la raíz;
#: v2: lista de impresoras + ajustes globales). Ver ``get_config_version``.
CONFIG_VERSION = "2"

#: Máximo de impresoras que se guardan (la UI no sondea las inactivas).
MAX_PRINTERS = 8

#: Impresoras de demostración cuando ``mock`` está activo y no hay ninguna
#: configurada. Solo existen en memoria (no se guardan hasta que se editen).
MOCK_PRINTERS: tuple[dict[str, str], ...] = (
    {"name": "AD5X demo (taller)", "ip": "192.0.2.10", "serial": "DEMO000000001",
     "check_code": ""},
    {"name": "AD5X demo (sala)", "ip": "192.0.2.11", "serial": "DEMO000000002",
     "check_code": ""},
)

#: Trabajos del modo demo; cada impresora falsa recibe uno distinto (por serial).
_MOCK_JOBS: tuple[str, ...] = (
    "AD5X_benchy_0.20mm_PLA_1h02m.gx",
    "Maceta_hexagonal_0.28mm_PETG.gx",
    "Soporte_mando_0.16mm_PLA_3h21m.gx",
)

#: Destinos del descubrimiento UDP (§9.1): multicast y broadcast.
DISCOVERY_TARGETS: tuple[tuple[str, int], ...] = (
    ("225.0.0.9", 19000),
    ("255.255.255.255", 48899),
)
DISCOVERY_TIMEOUT = 2.0

DEFAULT_CONFIG: dict[str, Any] = {
    "printers": [],            # [{name, ip, serial, check_code}]
    "active_printer": 0,       # índice en printers
    "poll_interval": DEFAULT_INTERVAL,
    "notify_on_finish": True,
    "notify_on_error": True,
    "mock": True,              # datos simulados si no hay impresora configurada
}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


class FlashForgeError(Exception):
    """Error controlado del cliente (red, protocolo, credenciales o firmware)."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        code: int | None = None,
        raw_message: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.raw_message = raw_message

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "message": str(self)}
        if self.code is not None:
            data["code"] = self.code
        if self.raw_message:
            data["raw_message"] = self.raw_message
        return data


#: Diagnósticos para los mensajes EXACTOS del firmware (§3.5). Se comparan en
#: minúsculas porque el firmware no garantiza el estilo.
_AUTH_HINTS: dict[str, tuple[str, str]] = {
    "sn is different": (
        "auth-serial",
        "Serial incorrecto: el número de serie no coincide con la impresora. "
        "Cópialo de Ajustes → Información del dispositivo (13 caracteres).",
    ),
    "access code is different": (
        "auth-code",
        "Check code incorrecto: copia el Printer ID de 8 caracteres que muestra "
        "la pantalla en el modo LAN-only.",
    ),
    "parameters is error": (
        "parameters",
        "La impresora rechazó la petición: faltan parámetros o el formato no es válido.",
    ),
    "parameters is different": (
        "parameters",
        "La impresora rechazó la petición: faltan parámetros o el formato no es válido.",
    ),
    "lan mode error": (
        "lan-mode",
        "LAN-only mode desactivado en la impresora: actívalo en Ajustes y reinicia la impresora.",
    ),
    "printer is busy": ("busy", "La impresora está ocupada con otra tarea."),
    "printer is busy.": ("busy", "La impresora está ocupada con otra tarea."),
    "file does not exist.": ("file", "El archivo no existe en la impresora."),
}


def _error_from_envelope(data: dict[str, Any]) -> FlashForgeError:
    """Traduce el envelope ``{code, message}`` del firmware a un error legible."""
    try:
        code: int | None = int(data.get("code", -1))
    except (TypeError, ValueError):
        code = None
    raw = str(data.get("message") or "").strip()

    hint = _AUTH_HINTS.get(raw.lower())
    if hint is not None:
        kind, message = hint
        return FlashForgeError(kind, message, code=code, raw_message=raw)

    if code == 0:
        return FlashForgeError(
            "printer", f"Respuesta inesperada de la impresora: {raw or 'sin mensaje'}.",
            code=code, raw_message=raw,
        )
    return FlashForgeError(
        "printer",
        f"La impresora devolvió un error (código {code}): {raw or 'sin mensaje'}.",
        code=code,
        raw_message=raw,
    )


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default  # descarta NaN


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_pid(value: Any) -> int:
    """``detail.pid`` llega como entero o como string hex (``"0026"`` = 38)."""
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text, 16)
        except ValueError:
            return _to_int(text)
    return _to_int(value)


def _truthy(value: Any) -> bool:
    """Acepta ``true``/``1``/``"open"`` para flags que el firmware mezcla."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "open")
    return bool(value)


def normalize_color(value: Any, *, strict: bool = True) -> str:
    """Normaliza un color a ``#RRGGBB`` en mayúsculas (§7.2).

    Con ``strict=False`` (lectura de ``/detail``) devuelve "" si no es un color
    válido en vez de lanzar.
    """
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if len(text) == 6 and all(ch in "0123456789ABCDEF" for ch in text):
        text = "#" + text
    valid = (
        len(text) == 7
        and text.startswith("#")
        and all(ch in "0123456789ABCDEF" for ch in text[1:])
    )
    if valid:
        return text
    if strict:
        raise FlashForgeError(
            "invalid",
            f"Color no válido: {value!r}. Usa el formato #RRGGBB (paleta de 24 colores).",
        )
    return ""


def normalize_host(value: Any) -> str:
    """Normaliza lo que escribe el usuario: sin esquema, sin ruta, sin barras.

    Acepta ``http://192.168.1.50``, ``192.168.1.50/`` o ``192.168.1.50:8898``.
    """
    host = str(value or "").strip()
    if not host:
        return ""
    if "://" in host:
        parsed = urllib.parse.urlsplit(host)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
    host = host.split("/", 1)[0].strip()
    return host


def split_host_port(value: Any, default_port: int = API_PORT) -> tuple[str, int]:
    """Separa ``host[:puerto]`` devolviendo (hostname, puerto). Solo IPv4/nombres."""
    host = normalize_host(value)
    if host and host.count(":") == 1:
        name, _, port_text = host.rpartition(":")
        if port_text.isdigit() and 0 < int(port_text) < 65536:
            return name, int(port_text)
    return host, default_port


def clamp_interval(value: Any) -> float:
    """Fuerza el intervalo de sondeo al rango [MIN_INTERVAL, MAX_INTERVAL]."""
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    if candidate != candidate or candidate in (float("inf"), float("-inf")):
        return DEFAULT_INTERVAL
    return max(MIN_INTERVAL, min(MAX_INTERVAL, candidate))


def _log(message: str) -> None:
    """Log a stderr: OrcaSlicer lo recoge en log/python_*.log."""
    try:
        print(f"[FlashForgeMonitor] {message}", file=sys.stderr, flush=True)
    except Exception:  # pragma: no cover - stderr siempre debería existir
        pass


# ---------------------------------------------------------------------------
# Cliente HTTP de la impresora
# ---------------------------------------------------------------------------


class FlashForgeClient:
    """Cliente mínimo de la API local de la AD5X.

    Todos los métodos devuelven estructuras JSON-compatibles (dict) y lanzan
    :class:`FlashForgeError` con un mensaje ya listo para mostrar al usuario.

    Con ``mock=True`` no se abre ninguna conexión: se devuelven datos simulados
    que evolucionan en el tiempo para poder trabajar en el dashboard.
    """

    def __init__(
        self,
        host: str = "",
        port: int = API_PORT,
        serial: str = "",
        check_code: str = "",
        *,
        mock: bool = False,
        timeout: float = REQUEST_TIMEOUT,
        tcp_port: int = TCP_PORT,
    ) -> None:
        self.host = normalize_host(host)
        self.port = int(port) if port else API_PORT
        self.serial = serial or ""
        self.check_code = check_code or ""
        self.mock = bool(mock)
        self.timeout = float(timeout)
        self.tcp_port = int(tcp_port) if tcp_port else TCP_PORT
        self._camera_url = ""  # URL anunciada por /detail (si hay cámara)
        self._last_slots: list[dict[str, Any]] = []  # caché de /detail para printGcode
        self._last_error_code = ""  # error activo, para errorCodeCtrl_cmd

        # Estado del modo demo (solo se toca desde el hilo de sondeo).
        # La semilla por serial hace que dos impresoras de demo se vean distintas.
        seed = sum(ord(ch) for ch in (self.serial or "demo"))
        self._mock_started = time.monotonic()
        self._mock_last_tick = self._mock_started
        self._mock_progress = 20.0 + float(seed % 45)
        self._mock_state = "printing"
        self._mock_job_name = _MOCK_JOBS[seed % len(_MOCK_JOBS)]
        self._mock_nozzle_target = 210.0 + float((seed % 3) * 5)
        self._mock_bed_target = 55.0 + float(seed % 10)
        self._mock_speed = 100
        self._mock_z_offset = 0.0
        self._mock_fan_part = 100
        self._mock_fan_aux = 0
        self._mock_light = "close"
        self._mock_error = ""
        self._mock_shutdown = 0
        self._mock_slots: list[dict[str, Any]] = [
            {"index": 0, "slot_id": 1, "material": "PLA", "color": "#F72224", "present": True},
            {"index": 1, "slot_id": 2, "material": "PLA", "color": "#FFFFFF", "present": True},
            {"index": 2, "slot_id": 3, "material": "PETG", "color": "#2750E0", "present": True},
            {"index": 3, "slot_id": 4, "material": "", "color": "", "present": False},
        ]

    # -- identidad ----------------------------------------------------------

    def matches(self, *, host: str, serial: str, check_code: str, mock: bool) -> bool:
        """True si el cliente ya representa esa configuración (evita recrearlo)."""
        return (
            self.host == normalize_host(host)
            and self.serial == (serial or "")
            and self.check_code == (check_code or "")
            and self.mock == bool(mock)
        )

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        mode = "mock" if self.mock else f"{self.host}:{self.port}"
        return f"FlashForgeClient({mode})"

    # -- transporte ---------------------------------------------------------

    def _require_credentials(self) -> None:
        """Comprueba lo mínimo antes de abrir un socket (mejor diagnóstico)."""
        if not self.host:
            raise FlashForgeError(
                "no-host",
                "Configura la IP de la impresora en Ajustes → FlashForge Monitor.",
            )
        if not self.serial:
            raise FlashForgeError(
                "auth-serial",
                "Falta el número de serie (SN) de la impresora.",
            )
        if not self.check_code:
            raise FlashForgeError(
                "auth-code",
                "Falta el check code: es el Printer ID de 8 caracteres que muestra "
                "la pantalla de la impresora (LAN-only mode).",
            )

    def _request(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """POST JSON contra ``http://host:8898`` y devuelve el envelope crudo.

        El firmware responde **siempre HTTP 200** (incluso en error): el
        resultado real viaja en ``code``/``message`` y lo valida :meth:`_call`.
        """
        self._require_credentials()

        url = f"http://{self.host}:{self.port}{path}"
        body = json.dumps(payload if payload is not None else {}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"OrcaSlicer-FlashForgeMonitor/{PLUGIN_VERSION}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=timeout if timeout is not None else self.timeout
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise FlashForgeError(
                "http", f"La impresora respondió HTTP {exc.code} en {path}."
            ) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise FlashForgeError(
                "network",
                f"No se pudo conectar con {self.host}:{self.port} ({reason}). "
                "Comprueba que está encendida y en LAN-only mode.",
            ) from exc
        except TimeoutError as exc:
            raise FlashForgeError(
                "timeout", f"Tiempo de espera agotado al contactar {self.host}:{self.port}."
            ) from exc
        except OSError as exc:
            raise FlashForgeError("network", f"Error de red: {exc}") from exc

        if not raw:
            # /checkCode responde 200 con cuerpo vacío si no pudo leer el JSON.
            raise FlashForgeError(
                "parameters",
                "La impresora respondió con un cuerpo vacío (petición no reconocida).",
            )
        return self._decode_body(raw, path)

    @staticmethod
    def _decode_body(raw: bytes, path: str) -> dict[str, Any]:
        try:
            decoded = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise FlashForgeError(
                "protocol", f"Respuesta no JSON de la impresora en {path}."
            ) from exc
        if not isinstance(decoded, dict):
            raise FlashForgeError("protocol", f"Respuesta inesperada de la impresora en {path}.")
        return decoded

    def _call(self, path: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Petición autenticada que exige ``code == 0`` (§3.2, §4)."""
        body: dict[str, Any] = {
            "serialNumber": self.serial,
            "checkCode": self.check_code,
        }
        if extra:
            body.update(extra)
        data = self._request(path, body)
        try:
            code = int(data.get("code", -1))
        except (TypeError, ValueError):
            code = -1
        if code != 0:
            raise _error_from_envelope(data)
        return data

    def _detail(self) -> dict[str, Any]:
        """``POST /detail`` → dict ``detail`` ya mapeado por los helpers."""
        data = self._call("/detail")
        detail = data.get("detail")
        if not isinstance(detail, dict):
            raise FlashForgeError(
                "protocol", "La impresora no devolvió el objeto 'detail'."
            )
        self._remember_detail(detail)
        return detail

    def _remember_detail(self, detail: dict[str, Any]) -> None:
        """Guarda cámara, slots y error activo para comandos posteriores."""
        url = str(detail.get("cameraStreamUrl") or "").strip()
        if url:
            self._camera_url = url
        ifs = self._map_ifs(detail)
        if ifs["slots"]:
            self._last_slots = ifs["slots"]
        self._last_error_code = str(detail.get("errorCode") or "").strip()

    def _get_raw(self, path: str, *, timeout: float | None = None) -> tuple[int, str, bytes]:
        """GET sin credenciales. Solo se usa para ``/getThum`` (§5.7)."""
        if not self.host:
            raise FlashForgeError("no-host", "Configura la IP de la impresora.")
        url = f"http://{self.host}:{self.port}{path}"
        request = urllib.request.Request(
            url,
            headers={"Accept": "*/*",
                     "User-Agent": f"OrcaSlicer-FlashForgeMonitor/{PLUGIN_VERSION}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=timeout if timeout is not None else self.timeout
            ) as response:
                return response.status, response.headers.get("Content-Type", ""), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type", ""), exc.read()
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise FlashForgeError(
                "network", f"No se pudo conectar con {self.host}:{self.port} ({reason})."
            ) from exc
        except OSError as exc:
            raise FlashForgeError("network", f"Error de red: {exc}") from exc

    def _tcp_command(self, command: str, *, timeout: float = 3.0) -> str:
        """Comando de texto por TCP 8899 (§10): ``~M104 S…`` + CRLF, sin auth.

        Se usa como fallback cuando ``temperatureCtl_cmd`` por HTTP no tiene
        efecto (§6.5). Solo se invoca desde acciones del usuario, nunca en sondeo.
        """
        if not self.host:
            raise FlashForgeError("no-host", "Configura la IP de la impresora.")
        host, _ = split_host_port(self.host, self.port)
        payload = f"~{command}\r\n".encode("ascii", "replace")
        chunks: list[bytes] = []
        try:
            with socket.create_connection((host, self.tcp_port), timeout=timeout) as sock:
                sock.sendall(payload)
                sock.settimeout(timeout)
                while True:
                    try:
                        data = sock.recv(4096)
                    except TimeoutError:
                        break
                    if not data:
                        break
                    chunks.append(data)
                    if b"\nok" in data or data.strip().endswith(b"ok"):
                        break
        except OSError as exc:
            raise FlashForgeError(
                "tcp",
                f"No se pudo usar el protocolo TCP {self.tcp_port} "
                f"(fallback de temperatura): {exc}",
            ) from exc
        reply = b"".join(chunks).decode("utf-8", "replace").strip()
        if "error" in reply.lower() or not reply:
            raise FlashForgeError("tcp", f"La impresora no confirmó el comando TCP: {reply!r}")
        return reply

    @staticmethod
    def _tcp_read_until_ok(sock: socket.socket, timeout: float) -> str:
        """Lee una respuesta ``~M…`` hasta la línea ``ok`` (o timeout)."""
        sock.settimeout(timeout)
        chunks: list[bytes] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = sock.recv(4096)
            except (TimeoutError, socket.timeout):
                break
            if not data:
                break
            chunks.append(data)
            joined = b"".join(chunks).strip().lower()
            if b"\nok" in joined or joined.endswith(b"ok"):
                break
        return b"".join(chunks).decode("utf-8", "replace").strip()

    @staticmethod
    def _parse_m119(reply: str) -> dict[str, Any]:
        """Parsea ``Endstop: X-max: … Y-max: … Z-min: …`` (§10).

        Firmware legacy responde 0/1 (1 = final de carrera pulsado). La AD5X
        responde posiciones de referencia (``X-max: 110 Y-max: 110 Z-min: 0``):
        X/Y > 0 y Z-min = 0 son el estado homed documentado en hardware.
        """
        line = ""
        for candidate in str(reply or "").replace("\r", "").split("\n"):
            if "X-max" in candidate and "Y-max" in candidate:
                line = candidate
                break

        def value(key: str) -> int | None:
            match = re.search(rf"{key}\s*:\s*(-?\d+)", line, re.IGNORECASE)
            return int(match.group(1)) if match else None

        xmax, ymax, zmin = value("X-max"), value("Y-max"), value("Z-min")
        status_match = re.search(r"MachineStatus\s*:\s*([A-Z_]+)", reply, re.IGNORECASE)
        move_match = re.search(r"MoveMode\s*:\s*([A-Z_]+)", reply, re.IGNORECASE)
        machine_status = status_match.group(1).upper() if status_match else ""
        move_mode = move_match.group(1).upper() if move_match else ""

        if xmax is None or ymax is None or zmin is None:
            return {
                "x": None, "y": None, "z": None, "homed": None,
                "machine_status": machine_status, "move_mode": move_mode,
                "raw": str(reply or "").strip(),
            }

        legacy = xmax in (0, 1) and ymax in (0, 1) and zmin in (0, 1)
        if legacy:
            x, y, z = xmax == 1, ymax == 1, zmin == 1
        else:
            x, y, z = xmax > 0, ymax > 0, zmin >= 0
        return {
            "x": bool(x), "y": bool(y), "z": bool(z),
            "homed": bool(x and y and z),
            "machine_status": machine_status, "move_mode": move_mode,
            "raw": str(reply or "").strip(),
        }

    def get_endstops(self, *, timeout: float = 3.0) -> dict[str, Any]:
        """Endstops/homing reales por TCP 8899 (``~M601 S1`` + ``~M119``).

        Solo lectura. Ante cualquier fallo de red devuelve ``homed: None`` para
        que la UI degrade sin chip y conserve el aviso de sesión como fallback.
        """
        if self.mock:
            return {
                "ok": True, "x": True, "y": True, "z": True, "homed": True,
                "machine_status": "READY", "move_mode": "READY",
                "raw": "mock: homed", "mock": True,
            }
        if not self.host:
            return {"ok": False, "x": None, "y": None, "z": None, "homed": None,
                    "error": "sin host"}

        host, _ = split_host_port(self.host, self.port)
        reply = ""
        try:
            with socket.create_connection((host, self.tcp_port), timeout=timeout) as sock:
                # El handshake es la práctica documentada; M119 también responde
                # sin él, así que un "Control Failed" no impide la lectura.
                sock.sendall(b"~M601 S1\r\n")
                self._tcp_read_until_ok(sock, timeout)
                sock.sendall(b"~M119\r\n")
                reply = self._tcp_read_until_ok(sock, timeout)
                try:
                    sock.sendall(b"~M602\r\n")
                    self._tcp_read_until_ok(sock, timeout)
                except OSError:
                    pass
        except OSError as exc:
            return {"ok": False, "x": None, "y": None, "z": None, "homed": None,
                    "error": str(exc)}

        parsed = self._parse_m119(reply)
        parsed["ok"] = parsed.get("homed") is not None
        if not parsed["ok"]:
            parsed["error"] = "respuesta M119 no reconocida"
        parsed["mock"] = False
        return parsed

    def _upload_stream(
        self,
        path: str,
        file_path: str,
        headers: dict[str, str],
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        """POST multipart en streaming (evita cargar 200 MB en memoria).

        ``/uploadGcode`` autentica por cabeceras (Método B, §3.3), no por body.
        Se usa ``http.client`` porque da longitud y envío por trozos con stdlib.
        """
        if not self.host:
            raise FlashForgeError("no-host", "Configura la IP de la impresora.")
        filename = os.path.basename(file_path).replace('"', "_").replace("\r", "_").replace("\n", "_")
        size = os.path.getsize(file_path)
        boundary = "----FlashForgeMonitor" + str(int(time.time() * 1000))
        pre = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="gcodeFile"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        post = f"\r\n--{boundary}--\r\n".encode("utf-8")
        total = len(pre) + size + len(post)

        host, port = split_host_port(self.host, self.port)
        connection = http.client.HTTPConnection(host, port, timeout=max(self.timeout, 30.0))
        try:
            connection.putrequest("POST", path, skip_accept_encoding=True)
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            for key, value in headers.items():
                connection.putheader(key, str(value))
            connection.putheader("Content-Length", str(total))
            connection.endheaders()
            connection.send(pre)

            sent = 0
            with open(file_path, "rb") as fh:
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    connection.send(chunk)
                    sent += len(chunk)
                    if progress is not None:
                        try:
                            progress(sent, size)
                        except Exception:  # un fallo del callback no corta la subida
                            pass
            connection.send(post)

            response = connection.getresponse()
            raw = response.read()
        except PermissionError as exc:
            # Típico del sandbox/Audit hook de Orca o del Flatpak al leer el archivo.
            raise FlashForgeError(
                "file",
                f"Sin permiso para leer {file_path}. Si OrcaSlicer pidió permiso, "
                "concédelo y vuelve a intentarlo (el Flatpak solo ve algunas rutas).",
            ) from exc
        except OSError as exc:
            raise FlashForgeError("network", f"Error durante la subida: {exc}") from exc
        finally:
            connection.close()

        if not raw:
            raise FlashForgeError("protocol", "La impresora no respondió a /uploadGcode.")
        data = self._decode_body(raw, "/uploadGcode")
        try:
            code = int(data.get("code", -1))
        except (TypeError, ValueError):
            code = -1
        if code != 0:
            raise _error_from_envelope(data)
        return data

    @staticmethod
    def _map_job(detail: dict[str, Any]) -> dict[str, Any]:
        # printProgress es un ratio 0.0-1.0 (§5.2); estimatedTime YA es el restante.
        ratio = _to_float(detail.get("printProgress"))
        progress = max(0.0, min(100.0, ratio * 100.0))
        remaining_s = max(0.0, _to_float(detail.get("estimatedTime")))
        return {
            "name": str(detail.get("printFileName") or ""),
            "progress": round(progress, 1),
            "remaining_min": int(round(remaining_s / 60.0)),
            "remaining_s": int(round(remaining_s)),
            "elapsed_s": _to_int(detail.get("printDuration")),
            "layer": _to_int(detail.get("printLayer")),
            "total_layers": _to_int(detail.get("targetPrintLayer")),
        }

    def _map_status(self, detail: dict[str, Any]) -> dict[str, Any]:
        """Traduce ``/detail`` a la estructura que consume el dashboard."""
        speed = _to_int(detail.get("printSpeedAdjust"), -1)
        if speed < 0:
            speed = _to_int(detail.get("currentPrintSpeed"))
        return {
            "state": str(detail.get("status") or "unknown"),
            "nozzle": {
                "current": round(_to_float(detail.get("rightTemp")), 1),
                "target": round(_to_float(detail.get("rightTargetTemp")), 1),
            },
            "bed": {
                "current": round(_to_float(detail.get("platTemp")), 1),
                "target": round(_to_float(detail.get("platTargetTemp")), 1),
            },
            "job": self._map_job(detail),
            "error_code": str(detail.get("errorCode") or ""),
            "pid": _parse_pid(detail.get("pid")),
            "machine": str(detail.get("name") or ""),
            "firmware": str(detail.get("firmwareVersion") or ""),
            "has_mat_station": _truthy(detail.get("hasMatlStation")),
            # Controles del módulo 1 (valores vivos para el panel).
            "speed": speed,
            "speed_current": _to_int(detail.get("currentPrintSpeed")),
            "z_offset": round(_to_float(detail.get("zAxisCompensation")), 2),
            "fan_part": _to_int(detail.get("coolingFanSpeed")),
            "fan_aux": _to_int(detail.get("coolingFanLeftSpeed")),
            "light": str(detail.get("lightStatus") or ""),
            "shutdown_minutes": (
                _to_int(detail.get("autoShutdownTime"))
                if str(detail.get("autoShutdown") or "").lower() == "open" else 0
            ),
            # cameraStreamUrl vacío no descarta la cámara: se usa el fallback 8080.
            # Ojo: en la AD5X real `camera` puede valer 2 sin módulo de cámara
            # (y el 8080 cerrado), así que solo 1 cuenta como "hay cámara".
            "camera_available": bool(str(detail.get("cameraStreamUrl") or "").strip())
            or _to_int(detail.get("camera")) == 1,
        }

    @staticmethod
    def _map_ifs(detail: dict[str, Any]) -> dict[str, Any]:
        """``detail.matlStationInfo`` → slots 0-based (slotId del firmware es 1-4)."""
        info = detail.get("matlStationInfo")
        if not isinstance(info, dict):
            info = {}
        slots: list[dict[str, Any]] = []
        for position, entry in enumerate(info.get("slotInfos") or []):
            if not isinstance(entry, dict):
                continue
            slot_id = _to_int(entry.get("slotId"), position + 1)
            slots.append({
                "index": max(0, slot_id - 1),
                "slot_id": slot_id,
                "material": str(entry.get("materialName") or ""),
                "color": normalize_color(entry.get("materialColor"), strict=False),
                "present": bool(entry.get("hasFilament")),
            })
        slots.sort(key=lambda item: item["slot_id"])
        return {
            "slots": slots,
            "active_slot": _to_int(info.get("currentSlot")),
            "state_action": _to_int(info.get("stateAction")),
            "slot_count": _to_int(info.get("slotCnt"), len(slots)),
            "has_station": _truthy(detail.get("hasMatlStation")) or bool(slots),
        }

    # -- API pública --------------------------------------------------------

    def test_connection(self) -> dict[str, Any]:
        """Valida serial + check code con ``POST /checkCode`` (§5.1).

        Ojo con la regla F4 (§3.4), confirmada en hardware: ``/checkCode``
        devuelve ``code 0`` con un check code válido **aunque el serial no
        coincida**. El serial lo valida ``/detail``, que es lo que sondea el
        dashboard; esta sonda sirve para comprobar el Printer ID.
        """
        if self.mock:
            return {"ok": True, "mock": True, "message": "Modo demo: no se abre ninguna conexión."}
        data = self._call("/checkCode")
        return {
            "ok": True,
            "mock": False,
            "code": _to_int(data.get("code"), 0),
            "message": "Check code válido.",
        }

    def get_status(self) -> dict[str, Any]:
        """Estado general: ``{state, nozzle, bed, job, error_code, ...}``.

        ``nozzle`` / ``bed``: ``{current: float, target: float}``.
        ``job``: ``{name, progress, remaining_min, layer, total_layers}``.
        """
        if self.mock:
            return self._mock_snapshot()
        return self._map_status(self._detail())

    def get_temperatures(self) -> dict[str, Any]:
        """Temperaturas actuales/objetivo de boquilla y cama."""
        if self.mock:
            snapshot = self._mock_snapshot()
            return {"nozzle": snapshot["nozzle"], "bed": snapshot["bed"], "mock": True}
        status = self._map_status(self._detail())
        return {"nozzle": status["nozzle"], "bed": status["bed"], "mock": False}

    def get_job_info(self) -> dict[str, Any]:
        """Trabajo en curso (nombre, progreso, tiempo restante, capas)."""
        if self.mock:
            snapshot = self._mock_snapshot()
            return {
                "job": snapshot["job"],
                "state": snapshot["state"],
                "error_code": snapshot["error_code"],
                "mock": True,
            }
        status = self._map_status(self._detail())
        return {
            "job": status["job"],
            "state": status["state"],
            "error_code": status["error_code"],
            "mock": False,
        }

    def get_ifs_slots(self) -> dict[str, Any]:
        """Slots de la estación de materiales IFS (una petición a ``/detail``).

        Devuelve ``{slots: [{index(0-3), slot_id(1-4), material, color, present}],
        active_slot(1-4), has_station}``.
        """
        if self.mock:
            return self._mock_ifs()
        return self._map_ifs(self._detail())

    def get_snapshot(self) -> dict[str, Any]:
        """Estado + IFS en **una sola** petición (lo que usa el hilo de sondeo)."""
        if self.mock:
            return {
                "status": self._mock_snapshot(),
                "ifs": self._mock_ifs(),
                "camera_url": "",
                "mock": True,
            }
        detail = self._detail()
        return {
            "status": self._map_status(detail),
            "ifs": self._map_ifs(detail),
            "camera_url": self.get_camera_url(),
            "mock": False,
        }

    def set_slot_material(self, slot: int, material: str, color: str) -> dict[str, Any]:
        """Asigna material/color a un slot IFS con ``msConfig_cmd`` (§7.2).

        ``slot`` es 0-based en esta API (0-3) y se convierte al ``slotId``
        1-based del firmware. ``color`` se normaliza a ``#RRGGBB`` mayúsculas.
        """
        try:
            index = int(slot)
        except (TypeError, ValueError):
            raise FlashForgeError("invalid", f"Slot inválido: {slot!r}") from None
        if not 0 <= index < 4:
            raise FlashForgeError("invalid", f"Slot fuera de rango: {index}")

        material = str(material or "").strip()[:32]
        if not material:
            raise FlashForgeError("invalid", "El material no puede estar vacío.")
        color = normalize_color(color)

        if self.mock:
            entry = self._mock_slots[index]
            entry["material"] = material
            entry["color"] = color
            entry["present"] = bool(material)
            return {"ok": True, "slot": dict(entry), "in_palette": color in _PALETTE_HEX,
                    "mock": True}

        slot_id = index + 1
        self._call("/control", {
            "payload": {
                "cmd": "msConfig_cmd",
                "args": {"slot": slot_id, "mt": material, "rgb": color},
            }
        })
        return {
            "ok": True,
            "slot": {
                "index": index,
                "slot_id": slot_id,
                "material": material,
                "color": color,
                "present": True,
            },
            "in_palette": color in _PALETTE_HEX,
            "mock": False,
        }

    def _ms_cmd(self, slot: int, action: int) -> dict[str, Any]:
        """``ms_cmd`` load/unload (§7.3): ``slot`` 0-based en el plugin, 1-4 en la API."""
        try:
            index = int(slot)
        except (TypeError, ValueError):
            raise FlashForgeError("invalid", f"Slot inválido: {slot!r}") from None
        if not 0 <= index < 4:
            raise FlashForgeError("invalid", f"Slot fuera de rango: {index}")
        if action not in (0, 1):
            raise FlashForgeError("invalid", f"Acción ms_cmd no válida: {action}")
        if self.mock:
            entry = self._mock_slots[index]
            entry["present"] = action == 0
            return {"ok": True, "slot": index + 1, "action": action, "mock": True}
        self._control("ms_cmd", {"slot": index + 1, "action": action})
        return {"ok": True, "slot": index + 1, "action": action, "mock": False}

    def load_filament(self, slot: int) -> dict[str, Any]:
        """Carga filamento del slot (``ms_cmd`` action 0)."""
        return self._ms_cmd(slot, 0)

    def unload_filament(self, slot: int) -> dict[str, Any]:
        """Descarga filamento (``ms_cmd`` action 1)."""
        return self._ms_cmd(slot, 1)

    def send_control(self, command: str) -> dict[str, Any]:
        """Envía una orden con ``jobCtl_cmd`` (§6.3): pause / resume / cancel."""
        actions = {"pause": "pause", "resume": "continue", "cancel": "cancel"}
        if command not in actions:
            raise FlashForgeError("invalid", f"Orden desconocida: {command!r}")

        if self.mock:
            if command == "pause" and self._mock_state == "printing":
                self._mock_state = "paused"
            elif command == "resume" and self._mock_state == "paused":
                self._mock_state = "printing"
                self._mock_last_tick = time.monotonic()
            elif command == "cancel":
                self._mock_state = "cancelled"
                self._mock_progress = 0.0
            return {"ok": True, "command": command, "mock": True}

        action = actions[command]
        # /control confirma el despacho, no el efecto: el worker re-lee /detail.
        self._call("/control", {
            "payload": {"cmd": "jobCtl_cmd", "args": {"jobID": "", "action": action}}
        })
        return {"ok": True, "command": command, "command_sent": action, "mock": False}

    # -- módulo 1: control de la impresora --------------------------------

    def _control(self, cmd: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Envoltorio de ``/control`` (§5.4). Responde Success siempre: verificar."""
        payload: dict[str, Any] = {"cmd": cmd}
        if args is not None:
            payload["args"] = args
        return self._call("/control", {"payload": payload})

    def _temperature_targets(self) -> tuple[float, float]:
        detail = self._detail()
        return (
            _to_float(detail.get("rightTargetTemp")),
            _to_float(detail.get("platTargetTemp")),
        )

    def _confirm_temperature(
        self, nozzle: int | None, bed: int | None, timeout: float = 2.0
    ) -> bool:
        """Espera a que ``/detail`` refleje los nuevos objetivos (§6.5)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.7)
            try:
                current_nozzle, current_bed = self._temperature_targets()
            except FlashForgeError:
                return False
            nozzle_ok = nozzle is None or abs(current_nozzle - nozzle) <= 1.0
            bed_ok = bed is None or abs(current_bed - bed) <= 1.0
            if nozzle_ok and bed_ok:
                return True
        return False

    def set_temperatures(
        self, nozzle: int | None = None, bed: int | None = None
    ) -> dict[str, Any]:
        """Ajusta setpoints con ``temperatureCtl_cmd`` (§6.5).

        La referencia advierte que en AD5X el comando HTTP existe pero la
        librería de referencia usa TCP. Se implementa tal cual §6.5:
        HTTP primero y, si ``/detail`` no lo refleja en ~2 s, fallback por TCP
        ``~M104 S<temp>`` / ``~M140 S<temp>`` en el puerto 8899.
        """
        if nozzle is not None:
            nozzle = max(0, min(NOZZLE_MAX_TEMP, int(round(float(nozzle)))))
        if bed is not None:
            bed = max(0, min(BED_MAX_TEMP, int(round(float(bed)))))
        if nozzle is None and bed is None:
            raise FlashForgeError("invalid", "Indica al menos una temperatura.")

        if self.mock:
            if nozzle is not None:
                self._mock_nozzle_target = float(nozzle)
            if bed is not None:
                self._mock_bed_target = float(bed)
            return {"ok": True, "via": "mock", "nozzle": nozzle, "bed": bed,
                    "mock": True}

        args: dict[str, Any] = {}
        if nozzle is not None:
            args["rightNozzle"] = nozzle
        if bed is not None:
            args["platform"] = bed
        self._control("temperatureCtl_cmd", args)

        if self._confirm_temperature(nozzle, bed):
            return {"ok": True, "via": "http", "nozzle": nozzle, "bed": bed,
                    "message": "Temperaturas aplicadas por HTTP."}

        # §6.5: fallback documentado por TCP para AD5X/5M.
        commands = []
        if nozzle is not None:
            commands.append(f"M104 S{nozzle}")
        if bed is not None:
            commands.append(f"M140 S{bed}")
        try:
            for command in commands:
                self._tcp_command(command)
        except FlashForgeError as exc:
            raise FlashForgeError(
                "tcp",
                "El comando HTTP se aceptó pero la impresora no cambió el objetivo; "
                f"el fallback TCP 8899 también falló: {exc}",
            ) from exc
        return {"ok": True, "via": "tcp", "nozzle": nozzle, "bed": bed,
                "message": "Temperaturas aplicadas por TCP 8899 (fallback de AD5X)."}

    def set_print_speed(self, percent: int) -> dict[str, Any]:
        """Velocidad de impresión en vivo (``printerCtl_cmd.speed``, 50–500)."""
        speed = max(SPEED_MIN, min(SPEED_MAX, int(round(float(percent)))))
        if self.mock:
            self._mock_speed = speed
            return {"ok": True, "speed": speed, "mock": True}
        self._control("printerCtl_cmd", {"speed": speed})
        return {"ok": True, "speed": speed, "mock": False}

    def set_z_offset(self, offset: float) -> dict[str, Any]:
        """Compensación Z en mm (``printerCtl_cmd.zAxisCompensation``, -5..+5)."""
        value = max(Z_OFFSET_MIN, min(Z_OFFSET_MAX, round(float(offset), 2)))
        if self.mock:
            self._mock_z_offset = value
            return {"ok": True, "z_offset": value, "mock": True}
        self._control("printerCtl_cmd", {"zAxisCompensation": value})
        return {"ok": True, "z_offset": value, "mock": False}

    def set_fans(self, part: int | None = None, aux: int | None = None) -> dict[str, Any]:
        """Ventiladores de pieza y auxiliar en % (``printerCtl_cmd``)."""
        args: dict[str, Any] = {}
        if part is not None:
            args["coolingFan"] = max(0, min(FAN_MAX_PERCENT, int(round(float(part)))))
        if aux is not None:
            args["coolingLeftFan"] = max(0, min(FAN_MAX_PERCENT, int(round(float(aux)))))
        if not args:
            raise FlashForgeError("invalid", "Indica al menos un ventilador.")
        if self.mock:
            if "coolingFan" in args:
                self._mock_fan_part = args["coolingFan"]
            if "coolingLeftFan" in args:
                self._mock_fan_aux = args["coolingLeftFan"]
            return {"ok": True, **args, "mock": True}
        self._control("printerCtl_cmd", args)
        return {"ok": True, **args, "mock": False}

    def set_led(self, on: bool) -> dict[str, Any]:
        """LED de la impresora (``lightControl_cmd``, §6.2)."""
        status = "open" if on else "close"
        if self.mock:
            self._mock_light = status
            return {"ok": True, "light": status, "mock": True}
        self._control("lightControl_cmd", {"status": status})
        return {"ok": True, "light": status, "mock": False}

    # -- módulo 2: archivos -----------------------------------------------

    @staticmethod
    def _map_tools(info: dict[str, Any]) -> list[dict[str, Any]]:
        """``gcodeToolDatas`` → lista compacta para la UI (tool → slot)."""
        tools: list[dict[str, Any]] = []
        for tool in info.get("gcodeToolDatas") or []:
            if not isinstance(tool, dict):
                continue
            tools.append({
                "tool_id": _to_int(tool.get("toolId")),
                "slot_id": _to_int(tool.get("slotId")),
                "material": str(tool.get("materialName") or ""),
                "color": normalize_color(tool.get("materialColor"), strict=False),
            })
        return tools

    def get_file_tools(self, filename: str) -> list[dict[str, Any]]:
        """Tools/colores que pide un archivo guardado (mapeo tool→slot).

        Una sola petición a ``/gcodeList``; la UI lo usa para el indicador AMS
        del trabajo en curso y para los puntos de color de la lista de archivos.
        """
        if self.mock or not filename:
            return []
        data = self._call("/gcodeList")
        for entry in data.get("gcodeListDetail") or []:
            if isinstance(entry, dict) and str(entry.get("gcodeFileName")) == filename:
                return self._map_tools(entry)
        return []

    def list_files(self, *, with_thumbs: bool = True, thumb_limit: int = 12) -> dict[str, Any]:
        """Lista de trabajos en ``/data`` (§5.5) con miniaturas opcionales."""
        if self.mock:
            files = [dict(entry) for entry in self._mock_file_list()]
            return {"files": files, "mock": True}

        data = self._call("/gcodeList")
        names = data.get("gcodeList") or []
        details = {}
        for entry in data.get("gcodeListDetail") or []:
            if isinstance(entry, dict) and entry.get("gcodeFileName"):
                details[str(entry["gcodeFileName"])] = entry

        files: list[dict[str, Any]] = []
        for name in names:
            info = details.get(str(name), {})
            tools = info.get("gcodeToolDatas") or []
            files.append({
                "name": str(name),
                "printing_time_s": _to_int(info.get("printingTime")),
                "filament_g": round(_to_float(info.get("totalFilamentWeight")), 1),
                "use_station": _truthy(info.get("useMatlStation")),
                "tool_count": _to_int(info.get("gcodeToolCnt"), len(tools) if tools else 0),
                "tools": self._map_tools(info),
                "thumb": "",
            })

        if with_thumbs:
            for entry in files[:max(0, int(thumb_limit))]:
                try:
                    entry["thumb"] = self.get_file_thumb(entry["name"])
                except FlashForgeError as exc:
                    _log(f"sin miniatura para {entry['name']}: {exc}")
        return {"files": files, "mock": False}

    def get_file_thumb(self, filename: str) -> str:
        """Miniatura de un archivo en ``data:image/...;base64`` (``/gcodeThumb``)."""
        if self.mock:
            return ""
        data = self._call("/gcodeThumb", {"fileName": str(filename)})
        payload = str(data.get("imageData") or "").strip()
        if not payload:
            raise FlashForgeError("file", f"El archivo {filename!r} no tiene miniatura.")
        return f"data:image/png;base64,{payload}"

    def get_current_thumb(self) -> str:
        """Miniatura del trabajo en curso con ``GET /getThum`` (BMP, §5.7).

        Respaldo cuando ``/gcodeThumb`` no aplica (el firmware solo devuelve
        miniatura del trabajo actual, y sin trabajo responde
        ``{"code":-1,"message":"thumbnail error"}``).
        """
        if self.mock:
            return ""
        status, content_type, body = self._get_raw("/getThum")
        if status != 200 or not body:
            return ""
        if "json" in content_type.lower() or body[:1] in (b"{", b"["):
            return ""  # {"code":-1,"message":"thumbnail error"}
        if content_type.lower().startswith("image/bmp") or body[:2] == b"BM":
            encoded = base64.b64encode(body).decode("ascii")
            return f"data:image/bmp;base64,{encoded}"
        return ""

    def _find_file_detail(self, filename: str) -> dict[str, Any]:
        data = self._call("/gcodeList")
        for entry in data.get("gcodeListDetail") or []:
            if isinstance(entry, dict) and str(entry.get("gcodeFileName")) == filename:
                return entry
        if filename in (data.get("gcodeList") or []):
            return {}
        raise FlashForgeError("file", f"El archivo {filename!r} no está en la impresora.")

    def _build_mappings(self, file_detail: dict[str, Any]) -> list[dict[str, Any]]:
        """Mapea tools del gcode (0-based) a slots IFS (1-based) (§7.5).

        Heurística: mismo ``materialName`` en un slot con filamento; si no hay
        coincidencia, se reparten los slots en orden. El color del tool manda
        sobre el del slot, como hace Flash Studio.
        """
        tools = [t for t in (file_detail.get("gcodeToolDatas") or []) if isinstance(t, dict)]
        if not tools:
            return []
        slots = self._last_slots
        if not slots:
            try:
                slots = self.get_ifs_slots()["slots"]
            except FlashForgeError:
                slots = []
        if not slots:
            raise FlashForgeError(
                "invalid",
                "El archivo usa la estación IFS pero no se han leído los slots.",
            )

        used: set[str] = set()
        mappings: list[dict[str, Any]] = []
        for index, tool in enumerate(tools):
            material = str(tool.get("materialName") or "").strip()
            candidates = [
                slot for slot in slots
                if slot.get("present") and slot.get("slot_id") not in used
            ]
            match = next(
                (slot for slot in candidates
                 if material and str(slot.get("material") or "").lower() == material.lower()),
                None,
            )
            if match is None and candidates:
                match = candidates[index % len(candidates)]
            if match is None:
                match = slots[index % len(slots)]
            used.add(match.get("slot_id"))
            tool_color = normalize_color(tool.get("materialColor"), strict=False) \
                or str(match.get("color") or "#FFFFFF")
            slot_color = str(match.get("color") or tool_color)
            mappings.append({
                "toolId": _to_int(tool.get("toolId"), index),
                "slotId": int(match.get("slot_id") or 1),
                "materialName": material or str(match.get("material") or ""),
                "toolMaterialColor": tool_color,
                "slotMaterialColor": slot_color,
            })
        return mappings

    def start_file(
        self,
        filename: str,
        *,
        leveling: bool = True,
        mappings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Imprime un archivo ya guardado con ``/printGcode`` (§5.8)."""
        name = str(filename or "").strip()
        if not name:
            raise FlashForgeError("invalid", "Falta el nombre del archivo.")
        if self.mock:
            self._mock_state = "printing"
            self._mock_progress = 0.0
            self._mock_job_name = name
            return {"ok": True, "file": name, "mock": True}

        detail = self._find_file_detail(name)
        use_station = _truthy(detail.get("useMatlStation"))
        tool_mappings = mappings if mappings is not None else (
            self._build_mappings(detail) if use_station else []
        )
        payload: dict[str, Any] = {
            "fileName": name,
            "levelingBeforePrint": bool(leveling),
            "firstLayerInspection": False,
            "flowCalibration": False,
            "timeLapseVideo": False,
            "useMatlStation": bool(tool_mappings),
            "gcodeToolCnt": len(tool_mappings),
            "materialMappings": tool_mappings,
        }
        self._call("/printGcode", payload)
        return {
            "ok": True,
            "file": name,
            "use_station": bool(tool_mappings),
            "tool_count": len(tool_mappings),
            "mock": False,
        }

    def upload_file(
        self,
        file_path: str,
        *,
        print_now: bool = False,
        leveling: bool = True,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Sube un .gcode/.3mf con ``/uploadGcode`` (multipart, auth por cabeceras)."""
        path = os.path.expanduser(str(file_path or "").strip())
        if not path:
            raise FlashForgeError("invalid", "Indica la ruta del archivo.")
        if not os.path.isfile(path):
            raise FlashForgeError("file", f"No existe el archivo {path!r}.")
        extension = os.path.splitext(path)[1].lower()
        if extension not in UPLOAD_EXTENSIONS:
            raise FlashForgeError(
                "invalid",
                f"Extensión {extension or '(sin extensión)'} no admitida. "
                f"Usa: {', '.join(UPLOAD_EXTENSIONS)}.",
            )
        size = os.path.getsize(path)
        if size <= 0:
            raise FlashForgeError("file", "El archivo está vacío.")
        if size > MAX_UPLOAD_BYTES:
            raise FlashForgeError(
                "file",
                f"El archivo ocupa {size / (1024 * 1024):.1f} MB; el límite es "
                f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
            )

        if self.mock:
            return {"ok": True, "file": os.path.basename(path), "size": size,
                    "print_now": bool(print_now), "mock": True}

        headers = {
            "serialNumber": self.serial,
            "checkCode": self.check_code,
            "fileSize": str(size),
            "printNow": "true" if print_now else "false",
            "levelingBeforePrint": "true" if leveling else "false",
            "flowCalibration": "false",
            "firstLayerInspection": "false",
            "timeLapseVideo": "false",
            "useMatlStation": "false",
            "gcodeToolCnt": "0",
            # §5.9: aquí va base64 de un JSON array (en /printGcode va normal).
            "materialMappings": base64.b64encode(b"[]").decode("ascii"),
        }
        self._upload_stream("/uploadGcode", path, headers, progress=progress)
        return {
            "ok": True,
            "file": os.path.basename(path),
            "size": size,
            "print_now": bool(print_now),
            "mock": False,
        }

    # -- módulo 3: movimiento y mantenimiento ------------------------------

    def home_axes(self) -> dict[str, Any]:
        """Home de todos los ejes (``homingCtrl_cmd``, solo AD5X)."""
        if self.mock:
            return {"ok": True, "mock": True}
        self._control("homingCtrl_cmd")
        return {"ok": True, "mock": False}

    def clear_platform(self) -> dict[str, Any]:
        """Cierra el diálogo de fin de impresión (``stateCtrl_cmd`` §6.7)."""
        if self.mock:
            self._mock_state = "ready"
            self._mock_progress = 0.0
            self._mock_job_name = "—"
            return {"ok": True, "mock": True}
        self._control("stateCtrl_cmd", {"action": "setClearPlatform"})
        return {"ok": True, "mock": False}

    def move_axis(self, axis: str, delta: float) -> dict[str, Any]:
        """Mueve un eje ``x``/``y``/``z`` ``delta`` mm (``moveCtrl_cmd``)."""
        name = str(axis or "").strip().lower()
        if name not in ("x", "y", "z"):
            raise FlashForgeError("invalid", f"Eje no válido: {axis!r}")
        distance = max(-200.0, min(200.0, round(float(delta), 2)))
        if distance == 0:
            raise FlashForgeError("invalid", "La distancia no puede ser 0.")
        if self.mock:
            return {"ok": True, "axis": name, "delta": distance, "mock": True}
        self._control("moveCtrl_cmd", {"axis": name, "delta": distance})
        return {"ok": True, "axis": name, "delta": distance, "mock": False}

    def extrude_filament(self, delta: float) -> dict[str, Any]:
        """Extruye (>0) o retrae (<0) ``delta`` mm (``extrudeCtrl_cmd``)."""
        distance = max(-100.0, min(100.0, round(float(delta), 2)))
        if distance == 0:
            raise FlashForgeError("invalid", "La distancia no puede ser 0.")
        if self.mock:
            return {"ok": True, "delta": distance, "mock": True}
        self._control("extrudeCtrl_cmd", {"axis": "e", "delta": distance})
        return {"ok": True, "delta": distance, "mock": False}

    def start_calibration(self, leveling: bool = True, vibration: bool = False) -> dict[str, Any]:
        """Nivelado / input shaper (``calibration_cmd``, §6.6).

        Ambos campos son obligatorios y solo el string exacto ``open`` activa.
        """
        if not leveling and not vibration:
            raise FlashForgeError("invalid", "Selecciona al menos una calibración.")
        args = {
            "levelingDetection": "open" if leveling else "close",
            "vibrationCompensation": "open" if vibration else "close",
        }
        if self.mock:
            return {"ok": True, **args, "mock": True}
        self._control("calibration_cmd", args)
        return {"ok": True, **args, "mock": False}

    def clear_errors(self, code: str | None = None) -> dict[str, Any]:
        """Limpia el error activo (``errorCodeCtrl_cmd``, §6.12).

        El firmware exige el ``errorCode`` exacto de ``/detail``.
        """
        error_code = str(code or self._last_error_code or "").strip()
        if not error_code:
            raise FlashForgeError("invalid", "No hay ningún error activo que limpiar.")
        if self.mock:
            self._mock_error = ""
            return {"ok": True, "error_code": error_code, "mock": True}
        self._control("errorCodeCtrl_cmd",
                      {"action": "clearErrorCode", "errorCode": error_code})
        return {"ok": True, "error_code": error_code, "mock": False}

    def set_auto_shutdown(self, minutes: int) -> dict[str, Any]:
        """Auto-apagado (``delayClose_cmd``, §6.8). ``0`` = desactivar."""
        value = max(0, min(1440, int(round(float(minutes)))))
        args = {
            "automaticShutdown": "open" if value > 0 else "close",
            "shutdownAfterTime": value,
        }
        if self.mock:
            self._mock_shutdown = value
            return {"ok": True, "minutes": value, "mock": True}
        self._control("delayClose_cmd", args)
        return {"ok": True, "minutes": value, "mock": False}

    def get_camera_url(self) -> str:
        """URL del stream MJPEG: la que anuncia ``/detail`` o el fallback 8080."""
        if self.mock or not self.host:
            return ""
        if self._camera_url:
            return self._camera_url
        hostname, _ = split_host_port(self.host)
        return f"http://{hostname}:{CAMERA_PORT}{CAMERA_PATH}"

    # -- datos simulados ----------------------------------------------------

    def _mock_file_list(self) -> list[dict[str, Any]]:
        """Archivos falsos para el modo demo (mismo formato que `list_files`)."""
        return [
            {"name": "Benchy_0.20mm_PLA.gx", "printing_time_s": 3720,
             "filament_g": 12.4, "use_station": False, "tool_count": 1, "thumb": "",
             "tools": [{"tool_id": 0, "slot_id": 1, "material": "PLA",
                        "color": "#F72224"}]},
            {"name": "Multicolor_llavero.3mf", "printing_time_s": 5400,
             "filament_g": 24.8, "use_station": True, "tool_count": 2, "thumb": "",
             "tools": [
                 {"tool_id": 0, "slot_id": 1, "material": "PLA", "color": "#F72224"},
                 {"tool_id": 1, "slot_id": 3, "material": "PETG", "color": "#2750E0"},
             ]},
            {"name": "Soporte_PETG.gcode", "printing_time_s": 7200,
             "filament_g": 41.0, "use_station": False, "tool_count": 1, "thumb": "",
             "tools": [{"tool_id": 0, "slot_id": 3, "material": "PETG",
                        "color": "#2750E0"}]},
        ]

    def _mock_ifs(self) -> dict[str, Any]:
        """Slots IFS falsos con el mismo formato que `_map_ifs`."""
        return {
            "slots": [dict(slot) for slot in self._mock_slots],
            "active_slot": 1,
            "state_action": 0,
            "slot_count": 4,
            "has_station": True,
            "mock": True,
        }

    def _mock_snapshot(self) -> dict[str, Any]:
        """Genera un estado plausible y cambiante para el modo demo."""
        now = time.monotonic()
        delta = max(0.0, min(10.0, now - self._mock_last_tick))
        self._mock_last_tick = now
        if self._mock_state == "printing":
            # 0.125 %/s ≈ 7.5 %/min: el progreso se mueve mientras se mira.
            self._mock_progress = min(99.0, self._mock_progress + delta * 0.125)

        elapsed = now - self._mock_started
        wobble = math.sin(elapsed / 7.0)

        base: dict[str, Any] = {
            "error_code": self._mock_error,
            "pid": 38,
            "machine": "AD5X (demo)",
            "firmware": "demo",
            "has_mat_station": True,
            "camera_available": False,
            "speed": self._mock_speed,
            "speed_current": self._mock_speed,
            "z_offset": self._mock_z_offset,
            "fan_part": self._mock_fan_part,
            "fan_aux": self._mock_fan_aux,
            "light": self._mock_light,
            "shutdown_minutes": self._mock_shutdown,
        }

        if self._mock_state in ("cancelled", "idle"):
            return {
                **base,
                "state": self._mock_state,
                "nozzle": {"current": round(38.0 + 1.5 * wobble, 1),
                           "target": 0.0 if self._mock_state == "idle" else self._mock_nozzle_target},
                "bed": {"current": round(29.0 + 0.3 * wobble, 1),
                        "target": 0.0 if self._mock_state == "idle" else self._mock_bed_target},
                "job": {
                    "name": "—",
                    "progress": 0.0,
                    "remaining_min": 0,
                    "remaining_s": 0,
                    "elapsed_s": 0,
                    "layer": 0,
                    "total_layers": 0,
                },
            }

        paused = self._mock_state == "paused"
        progress = self._mock_progress
        total_minutes = 120.0
        remaining = max(1, int(round((100.0 - progress) / 100.0 * total_minutes)))
        layers_total = 280

        return {
            **base,
            "state": self._mock_state,
            "nozzle": {
                "current": round(self._mock_nozzle_target
                                 + (0.0 if paused else 1.6 * wobble)
                                 - (6.0 if paused else 0.0), 1),
                "target": self._mock_nozzle_target,
            },
            "bed": {
                "current": round(self._mock_bed_target
                                 + (0.0 if paused else 0.6 * math.sin(elapsed / 11.0)), 1),
                "target": self._mock_bed_target,
            },
            "job": {
                "name": self._mock_job_name,
                "progress": round(progress, 1),
                "remaining_min": remaining,
                "remaining_s": remaining * 60,
                "elapsed_s": int(elapsed),
                "layer": int(progress / 100.0 * layers_total),
                "total_layers": layers_total,
            },
        }


# ---------------------------------------------------------------------------
# Descubrimiento UDP (§9)
# ---------------------------------------------------------------------------


def _pid_model(pid: int) -> str:
    """Modelo a partir del PID del anuncio (§9.2): 0x0026 = AD5X."""
    return {
        0x0026: "AD5X",
        0x0023: "Adventurer 5M",
        0x0024: "Adventurer 5M Pro",
    }.get(pid, f"PID 0x{pid:04X}")


def parse_discovery_packet(data: bytes, ip: str) -> dict[str, Any] | None:
    """Parsea la respuesta de 276 B del descubrimiento (§9.2)."""
    if len(data) < 276:
        return None

    def _text(start: int, size: int) -> str:
        return data[start:start + size].split(b"\0")[0].decode("utf-8", "replace").strip()

    try:
        pid = struct.unpack(">H", data[0x88:0x8A])[0]
        http_port = struct.unpack(">H", data[0x8E:0x90])[0]
    except struct.error:
        return None
    return {
        "ip": ip,
        "name": _text(0x00, 0x80),
        "serial": _text(0x92, 0x80),
        "pid": pid,
        "model": _pid_model(pid),
        "http_port": http_port or API_PORT,
    }


def discover_printers(
    timeout: float = DISCOVERY_TIMEOUT,
    targets: "list[tuple[str, int]] | tuple[tuple[str, int], ...] | None" = None,
) -> list[dict[str, Any]]:
    """Broadcast/multicast UDP de un solo intento (§9.1).

    No requiere credenciales. Lanza :class:`FlashForgeError` si el host bloquea
    los sockets (audit hook / sandbox) o si la red falla. Devuelve una lista
    vacía si simplemente no responde nadie.
    """
    destinations = list(targets if targets is not None else DISCOVERY_TARGETS)
    found: dict[str, dict[str, Any]] = {}
    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # Sin bind(): las respuestas UDP vuelven solas al puerto efímero de
        # origen, así el plugin NO abre ningún puerto local (perfil de
        # seguridad solo-conexiones-de-salida; verificado en hardware).
        for host, port in destinations:
            try:
                sock.sendto(b"", (host, port))
            except OSError as exc:
                # Un destino puede fallar (p.ej. multicast sin ruta); probar el otro.
                _log(f"descubrimiento: no se pudo enviar a {host}:{port}: {exc}")
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, (ip, _port) = sock.recvfrom(2048)
            except TimeoutError:
                break
            parsed = parse_discovery_packet(data, ip)
            if parsed and parsed["serial"]:
                found.setdefault(ip, parsed)
    except PermissionError as exc:
        raise FlashForgeError(
            "permission",
            "OrcaSlicer bloqueó el descubrimiento UDP (broadcast). "
            "Acepta el permiso de red o introduce la IP manualmente.",
        ) from exc
    except OSError as exc:
        raise FlashForgeError(
            "network", f"No se pudo hacer el descubrimiento UDP: {exc}"
        ) from exc
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    return sorted(found.values(), key=lambda item: item["ip"])


# ---------------------------------------------------------------------------
# HTML del dashboard
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>FlashForge Monitor</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    min-height: 100vh;
    padding: 14px 16px 32px;
    background: var(--orca-bg, #1c1d21);
    color: var(--orca-fg, #e7e7ea);
    font-family: var(--orca-font, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif);
    font-size: 14px;
    line-height: 1.45;
    --surface: rgba(127, 127, 127, .07);
    --surface-2: rgba(127, 127, 127, .12);
    --surface-3: rgba(127, 127, 127, .18);
    --line: var(--orca-border, #3a3d45);
    --muted: color-mix(in srgb, var(--orca-fg, #e7e7ea) 65%, var(--orca-bg, #1c1d21));
    --accent: var(--orca-accent, #3b82f6);
    --accent-fg: var(--orca-accent-fg, #fff);
    --ok: #2fbf71;
    --warn: #e0a02a;
    --bad: #e05a55;
    --shadow: 0 8px 24px rgba(0, 0, 0, .22);
    --gap: 14px;
    --pad: 14px;
  }
  ::-webkit-scrollbar { width: 9px; height: 9px; }
  ::-webkit-scrollbar-thumb { background: var(--surface-3); border-radius: 99px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--muted); }
  ::-webkit-scrollbar-track { background: transparent; }
  button, input, select { font-family: inherit; }
  button { color: inherit; }
  h1, h2, h3 { margin: 0; font-weight: 650; }
  [hidden] { display: none !important; }
  .muted { color: var(--muted); font-size: 12px; }
  .mono { font-variant-numeric: tabular-nums; }
  .icon { display: inline-flex; width: 18px; height: 18px; flex: 0 0 auto; }
  .icon svg { width: 100%; height: 100%; }

  /* ---------- topbar ---------- */
  .topbar {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    flex-wrap: wrap; margin-bottom: 12px;
  }
  .brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .logo {
    width: 36px; height: 36px; border-radius: 10px; display: flex; align-items: center; justify-content: center;
    background: var(--accent); color: var(--accent-fg);
    box-shadow: 0 4px 12px color-mix(in srgb, var(--accent) 40%, transparent);
  }
  .logo .icon { width: 20px; height: 20px; }
  .brand-title { font-weight: 650; font-size: 15px; letter-spacing: .01em; }
  .brand-sub { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 6px; max-width: 52vw; }
  .brand-sub span:last-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .select {
    background: var(--surface); border: 1px solid var(--line); color: var(--orca-fg, #e7e7ea);
    border-radius: 9px; padding: 7px 10px; font-size: 12.5px; max-width: 220px;
  }
  .icon-btn {
    width: 36px; height: 36px; border-radius: 9px; border: 1px solid var(--line);
    background: var(--surface); display: inline-flex; align-items: center; justify-content: center;
    cursor: pointer; transition: background .15s, transform .15s, border-color .15s;
  }
  .icon-btn:hover { background: var(--surface-2); transform: translateY(-1px); }
  .icon-btn:active { transform: translateY(0); }
  .icon-btn.spinning .icon { animation: spin 1s linear infinite; }

  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--muted); flex: 0 0 auto; }
  .dot.on { background: var(--ok); box-shadow: 0 0 0 0 color-mix(in srgb, var(--ok) 60%, transparent); animation: pulse 2s infinite; }
  .dot.demo { background: var(--warn); }
  .dot.off { background: var(--bad); }
  .dot.wait { background: var(--accent); animation: pulse 1.2s infinite; }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 color-mix(in srgb, currentColor 55%, transparent); }
    70% { box-shadow: 0 0 0 6px transparent; }
    100% { box-shadow: 0 0 0 0 transparent; }
  }
  .dot.on { color: var(--ok); }
  .dot.wait { color: var(--accent); }
  .dot.demo { color: var(--warn); }
  .dot.off { color: var(--bad); }

  .badge {
    font-size: 10px; letter-spacing: .08em; font-weight: 700; padding: 3px 7px;
    border-radius: 999px; background: var(--warn); color: #241a02;
  }

  /* ---------- banner ---------- */
  .banner {
    display: none; align-items: center; justify-content: space-between; gap: 12px;
    padding: 9px 12px; border-radius: 10px; margin-bottom: 12px; font-size: 13px;
    border: 1px solid transparent;
  }
  .banner.show { display: flex; }
  .banner .msg { flex: 1 1 auto; }
  .banner.error { background: color-mix(in srgb, var(--bad) 14%, transparent); border-color: color-mix(in srgb, var(--bad) 45%, transparent); }
  .banner.warn { background: color-mix(in srgb, var(--warn) 15%, transparent); border-color: color-mix(in srgb, var(--warn) 45%, transparent); }
  .banner.info { background: color-mix(in srgb, var(--accent) 13%, transparent); border-color: color-mix(in srgb, var(--accent) 40%, transparent); }
  .banner.ok { background: color-mix(in srgb, var(--ok) 14%, transparent); border-color: color-mix(in srgb, var(--ok) 45%, transparent); }

  /* ---------- botones ---------- */
  .btn {
    display: inline-flex; align-items: center; gap: 7px; justify-content: center;
    border: 1px solid var(--line); background: var(--surface); color: var(--orca-fg, #e7e7ea);
    border-radius: 10px; padding: 8px 13px; font-size: 13px; font-weight: 550; cursor: pointer;
    transition: background .15s, transform .15s, border-color .15s, opacity .15s;
    white-space: nowrap;
  }
  .btn:hover:not(:disabled) { background: var(--surface-2); transform: translateY(-1px); }
  .btn:active:not(:disabled) { transform: translateY(0); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .btn.primary { background: var(--accent); border-color: transparent; color: var(--accent-fg); }
  .btn.primary:hover:not(:disabled) { background: color-mix(in srgb, var(--accent) 88%, #000); }
  .btn.danger { background: var(--bad); border-color: transparent; color: #fff; }
  .btn.danger-ghost { border-color: color-mix(in srgb, var(--bad) 55%, transparent); color: var(--bad); background: transparent; }
  .btn.danger-ghost:hover:not(:disabled) { background: color-mix(in srgb, var(--bad) 12%, transparent); }
  .btn.ghost { background: transparent; }
  .btn.sm { padding: 5px 9px; font-size: 12px; border-radius: 8px; }
  .btn.icon-only { padding: 8px; }
  .btn .icon { width: 16px; height: 16px; }
  .btn:focus-visible, .icon-btn:focus-visible, .sensor:focus-visible, .slot:focus-visible,
  .pill:focus-visible, input:focus-visible, select:focus-visible,
  .z-btn:focus-visible, .e-btn:focus-visible, .swatch:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 2px;
  }

  /* ---------- layout ---------- */
  .dash { display: flex; flex-direction: column; gap: var(--gap); }
  .row-top {
    display: grid; grid-template-columns: minmax(0, 70fr) minmax(320px, 30fr);
    gap: var(--gap); align-items: stretch;
  }
  .col-left {
    display: flex; flex-direction: column; gap: var(--gap); min-width: 0;
  }
  @media (max-width: 880px) {
    .row-top { grid-template-columns: 1fr; }
    .control-card { max-width: 480px; margin-left: auto; margin-right: auto; }
  }

  /* ---------- hero ---------- */
  .hero {
    display: grid; grid-template-columns: minmax(0, 38%) minmax(0, 1fr); gap: var(--gap);
    background: var(--surface); border: 1px solid var(--line); border-radius: 14px;
    padding: var(--pad); min-width: 0;
    background-image: linear-gradient(135deg, color-mix(in srgb, var(--accent) 7%, transparent), transparent 45%);
  }
  .hero.idle { grid-template-columns: 120px minmax(0, 1fr); gap: 12px; padding: 12px 14px; }
  .hero.idle .hero-thumb { width: 120px; height: 120px; min-height: 0; }
  .hero.idle .hero-thumb-empty .icon { width: 26px; height: 26px; }
  .hero.idle .hero-thumb-empty span:last-child { display: none; }
  .hero.idle .progress { display: none; }
  .hero.idle .hero-body { justify-content: center; gap: 8px; }
  .hero.idle .hero-title h1 { font-size: 20px; }
  .hero.idle .hero-actions { margin-top: 2px; }
  .hero.transient { grid-template-columns: 76px minmax(0, 1fr); gap: 16px; align-items: center; padding: 14px 16px; }
  .hero.transient .hero-thumb,
  .hero.transient .hero-body { display: none; }
  .hero-transient { grid-column: 1 / -1; display: flex; align-items: center; gap: 16px; min-width: 0; }
  .hero-transient[hidden] { display: none; }
  .transient-icon {
    width: 62px; height: 62px; border-radius: 16px; flex: 0 0 auto;
    display: flex; align-items: center; justify-content: center;
    color: var(--accent); background: color-mix(in srgb, var(--accent) 13%, var(--surface-2));
    animation: transient-pulse 1.8s ease-in-out infinite;
  }
  .transient-icon .icon { width: 30px; height: 30px; }
  .transient-body { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; gap: 7px; }
  .transient-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .transient-head h1 { font-size: 22px; line-height: 1.2; }
  .transient-text { margin: 0; font-size: 12.5px; color: var(--muted); }
  .transient-temps { display: flex; gap: 16px; flex-wrap: wrap; font-size: 13px; color: var(--muted); }
  .transient-temps b { color: var(--orca-fg, #e7e7ea); font-variant-numeric: tabular-nums; }
  .transient-temps b + span { color: var(--muted); }
  .transient-meter {
    height: 6px; border-radius: 999px; background: var(--surface-3); overflow: hidden;
    max-width: 360px;
  }
  .transient-meter[hidden] { display: none; }
  .transient-meter i {
    display: block; height: 100%; width: 34%; border-radius: 999px;
    background: linear-gradient(90deg, var(--accent), color-mix(in srgb, var(--accent) 55%, var(--orca-bg)));
    animation: transient-slide 1.35s ease-in-out infinite;
  }
  @keyframes transient-slide {
    0% { transform: translateX(-110%); }
    100% { transform: translateX(310%); }
  }
  @keyframes transient-pulse {
    0%, 100% { transform: scale(1); box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent) 20%, transparent); }
    50% { transform: scale(1.04); box-shadow: 0 0 0 7px color-mix(in srgb, var(--accent) 0%, transparent); }
  }
  @media (prefers-reduced-motion: reduce) {
    .hero.transient .transient-icon { animation: none; }
    .hero.transient .transient-meter i { animation: none; width: 64%; }
  }
  .hero.finished { grid-template-columns: 72px minmax(0, 1fr); gap: 16px; align-items: center; padding: 14px 16px; }
  .hero.finished .hero-thumb,
  .hero.finished .hero-body { display: none; }
  .hero-finished { grid-column: 1 / -1; display: flex; align-items: center; gap: 16px; min-width: 0; }
  .hero-finished[hidden] { display: none; }
  .finished-check {
    width: 58px; height: 58px; border-radius: 50%; flex: 0 0 auto;
    display: flex; align-items: center; justify-content: center;
    color: var(--ok); background: color-mix(in srgb, var(--ok) 16%, var(--surface-2));
    border: 1px solid color-mix(in srgb, var(--ok) 45%, transparent);
  }
  .finished-check .icon { width: 30px; height: 30px; }
  .finished-body { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; gap: 6px; }
  .finished-body h1 { font-size: 22px; line-height: 1.2; }
  .finished-time { margin: 0; font-size: 12.5px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .finished-time[hidden] { display: none; }
  .finished-text { margin: 0; font-size: 12.5px; color: var(--muted); }
  .finished-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 4px; }
  .finished-actions .btn { padding: 9px 16px; }
  @media (max-width: 720px) {
    .hero.finished { grid-template-columns: 48px minmax(0, 1fr); gap: 12px; }
    .finished-check { width: 48px; height: 48px; }
    .finished-check .icon { width: 25px; height: 25px; }
  }
  @media (max-width: 720px) {
    .hero { grid-template-columns: 104px minmax(0, 1fr); }
    .hero .hero-thumb { width: 104px; height: 104px; min-height: 0; }
    .hero .progress-pct { font-size: 24px; }
    .hero.idle { grid-template-columns: 96px minmax(0, 1fr); }
    .hero.idle .hero-thumb { width: 96px; height: 96px; }
  }
  .hero-thumb {
    position: relative; border-radius: 10px; overflow: hidden; background: var(--surface-2);
    min-height: 220px; height: 100%; display: flex; align-items: center; justify-content: center;
  }
  .hero-thumb img { width: 100%; height: 100%; object-fit: contain; display: none; }
  .hero-thumb.has-image img { display: block; }
  .hero-thumb-empty { display: flex; flex-direction: column; align-items: center; gap: 6px; color: var(--muted); font-size: 12px; }
  .hero-thumb.has-image .hero-thumb-empty { display: none; }
  .hero-thumb-empty .icon { width: 34px; height: 34px; opacity: .55; }
  .hero-body { display: flex; flex-direction: column; gap: 14px; min-width: 0; justify-content: center; }
  .hero-meta { margin: 0; }
  .hero-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
  .hero-title { min-width: 0; }
  .hero-title h1 {
    font-size: 24px; line-height: 1.2; overflow: hidden; overflow-wrap: anywhere;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  }
  .hero-sub { font-size: 12px; color: var(--muted); margin-top: 3px; }
  .chip {
    display: inline-flex; align-items: center; gap: 5px; font-size: 12px; font-weight: 600;
    padding: 4px 9px; border-radius: 999px; background: var(--surface-2); white-space: nowrap;
    border: 1px solid transparent;
  }
  .chip .dot { width: 7px; height: 7px; }
  .chip.printing { background: color-mix(in srgb, var(--ok) 18%, transparent); color: var(--ok); }
  .chip.paused { background: color-mix(in srgb, var(--warn) 20%, transparent); color: var(--warn); }
  .chip.error { background: color-mix(in srgb, var(--bad) 18%, transparent); color: var(--bad); }
  .chip.done { background: color-mix(in srgb, var(--accent) 18%, transparent); color: var(--accent); }
  .chip.idle { color: var(--muted); }

  .progress { display: flex; flex-direction: column; gap: 6px; }
  .bar { height: 8px; border-radius: 999px; background: var(--surface-3); overflow: hidden; }
  .bar > i { display: block; height: 100%; width: 0%; border-radius: 999px; background: var(--accent); transition: width .5s cubic-bezier(.2,.8,.2,1); }
  .bar.big { height: 16px; }
  .bar.big > i {
    background: linear-gradient(90deg, var(--accent), color-mix(in srgb, var(--accent) 65%, #fff));
  }
  .progress-row { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  .progress-pct { font-size: 30px; font-weight: 700; font-variant-numeric: tabular-nums; letter-spacing: -.02em; }
  .progress-row .muted { display: inline-flex; align-items: center; gap: 4px; }
  .hero-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 4px; }
  .hero-actions .btn { padding: 9px 16px; }

  /* ---------- meta chips ---------- */
  .meta-row { display: flex; gap: 8px; flex-wrap: wrap; }
  .meta-chip {
    display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted);
    background: var(--surface); border: 1px solid var(--line); border-radius: 999px; padding: 4px 10px;
  }
  .meta-chip .icon { width: 13px; height: 13px; }
  .meta-chip.error { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 45%, transparent); }
  .meta-chip .btn { padding: 2px 7px; font-size: 11px; }

  /* ---------- sensores ---------- */
  .sensors { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--gap); }
  .col-left > .sensors { flex: 1 1 auto; grid-auto-rows: minmax(0, 1fr); align-content: stretch; }
  .col-left .sensor { justify-content: flex-start; padding-bottom: 10px; }
  .col-left .sensor-value { margin-top: auto; }
  @media (max-width: 640px) { .sensors { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  .sensor {
    text-align: left; border: 1px solid var(--line); background: var(--surface);
    border-radius: 12px; padding: var(--pad); cursor: pointer; display: flex; flex-direction: column; gap: 6px;
    transition: background .15s, transform .15s, border-color .15s, box-shadow .15s; min-width: 0;
    position: relative; overflow: hidden;
  }
  .sensor:hover:not(:disabled) { background: var(--surface-2); transform: translateY(-2px); box-shadow: var(--shadow); }
  .sensor:disabled { opacity: .5; cursor: not-allowed; }
  .sensor-icon { color: var(--muted); }
  .sensor-icon .icon { width: 22px; height: 22px; }
  .sensor:hover .sensor-icon { color: var(--accent); }
  .sensor-label { font-size: 12px; letter-spacing: .07em; text-transform: uppercase; color: var(--muted); font-weight: 600; }
  .sensor-value { font-size: 30px; font-weight: 700; line-height: 1.05; font-variant-numeric: tabular-nums; letter-spacing: -.02em; }
  .sensor-value small { font-size: 14px; font-weight: 500; color: var(--muted); margin-left: 2px; }
  .sensor-target { font-size: 12px; color: var(--muted); }
  .sensor .edit-hint { position: absolute; top: 10px; right: 10px; opacity: 0; transition: opacity .15s; color: var(--muted); }
  .sensor:hover .edit-hint { opacity: 1; }
  .sensor .edit-hint .icon { width: 14px; height: 14px; }
  .sensor.hot .sensor-value b { color: #f97316; }
  .sensor.cool .sensor-value b { color: #38bdf8; }

  /* ---------- mini fila (z-offset / led) ---------- */
  .mini-row { display: flex; gap: var(--gap); flex-wrap: wrap; }
  .mini {
    display: inline-flex; align-items: center; gap: 8px; font-size: 12.5px;
    background: var(--surface); border: 1px solid var(--line); border-radius: 999px; padding: 6px 10px 6px 12px;
  }
  .mini .icon { width: 15px; height: 15px; color: var(--muted); }
  .mini b { font-variant-numeric: tabular-nums; }
  .mini .btn { padding: 3px 9px; font-size: 11.5px; }

  /* ---------- tarjetas / AMS ---------- */
  .card {
    background: var(--surface); border: 1px solid var(--line); border-radius: 12px;
    padding: var(--pad); min-width: 0;
  }
  .card-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 10px; }
  .card-head h2 { font-size: 13px; letter-spacing: .04em; text-transform: uppercase; color: var(--muted); }
  .ams { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
  @media (max-width: 640px) { .ams { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  .slot {
    position: relative; display: flex; flex-direction: column; align-items: center; gap: 6px;
    padding: 14px 10px 16px; border: 1px solid var(--line); border-radius: 12px; background: var(--surface-2);
    cursor: pointer; transition: transform .15s, border-color .15s, background .15s; min-width: 0;
  }
  .slot:hover { transform: translateY(-2px); background: var(--surface-3); }
  .slot.empty { opacity: .6; border-style: dashed; }
  .slot.active { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent) inset; }
  .slot-ring {
    width: 54px; height: 54px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
    border: 5px solid var(--ring, var(--muted)); background: color-mix(in srgb, var(--ring, #888) 16%, transparent);
    flex: 0 0 auto;
  }
  .slot-ring span { font-size: 15px; font-weight: 700; }
  .slot-ring.low-contrast {
    box-shadow: 0 0 0 1.5px color-mix(in srgb, var(--orca-fg, #e7e7ea) 45%, transparent);
  }
  .slot-text { display: flex; flex-direction: column; align-items: center; gap: 1px; min-width: 0; }
  .slot-mat { font-size: 12.5px; font-weight: 650; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .slot-sub { display: none; }
  .slot-tag {
    position: absolute; top: 7px; right: 7px; z-index: 1; max-width: calc(100% - 14px);
    font-size: 10px; font-weight: 700; letter-spacing: .05em; white-space: nowrap;
    text-transform: uppercase; background: var(--accent); color: var(--accent-fg); border-radius: 999px;
    padding: 2px 6px; overflow: hidden; text-overflow: ellipsis;
  }
  .slot-tools { display: flex; gap: 3px; flex: 0 0 auto; min-height: 14px; }
  .tool-badge {
    font-size: 10.5px; font-weight: 700; padding: 1px 5px; border-radius: 4px;
    background: var(--surface-3); color: var(--fg, inherit);
  }

  /* ---------- paneles ---------- */
  .panel { border: 1px solid var(--line); border-radius: 12px; background: var(--surface); }
  .panel > summary {
    cursor: pointer; padding: 12px 14px; font-weight: 600; font-size: 13px; list-style: none;
    display: flex; align-items: center; gap: 8px;
  }
  .panel > summary::-webkit-details-marker { display: none; }
  .panel > summary .icon { width: 17px; height: 17px; color: var(--muted); }
  .panel > summary .chev { margin-left: auto; transition: transform .2s; color: var(--muted); }
  .panel[open] > summary .chev { transform: rotate(180deg); }
  .panel-body { padding: 0 14px 14px; display: flex; flex-direction: column; gap: 14px; }
  .group { border-top: 1px dashed var(--line); padding-top: 12px; }
  .group:first-child { border-top: 0; padding-top: 0; }
  .group-title { font-size: 12px; letter-spacing: .07em; text-transform: uppercase; color: var(--muted); font-weight: 600; margin-bottom: 8px; }
  .row { display: flex; align-items: center; gap: 8px; justify-content: space-between; flex-wrap: wrap; }
  .controls-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .hint { font-size: 12px; color: var(--muted); margin-top: 6px; }
  .warn-text { font-size: 12px; color: var(--warn); margin-top: 6px; }

  .pill {
    border: 1px solid var(--line); background: transparent; border-radius: 999px; padding: 6px 12px;
    font-size: 12px; font-weight: 600; cursor: pointer; display: inline-flex; align-items: center; gap: 6px;
    transition: background .15s, border-color .15s, color .15s;
  }
  .pill:hover:not(:disabled) { background: var(--surface-2); }
  .pill.active { background: var(--accent); border-color: transparent; color: var(--accent-fg); }
  .pill:disabled { opacity: .4; cursor: not-allowed; }
  .pill .icon { width: 14px; height: 14px; }

  .control-card {
    width: 100%; display: flex; flex-direction: column; gap: 11px;
    justify-content: space-between; padding: 16px; padding-bottom: 12px;
    position: relative; height: 100%; align-self: stretch;
    /* Tonos del dial/Z/E derivados del tema (Bambu usa #EEEEEE/#CECECE sobre blanco). */
    --dial-g1: color-mix(in srgb, var(--orca-fg, #e7e7ea) 9%, var(--orca-bg, #1c1d21));
    --dial-g2: color-mix(in srgb, var(--orca-fg, #e7e7ea) 20%, var(--orca-bg, #1c1d21));
    --dial-pressed: color-mix(in srgb, var(--orca-fg, #e7e7ea) 32%, var(--orca-bg, #1c1d21));
    --dial-num: color-mix(in srgb, var(--orca-fg, #e7e7ea) 86%, var(--orca-bg, #1c1d21));
    --dial-gap: var(--orca-bg, #1c1d21);
  }
  .control-head { margin-bottom: 0; }
  .control-head h2 { font-size: 13px; letter-spacing: .04em; text-transform: uppercase; color: var(--muted); }
  .control-head .muted { font-size: 12px; }
  .control-head { gap: 6px; flex-wrap: wrap; }
  .control-head .muted { margin-left: auto; }
  .homing-chip { font-size: 11.5px; padding: 3px 8px; gap: 4px; }
  .homing-chip .icon { width: 13px; height: 13px; }
  .homing-chip.ok { background: color-mix(in srgb, var(--ok) 18%, transparent); color: var(--ok); }
  .homing-chip.warn { background: color-mix(in srgb, var(--warn) 20%, transparent); color: var(--warn); }
  .pop-filament { display: flex; flex-direction: column; gap: 6px; margin-top: 10px; }
  .pop-filament .btn { width: 100%; }
  .pop-filament-note { margin-top: 6px; }

  .motion-body {
    display: flex; align-items: center; justify-content: center; gap: 10px;
    position: relative; min-width: 0;
  }
  .dial {
    flex: 1 1 auto; min-width: 0; width: auto; max-width: 240px;
    aspect-ratio: 1; height: auto; border-radius: 50%;
  }
  .dial svg { display: block; width: 100%; height: 100%; }
  .dial:focus { outline: none; }
  .dial:focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; border-radius: 50%; }
  .jog-zone {
    fill: var(--dial-g1); stroke: transparent; stroke-width: 2.5; stroke-linejoin: round;
    cursor: pointer; transition: fill .12s ease, stroke .12s ease;
  }
  .jog-zone.inner { fill: var(--dial-g2); }
  .jog-zone:hover { stroke: var(--accent); }
  .jog-zone:active { fill: var(--dial-pressed); stroke: var(--accent); }
  .jog-zone.flash { fill: var(--dial-pressed); stroke: var(--accent); }
  .jog-gap { fill: none; stroke: var(--dial-gap); stroke-width: 4; pointer-events: none; }
  .jog-home { fill: var(--dial-g1); cursor: pointer; stroke: transparent; stroke-width: 2.5; transition: fill .12s, stroke .12s; }
  .jog-home:hover { stroke: var(--accent); }
  .jog-home:active { fill: var(--dial-pressed); stroke: var(--accent); }
  .dial-axis { fill: var(--orca-fg, #e7e7ea); font-size: 18px; font-weight: 700; text-anchor: middle; dominant-baseline: middle; pointer-events: none; }
  .dial-num { fill: var(--dial-num); font-size: 19px; font-weight: 500; text-anchor: middle; dominant-baseline: middle; pointer-events: none; }
  .dial[aria-disabled="true"] { opacity: .55; }
  .dial[aria-disabled="true"] .jog-zone,
  .dial[aria-disabled="true"] .jog-home { pointer-events: none; cursor: not-allowed; }
  .dial[aria-disabled="true"] .dial-axis { fill: var(--muted); }

  .z-strip { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; justify-content: space-between; }
  .z-btn {
    width: 48px; height: 48px; border: 0; border-radius: 9px; background: var(--dial-g1);
    color: var(--orca-fg, #e7e7ea); display: flex; align-items: center;
    justify-content: center; gap: 5px; font-size: 13px; font-weight: 650; line-height: 1;
    cursor: pointer; transition: background .15s, box-shadow .15s, opacity .15s;
  }
  .z-btn .icon { width: 16px; height: 16px; }
  .z-btn.z1 { background: var(--dial-g2); }
  .z-btn:hover:not(:disabled) { box-shadow: inset 0 0 0 2px var(--accent); }
  .z-btn:active:not(:disabled) { background: var(--dial-pressed); }
  .z-btn:disabled { opacity: .45; cursor: not-allowed; }
  .z-label { font-size: 12px; font-weight: 600; color: var(--muted); padding: 0 2px; }
  .z-caption { text-align: center; }
  .dial-keys { text-align: center; margin: 0; line-height: 1.35; }

  .motion-ext {
    width: 110px; flex: 0 0 110px; display: flex; flex-direction: column; align-items: center;
    justify-content: center; gap: 8px;
    border-left: 1px solid var(--line); padding-left: 10px; align-self: stretch;
  }
  .e-btn {
    width: 44px; height: 44px; border: 2px solid var(--dial-g1); border-radius: 11px;
    background: var(--dial-g1); color: var(--orca-fg, #e7e7ea); display: flex; align-items: center;
    justify-content: center; cursor: pointer; transition: border-color .15s, background .15s, opacity .15s;
  }
  .e-btn .icon { width: 20px; height: 20px; }
  .e-btn:hover:not(:disabled) { border-color: var(--accent); }
  .e-btn:active:not(:disabled) { background: var(--dial-pressed); }
  .e-btn:disabled { opacity: .45; cursor: not-allowed; }
  .e-art { width: 34px; height: 84px; color: var(--dial-num); }
  .e-art svg { width: 100%; height: 100%; display: block; }
  .e-label { font-size: 13px; font-weight: 600; color: var(--muted); }
  .e-step { font-size: 12px; color: var(--muted); }

  .motion-lock-note { font-size: 12px; color: var(--warn); text-align: center; }
  .motion-veil {
    position: absolute; inset: 0; border-radius: 14px; z-index: 3;
    display: flex; align-items: center; justify-content: center; gap: 8px;
    background: color-mix(in srgb, var(--orca-bg, #1c1d21) 82%, transparent);
    color: var(--muted); font-size: 12.5px; text-align: center; padding: 16px;
    backdrop-filter: blur(1px);
  }
  .motion-veil .icon { width: 16px; height: 16px; }

  .not-homed {
    display: flex; align-items: flex-start; gap: 8px; flex-wrap: wrap;
    max-width: 100%; padding: 6px 0 6px 8px; font-size: 12px;
    border: 0; border-left: 3px solid var(--warn); background: transparent; border-radius: 0;
  }
  .not-homed .icon { width: 14px; height: 14px; color: var(--warn); flex: 0 0 auto; margin-top: 2px; }
  .not-homed span:nth-child(2) { flex: 1 1 140px; }
  .not-homed .btn { padding: 2px 8px; font-size: 11.5px; }

  .control-extras {
    display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px;
    border-top: 1px dashed var(--line); padding-top: 12px;
  }
  .extras-btn {
    display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px;
    min-height: 56px; padding: 6px 4px; border-radius: 9px;
    border: 1px solid color-mix(in srgb, var(--orca-fg, #e7e7ea) 24%, var(--orca-bg, #1c1d21));
    background: var(--dial-g1); color: var(--orca-fg, #e7e7ea);
    font-size: 12px; font-weight: 600; line-height: 1.15; text-align: center; cursor: pointer;
    transition: background .15s, border-color .15s, opacity .15s;
  }
  .extras-btn .icon { width: 17px; height: 17px; color: currentColor; }
  .extras-btn:hover:not(:disabled) { background: var(--dial-g2); border-color: var(--accent); }
  .extras-btn:active:not(:disabled) { background: var(--dial-pressed); }
  .extras-btn:disabled { opacity: .72; cursor: not-allowed; }
  .extras-btn.armed { position: relative; }
  .extras-btn.armed::after {
    content: ""; position: absolute; top: 6px; right: 6px; width: 6px; height: 6px;
    border-radius: 50%; background: var(--accent);
  }

  .grid-actions { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px; }

  /* ---------- gráfica ---------- */
  #temp-chart { width: 100%; height: 180px; display: block; }
  .chart-legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--muted); margin-top: 8px; }
  .chart-legend i { display: inline-block; width: 12px; height: 3px; border-radius: 2px; margin-right: 5px; vertical-align: middle; }

  /* ---------- archivos ---------- */
  .files-scroll { max-height: 430px; overflow: auto; padding-right: 4px; }
  .files { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
  .file-card {
    position: relative; border: 1px solid var(--line); border-radius: 12px; overflow: hidden;
    background: var(--surface-2); display: flex; flex-direction: column;
    transition: transform .15s, box-shadow .15s;
  }
  .file-card:hover { transform: translateY(-2px); box-shadow: var(--shadow); }
  .file-thumb { height: 110px; background: var(--surface-3); display: flex; align-items: center; justify-content: center; overflow: hidden; }
  .file-thumb img { width: 100%; height: 100%; object-fit: contain; }
  .file-thumb span { font-size: 11px; color: var(--muted); }
  .file-info { padding: 8px 10px 10px; display: flex; flex-direction: column; gap: 5px; min-width: 0; }
  .file-name { font-size: 12.5px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .file-chips { display: flex; gap: 5px; flex-wrap: wrap; align-items: center; }
  .file-chip { font-size: 12px; color: var(--muted); display: inline-flex; align-items: center; gap: 3px; }
  .file-chip .icon { width: 11px; height: 11px; }
  .tool-dot { width: 9px; height: 9px; border-radius: 50%; border: 1px solid rgba(127, 127, 127, .4); }
  .file-overlay {
    position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    background: color-mix(in srgb, var(--orca-bg, #111) 62%, transparent);
    opacity: 0; transition: opacity .18s; pointer-events: none;
  }
  .file-card:hover .file-overlay, .file-card:focus-within .file-overlay { opacity: 1; pointer-events: auto; }
  .file-overlay .btn { box-shadow: var(--shadow); }
  .files-empty { padding: 22px; text-align: center; color: var(--muted); font-size: 12.5px; }
  .progress-line { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--muted); margin-top: 8px; }
  .progress-line .bar { flex: 1; }

  /* ---------- inputs ---------- */
  input[type=text], input[type=password], input[type=number], select {
    background: var(--surface-2); border: 1px solid var(--line); border-radius: 8px;
    color: var(--orca-fg, #e7e7ea); padding: 7px 9px; font-size: 13px; font-family: inherit;
  }
  input[type=number] { width: 90px; }
  input[type=number]::-webkit-outer-spin-button, input[type=number]::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }
  .check-inline { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; }

  /* ---------- popover ---------- */
  .pop-backdrop { position: fixed; inset: 0; z-index: 60; background: transparent; }
  .popover {
    position: fixed; z-index: 61; width: min(320px, calc(100vw - 24px));
    background: var(--orca-bg, #1c1d21); border: 1px solid var(--line); border-radius: 12px;
    box-shadow: var(--shadow); padding: 14px; display: none;
    color: var(--orca-fg, #e7e7ea);
  }
  .popover.open { display: block; animation: pop-in .16s ease-out; }
  @keyframes pop-in { from { opacity: 0; transform: translateY(-4px) scale(.98); } to { opacity: 1; transform: none; } }
  .pop-title { font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); font-weight: 600; margin-bottom: 8px; }
  .pop-value { display: flex; align-items: baseline; gap: 6px; margin-bottom: 10px; }
  .pop-value b { font-size: 28px; font-variant-numeric: tabular-nums; }
  .pop-value small { color: var(--muted); }
  .pop-grid { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
  .pop-grid .btn { flex: 1 1 auto; }
  .pop-row { display: flex; gap: 6px; align-items: center; margin-top: 10px; }
  .pop-row input { flex: 1; width: auto; }
  .pop-actions { display: flex; gap: 6px; justify-content: flex-end; margin-top: 12px; }
  .palette { display: grid; grid-template-columns: repeat(8, 1fr); gap: 6px; margin: 10px 0; }
  .swatch {
    aspect-ratio: 1; border-radius: 50%; border: 2px solid transparent; cursor: pointer; padding: 0;
    box-shadow: inset 0 0 0 1px rgba(127, 127, 127, .35);
  }
  .swatch.selected { border-color: var(--accent); box-shadow: 0 0 0 2px var(--orca-bg, #111); }
  .field { display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; }
  .field label { font-size: 11.5px; color: var(--muted); }

  /* ---------- modal ---------- */
  .modal-backdrop {
    position: fixed; inset: 0; z-index: 80; background: color-mix(in srgb, #000 55%, transparent);
    display: flex; align-items: center; justify-content: center; padding: 16px;
    opacity: 0; transition: opacity .16s;
  }
  .modal-backdrop.show { opacity: 1; }
  .modal {
    width: min(420px, 100%); background: var(--orca-bg, #1c1d21); border: 1px solid var(--line);
    border-radius: 14px; box-shadow: var(--shadow); padding: 18px;
    transform: translateY(8px); transition: transform .18s;
  }
  .modal-backdrop.show .modal { transform: none; }
  .modal h3 { font-size: 16px; margin-bottom: 6px; }
  .modal p { margin: 0; font-size: 13px; color: var(--muted); }
  .modal-ack { display: flex; gap: 8px; align-items: flex-start; margin-top: 12px; font-size: 12.5px; }
  .modal-ack input { margin-top: 2px; }
  .modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 16px; }

  /* ---------- toasts ---------- */
  .toasts { position: fixed; bottom: 14px; right: 14px; z-index: 90; display: flex; flex-direction: column; gap: 8px; max-width: 340px; }
  .toast {
    display: flex; align-items: center; gap: 8px; background: var(--orca-bg, #1c1d21);
    border: 1px solid var(--line); border-left: 3px solid var(--accent);
    border-radius: 10px; padding: 9px 12px; font-size: 12.5px; box-shadow: var(--shadow);
    animation: toast-in .2s ease-out;
  }
  .toast.warn { border-left-color: var(--warn); }
  .toast.error { border-left-color: var(--bad); }
  .toast.ok { border-left-color: var(--ok); }
  .toast .icon { width: 15px; height: 15px; }
  .toast.out { animation: toast-out .2s ease-in forwards; }
  @keyframes toast-in { from { opacity: 0; transform: translateX(12px); } to { opacity: 1; transform: none; } }
  @keyframes toast-out { to { opacity: 0; transform: translateX(12px); } }

  /* ---------- drawer ---------- */
  .drawer-backdrop { position: fixed; inset: 0; z-index: 50; background: color-mix(in srgb, #000 42%, transparent); opacity: 0; pointer-events: none; transition: opacity .2s; }
  .drawer-backdrop.show { opacity: 1; pointer-events: auto; }
  .drawer {
    position: fixed; top: 0; right: 0; bottom: 0; width: min(420px, 96vw); z-index: 51;
    background: var(--orca-bg, #1c1d21); border-left: 1px solid var(--line);
    box-shadow: -18px 0 44px rgba(0, 0, 0, .35); padding: 16px 16px 24px; overflow: auto;
    transform: translateX(103%); transition: transform .22s cubic-bezier(.2,.8,.2,1);
  }
  .drawer.open { transform: none; }
  .drawer-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 4px; }
  .drawer-head h2 { font-size: 16px; }
  .printer-list { display: flex; flex-direction: column; gap: 8px; margin-top: 8px; }
  .printer-row { border: 1px solid var(--line); border-radius: 10px; padding: 9px; display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
  .printer-row .wide { grid-column: 1 / -1; }
  .printer-row input { width: 100%; }
  .printer-row-head { grid-column: 1 / -1; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .discover-results { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; padding: 8px; border: 1px dashed var(--line); border-radius: 10px; }
  .discover-results .btn { text-align: left; white-space: normal; }

  footer { margin-top: 14px; font-size: 11px; color: var(--muted); display: flex; gap: 8px; flex-wrap: wrap; }

  .modal .btn:focus-visible, .modal input:focus-visible, .modal-actions .btn:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 2px;
  }
  #modal-extra { margin-right: auto; }

  /* ---------- sin puntero (táctil): acciones visibles ---------- */
  @media (hover: none), (pointer: coarse) {
    .file-overlay {
      position: static; opacity: 1; pointer-events: auto;
      background: transparent; padding: 0 10px 10px;
    }
    .file-overlay .btn { width: 100%; }
  }

  /* ---------- movimiento reducido ---------- */
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      animation-duration: .001ms !important;
      animation-iteration-count: 1 !important;
      transition-duration: .001ms !important;
      scroll-behavior: auto !important;
    }
    .fan-blades { animation: none !important; }
    .dot { animation: none !important; }
  }

  /* ---------- skeleton ---------- */
  body.booting .skeleton {
    color: transparent !important; border-radius: 6px;
    background: linear-gradient(90deg, var(--surface-2) 25%, var(--surface-3) 37%, var(--surface-2) 63%);
    background-size: 400% 100%; animation: shimmer 1.3s ease-in-out infinite;
  }
  @keyframes shimmer { 0% { background-position: 100% 0; } 100% { background-position: 0 0; } }

  /* ---------- animaciones ---------- */
  .fan-blades { transform-origin: 50% 50%; }
  .spinning .fan-blades { animation: spin var(--spin, 1.4s) linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body class="booting">

<header class="topbar">
  <div class="brand">
    <div class="logo"><span class="icon" data-icon="printer"></span></div>
    <div>
      <div class="brand-title">FlashForge Monitor</div>
      <div class="brand-sub">
        <span class="dot wait" id="conn-dot"></span>
        <span id="conn-label">Conectando…</span>
      </div>
    </div>
  </div>
  <div class="toolbar">
    <select id="printer-select" class="select" title="Impresora activa"></select>
    <span class="badge" id="demo-badge" hidden>DEMO</span>
    <button class="icon-btn" id="btn-refresh" type="button" title="Refrescar" aria-label="Refrescar">
      <span class="icon" data-icon="refresh"></span>
    </button>
    <button class="icon-btn" id="btn-config" type="button" title="Configuración" aria-label="Configuración">
      <span class="icon" data-icon="settings"></span>
    </button>
  </div>
</header>

<div class="banner" id="banner">
  <span class="msg" id="banner-text"></span>
  <button class="btn sm" id="banner-btn" type="button" hidden></button>
</div>

<div class="dash">

  <div class="row-top">
    <div class="col-left">
          <!-- HERO del trabajo -->
          <section class="hero" aria-label="Trabajo en curso">
            <div class="hero-thumb" id="hero-thumb">
              <img id="job-thumb-img" alt="Miniatura del trabajo" />
              <div class="hero-thumb-empty">
                <span class="icon" data-icon="image"></span>
                <span>Sin vista previa</span>
              </div>
            </div>
            <div class="hero-body">
              <div class="hero-top">
                <div class="hero-title">
                  <h1 id="job-name" class="skeleton">Sin trabajo</h1>
                  <div class="hero-sub" id="job-sub">—</div>
                </div>
                <span class="chip idle" id="job-state"><span class="dot"></span><span id="job-state-text">—</span></span>
              </div>
              <div class="progress">
                <div class="bar big"><i id="job-bar"></i></div>
                <div class="progress-row">
                  <span class="progress-pct mono" id="job-progress">—</span>
                  <span class="muted"><span class="icon" data-icon="clock"></span><span id="job-remaining">—</span></span>
                  <span class="muted"><span class="icon" data-icon="layers"></span><span id="job-layer"></span></span>
                </div>
              </div>
              <div class="meta-row hero-meta">
                <span class="meta-chip" id="chip-machine"><span class="icon" data-icon="chip"></span><span>—</span></span>
                <span class="meta-chip" id="chip-firmware"><span class="icon" data-icon="tag"></span><span>—</span></span>
                <span class="meta-chip error" id="chip-error" hidden>
                  <span class="icon" data-icon="alert"></span><span id="chip-error-text">—</span>
                  <button class="btn sm" id="btn-clear-error" type="button">Limpiar</button>
                </span>
              </div>
              <div class="hero-actions">
                <button class="btn primary" id="btn-pause" type="button" disabled>
                  <span class="icon" data-icon="pause"></span><span id="btn-pause-label">Pausar</span>
                </button>
                <button class="btn danger-ghost" id="btn-cancel" type="button" disabled>
                  <span class="icon" data-icon="stop"></span><span>Detener</span>
                </button>
              </div>
            </div>
            <div class="hero-transient" id="hero-transient" hidden>
              <div class="transient-icon">
                <span class="icon" id="transient-icon" data-icon="activity" aria-hidden="true"></span>
              </div>
              <div class="transient-body">
                <div class="transient-head">
                  <h1 id="transient-title">Ocupada…</h1>
                  <span class="chip printing" id="transient-chip">Ocupada</span>
                </div>
                <p class="transient-text" id="transient-text">La impresora está trabajando.</p>
                <div class="transient-temps" id="transient-temps" hidden>
                  <span>Boquilla <b id="transient-nozzle">—</b></span>
                  <span>Cama <b id="transient-bed">—</b></span>
                </div>
                <div class="transient-meter" id="transient-meter"><i></i></div>
              </div>
            </div>
            <div class="hero-finished" id="hero-finished" hidden>
              <div class="finished-check">
                <span class="icon" data-icon="check" aria-hidden="true"></span>
              </div>
              <div class="finished-body">
                <h1 id="finished-title">Impresión completada.</h1>
                <p class="finished-time" id="finished-time" hidden>Tiempo utilizado: —</p>
                <p class="finished-text">Después de limpiar la cama, pulsa para desbloquear la impresora.</p>
                <div class="finished-actions">
                  <button class="btn primary" id="btn-confirm-platform" type="button"
                          aria-label="Confirmar y desbloquear la impresora">Confirme</button>
                  <button class="btn" id="btn-reprint" type="button"
                          aria-label="Volver a imprimir el mismo archivo">Volver a imprimir</button>
                </div>
              </div>
            </div>
          </section>

        <!-- Sensores -->
        <div class="sensors">
          <button class="sensor" id="sensor-nozzle" type="button" data-popover="nozzle">
            <span class="sensor-icon"><span class="icon" data-icon="thermometer"></span></span>
            <span class="sensor-label">Boquilla</span>
            <span class="sensor-value"><b id="nozzle-cur" class="skeleton">—</b><small>°C</small></span>
            <span class="sensor-target" id="nozzle-target">objetivo —</span>
            <span class="edit-hint"><span class="icon" data-icon="chevron"></span></span>
          </button>
          <button class="sensor cool" id="sensor-bed" type="button" data-popover="bed">
            <span class="sensor-icon"><span class="icon" data-icon="bed"></span></span>
            <span class="sensor-label">Cama</span>
            <span class="sensor-value"><b id="bed-cur" class="skeleton">—</b><small>°C</small></span>
            <span class="sensor-target" id="bed-target">objetivo —</span>
            <span class="edit-hint"><span class="icon" data-icon="chevron"></span></span>
          </button>
          <button class="sensor" id="sensor-speed" type="button" data-popover="speed">
            <span class="sensor-icon"><span class="icon" data-icon="gauge"></span></span>
            <span class="sensor-label">Velocidad</span>
            <span class="sensor-value"><b id="speed-cur" class="skeleton">—</b><small>%</small></span>
            <span class="sensor-target" id="speed-target">solo imprimiendo</span>
            <span class="edit-hint"><span class="icon" data-icon="chevron"></span></span>
          </button>
          <button class="sensor" id="sensor-fan" type="button" data-popover="fan">
            <span class="sensor-icon"><span class="icon" data-icon="fan" id="fan-icon"></span></span>
            <span class="sensor-label">Ventilador</span>
            <span class="sensor-value"><b id="fan-cur" class="skeleton">—</b><small>%</small></span>
            <span class="sensor-target" id="fan-sub">aux —</span>
            <span class="edit-hint"><span class="icon" data-icon="chevron"></span></span>
          </button>
        </div>
    </div>

        <!-- CONTROL (fijo, al lado del hero) -->
        <aside class="card control-card" id="motion-card" aria-label="Control de movimiento">
          <div class="card-head control-head">
            <h2>Control</h2>
            <span class="chip homing-chip" id="homing-chip" hidden>
              <span class="icon" data-icon="home" aria-hidden="true"></span>
              <span id="homing-chip-text">—</span>
            </span>
            <span class="muted" id="control-mode">—</span>
          </div>

          <div class="motion-body">
            <div class="dial" id="dial-xy" tabindex="0" role="group"
                 aria-label="Dial de ejes XY. Flechas mueven 1 mm, Mayúsculas y flechas 10 mm, Enter hace home."
                 aria-describedby="dial-hint" aria-disabled="false">
              <svg id="dial-svg" viewBox="0 0 258 258" role="group" aria-label="Zonas de movimiento XY"></svg>
            </div>
            <aside class="motion-ext" role="group" aria-label="Extrusor">
              <button class="e-btn" id="e-retract" type="button" aria-label="Retraer filamento 10 mm">
                <span class="icon" data-icon="arrow-up"></span>
              </button>
              <div class="e-art" aria-hidden="true">
                <svg viewBox="0 0 45 104" fill="none" stroke="currentColor" stroke-width="1.8"
                     stroke-linecap="round" stroke-linejoin="round">
                  <path d="M22.5 4v26" opacity=".45"/>
                  <rect x="11" y="30" width="23" height="24" rx="4"/>
                  <path d="M22.5 54v14"/>
                  <path d="M13 68h19l-4 14H17z"/>
                  <path d="M22.5 82v14" opacity=".45"/>
                  <path d="M18 88l4.5 6 4.5-6" opacity=".8"/>
                </svg>
              </div>
              <button class="e-btn" id="e-extrude" type="button" aria-label="Extruir filamento 10 mm">
                <span class="icon" data-icon="arrow-down"></span>
              </button>
              <div class="e-label">Extrusor</div>
              <div class="e-step">±10 mm</div>
            </aside>
          </div>

          <div class="z-strip" role="group" aria-label="Eje Z (cama)">
            <button class="z-btn z10" type="button" data-z="-10" aria-label="Subir la cama 10 mm (Z menos 10)">
              <span class="icon" data-icon="bed-up"></span><span>10</span>
            </button>
            <button class="z-btn z1" type="button" data-z="-1" aria-label="Subir la cama 1 mm (Z menos 1)">
              <span class="icon" data-icon="bed-up"></span><span>1</span>
            </button>
            <span class="z-label" aria-hidden="true">Cama</span>
            <button class="z-btn z1" type="button" data-z="1" aria-label="Bajar la cama 1 mm (Z más 1)">
              <span class="icon" data-icon="bed-down"></span><span>1</span>
            </button>
            <button class="z-btn z10" type="button" data-z="10" aria-label="Bajar la cama 10 mm (Z más 10)">
              <span class="icon" data-icon="bed-down"></span><span>10</span>
            </button>
          </div>
          <div class="hint z-caption" style="margin:0">
            ▲ sube la cama (Z−) · ▼ la baja (Z+)
          </div>
          <p class="hint dial-keys" id="dial-hint">
            ↑↓←→ 1 mm · Shift 10 mm · Enter Home
          </p>

          <div class="not-homed" id="not-homed-note" hidden>
            <span class="icon" data-icon="alert"></span>
            <span>Haz <strong>Home</strong> antes de mover para no salir del área imprimible.</span>
            <button class="btn sm" id="not-homed-home" type="button">Ir a Home</button>
          </div>
          <div class="motion-lock-note" id="motion-lock-note" hidden>
            Movimiento bloqueado mientras imprime o está en pausa.
          </div>

          <div class="control-extras">
            <button class="extras-btn" id="m-cal-level" type="button" title="Nivelado de cama">
              <span class="icon" data-icon="bed" aria-hidden="true"></span><span>Nivelado</span>
            </button>
            <button class="extras-btn" id="m-cal-vibr" type="button" title="Input shaper">
              <span class="icon" data-icon="activity" aria-hidden="true"></span><span>Input shaper</span>
            </button>
            <button class="extras-btn" id="btn-shutdown" type="button" title="Auto-apagado">
              <span class="icon" data-icon="clock" aria-hidden="true"></span><span>Apagado</span>
            </button>
            <button class="extras-btn" id="m-error-clear" type="button" title="Limpiar error" disabled>
              <span class="icon" data-icon="check" aria-hidden="true"></span><span>Limpiar errores</span>
            </button>
          </div>

          <div class="motion-veil" id="motion-veil" hidden>
            <span class="icon" data-icon="alert"></span>
            <span>Sin conexión: los controles están deshabilitados.</span>
          </div>
        </aside>
      </div>

  <!-- AMS / IFS (ancho completo) -->
  <section class="card" id="ifs-card" aria-label="Estación de materiales">
    <div class="card-head"><h2>Estación de materiales (IFS)</h2><span class="muted" id="ifs-label">—</span></div>
    <div class="ams" id="slots"></div>
  </section>

  <div class="mini-row">
    <span class="mini">
      <span class="icon" data-icon="layers"></span>
      <span>Z-offset</span><b id="z-live">—</b>
      <button class="btn ghost" id="btn-z" type="button">Ajustar</button>
    </span>
    <span class="mini">
      <span class="icon" data-icon="bulb"></span>
      <span>Luz</span><b id="led-live">—</b>
      <button class="btn ghost" id="btn-led" type="button">Encender</button>
    </span>
  </div>

  <!-- Cámara -->
  <section class="card" id="camera-card" hidden>
    <div class="card-head"><h2>Cámara</h2><span class="muted" id="cam-label">—</span></div>
    <div class="camera-wrap" style="position:relative;background:#0b0b0d;border-radius:10px;min-height:150px;display:flex;align-items:center;justify-content:center;overflow:hidden">
      <img id="cam" alt="Cámara de la impresora" style="display:none;max-width:100%;max-height:340px;object-fit:contain" />
      <div id="cam-empty" style="padding:24px;text-align:center;color:var(--muted);font-size:12.5px">Sin señal de cámara</div>
    </div>
    <div class="row" style="margin-top:8px">
      <span class="muted">Stream MJPEG por red local (puerto 8080)</span>
      <button class="btn sm" id="btn-cam" type="button">Reconectar</button>
    </div>
  </section>

  <!-- Histórico -->
  <details class="panel" id="panel-temps">
    <summary><span class="icon" data-icon="chart"></span>Temperaturas (histórico)<span class="icon chev" data-icon="chevron"></span></summary>
    <div class="panel-body">
      <canvas id="temp-chart" height="180"></canvas>
      <div class="chart-legend">
        <span><i style="background:#f97316"></i>Boquilla</span>
        <span><i style="background:var(--accent)"></i>Objetivo boquilla</span>
        <span><i style="background:#38bdf8"></i>Cama</span>
        <span><i style="background:#0f766e"></i>Objetivo cama</span>
      </div>
      <div class="hint">Últimas ~120 muestras (10 min con sondeo cada 5 s). Se redibuja una vez por segundo.</div>
    </div>
  </details>

<!-- Archivos -->
<details class="panel" id="panel-files">
  <summary><span class="icon" data-icon="folder"></span>Archivos<span class="icon chev" data-icon="chevron"></span></summary>
  <div class="panel-body">

    <div class="group">
      <div class="group-title">Subir G-code / 3MF</div>
      <div class="controls-row">
        <input type="text" id="u-path" placeholder="/home/usuario/Descargas/pieza.gcode" spellcheck="false" autocomplete="off" style="flex:1 1 280px" />
        <label class="check-inline"><input type="checkbox" id="u-print" /> Imprimir al terminar</label>
        <button class="btn primary" id="u-send" type="button">
          <span class="icon" data-icon="upload"></span>Subir
        </button>
      </div>
      <div class="hint">
        La página no puede abrir el selector de archivos: escribe la ruta completa (legible por el Flatpak).
        Límite 200 MB. La primera lectura puede pedir permiso a Orca.
      </div>
      <div class="progress-line" id="u-progress" hidden>
        <span id="u-progress-text">Subiendo…</span>
        <div class="bar"><i id="u-bar"></i></div>
      </div>
    </div>

    <div class="group">
      <div class="row">
        <div class="group-title" style="margin:0">Trabajos guardados</div>
        <button class="btn sm" id="f-refresh" type="button">
          <span class="icon" data-icon="refresh"></span>Actualizar
        </button>
      </div>
      <div class="files-scroll">
        <div class="files" id="file-list">
          <div class="files-empty">Abre esta sección para cargar los archivos de la impresora.</div>
        </div>
      </div>
      <div class="hint">La API no permite borrar archivos: no hay botón de borrado.</div>
    </div>

  </div>
</details>

</div><!-- /.dash -->

<footer>
  <span>FlashForge Monitor v{{VERSION}}</span>
  <span>·</span>
  <span>API local HTTP 8898 · multi-impresora · UDP discovery</span>
</footer>

<!-- Drawer de configuración -->
<div class="drawer-backdrop" id="drawer-backdrop"></div>
<aside class="drawer" id="drawer" aria-label="Configuración">
  <div class="drawer-head">
    <h2>Configuración</h2>
    <button class="btn sm ghost" id="drawer-close" type="button">Cerrar</button>
  </div>
  <div class="muted">Impresoras y ajustes del plugin.</div>

  <div class="row" style="margin-top:14px">
    <div class="group-title" style="margin:0">Impresoras</div>
    <div class="controls-row">
      <button class="btn sm" id="p-discover" type="button">
        <span class="icon" data-icon="wifi"></span>Buscar
      </button>
      <button class="btn sm" id="p-add" type="button">
        <span class="icon" data-icon="plus"></span>Añadir
      </button>
    </div>
  </div>

  <div class="discover-results" id="discover-results" hidden></div>
  <div class="printer-list" id="printer-list"></div>
  <div class="hint">
    La impresora <strong>activa</strong> es la que se monitorea. El descubrimiento
    UDP puede estar bloqueado dentro del Flatpak: si no encuentra nada, escribe la IP.
  </div>

  <div class="field" style="margin-top:14px">
    <label for="f-interval">Intervalo de sondeo (segundos)</label>
    <input type="number" id="f-interval" min="1" max="120" step="1" value="5" />
  </div>

  <label class="check-inline" for="f-mock" style="margin-top:8px">
    <input type="checkbox" id="f-mock" checked />
    <span>Modo demo (datos simulados)</span>
  </label>
  <label class="check-inline" for="f-notify-finish" style="margin-top:8px">
    <input type="checkbox" id="f-notify-finish" checked />
    <span>Avisar al terminar un trabajo</span>
  </label>
  <label class="check-inline" for="f-notify-error" style="margin-top:8px">
    <input type="checkbox" id="f-notify-error" checked />
    <span>Avisar de errores</span>
  </label>

  <div class="controls-row" style="justify-content:flex-end;margin-top:18px">
    <button class="btn" id="f-close" type="button">Cancelar</button>
    <button class="btn primary" id="f-save" type="button">Guardar</button>
  </div>
  <div class="hint" style="margin-top:10px">
    Los mismos ajustes están en Plugins → FlashForge Monitor (sin el buscador UDP).
  </div>
</aside>

<!-- Popover y modal -->
<div class="pop-backdrop" id="pop-backdrop" hidden></div>
<div class="popover" id="popover" role="dialog" aria-modal="false"></div>

<div class="modal-backdrop" id="modal-backdrop" hidden>
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
    <h3 id="modal-title">Confirmar</h3>
    <p id="modal-text"></p>
    <label class="modal-ack" id="modal-ack-wrap" hidden>
      <input type="checkbox" id="modal-ack" />
      <span id="modal-ack-text">Entiendo el riesgo y quiero continuar.</span>
    </label>
    <div class="modal-actions">
      <button class="btn ghost" id="modal-extra" type="button" hidden></button>
      <button class="btn" id="modal-cancel" type="button">Cancelar</button>
      <button class="btn primary" id="modal-confirm" type="button">Confirmar</button>
    </div>
  </div>
</div>

<div class="toasts" id="toasts" aria-live="polite"></div>

<datalist id="material-list"></datalist>

<script>
(function () {
  "use strict";

  /* ------------------------------------------------------------------ *
   *  Iconos SVG inline (stroke = currentColor)
   * ------------------------------------------------------------------ */
  var ICONS = {
    printer: '<path d="M6 9V3h12v6"/><rect x="3" y="9" width="18" height="7" rx="2"/><path d="M7 16h10v5H7z"/><path d="M17 12h1"/>',
    thermometer: '<path d="M14 14.76V5a2 2 0 1 0-4 0v9.76a4 4 0 1 0 4 0Z"/><path d="M12 9v6"/>',
    bed: '<path d="M4 17h16"/><path d="M6 17V9l6-4 6 4v8"/><path d="M9 13h6"/>',
    fan: '<circle cx="12" cy="12" r="2"/><g class="fan-blades"><path d="M12 10c0-3 1-5 4-5 2 0 3 2 2 4-1 1.5-3 2-6 1"/><path d="M14 12c3 0 5 1 5 4 0 2-2 3-4 2-1.5-1-2-3-1-6"/><path d="M12 14c0 3-1 5-4 5-2 0-3-2-2-4 1-1.5 3-2 6-1"/><path d="M10 12c-3 0-5-1-5-4 0-2 2-3 4-2 1.5 1 2 3 1 6"/></g>',
    gauge: '<path d="M4 18a8 8 0 1 1 16 0"/><path d="M12 14l4-4"/><circle cx="12" cy="14" r="1.5"/>',
    spool: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="M12 4v3M12 17v3M4 12h3M17 12h3"/>',
    home: '<path d="M4 11l8-7 8 7"/><path d="M6 10v9h12v-9"/>',
    "arrow-up": '<path d="M12 19V5"/><path d="M6 11l6-6 6 6"/>',
    "arrow-down": '<path d="M12 5v14"/><path d="M6 13l6 6 6-6"/>',
    "arrow-left": '<path d="M19 12H5"/><path d="M11 6l-6 6 6 6"/>',
    "arrow-right": '<path d="M5 12h14"/><path d="M13 6l6 6-6 6"/>',
    wrench: '<path d="M15 6a4 4 0 0 0-5 5l-6 6 3 3 6-6a4 4 0 0 0 5-5l-3 3-3-3 3-3Z"/>',
    bulb: '<path d="M9 18h6"/><path d="M10 21h4"/><path d="M12 3a6 6 0 0 0-3 11c.7.6 1 1.2 1 2h4c0-.8.3-1.4 1-2a6 6 0 0 0-3-11Z"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/>',
    folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
    play: '<path d="M7 4l13 8-13 8Z"/>',
    pause: '<path d="M8 5v14M16 5v14"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    alert: '<path d="M12 3l9 16H3Z"/><path d="M12 9v5"/><path d="M12 17h.01"/>',
    check: '<path d="M4 12l5 5L20 6"/>',
    refresh: '<path d="M20 12a8 8 0 1 1-2.3-5.6"/><path d="M20 4v5h-5"/>',
    camera: '<path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2Z"/><circle cx="12" cy="13" r="4"/>',
    chip: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
    upload: '<path d="M12 16V4"/><path d="M6 10l6-6 6 6"/><path d="M4 20h16"/>',
    tag: '<path d="M20 12l-8 8-9-9V3h8Z"/><circle cx="7.5" cy="7.5" r="1.2"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    layers: '<path d="M12 3l9 5-9 5-9-5Z"/><path d="M3 13l9 5 9-5"/><path d="M3 17l9 5 9-5"/>',
    image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/>',
    chart: '<path d="M4 20V4"/><path d="M4 20h16"/><path d="M8 16l3-5 3 3 4-6"/>',
    chevron: '<path d="M6 9l6 6 6-6"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    wifi: '<path d="M5 12a10 10 0 0 1 14 0"/><path d="M8 15a6 6 0 0 1 8 0"/><path d="M11 18a2 2 0 0 1 2 0"/><circle cx="12" cy="20" r=".8"/>',
    activity: '<path d="M3 12h4l3 8 4-16 3 8h4"/>',
    "bed-up": '<path d="M7 15l5-5 5 5"/><path d="M12 10v8"/><path d="M5 21h14"/>',
    "bed-down": '<path d="M7 12l5 5 5-5"/><path d="M12 6v8"/><path d="M5 21h14"/>',
    scale: '<path d="M12 3v18"/><path d="M5 8l7-5 7 5"/><path d="M5 8l-3 7h6Z"/><path d="M19 8l-3 7h6Z"/>',
    x: '<path d="M6 6l12 12M18 6L6 18"/>'
  };

  function paintIcons(root) {
    var scope = root || document;
    var nodes = scope.querySelectorAll("[data-icon]");
    for (var i = 0; i < nodes.length; i++) {
      var name = nodes[i].getAttribute("data-icon");
      var markup = ICONS[name];
      if (!markup) { continue; }
      nodes[i].innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        markup + "</svg>";
    }
  }

  function setIcon(el, name) {
    if (!el) { return; }
    el.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (ICONS[name] || "") + "</svg>";
  }

  /* ------------------------------------------------------------------ *
   *  Estado de la UI
   * ------------------------------------------------------------------ */
  var bridge = (typeof window !== "undefined" && window.orca) ? window.orca : null;
  var cfg = {};
  var intervalSec = 5;
  var ui = {
    status: null,
    ifs: null,
    files: [],
    palette: [],
    materials: [],
    lastStatusAt: 0,
    bootAt: Date.now(),
    awaitingFirst: true,
    printerKey: "",
    bannerSticky: false,
    bannerAction: null,
    slotIndex: 0,
    slotColor: "",
    filesLoaded: false,
    slotsSignature: "",
    confirmAction: null,
    modalOpen: false,
    popOpen: false,
    modalCloseTimer: null,
    motionLocked: true,
    connected: false,
    hasEverConnected: false,
    homingNoticeSeen: false,
    homed: null,
    intent: null,
    completedJob: "",
    platformBusy: false,
    popAnchor: null,
    lastFocus: null,
    extraAction: null
  };

  function $(id) { return document.getElementById(id); }
  function setText(id, text) { var el = $(id); if (el) { el.textContent = text; } }
  function setHTML(id, html) { var el = $(id); if (el) { el.innerHTML = html; } }
  function num(value, digits) {
    var n = Number(value);
    if (value === null || value === undefined || !isFinite(n)) { return "—"; }
    return n.toFixed(digits);
  }
  function fmtMin(minutes) {
    var m = Math.round(Number(minutes));
    if (!isFinite(m) || m <= 0) { return "—"; }
    if (m < 60) { return m + " min"; }
    var h = Math.floor(m / 60), rest = m % 60;
    return rest === 0 ? h + " h" : h + " h " + rest + " min";
  }
  function colorLuminance(value) {
    var match = /^#?([0-9a-f]{6})$/i.exec(String(value || "").trim());
    if (!match) { return 1; }
    var n = parseInt(match[1], 16);
    function channel(c) {
      c /= 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    }
    return 0.2126 * channel(n >> 16 & 255) + 0.7152 * channel(n >> 8 & 255)
      + 0.0722 * channel(n & 255);
  }

  function isValidColor(value) {
    return typeof value === "string" && /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(value);
  }
  function pct(current, target) {
    var c = Number(current), t = Number(target);
    if (!isFinite(c) || !isFinite(t) || t <= 0) { return 0; }
    return Math.max(0, Math.min(100, (c / t) * 100));
  }
  function setBar(id, value) {
    var el = $(id);
    if (el) { el.style.width = Math.max(0, Math.min(100, value)) + "%"; }
  }

  /* ------------------------------------------------------------------ *
   *  Toasts, banner, modal, popover
   * ------------------------------------------------------------------ */
  function toast(text, kind) {
    var host = $("toasts");
    if (!host || !text) { return; }
    var el = document.createElement("div");
    el.className = "toast " + (kind || "ok");
    el.innerHTML = '<span class="icon" data-icon="' +
      (kind === "error" ? "alert" : (kind === "warn" ? "alert" : "check")) +
      '"></span><span></span>';
    el.lastChild.textContent = text;
    paintIcons(el);
    host.appendChild(el);
    while (host.children.length > 3) { host.removeChild(host.firstChild); }
    setTimeout(function () {
      el.className += " out";
      setTimeout(function () { if (el.parentNode) { el.parentNode.removeChild(el); } }, 220);
    }, 3600);
  }

  function showBanner(kind, text, actionLabel, actionFn, sticky) {
    var el = $("banner");
    el.className = "banner show " + kind;
    $("banner-text").textContent = text;
    var btn = $("banner-btn");
    if (actionLabel && typeof actionFn === "function") {
      btn.textContent = actionLabel;
      btn.hidden = false;
      ui.bannerAction = actionFn;
    } else {
      btn.hidden = true;
      ui.bannerAction = null;
    }
    ui.bannerSticky = !!sticky;
  }
  function hideBanner() {
    $("banner").className = "banner";
    ui.bannerSticky = false;
    ui.bannerAction = null;
  }

  function openModal(options) {
    var opts = options || {};
    ui.lastFocus = document.activeElement;
    $("modal-title").textContent = opts.title || "Confirmar";
    $("modal-text").textContent = opts.text || "";
    var ackWrap = $("modal-ack-wrap");
    ackWrap.hidden = !opts.ack;
    $("modal-ack").checked = false;
    $("modal-ack-text").textContent = opts.ackText || "Entiendo el riesgo y quiero continuar.";
    var confirm = $("modal-confirm");
    confirm.textContent = opts.confirmLabel || "Confirmar";
    confirm.className = "btn " + (opts.danger ? "danger" : "primary");
    confirm.disabled = !!opts.ack;
    ui.confirmAction = opts.onConfirm || null;

    // Botón extra opcional (p. ej. "Calentar a 200 °C").
    var extra = $("modal-extra");
    ui.extraAction = typeof opts.onExtra === "function" ? opts.onExtra : null;
    extra.hidden = !opts.extraLabel;
    extra.textContent = opts.extraLabel || "";
    var cancel = $("modal-cancel");
    cancel.hidden = !!opts.hideCancel;
    cancel.textContent = opts.cancelLabel || "Cancelar";

    ui.modalOpen = true;
    if (ui.modalCloseTimer) { clearTimeout(ui.modalCloseTimer); ui.modalCloseTimer = null; }
    var backdrop = $("modal-backdrop");
    backdrop.hidden = false;
    requestAnimationFrame(function () { backdrop.classList.add("show"); });
    setTimeout(function () { (opts.ack ? $("modal-ack") : confirm).focus(); }, 40);
  }
  function closeModal() {
    var backdrop = $("modal-backdrop");
    ui.modalOpen = false;
    backdrop.classList.remove("show");
    if (ui.modalCloseTimer) { clearTimeout(ui.modalCloseTimer); }
    ui.modalCloseTimer = setTimeout(function () {
      backdrop.hidden = true;
      ui.modalCloseTimer = null;
    }, 160);
    ui.confirmAction = null;
    ui.extraAction = null;
    var previous = ui.lastFocus;
    ui.lastFocus = null;
    if (previous && typeof previous.focus === "function") {
      setTimeout(function () { try { previous.focus(); } catch (err) {} }, 40);
    }
  }

  function anchorPopover(pop, anchorEl) {
    if (!pop || !anchorEl) { return; }
    var margin = 8;
    var gap = 8;
    var vw = document.documentElement.clientWidth || window.innerWidth;
    var vh = window.innerHeight || document.documentElement.clientHeight;
    pop.style.maxHeight = Math.max(160, vh - margin * 2) + "px";
    pop.style.overflowY = "auto";
    var rect = anchorEl.getBoundingClientRect();
    var width = pop.offsetWidth || Math.min(320, vw - 24);
    var height = pop.offsetHeight || 200;
    function clamp(value, min, max) { return Math.max(min, Math.min(max, value)); }
    var maxLeft = Math.max(margin, vw - width - margin);
    var maxTop = Math.max(margin, vh - height - margin);
    var left;
    var top;
    // Preferencia: a la derecha del trigger; si no cabe, a la izquierda.
    if (rect.right + gap + width <= vw - margin) {
      left = rect.right + gap;
      top = clamp(rect.top, margin, maxTop);
    } else if (rect.left - gap - width >= margin) {
      left = rect.left - gap - width;
      top = clamp(rect.top, margin, maxTop);
    } else {
      // Sin sitio horizontal: debajo si cabe, si no encima, con clamping final.
      left = clamp(rect.left + rect.width / 2 - width / 2, margin, maxLeft);
      if (rect.bottom + gap + height <= vh - margin) {
        top = rect.bottom + gap;
      } else if (rect.top - gap - height >= margin) {
        top = rect.top - gap - height;
      } else {
        top = clamp(rect.bottom + gap, margin, maxTop);
      }
    }
    pop.style.left = Math.round(clamp(left, margin, maxLeft)) + "px";
    pop.style.top = Math.round(clamp(top, margin, maxTop)) + "px";
  }

  function repositionPopover() {
    if (!ui.popOpen || !ui.popAnchor) { return; }
    anchorPopover($("popover"), ui.popAnchor);
  }

  function openPopover(anchor, html, onMount) {
    var pop = $("popover");
    pop.innerHTML = html;
    pop.hidden = false;
    pop.classList.add("open");
    ui.popOpen = true;
    ui.popAnchor = anchor || null;
    $("pop-backdrop").hidden = false;
    paintIcons(pop);
    if (onMount) { onMount(pop); }
    anchorPopover(pop, anchor);
    // Reposiciona tras el primer frame (iconos/fuentes ya medidos).
    if (typeof requestAnimationFrame === "function") {
      requestAnimationFrame(function () { if (ui.popOpen) { repositionPopover(); } });
    }
  }
  function closePopover() {
    var pop = $("popover");
    ui.popOpen = false;
    ui.popAnchor = null;
    pop.classList.remove("open");
    pop.hidden = true;
    $("pop-backdrop").hidden = true;
  }

  /* ------------------------------------------------------------------ *
   *  Envío al plugin
   * ------------------------------------------------------------------ */
  function send(obj) {
    if (!bridge || typeof bridge.postMessage !== "function") {
      showBanner("warn", "El puente de OrcaSlicer no está disponible.", null, null, true);
      return false;
    }
    try {
      bridge.postMessage(obj);
      return true;
    } catch (err) {
      showBanner("error", "No se pudo enviar el mensaje al plugin: " + err, null, null, false);
      return false;
    }
  }

  /* ------------------------------------------------------------------ *
   *  Estado del trabajo (hero, chips, sensores)
   * ------------------------------------------------------------------ */
  function stateLabel(state) {
    var map = {
      ready: "Lista", busy: "Ocupada", calibrate_doing: "Calibrando", heating: "Calentando",
      printing: "Imprimiendo", pausing: "Pausando", pause: "En pausa", paused: "En pausa",
      paused_p: "En pausa", canceling: "Cancelando", cancel: "Cancelada", cancelled: "Cancelada",
      completed: "Terminada", error: "Error", downloading: "Recibiendo", loading: "Cargando",
      sending: "Enviando", unzipping: "Descomprimiendo", cloud_slicing: "Laminando",
      idle: "En reposo"
    };
    return map[state] || state || "desconocido";
  }
  function stateClass(state) {
    if (state === "error") { return "error"; }
    if (state === "printing" || state === "heating" || state === "resuming" ||
        state === "calibrate_doing" || state === "sending" || state === "downloading" ||
        state === "loading") { return "printing"; }
    if (state === "pause" || state === "paused" || state === "paused_p" ||
        state === "pausing") { return "paused"; }
    if (state === "completed") { return "done"; }
    return "idle";
  }
  function isWorking(state) {
    return state === "printing" || state === "heating" || state === "resuming" ||
      state === "pausing" || state === "pause" || state === "paused" ||
      state === "paused_p" || state === "calibrate_doing";
  }

  var KNOWN_STATES = {
    ready: 1, busy: 1, calibrate_doing: 1, heating: 1, printing: 1,
    pausing: 1, pause: 1, paused: 1, paused_p: 1, canceling: 1, cancel: 1,
    cancelled: 1, completed: 1, finish: 1, error: 1, downloading: 1,
    loading: 1, cloud_slicing: 1, sending: 1, unzipping: 1, resuming: 1, idle: 1
  };
  var TRANSIENT_STATES = {
    calibrate_doing: 1, heating: 1, loading: 1, busy: 1,
    downloading: 1, sending: 1, unzipping: 1, cloud_slicing: 1
  };
  function isTransientState(state) { return !!TRANSIENT_STATES[state]; }
  function blocksMotion(state) {
    return isWorking(state) || isTransientState(state);
  }

  function fmtDuration(seconds) {
    var total = Math.max(0, Math.round(Number(seconds) || 0));
    var hours = Math.floor(total / 3600);
    var minutes = Math.floor((total % 3600) / 60);
    if (hours > 0) { return hours + "h " + minutes + "min"; }
    if (minutes > 0) { return minutes + " min"; }
    return total + " s";
  }

  function setPlatformBusy(busy) {
    ui.platformBusy = Boolean(busy);
    var confirm = $("btn-confirm-platform");
    var reprint = $("btn-reprint");
    if (confirm) {
      confirm.disabled = ui.platformBusy;
      confirm.textContent = ui.platformBusy ? "Confirmando…" : "Confirme";
      confirm.setAttribute("aria-busy", ui.platformBusy ? "true" : "false");
    }
    if (reprint) { reprint.disabled = ui.platformBusy; }
  }

  function renderFinished(state, msg, job) {
    var wrap = $("hero-finished");
    if (!wrap) { return; }
    wrap.hidden = false;
    var cancelled = state === "cancel" || state === "cancelled";
    setText("finished-title", cancelled ? "Impresión cancelada." : "Impresión completada.");
    var elapsed = Number(msg.completed_elapsed_s) || Number(job.elapsed_s) || 0;
    var time = $("finished-time");
    if (elapsed > 0) {
      time.hidden = false;
      setText("finished-time", "Tiempo utilizado: " + fmtDuration(elapsed));
    } else {
      time.hidden = true;
    }
    var file = String(msg.completed_job || job.name || "").trim();
    ui.completedJob = (file && file !== "—") ? file : "";
    var reprint = $("btn-reprint");
    if (reprint) { reprint.hidden = !ui.completedJob; }
    if (!ui.platformBusy) { setPlatformBusy(false); }
  }

  function confirmPlatform() {
    if (ui.platformBusy) { return; }
    setPlatformBusy(true);
    send({ type: "clear-platform" });
  }

  function reprintCompleted() {
    if (ui.platformBusy || !ui.completedJob) { return; }
    setPlatformBusy(true);
    send({ type: "start-file", file: ui.completedJob, leveling: true, clear_first: true });
  }

  function renderTransient(state, msg, nozzle, bed, intent) {
    var wrap = $("hero-transient");
    if (!wrap) { return; }
    wrap.hidden = false;
    var iconName = "settings";
    var title = "Ocupada…";
    var chipText = "Ocupada";
    var text = "Ocupada — operación iniciada fuera de la pestaña.";
    var meter = false;
    var temps = false;
    var intentKind = intent ? String(intent.kind || "") : "";
    var slot = intent && intent.slot ? intent.slot : 0;

    if (intentKind === "leveling") {
      iconName = "activity";
      title = "Nivelando…";
      chipText = "Nivelado";
      text = "La impresora se mueve sola — no la interrumpas.";
      meter = true;
    } else if (intentKind === "input_shaper") {
      iconName = "activity";
      title = "Input shaper…";
      chipText = "Input shaper";
      text = "La impresora se mueve sola — no la interrumpas.";
      meter = true;
    } else if (intentKind === "load" || intentKind === "unload") {
      iconName = "layers";
      var suffix = slot ? " (slot " + slot + ")" : "";
      title = (intentKind === "load" ? "Cargando filamento" : "Descargando filamento") + suffix + "…";
      chipText = "Filamento";
      text = "La impresora está moviendo filamento" + (slot ? " del slot " + slot : "") +
        ". No la interrumpas.";
      meter = true;
    } else if (intentKind === "homing") {
      iconName = "home";
      title = "Haciendo home…";
      chipText = "Homing";
      text = "Localizando los ejes. No la interrumpas.";
      meter = true;
    } else if (state === "calibrate_doing") {
      var kind = String(msg.last_calibration || "");
      iconName = "activity";
      title = kind === "level" ? "Nivelando…"
        : (kind === "vibr" ? "Input shaper…" : "Calibrando…");
      chipText = kind === "vibr" ? "Input shaper" : (kind === "level" ? "Nivelado" : "Calibrando");
      text = "La impresora se mueve sola — no la interrumpas.";
      meter = true;
    } else if (state === "heating") {
      iconName = "thermometer";
      title = "Calentando…";
      chipText = "Calentando";
      text = "Esperando a que boquilla y cama alcancen el objetivo.";
      temps = true;
    } else if (state === "loading") {
      var action = String(msg.last_filament_action || "");
      iconName = "layers";
      title = action === "load" ? "Cargando filamento…"
        : (action === "unload" ? "Descargando filamento…" : "Cargando/Descargando filamento…");
      chipText = "Filamento";
      text = "La impresora está moviendo filamento. No la interrumpas.";
      meter = true;
    } else if (state === "downloading") {
      iconName = "refresh";
      title = "Recibiendo archivo…";
      chipText = "Recibiendo";
      text = "La impresora está descargando el trabajo.";
      meter = true;
    } else {
      text = "Ocupada — operación iniciada fuera de la pestaña.";
    }

    setIcon($("transient-icon"), iconName);
    setText("transient-title", title);
    var chip = $("transient-chip");
    chip.className = "chip " + (intent ? "printing" : stateClass(state));
    setText("transient-chip", chipText);
    setText("transient-text", text);
    $("transient-meter").hidden = !meter;
    $("transient-temps").hidden = !temps;
    if (temps) {
      setText("transient-nozzle", num(nozzle.current, 1) + " → " + num(nozzle.target, 0) + " °C");
      setText("transient-bed", num(bed.current, 1) + " → " + num(bed.target, 0) + " °C");
    }
  }

  function activePrinterInfo() {
    var printers = Array.isArray(cfg.printers) ? cfg.printers : [];
    var index = Number(cfg.active_printer) || 0;
    return (index >= 0 && index < printers.length) ? printers[index] : {};
  }

  function printerLabel() {
    var printer = activePrinterInfo();
    if (!printer || !printer.name && !printer.ip) { return "sin impresora"; }
    return (printer.name || "Impresora") + (printer.ip ? " · " + printer.ip : "");
  }

  function setConn(kind, label) {
    var dot = $("conn-dot");
    if (dot) { dot.className = "dot " + kind; }
    setText("conn-label", label);
  }

  function renderStatus(msg) {
    ui.status = msg;
    ui.awaitingFirst = false;
    ui.lastStatusAt = Date.now();
    document.body.classList.remove("booting");

    var nozzle = msg.nozzle || {};
    var bed = msg.bed || {};
    var job = msg.job || {};
    if (Object.prototype.hasOwnProperty.call(msg, "intent")) {
      ui.intent = msg.intent || null;
    }
    var rawState = String(msg.state || "idle");
    var state = KNOWN_STATES[rawState] ? rawState : "busy";
    var intentActive = Boolean(ui.intent && ui.intent.kind);
    var transient = isTransientState(state) || intentActive;
    var finished = !transient && (state === "completed" || state === "cancel"
      || state === "cancelled");
    var errorCode = String(msg.error_code || "").trim();

    // Hero
    setText("job-name", job.name || "Sin trabajo");
    setText("job-sub", job.name ? printerLabel() : "Impresora en reposo");
    var chip = $("job-state");
    chip.className = "chip " + stateClass(state);
    setText("job-state-text", stateLabel(state));
    // Meta chips
    setText("chip-machine", msg.machine || msg.host || "—");
    setText("chip-firmware", msg.firmware ? "fw " + msg.firmware : "—");
    $("chip-error").hidden = !errorCode;
    if (errorCode) { setText("chip-error-text", "Error " + errorCode); }

    var progress = Number(job.progress);
    if (!isFinite(progress)) { progress = 0; }
    progress = Math.max(0, Math.min(100, progress));
    setText("job-progress", progress > 0 ? num(progress, 1) + " %" : "—");
    setBar("job-bar", progress);
    setText("job-remaining", fmtMin(job.remaining_min));
    setText("job-layer", Number(job.total_layers) > 0 ? "Capa " + job.layer + " / " + job.total_layers : "");

    var paused = (state === "pause" || state === "paused" || state === "paused_p");
    var active = isWorking(state);
    var jobActive = active && !transient;
    var hasJob = Boolean(String(job.name || "").trim() && job.name !== "—")
      || Number(job.progress) > 0 || active;
    var heroEl = document.querySelector(".hero");
    heroEl.classList.toggle("has-job", hasJob);
    heroEl.classList.toggle("idle", !hasJob && !transient && !finished);
    heroEl.classList.toggle("transient", transient);
    heroEl.classList.toggle("finished", finished);
    if (transient) {
      $("hero-finished").hidden = true;
      renderTransient(state, msg, nozzle, bed, ui.intent);
    } else if (finished) {
      $("hero-transient").hidden = true;
      renderFinished(state, msg, job);
    } else {
      $("hero-transient").hidden = true;
      $("hero-finished").hidden = true;
      setPlatformBusy(false);
    }

    var wasConnected = ui.connected;
    ui.connected = true;
    if (ui.hasEverConnected && !wasConnected) {
      // Tras reconectar se vuelve a mostrar el aviso de homing una vez.
      resetHomingNote();
    }
    ui.hasEverConnected = true;
    var motionBlocked = blocksMotion(state) || intentActive;
    setMotionLocked(motionBlocked, transient ? "transient" : "printing");
    maybeShowHomingNote();

    var pauseBtn = $("btn-pause");
    pauseBtn.disabled = !jobActive;
    setText("btn-pause-label", paused ? "Reanudar" : "Pausar");
    setIcon(pauseBtn.querySelector(".icon"), paused ? "play" : "pause");
    var cancelBtn = $("btn-cancel");
    cancelBtn.disabled = !jobActive;
    pauseBtn.setAttribute("aria-disabled", pauseBtn.disabled ? "true" : "false");
    cancelBtn.setAttribute("aria-disabled", cancelBtn.disabled ? "true" : "false");

    // Thumbnail
    var thumb = errorCode ? "" : String(msg.job_thumb || "");
    var thumbBox = $("hero-thumb");
    if (thumb) {
      $("job-thumb-img").src = thumb;
      thumbBox.classList.add("has-image");
    } else {
      $("job-thumb-img").removeAttribute("src");
      thumbBox.classList.remove("has-image");
    }

    // Sensores
    setText("nozzle-cur", num(nozzle.current, 1));
    setText("nozzle-target", "objetivo " + num(nozzle.target, 0) + " °C");
    setText("bed-cur", num(bed.current, 1));
    setText("bed-target", "objetivo " + num(bed.target, 0) + " °C");
    setText("speed-cur", num(msg.speed, 0));
    setText("speed-target", jobActive ? "preset actual" : "solo imprimiendo");
    setText("fan-cur", num(msg.fan_part, 0));
    setText("fan-sub", "aux " + num(msg.fan_aux, 0) + " %");
    $("sensor-speed").disabled = !jobActive;

    var fanIcon = $("fan-icon");
    if (fanIcon) {
      var fanSpeed = Number(msg.fan_part) || 0;
      fanIcon.classList.toggle("spinning", fanSpeed > 0);
      fanIcon.style.setProperty("--spin", Math.max(0.35, 2.2 - fanSpeed / 60).toFixed(2) + "s");
    }
    $("sensor-nozzle").classList.toggle("hot", Number(nozzle.current) >= 60);
    $("sensor-bed").classList.toggle("cool", Number(bed.current) < 60);

    setText("z-live", num(msg.z_offset, 2) + " mm");
    setText("led-live", msg.light === "open" ? "encendida" : (msg.light === "close" ? "apagada" : "—"));
    $("btn-led").textContent = msg.light === "open" ? "Apagar" : "Encender";

    // Error activo (el chip del hero lo muestra; el botón de extras lo limpia)
    $("m-error-clear").disabled = !errorCode;
    var shutdownMinutes = Number(msg.shutdown_minutes) || 0;
    var shutdownButton = $("btn-shutdown");
    if (shutdownButton) {
      shutdownButton.classList.toggle("armed", shutdownMinutes > 0);
      var shutdownLabel = shutdownMinutes > 0
        ? "Auto-apagado: " + shutdownMinutes + " min" : "Auto-apagado: desactivado";
      shutdownButton.title = shutdownLabel;
      shutdownButton.setAttribute("aria-label", shutdownLabel);
    }

    // Temperaturas → gráfica
    pushTempSample(Number(nozzle.current) || 0, Number(nozzle.target) || 0,
                   Number(bed.current) || 0, Number(bed.target) || 0);

    // Cámara
    var camera = $("camera-card");
    if (msg.camera_available === false) {
      camera.hidden = true;
      updateCamera("");
    } else {
      camera.hidden = false;
      updateCamera(msg.camera_url || "");
    }

    // Conexión
    if (msg.mock) {
      setConn("demo", "Modo demo · " + printerLabel());
    } else if (msg.connected === false) {
      setConn("off", "Sin conexión · " + printerLabel());
    } else {
      setConn("on", printerLabel() + (msg.firmware ? " · fw " + msg.firmware : ""));
    }

    if (!ui.bannerSticky) { hideBanner(); }
    renderSlots();
  }

  function updateCamera(url) {
    var img = $("cam");
    var empty = $("cam-empty");
    if (!url) {
      img.removeAttribute("src");
      img.style.display = "none";
      empty.style.display = "flex";
      empty.textContent = cfg.mock ? "Cámara no disponible en modo demo" : "Sin señal de cámara";
      setText("cam-label", "—");
      img.setAttribute("data-url", "");
      return;
    }
    setText("cam-label", "en vivo");
    if (img.getAttribute("data-url") === url) { return; }
    img.setAttribute("data-url", url);
    img.style.display = "block";
    empty.style.display = "none";
    img.src = url + (url.indexOf("?") === -1 ? "?" : "&") + "_t=" + Date.now();
  }

  /* ------------------------------------------------------------------ *
   *  AMS / IFS
   * ------------------------------------------------------------------ */
  function renderSlots() {
    var host = $("slots");
    if (!host) { return; }
    var ifs = ui.ifs;

    if (!ifs) {
      if (ui.slotsSignature !== "loading") {
        ui.slotsSignature = "loading";
        host.innerHTML = '<div class="files-empty">Leyendo la estación…</div>';
      }
      return;
    }
    if (ifs.has_station === false && (!ifs.slots || !ifs.slots.length)) {
      if (ui.slotsSignature !== "nostation") {
        ui.slotsSignature = "nostation";
        host.innerHTML = '<div class="files-empty">Esta impresora no tiene estación IFS.</div>';
        setText("ifs-label", "no disponible");
      }
      return;
    }

    var slots = Array.isArray(ifs.slots) ? ifs.slots : [];
    var tools = (ui.status && Array.isArray(ui.status.job_tools)) ? ui.status.job_tools : [];
    var activeId = Number(ifs.active_slot) || 0;

    // Evita reconstruir el DOM en cada sondeo (conserva el foco del teclado).
    var signature = JSON.stringify([slots, activeId, tools, ifs.mock]);
    if (signature === ui.slotsSignature) { return; }
    ui.slotsSignature = signature;
    host.innerHTML = "";

    for (var i = 0; i < Math.max(4, slots.length); i++) {
      var slot = slots[i] || { index: i, slot_id: i + 1, material: "", color: "", present: false };
      var button = document.createElement("button");
      button.type = "button";
      button.className = "slot" + (slot.present === false ? " empty" : "") +
        (Number(slot.slot_id) === activeId && activeId > 0 ? " active" : "");
      button.setAttribute("data-slot", String(i));

      var ring = document.createElement("span");
      ring.className = "slot-ring";
      var ringColor = isValidColor(slot.color) ? slot.color : "var(--muted)";
      ring.style.setProperty("--ring", ringColor);
      if (isValidColor(slot.color) && colorLuminance(slot.color) < 0.16) {
        ring.classList.add("low-contrast");
      }
      var number = document.createElement("span");
      number.textContent = String(slot.slot_id || (i + 1));
      ring.appendChild(number);
      button.appendChild(ring);

      var mat = document.createElement("span");
      mat.className = "slot-mat";
      mat.textContent = slot.material || "Vacío";
      button.appendChild(mat);

      var toolBox = document.createElement("span");
      toolBox.className = "slot-tools";
      tools.forEach(function (tool) {
        if (Number(tool.slot_id) !== Number(slot.slot_id)) { return; }
        var badge = document.createElement("span");
        badge.className = "tool-badge";
        badge.textContent = "T" + tool.tool_id;
        badge.title = "Tool " + tool.tool_id + " · " + (tool.material || "");
        toolBox.appendChild(badge);
      });
      button.appendChild(toolBox);

      if (Number(slot.slot_id) === activeId && activeId > 0) {
        var tag = document.createElement("span");
        tag.className = "slot-tag";
        tag.textContent = "en uso";
        button.appendChild(tag);
      }

      (function (index, data) {
        button.addEventListener("click", function () { openSlotPopover(this, index, data); });
      })(i, slot);
      host.appendChild(button);
    }

    var label = (ifs.slot_count ? ifs.slot_count : 4) + " slots";
    if (activeId) { label += " · activo " + activeId; }
    if (ifs.mock) { label += " · demo"; }
    setText("ifs-label", label);
  }

  function openSlotPopover(anchor, index, slot) {
    ui.slotIndex = index;
    ui.slotColor = isValidColor(slot.color) ? slot.color.toUpperCase() : "#FFFFFF";
    var palette = Array.isArray(ui.palette) ? ui.palette : [];
    var swatches = palette.map(function (entry) {
      var selected = String(entry.hex || "").toUpperCase() === ui.slotColor ? " selected" : "";
      return '<button type="button" class="swatch' + selected + '" data-color="' +
        String(entry.hex || "").toUpperCase() + '" title="' + (entry.name || entry.hex) +
        '" style="background:' + String(entry.hex || "#888") + '"></button>';
    }).join("");

    var html =
      '<div class="pop-title">Slot ' + (slot.slot_id || (index + 1)) + '</div>' +
      '<div class="field"><label for="pop-material">Material</label>' +
      '<input type="text" id="pop-material" list="material-list" maxlength="32" value="' +
      String(slot.material || "").replace(/"/g, "&quot;") + '" placeholder="PLA, PETG…"></div>' +
      '<div class="hint">Color (paleta de 24)</div>' +
      '<div class="palette" id="pop-palette">' + (swatches || '<span class="hint">Sin paleta cargada</span>') + '</div>' +
      '<div class="pop-filament">' +
      '<button class="btn" type="button" id="pop-load"' + filamentDisabled() + '>Cargar filamento aquí</button>' +
      '<button class="btn" type="button" id="pop-unload"' + filamentDisabled() + '>Descargar filamento</button>' +
      '</div>' +
      (filamentBusy()
        ? '<div class="hint pop-filament-note">No disponible mientras imprime o está en pausa.</div>'
        : '<div class="hint pop-filament-note">Cargar mueve filamento del slot al extrusor; descargar lo retrae.</div>') +
      '<div class="pop-actions">' +
      '<button class="btn" type="button" data-close>Cancelar</button>' +
      '<button class="btn primary" type="button" id="pop-save">Guardar</button>' +
      '</div>';

    openPopover(anchor, html, function (pop) {
      pop.querySelectorAll(".swatch").forEach(function (swatch) {
        swatch.addEventListener("click", function () {
          pop.querySelectorAll(".swatch").forEach(function (other) { other.classList.remove("selected"); });
          swatch.classList.add("selected");
          ui.slotColor = swatch.getAttribute("data-color");
        });
      });
      pop.querySelector("[data-close]").addEventListener("click", closePopover);
      pop.querySelector("#pop-save").addEventListener("click", function () {
        var material = pop.querySelector("#pop-material").value.trim();
        if (!material) { toast("Escribe un material", "warn"); return; }
        send({ type: "set-slot", slot: ui.slotIndex, material: material, color: ui.slotColor });
        closePopover();
      });
      var load = pop.querySelector("#pop-load");
      var unload = pop.querySelector("#pop-unload");
      if (load) { load.addEventListener("click", function () { filamentAction("load"); }); }
      if (unload) { unload.addEventListener("click", function () { filamentAction("unload"); }); }
    });
  }

  function filamentBusy() {
    if (ui.intent) { return true; }
    if (!ui.status) { return false; }
    var state = String(ui.status.state || "");
    return blocksMotion(KNOWN_STATES[state] ? state : "busy");
  }

  function filamentDisabled() {
    return filamentBusy()
      ? ' disabled title="No disponible mientras imprime o está en pausa"'
      : "";
  }

  function filamentAction(mode) {
    if (filamentBusy()) { return; }
    var slot = ui.slotIndex;
    var label = mode === "load" ? "Cargar filamento" : "Descargar filamento";
    var current = Number(ui.status && ui.status.nozzle ? ui.status.nozzle.current : 0) || 0;
    if (current < 170) {
      closePopover();
      openModal({
        title: "Aviso",
        text: "Calienta la boquilla por encima de 170 °C antes de cargar o descargar filamento.",
        confirmLabel: "OK",
        hideCancel: true,
        extraLabel: "Calentar a 200 °C",
        onExtra: function () { send({ type: "set-temps", nozzle: 200, bed: null }); }
      });
      return;
    }
    closePopover();
    openModal({
      title: label,
      text: mode === "load"
        ? "La impresora moverá filamento desde el slot " + (slot + 1) + " hacia el extrusor. ¿Continuar?"
        : "La impresora retraerá el filamento del extrusor hacia el slot " + (slot + 1) + ". ¿Continuar?",
      confirmLabel: mode === "load" ? "Cargar" : "Descargar",
      onConfirm: function () {
        send({ type: mode === "load" ? "ms-load" : "ms-unload", slot: slot });
      }
    });
  }

  /* ------------------------------------------------------------------ *
   *  Popovers de sensores
   * ------------------------------------------------------------------ */
  function tempPopover(anchor, kind) {
    var status = ui.status || {};
    var isNozzle = kind === "nozzle";
    var current = isNozzle ? (status.nozzle || {}) : (status.bed || {});
    var quick = isNozzle ? [0, 170, 200, 220, 250] : [0, 50, 60, 80, 100];
    var max = isNozzle ? 260 : 100;
    var label = isNozzle ? "Boquilla" : "Cama";
    var html =
      '<div class="pop-title">' + label + '</div>' +
      '<div class="pop-value"><b id="pop-current">' + num(current.current, 1) + '</b><small>°C actual · objetivo ' +
      num(current.target, 0) + ' °C</small></div>' +
      '<div class="pop-grid">' +
      quick.map(function (value) {
        return '<button class="btn sm" type="button" data-quick="' + value + '">' +
          (value === 0 ? "Off" : value + "°") + '</button>';
      }).join("") +
      '</div>' +
      '<div class="pop-row">' +
      '<input type="number" id="pop-temp" min="0" max="' + max + '" step="1" placeholder="' +
      Math.round(Number(current.target) || 0) + '" />' +
      '<button class="btn primary" type="button" id="pop-apply">Aplicar</button>' +
      '</div>' +
      '<div class="pop-actions">' +
      '<button class="btn danger-ghost" type="button" id="pop-off">Apagar</button>' +
      '</div>';

    openPopover(anchor, html, function (pop) {
      var input = pop.querySelector("#pop-temp");
      pop.querySelectorAll("[data-quick]").forEach(function (button) {
        button.addEventListener("click", function () {
          input.value = button.getAttribute("data-quick");
        });
      });
      pop.querySelector("#pop-apply").addEventListener("click", function () {
        var value = input.value.trim();
        var payload = { type: "set-temps" };
        if (value !== "") {
          var parsed = Number(value);
          if (!isFinite(parsed) || parsed < 0 || parsed > max) {
            toast("Temperatura fuera de rango (0–" + max + " °C)", "warn");
            return;
          }
          if (isNozzle) { payload.nozzle = parsed; } else { payload.bed = parsed; }
        } else if (Number(current.target) > 0) {
          if (isNozzle) { payload.nozzle = Number(current.target); } else { payload.bed = Number(current.target); }
        } else {
          toast("Indica una temperatura", "warn");
          return;
        }
        send(payload);
        closePopover();
      });
      pop.querySelector("#pop-off").addEventListener("click", function () {
        send({ type: "set-temps", nozzle: isNozzle ? 0 : null, bed: isNozzle ? null : 0 });
        closePopover();
      });
    });
  }

  function speedPopover(anchor) {
    var status = ui.status || {};
    var speed = Number(status.speed) || 0;
    var presets = [
      { value: 50, label: "Silent", icon: "activity" },
      { value: 100, label: "Standard", icon: "gauge" },
      { value: 124, label: "Sport", icon: "arrow-up" },
      { value: 166, label: "Ludicrous", icon: "alert" }
    ];
    var html =
      '<div class="pop-title">Velocidad de impresión</div>' +
      '<div class="pop-value"><b>' + num(speed, 0) + '</b><small>% actual</small></div>' +
      '<div class="pop-grid">' +
      presets.map(function (preset) {
        var active = Math.abs(speed - preset.value) <= 3;
        return '<button class="btn sm' + (active ? " active" : "") + '" type="button" data-speed="' +
          preset.value + '" aria-pressed="' + (active ? "true" : "false") + '">' +
          preset.label + " " + preset.value + '%</button>';
      }).join("") +
      '</div>' +
      '<div class="pop-row">' +
      '<input type="number" id="pop-speed" min="50" max="500" step="1" placeholder="' +
      (speed || 100) + '" />' +
      '<button class="btn primary" type="button" id="pop-apply">Aplicar</button>' +
      '</div>' +
      '<div class="hint">Presets de Bambu Studio. Rango del firmware: 50–500 %.</div>';

    openPopover(anchor, html, function (pop) {
      var input = pop.querySelector("#pop-speed");
      pop.querySelectorAll("[data-speed]").forEach(function (button) {
        button.addEventListener("click", function () {
          var value = Number(button.getAttribute("data-speed"));
          send({ type: "set-speed", percent: value });
          closePopover();
        });
      });
      pop.querySelector("#pop-apply").addEventListener("click", function () {
        var value = Number(input.value);
        if (!isFinite(value) || value < 50 || value > 500) {
          toast("Velocidad entre 50 y 500 %", "warn");
          return;
        }
        send({ type: "set-speed", percent: value });
        closePopover();
      });
    });
  }

  function fanPopover(anchor) {
    var status = ui.status || {};
    var html =
      '<div class="pop-title">Ventiladores</div>' +
      '<div class="field"><label for="pop-fan-part">Pieza (%)</label>' +
      '<input type="number" id="pop-fan-part" min="0" max="100" step="1" placeholder="' +
      (Number(status.fan_part) || 0) + '" /></div>' +
      '<div class="field"><label for="pop-fan-aux">Auxiliar (%)</label>' +
      '<input type="number" id="pop-fan-aux" min="0" max="100" step="1" placeholder="' +
      (Number(status.fan_aux) || 0) + '" /></div>' +
      '<div class="pop-grid">' +
      '<button class="btn sm" type="button" data-fan="0">0 %</button>' +
      '<button class="btn sm" type="button" data-fan="50">50 %</button>' +
      '<button class="btn sm" type="button" data-fan="100">100 %</button>' +
      '</div>' +
      '<div class="pop-actions">' +
      '<button class="btn" type="button" data-close>Cancelar</button>' +
      '<button class="btn primary" type="button" id="pop-apply">Aplicar</button>' +
      '</div>' +
      '<div class="hint">Vacío = no cambiar. 0 % apaga el ventilador.</div>';

    openPopover(anchor, html, function (pop) {
      var part = pop.querySelector("#pop-fan-part");
      var aux = pop.querySelector("#pop-fan-aux");
      pop.querySelectorAll("[data-fan]").forEach(function (button) {
        button.addEventListener("click", function () {
          part.value = button.getAttribute("data-fan");
          aux.value = button.getAttribute("data-fan");
        });
      });
      pop.querySelector("[data-close]").addEventListener("click", closePopover);
      pop.querySelector("#pop-apply").addEventListener("click", function () {
        var payload = { type: "set-fan" };
        if (part.value.trim() !== "") {
          var p = Number(part.value);
          if (!isFinite(p) || p < 0 || p > 100) { toast("Ventilador 0–100 %", "warn"); return; }
          payload.part = p;
        }
        if (aux.value.trim() !== "") {
          var a = Number(aux.value);
          if (!isFinite(a) || a < 0 || a > 100) { toast("Ventilador 0–100 %", "warn"); return; }
          payload.aux = a;
        }
        if (payload.part === undefined && payload.aux === undefined) {
          toast("Indica al menos un ventilador", "warn");
          return;
        }
        send(payload);
        closePopover();
      });
    });
  }

  function zOffsetPopover(anchor) {
    var status = ui.status || {};
    var current = Number(status.z_offset) || 0;
    var html =
      '<div class="pop-title">Compensación Z</div>' +
      '<div class="pop-value"><b id="pop-z">' + num(current, 2) + '</b><small>mm actual</small></div>' +
      '<div class="pop-row">' +
      '<button class="btn" type="button" data-delta="-0.05">−0,05</button>' +
      '<input type="number" id="pop-z-input" min="-5" max="5" step="0.01" value="' + current + '" />' +
      '<button class="btn" type="button" data-delta="0.05">+0,05</button>' +
      '</div>' +
      '<div class="pop-actions">' +
      '<button class="btn danger-ghost" type="button" id="pop-z-off">0,00</button>' +
      '<button class="btn primary" type="button" id="pop-apply">Aplicar</button>' +
      '</div>';

    openPopover(anchor, html, function (pop) {
      var input = pop.querySelector("#pop-z-input");
      pop.querySelectorAll("[data-delta]").forEach(function (button) {
        button.addEventListener("click", function () {
          var delta = Number(button.getAttribute("data-delta"));
          var next = Math.max(-5, Math.min(5, (Number(input.value) || 0) + delta));
          input.value = String(Math.round(next * 100) / 100);
        });
      });
      pop.querySelector("#pop-z-off").addEventListener("click", function () {
        send({ type: "set-zoffset", offset: 0 });
        closePopover();
      });
      pop.querySelector("#pop-apply").addEventListener("click", function () {
        var value = Number(input.value);
        if (!isFinite(value) || value < -5 || value > 5) { toast("Z-offset −5…+5 mm", "warn"); return; }
        send({ type: "set-zoffset", offset: value });
        closePopover();
      });
    });
  }

  /* ------------------------------------------------------------------ *
   *  Archivos
   * ------------------------------------------------------------------ */
  function requestFiles() {
    $("file-list").innerHTML = '<div class="files-empty">Cargando…</div>';
    ui.filesLoaded = false;
    send({ type: "list-files" });
  }

  function renderFiles(msg) {
    var list = $("file-list");
    ui.filesLoaded = true;
    list.innerHTML = "";
    if (!msg.ok) {
      var failure = document.createElement("div");
      failure.className = "files-empty";
      failure.textContent = msg.message || "No se pudo leer la lista.";
      list.appendChild(failure);
      return;
    }
    var files = Array.isArray(msg.files) ? msg.files : [];
    ui.files = files;
    if (!files.length) {
      list.innerHTML = '<div class="files-empty">No hay archivos guardados.</div>';
      return;
    }
    files.forEach(function (entry) {
      var card = document.createElement("div");
      card.className = "file-card";
      card.setAttribute("tabindex", "0");

      var thumb = document.createElement("div");
      thumb.className = "file-thumb";
      if (entry.thumb) {
        var img = document.createElement("img");
        img.src = entry.thumb;
        img.alt = "";
        thumb.appendChild(img);
      } else {
        var ph = document.createElement("span");
        ph.textContent = "sin miniatura";
        thumb.appendChild(ph);
      }
      card.appendChild(thumb);

      var info = document.createElement("div");
      info.className = "file-info";
      var name = document.createElement("div");
      name.className = "file-name";
      name.textContent = entry.name;
      name.title = entry.name;
      info.appendChild(name);

      var chips = document.createElement("div");
      chips.className = "file-chips";
      if (entry.printing_time_s) {
        var time = document.createElement("span");
        time.className = "file-chip";
        time.innerHTML = '<span class="icon" data-icon="clock"></span>' +
          fmtMin(Math.round(entry.printing_time_s / 60));
        chips.appendChild(time);
      }
      if (entry.filament_g) {
        var grams = document.createElement("span");
        grams.className = "file-chip";
        grams.innerHTML = '<span class="icon" data-icon="scale"></span>' + entry.filament_g + " g";
        chips.appendChild(grams);
      }
      (entry.tools || []).slice(0, 4).forEach(function (tool) {
        if (!isValidColor(tool.color)) { return; }
        var dot = document.createElement("span");
        dot.className = "tool-dot";
        dot.style.background = tool.color;
        dot.title = "T" + tool.tool_id + " · slot " + tool.slot_id;
        chips.appendChild(dot);
      });
      if (entry.use_station) {
        var ifs = document.createElement("span");
        ifs.className = "file-chip";
        ifs.textContent = "IFS";
        chips.appendChild(ifs);
      }
      info.appendChild(chips);
      card.appendChild(info);

      var overlay = document.createElement("div");
      overlay.className = "file-overlay";
      var print = document.createElement("button");
      print.type = "button";
      print.className = "btn primary sm";
      print.textContent = "Imprimir";
      var current = entry.name === msg.current_job;
      if (current) { print.disabled = true; print.textContent = "En curso"; }
      print.addEventListener("click", function (event) {
        event.stopPropagation();
        confirmStartFile(entry);
      });
      overlay.appendChild(print);
      card.appendChild(overlay);
      paintIcons(card);
      list.appendChild(card);
    });
  }

  function confirmStartFile(entry) {
    var state = String((ui.status && ui.status.state) || "");
    if (state === "completed" || state === "cancel" || state === "cancelled") {
      openModal({
        title: "La impresora espera confirmación",
        text: "La impresora terminó un trabajo y espera confirmación — ¿limpiar la plataforma e imprimir «"
          + entry.name + "»?",
        confirmLabel: "Limpiar e imprimir",
        onConfirm: function () {
          send({ type: "start-file", file: entry.name, leveling: true, clear_first: true });
        }
      });
      return;
    }
    var extra = entry.use_station ? " Se usarán los colores de la estación IFS." : "";
    openModal({
      title: "Imprimir «" + entry.name + "»",
      text: "La impresión empezará inmediatamente y la impresora debe estar libre." + extra,
      confirmLabel: "Imprimir",
      onConfirm: function () {
        send({ type: "start-file", file: entry.name, leveling: true });
      }
    });
  }

  /* ------------------------------------------------------------------ *
   *  Mantenimiento
   * ------------------------------------------------------------------ */
  /* ------------------------------------------------------------------ *
   *  Control de movimiento: dial XY, tira Z y extrusor
   * ------------------------------------------------------------------ */
  var DIAL = { cx: 129, cy: 129, outer: 105, inner: 58, home: 23, blank: 24 };
  var HOMING_KEY = "flashforge.homingNote.v1";

  function dialPoint(radius, degrees) {
    var radians = (degrees * Math.PI) / 180;
    return [DIAL.cx + radius * Math.cos(radians), DIAL.cy + radius * Math.sin(radians)];
  }

  function annularPath(rOuter, rInner, startDeg, endDeg) {
    function round(value) { return value.toFixed(2); }
    var so = dialPoint(rOuter, startDeg);
    var eo = dialPoint(rOuter, endDeg);
    var si = dialPoint(rInner, startDeg);
    var ei = dialPoint(rInner, endDeg);
    return "M " + round(so[0]) + " " + round(so[1]) +
      " A " + rOuter + " " + rOuter + " 0 0 1 " + round(eo[0]) + " " + round(eo[1]) +
      " L " + round(ei[0]) + " " + round(ei[1]) +
      " A " + rInner + " " + rInner + " 0 0 0 " + round(si[0]) + " " + round(si[1]) + " Z";
  }

  function buildDial() {
    var svg = $("dial-svg");
    if (!svg) { return; }
    var NS = "http://www.w3.org/2000/svg";
    function node(name, attrs, text) {
      var element = document.createElementNS(NS, name);
      Object.keys(attrs).forEach(function (key) { element.setAttribute(key, attrs[key]); });
      if (text) { element.textContent = text; }
      return element;
    }
    svg.innerHTML = "";

    var directions = [
      { axis: "y", sign: 1, start: -135, end: -45 },
      { axis: "x", sign: 1, start: -45, end: 45 },
      { axis: "y", sign: -1, start: 45, end: 135 },
      { axis: "x", sign: -1, start: 135, end: 225 }
    ];
    [
      { step: 10, r1: DIAL.inner, r2: DIAL.outer, extra: "" },
      { step: 1, r1: DIAL.blank, r2: DIAL.inner, extra: " inner" }
    ].forEach(function (ring) {
      directions.forEach(function (dir) {
        var delta = dir.sign * ring.step;
        var label = "Mover " + dir.axis.toUpperCase() + " " + (delta > 0 ? "+" : "") + delta + " mm";
        var path = node("path", {
          d: annularPath(ring.r2, ring.r1, dir.start, dir.end),
          "class": "jog-zone" + ring.extra,
          "data-axis": dir.axis,
          "data-delta": String(delta),
          role: "button",
          "aria-label": label,
          "aria-disabled": "false"
        });
        path.appendChild(node("title", {}, label));
        svg.appendChild(path);
      });
    });

    // Cruz diagonal a 45° y aros separadores (4 px), como AxisCtrlButton.
    [[-45, 135], [45, 225]].forEach(function (pair) {
      var from = dialPoint(DIAL.outer, pair[0]);
      var to = dialPoint(DIAL.outer, pair[1]);
      svg.appendChild(node("line", {
        x1: from[0], y1: from[1], x2: to[0], y2: to[1], "class": "jog-gap"
      }));
    });
    [DIAL.inner, DIAL.blank].forEach(function (radius) {
      svg.appendChild(node("circle", {
        cx: DIAL.cx, cy: DIAL.cy, r: radius, "class": "jog-gap"
      }));
    });

    var home = node("circle", {
      cx: DIAL.cx, cy: DIAL.cy, r: DIAL.home, "class": "jog-home",
      role: "button", "aria-label": "Hacer home de todos los ejes"
    });
    home.appendChild(node("title", {}, "Auto homing (todos los ejes)"));
    svg.appendChild(home);
    var house = node("g", { transform: "translate(" + DIAL.cx + " " + DIAL.cy + ")", "pointer-events": "none" });
    house.appendChild(node("path", {
      d: "M -7 0.5 L 0 -6.5 L 7 0.5 M -4.5 -0.5 v6.5 h9 v-6.5",
      fill: "none", stroke: "var(--accent)", "stroke-width": "2",
      "stroke-linecap": "round", "stroke-linejoin": "round"
    }));
    svg.appendChild(house);

    [{ text: "Y", radius: 88, deg: -90 }, { text: "X", radius: 88, deg: 0 },
     { text: "-Y", radius: 88, deg: 90 }, { text: "-X", radius: 88, deg: 180 }
    ].forEach(function (axis) {
      var pos = dialPoint(axis.radius, axis.deg);
      svg.appendChild(node("text", {
        x: pos[0], y: pos[1], "class": "dial-axis"
      }, axis.text));
    });
    [{ text: "+10", radius: 84, deg: -45 }, { text: "+1", radius: 44, deg: -45 },
     { text: "-1", radius: 44, deg: 135 }, { text: "-10", radius: 84, deg: 135 }
    ].forEach(function (num) {
      var pos = dialPoint(num.radius, num.deg);
      // Desplazadas ~6 px hacia el sector superior: fuera de la cruz blanca,
      // alineadas con la diagonal (como en AxisCtrlButton).
      var x = pos[0] - 6;
      var y = pos[1] - 6;
      svg.appendChild(node("text", {
        x: x, y: y, "class": "dial-num",
        transform: "rotate(-45 " + x + " " + y + ")"
      }, num.text));
    });

    svg.querySelectorAll(".jog-zone").forEach(function (zone) {
      zone.addEventListener("click", function () {
        jogAxis(zone.getAttribute("data-axis"), Number(zone.getAttribute("data-delta")), zone);
      });
    });
    home.addEventListener("click", openHomeModal);

    $("dial-xy").addEventListener("keydown", function (event) {
      if (ui.motionLocked) { return; }
      if (event.key === "Enter") { event.preventDefault(); openHomeModal(); return; }
      var moves = {
        ArrowUp: ["y", 1], ArrowDown: ["y", -1],
        ArrowRight: ["x", 1], ArrowLeft: ["x", -1]
      };
      var move = moves[event.key];
      if (!move) { return; }
      event.preventDefault();
      var step = event.shiftKey ? 10 : 1;
      var delta = move[1] * step;
      var zone = svg.querySelector('.jog-zone[data-axis="' + move[0] + '"][data-delta="' + delta + '"]');
      jogAxis(move[0], delta, zone);
    });
  }

  function flashZone(zone) {
    if (!zone) { return; }
    zone.classList.add("flash");
    setTimeout(function () { zone.classList.remove("flash"); }, 140);
  }

  function renderHomingChip() {
    var chip = $("homing-chip");
    if (!chip) { return; }
    if (ui.homed !== true && ui.homed !== false) {
      chip.hidden = true;
      return;
    }
    chip.hidden = false;
    chip.className = "chip homing-chip " + (ui.homed ? "ok" : "warn");
    setText("homing-chip-text", ui.homed ? "Homed ✓" : "Sin homing");
    chip.title = ui.homed
      ? "Finales de carrera confirmados por ~M119"
      : "La impresora no ha confirmado el homing (~M119)";
  }

  function openHomedRequiredModal() {
    openModal({
      title: "Ubica el cabezal",
      text: "La impresora no ha confirmado el homing (~M119). Pulsa «Ir a Home» para localizar los ejes antes de mover el cabezal.",
      confirmLabel: "Ir a Home",
      cancelLabel: "Cerrar",
      onConfirm: openHomeModal
    });
  }

  function requireHomed() {
    if (ui.homed === false) {
      openHomedRequiredModal();
      return false;
    }
    return true;
  }

  function jogAxis(axis, delta, zone) {
    if (ui.motionLocked) { return; }
    if (!requireHomed()) { return; }
    flashZone(zone);
    send({ type: "move", axis: axis, delta: delta });
  }

  function openHomeModal() {
    if (ui.motionLocked) { return; }
    openModal({
      title: "Auto homing",
      text: "¿Iniciar auto homing? Se moverán TODOS los ejes, porque la API de la impresora solo permite home completo.",
      confirmLabel: "Homing",
      onConfirm: function () { send({ type: "home" }); }
    });
  }

  function setMotionLocked(locked, reason) {
    ui.motionLocked = locked;
    var dial = $("dial-xy");
    dial.setAttribute("aria-disabled", locked ? "true" : "false");
    dial.tabIndex = locked ? -1 : 0;
    var zones = dial.querySelectorAll(".jog-zone, .jog-home");
    for (var z = 0; z < zones.length; z++) {
      zones[z].setAttribute("aria-disabled", locked ? "true" : "false");
    }
    var buttons = document.querySelectorAll("#motion-card .z-btn, #motion-card .e-btn");
    for (var i = 0; i < buttons.length; i++) { buttons[i].disabled = locked; }
    $("motion-lock-note").hidden = !(locked && (reason === "printing" || reason === "transient"));
    $("motion-lock-note").textContent = reason === "transient"
      ? "Controles bloqueados mientras la impresora trabaja."
      : "Movimiento bloqueado mientras imprime o está en pausa.";
    var offline = reason === "offline";
    $("motion-veil").hidden = !offline;
    ["m-cal-level", "m-cal-vibr", "btn-shutdown"].forEach(function (id) {
      var control = $(id);
      if (control) { control.disabled = offline; }
    });
    $("m-error-clear").disabled = offline ||
      !(ui.status && String(ui.status.error_code || "").trim());
    setText("control-mode", offline ? "sin conexión" : (locked ? "bloqueado" : "listo"));
  }

  function extrude(direction) {
    if (ui.motionLocked) { return; }
    if (!requireHomed()) { return; }
    var current = Number(ui.status && ui.status.nozzle ? ui.status.nozzle.current : 0) || 0;
    if (current < 170) {
      openModal({
        title: "Aviso",
        text: "Calienta la boquilla por encima de 170 °C antes de cargar o descargar filamento.",
        confirmLabel: "OK",
        hideCancel: true,
        extraLabel: "Calentar a 200 °C",
        onExtra: function () { send({ type: "set-temps", nozzle: 200, bed: null }); }
      });
      return;
    }
    // Paso fijo ±10 mm y ▲ = retraer / ▼ = extruir (semántica de Bambu).
    send({ type: "extrude", delta: direction * 10 });
  }

  function homingNoteSeen() {
    try { return sessionStorage.getItem(HOMING_KEY) === "1"; }
    catch (err) { return ui.homingNoticeSeen; }
  }
  function markHomingNoteSeen() {
    ui.homingNoticeSeen = true;
    try { sessionStorage.setItem(HOMING_KEY, "1"); } catch (err) {}
  }
  function resetHomingNote() {
    ui.homingNoticeSeen = false;
    try { sessionStorage.removeItem(HOMING_KEY); } catch (err) {}
    $("not-homed-note").hidden = true;
  }
  function maybeShowHomingNote() {
    if (ui.homed === true) {
      $("not-homed-note").hidden = true;
      return;
    }
    if (ui.homed === false) {
      $("not-homed-note").hidden = false;
      return;
    }
    if (ui.homingNoticeSeen || homingNoteSeen()) { return; }
    $("not-homed-note").hidden = false;
    markHomingNoteSeen();
  }

  function calibrate(mode) {
    var isLevel = mode === "level";
    openModal({
      title: isLevel ? "Nivelado de cama" : "Input shaper",
      text: "La impresora se moverá sola durante la calibración. No toques la máquina ni la mesa.",
      confirmLabel: "Iniciar",
      danger: true,
      ack: true,
      ackText: "Entiendo que la impresora se moverá sola.",
      onConfirm: function () {
        send({ type: "calibrate", leveling: isLevel, vibration: !isLevel });
      }
    });
  }

  function shutdownPopover(anchor) {
    var current = Number((ui.status || {}).shutdown_minutes) || 0;
    var options = [
      { value: 0, label: "Desactivado" },
      { value: 30, label: "30 min" },
      { value: 60, label: "60 min" },
      { value: 120, label: "120 min" }
    ];
    var html =
      '<div class="pop-title">Auto-apagado</div>' +
      '<div class="pop-grid" style="flex-direction:column;align-items:stretch">' +
      options.map(function (option) {
        var active = option.value === current;
        return '<button class="btn sm' + (active ? " active" : "") + '" type="button" data-shutdown="' +
          option.value + '" aria-pressed="' + (active ? "true" : "false") + '">' +
          option.label + "</button>";
      }).join("") +
      '</div>' +
      '<div class="hint">La impresora se apagará sola tras el tiempo indicado.</div>';
    openPopover(anchor, html, function (pop) {
      pop.querySelectorAll("[data-shutdown]").forEach(function (button) {
        button.addEventListener("click", function () {
          var minutes = Number(button.getAttribute("data-shutdown"));
          closePopover();
          setShutdown(minutes);
        });
      });
    });
  }

  function setShutdown(minutes) {
    openModal({
      title: minutes > 0 ? "Auto-apagado en " + minutes + " min" : "Desactivar auto-apagado",
      text: minutes > 0
        ? "La impresora se apagará sola tras " + minutes + " minutos."
        : "Se cancelará el apagado programado.",
      confirmLabel: "Aplicar",
      onConfirm: function () { send({ type: "set-shutdown", minutes: minutes }); }
    });
  }

  function clearError() {
    var code = String((ui.status || {}).error_code || "");
    if (!code) { return; }
    openModal({
      title: "Limpiar error " + code,
      text: "Se enviará el código exacto que reporta la impresora. Asegúrate de haber resuelto la causa.",
      confirmLabel: "Limpiar",
      onConfirm: function () { send({ type: "clear-errors", code: code }); }
    });
  }

  function stopJob() {
    var name = (ui.status && ui.status.job ? ui.status.job.name : "") || "el trabajo";
    openModal({
      title: "Detener la impresión",
      text: "Se cancelará «" + name + "». Esta acción no se puede deshacer.",
      confirmLabel: "Detener",
      danger: true,
      onConfirm: function () { send({ type: "control", action: "cancel" }); }
    });
  }

  /* ------------------------------------------------------------------ *
   *  Gráfica de temperaturas
   * ------------------------------------------------------------------ */
  var tempHistory = { max: 120, nozzle: [], nozzleTarget: [], bed: [], bedTarget: [] };
  function resetTempHistory() {
    tempHistory.nozzle = [];
    tempHistory.nozzleTarget = [];
    tempHistory.bed = [];
    tempHistory.bedTarget = [];
    drawChart();
  }
  function pushTempSample(nozzle, nozzleTarget, bed, bedTarget) {
    tempHistory.nozzle.push(nozzle);
    tempHistory.nozzleTarget.push(nozzleTarget);
    tempHistory.bed.push(bed);
    tempHistory.bedTarget.push(bedTarget);
    ["nozzle", "nozzleTarget", "bed", "bedTarget"].forEach(function (key) {
      while (tempHistory[key].length > tempHistory.max) { tempHistory[key].shift(); }
    });
  }
  function drawChart() {
    var canvas = $("temp-chart");
    var panel = $("panel-temps");
    if (!canvas) { return; }
    if (panel && !panel.open) { return; }
    var dpr = window.devicePixelRatio || 1;
    var width = canvas.clientWidth || 600;
    var height = 180;
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
    }
    var ctx = canvas.getContext("2d");
    if (!ctx) { return; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    var styles = getComputedStyle(document.body);
    var accent = (styles.getPropertyValue("--accent") || "#3b82f6").trim() || "#3b82f6";
    var muted = (styles.getPropertyValue("--muted") || "#9aa0a8").trim() || "#9aa0a8";
    var line = (styles.getPropertyValue("--line") || "#3a3d45").trim() || "#3a3d45";

    var padLeft = 34, padRight = 8, padTop = 8, padBottom = 16;
    var plotWidth = Math.max(10, width - padLeft - padRight);
    var plotHeight = Math.max(10, height - padTop - padBottom);
    var values = tempHistory.nozzle.concat(tempHistory.nozzleTarget, tempHistory.bed, tempHistory.bedTarget);
    var maxValue = 0;
    values.forEach(function (value) { if (value > maxValue) { maxValue = value; } });
    var yMax = Math.max(100, Math.ceil((maxValue + 10) / 50) * 50);

    function yPos(value) { return padTop + plotHeight - (Math.max(0, Math.min(yMax, value)) / yMax) * plotHeight; }
    function xPos(index, count) {
      return count <= 1 ? padLeft : padLeft + (index / (count - 1)) * plotWidth;
    }

    ctx.font = "10px system-ui, sans-serif";
    ctx.fillStyle = muted;
    ctx.strokeStyle = line;
    ctx.lineWidth = 1;
    for (var grid = 0; grid <= 5; grid++) {
      var gy = padTop + plotHeight - (grid / 5) * plotHeight;
      ctx.globalAlpha = 0.45;
      ctx.beginPath(); ctx.moveTo(padLeft, gy); ctx.lineTo(padLeft + plotWidth, gy); ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillText(String(Math.round((grid / 5) * yMax)), 4, gy + 3);
    }

    function drawLine(series, color, dashed) {
      if (series.length < 2) { return; }
      ctx.beginPath();
      ctx.strokeStyle = color;
      ctx.lineWidth = dashed ? 1.5 : 2;
      ctx.setLineDash(dashed ? [4, 3] : []);
      series.forEach(function (value, index) {
        var px = xPos(index, series.length), py = yPos(value);
        if (index === 0) { ctx.moveTo(px, py); } else { ctx.lineTo(px, py); }
      });
      ctx.stroke();
      ctx.setLineDash([]);
    }
    drawLine(tempHistory.nozzleTarget, accent, true);
    drawLine(tempHistory.bedTarget, "#0f766e", true);
    drawLine(tempHistory.nozzle, "#f97316", false);
    drawLine(tempHistory.bed, "#38bdf8", false);
    if (!values.length) {
      ctx.fillStyle = muted;
      ctx.fillText("Sin datos todavía", padLeft + 8, padTop + 20);
    }
  }

  /* ------------------------------------------------------------------ *
   *  Configuración multi-impresora
   * ------------------------------------------------------------------ */
  function fillPrinterSelect() {
    var select = $("printer-select");
    var printers = Array.isArray(cfg.printers) ? cfg.printers : [];
    select.innerHTML = "";
    if (!printers.length) {
      var empty = document.createElement("option");
      empty.value = "0";
      empty.textContent = "Sin impresora";
      select.appendChild(empty);
      select.disabled = true;
      return;
    }
    printers.forEach(function (printer, index) {
      var option = document.createElement("option");
      option.value = String(index);
      option.textContent = (printer.name || ("Impresora " + (index + 1)))
        + (printer.ip ? " · " + printer.ip : "");
      select.appendChild(option);
    });
    select.disabled = printers.length < 2;
    select.value = String(Number(cfg.active_printer) || 0);
  }

  function applyConfig(next) {
    var previous = cfg.active_printer;
    cfg = (next && typeof next === "object") ? next : {};
    intervalSec = Math.max(1, Number(cfg.poll_interval || cfg.interval) || 5);
    $("demo-badge").hidden = !cfg.mock;
    fillPrinterSelect();
    if (previous !== cfg.active_printer) {
      resetTempHistory();
      ui.homed = null;
      renderHomingChip();
    }
    var printer = (cfg.printers || [])[Number(cfg.active_printer) || 0];
    if (!printer || (!printer.ip && !cfg.mock)) {
      showBanner("warn",
        cfg.mock ? "Configura tu impresora; mientras tanto se muestran datos de demostración."
                 : "Configura tu impresora para empezar.",
        "Configurar", openDrawer, true);
    } else if (ui.bannerSticky) {
      hideBanner();
    }
    if (!$("drawer").classList.contains("open")) { fillForm(); }
  }

  function renderPrinterRows(printers, activeIndex) {
    var list = $("printer-list");
    list.innerHTML = "";
    if (!printers.length) {
      list.innerHTML = '<div class="hint">No hay impresoras. Añade una o pulsa «Buscar».</div>';
      return;
    }
    printers.forEach(function (printer, index) {
      var row = document.createElement("div");
      row.className = "printer-row";
      row.setAttribute("data-index", String(index));

      var head = document.createElement("div");
      head.className = "printer-row-head";
      var label = document.createElement("label");
      label.className = "check-inline";
      var radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "p-active";
      radio.value = String(index);
      radio.checked = index === activeIndex;
      label.appendChild(radio);
      label.appendChild(document.createTextNode(" Activa"));
      head.appendChild(label);
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn sm danger-ghost";
      remove.setAttribute("data-remove", String(index));
      remove.textContent = "Quitar";
      head.appendChild(remove);
      row.appendChild(head);

      function field(name, placeholder, wide, type) {
        var input = document.createElement("input");
        input.type = type || "text";
        input.setAttribute("data-field", name);
        input.placeholder = name === "check_code" && printer.has_check_code
          ? "•••••••• (guardado)" : placeholder;
        if (wide) { input.className = "wide"; }
        input.value = printer[name] || "";
        if (name === "check_code") { input.value = ""; }
        row.appendChild(input);
      }
      field("name", "Nombre (AD5X taller)", true);
      field("ip", "IP (192.168.1.50)");
      field("serial", "Número de serie");
      field("check_code", "Printer ID (8 caracteres)", true, "password");
      list.appendChild(row);
    });
  }

  function readRow(row) {
    var entry = {};
    ["name", "ip", "serial", "check_code"].forEach(function (name) {
      var input = row.querySelector('[data-field="' + name + '"]');
      var value = input ? input.value.trim() : "";
      if (name === "check_code") { if (value) { entry.check_code = value; } }
      else { entry[name] = value; }
    });
    return entry;
  }
  function readPrinters(keepEmpty) {
    var rows = document.querySelectorAll("#printer-list .printer-row");
    var printers = [];
    for (var i = 0; i < rows.length; i++) {
      var entry = readRow(rows[i]);
      if (keepEmpty || entry.name || entry.ip || entry.serial) { printers.push(entry); }
    }
    return printers;
  }
  function selectedPrinterIndex() {
    var radios = document.querySelectorAll('input[name="p-active"]');
    for (var i = 0; i < radios.length; i++) { if (radios[i].checked) { return Number(radios[i].value); } }
    return 0;
  }
  function fillForm() {
    var printers = (Array.isArray(cfg.printers) ? cfg.printers : []).map(function (printer) {
      return {
        name: printer.name || "", ip: printer.ip || "", serial: printer.serial || "",
        has_check_code: !!(printer.has_check_code || printer.check_code)
      };
    });
    renderPrinterRows(printers, Number(cfg.active_printer) || 0);
    $("f-interval").value = String(Math.round(intervalSec));
    $("f-mock").checked = !!cfg.mock;
    $("f-notify-finish").checked = cfg.notify_on_finish !== false;
    $("f-notify-error").checked = cfg.notify_on_error !== false;
  }
  function addPrinter() {
    var printers = readPrinters(true);
    printers.push({ name: "", ip: "", serial: "", check_code: "" });
    renderPrinterRows(printers, Math.min(selectedPrinterIndex(), printers.length - 1));
  }
  function removePrinter(index) {
    var printers = readPrinters(true);
    if (index < 0 || index >= printers.length) { return; }
    printers.splice(index, 1);
    renderPrinterRows(printers, Math.min(selectedPrinterIndex(), Math.max(0, printers.length - 1)));
  }
  function renderDiscovered(msg) {
    var box = $("discover-results");
    box.hidden = false;
    box.innerHTML = "";
    if (!msg.ok) {
      var failure = document.createElement("div");
      failure.className = "warn-text";
      failure.textContent = msg.message || "No se pudo buscar impresoras.";
      box.appendChild(failure);
      return;
    }
    var printers = Array.isArray(msg.printers) ? msg.printers : [];
    if (!printers.length) {
      var none = document.createElement("div");
      none.className = "hint";
      none.textContent = msg.message || "No se encontraron impresoras — introduce la IP manualmente.";
      box.appendChild(none);
      return;
    }
    var title = document.createElement("div");
    title.className = "hint";
    title.textContent = "Encontradas (toca para añadir):";
    box.appendChild(title);
    printers.forEach(function (printer) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "btn sm";
      button.textContent = (printer.model || printer.name || "Impresora")
        + " · " + printer.ip + (printer.serial ? " · " + printer.serial : "");
      button.addEventListener("click", function () {
        var printers2 = readPrinters(true);
        var exists = printers2.some(function (entry) { return entry.ip === printer.ip; });
        if (exists) { toast("Esa impresora ya está en la lista", "warn"); return; }
        printers2.push({
          name: printer.model || printer.name || "Impresora",
          ip: printer.ip || "", serial: printer.serial || "", check_code: ""
        });
        renderPrinterRows(printers2, Math.min(selectedPrinterIndex(), printers2.length - 1));
        toast("Añadida " + printer.ip + " — escribe su Printer ID");
      });
      box.appendChild(button);
    });
  }
  function openDrawer() {
    fillForm();
    $("drawer").classList.add("open");
    $("drawer-backdrop").classList.add("show");
    setTimeout(function () {
      try {
        var first = document.querySelector('#printer-list input[data-field="check_code"]');
        (first || $("p-add")).focus();
      } catch (err) {}
    }, 60);
  }
  function closeDrawer() {
    $("drawer").classList.remove("open");
    $("drawer-backdrop").classList.remove("show");
  }
  function saveForm() {
    var interval = Number($("f-interval").value);
    if (!isFinite(interval)) { interval = 5; }
    interval = Math.round(Math.min(120, Math.max(1, interval)));

    var rows = document.querySelectorAll("#printer-list .printer-row");
    var printers = [];
    var activeIndex = -1;
    for (var i = 0; i < rows.length; i++) {
      var entry = readRow(rows[i]);
      var isActive = rows[i].querySelector('input[name="p-active"]').checked;
      if (!entry.name && !entry.ip && !entry.serial) { continue; }
      if (isActive) { activeIndex = printers.length; }
      printers.push(entry);
    }
    if (printers.length && activeIndex < 0) { activeIndex = 0; }

    var btn = $("f-save");
    btn.disabled = true;
    setTimeout(function () { btn.disabled = false; }, 1200);
    if (send({
      type: "save-config",
      config: {
        printers: printers,
        active_printer: Math.max(0, activeIndex),
        poll_interval: interval,
        mock: $("f-mock").checked,
        notify_on_finish: $("f-notify-finish").checked,
        notify_on_error: $("f-notify-error").checked
      }
    })) {
      toast("Guardando configuración…");
    }
  }

  /* ------------------------------------------------------------------ *
   *  Mensajes del plugin
   * ------------------------------------------------------------------ */
  function onHostMessage(raw) {
    var msg = raw;
    if (typeof raw === "string") {
      try { msg = JSON.parse(raw); } catch (err) { return; }
    }
    if (!msg || typeof msg !== "object") { return; }

    switch (msg.type) {
      case "status":
        renderStatus(msg);
        break;
      case "intent":
        ui.intent = msg.intent || null;
        if (ui.status) {
          ui.status.intent = ui.intent;
          renderStatus(ui.status);
        }
        break;
      case "homing":
        ui.homed = msg.homed === true ? true : (msg.homed === false ? false : null);
        renderHomingChip();
        maybeShowHomingNote();
        break;
      case "ifs":
        ui.ifs = msg;
        ui.palette = Array.isArray(msg.palette) ? msg.palette : ui.palette;
        ui.materials = Array.isArray(msg.materials) ? msg.materials : ui.materials;
        fillMaterialList();
        renderSlots();
        break;
      case "config":
        applyConfig(msg.config || {});
        break;
      case "error":
        document.body.classList.remove("booting");
        if (msg.scope === "ifs") {
          toast(msg.message || "No se pudo leer el IFS", "warn");
        } else if (msg.needs_setup) {
          ui.connected = false;
          setMotionLocked(true, "offline");
          setConn("off", "Sin configurar");
          showBanner("warn", msg.message || "Configura tu impresora.", "Configurar", openDrawer, true);
        } else {
          ui.connected = false;
          setMotionLocked(true, "offline");
          var retry = Math.max(0, Math.round(Number(msg.retry_in) || 0));
          setConn("off", retry > 0
            ? "Error de conexión · reintentando… (" + retry + " s)"
            : "Error de conexión");
          showBanner("error", msg.message || "Error desconocido.", "Reintentar", function () {
            setConn("wait", "Reintentando…");
            send({ type: "refresh" });
          }, false);
        }
        break;
      case "config-saved":
        if (msg.ok) {
          if (msg.config) { applyConfig(msg.config); }
          closeDrawer();
          toast("Configuración guardada");
          send({ type: "refresh" });
        } else {
          showBanner("error", msg.message || "No se pudo guardar la configuración.", null, null, true);
        }
        break;
      case "control-ack":
        if (msg.ok) { toast("Orden enviada: " + (msg.action || "ok")); }
        else { showBanner("error", msg.message || "La impresora rechazó la orden.", null, null, false); }
        break;
      case "slot-saved":
        if (msg.ok) { toast(msg.message || "Slot actualizado"); }
        else { showBanner("error", msg.message || "No se pudo actualizar el slot.", null, null, false); }
        break;
      case "command-ack":
        if (msg.command === "clear-platform" || msg.command === "start-file") {
          setPlatformBusy(false);
        }
        if (msg.command === "clear-platform" && !msg.ok) {
          toast(msg.message || "No se pudo limpiar la plataforma.", "warn");
        }
        if (msg.ok) { toast(msg.message || "Orden enviada"); }
        else { showBanner("error", msg.message || "La impresora rechazó la orden.", null, null, false); }
        break;
      case "files":
        renderFiles(msg);
        break;
      case "upload-progress": {
        var total = Number(msg.total) || 0;
        var sent = Number(msg.sent) || 0;
        $("u-progress").hidden = false;
        $("u-send").disabled = true;
        $("u-progress-text").textContent = total > 0
          ? "Subiendo " + Math.round((sent / total) * 100) + " % ("
            + (sent / 1048576).toFixed(1) + " / " + (total / 1048576).toFixed(1) + " MB)"
          : "Subiendo…";
        $("u-bar").style.width = total > 0 ? Math.min(100, (sent / total) * 100) + "%" : "0%";
        break;
      }
      case "upload":
        $("u-send").disabled = false;
        $("u-progress").hidden = true;
        if (msg.ok) {
          toast(msg.message || "Archivo subido");
          $("u-path").value = "";
          requestFiles();
        } else {
          showBanner("error", msg.message || "No se pudo subir el archivo.", null, null, false);
        }
        break;
      case "notification":
        toast(msg.message || "Aviso", msg.level === "warning" ? "warn" : "ok");
        showBanner(msg.level === "warning" ? "warn" : "ok", msg.message || "Aviso",
                   null, null, false);
        break;
      case "discovering":
        $("discover-results").hidden = false;
        $("discover-results").textContent = "Buscando impresoras (2 s)…";
        break;
      case "discovered":
        renderDiscovered(msg);
        break;
      case "active-printer":
        if (msg.ok) { toast(msg.message || "Impresora cambiada"); }
        else { showBanner("error", msg.message || "No se pudo cambiar de impresora.", null, null, false); }
        break;
      case "notice":
        if (msg.message) { toast(msg.message); }
        break;
      default:
        break;
    }
  }

  function fillMaterialList() {
    var list = $("material-list");
    list.innerHTML = "";
    (ui.materials || []).forEach(function (material) {
      var option = document.createElement("option");
      option.value = String(material);
      list.appendChild(option);
    });
  }

  /* ------------------------------------------------------------------ *
   *  Watchdog y arranque
   * ------------------------------------------------------------------ */
  function watchdog() {
    if (ui.awaitingFirst) {
      if (Date.now() - ui.bootAt > 12000) { setConn("off", "Sin respuesta del plugin"); }
      return;
    }
    if (Date.now() - ui.lastStatusAt > Math.max(15000, intervalSec * 4000)) {
      setConn("off", "Sin respuesta del plugin");
    }
  }

  function boot() {
    paintIcons(document);
    buildDial();
    setMotionLocked(true, "offline");

    $("banner-btn").addEventListener("click", function () {
      if (typeof ui.bannerAction === "function") { ui.bannerAction(); }
    });

    $("btn-refresh").addEventListener("click", function () {
      setConn("wait", "Actualizando…");
      $("btn-refresh").classList.add("spinning");
      if (send({ type: "refresh" })) { ui.awaitingFirst = false; }
      setTimeout(function () { $("btn-refresh").classList.remove("spinning"); }, 900);
    });
    $("btn-config").addEventListener("click", openDrawer);
    $("drawer-close").addEventListener("click", closeDrawer);
    $("drawer-backdrop").addEventListener("click", closeDrawer);

    // Hero
    $("btn-pause").addEventListener("click", function () {
      var paused = $("btn-pause-label").textContent === "Reanudar";
      send({ type: "control", action: paused ? "resume" : "pause" });
    });
    $("btn-cancel").addEventListener("click", stopJob);
    $("btn-confirm-platform").addEventListener("click", confirmPlatform);
    $("btn-reprint").addEventListener("click", reprintCompleted);
    $("btn-clear-error").addEventListener("click", clearError);

    // Sensores → popovers
    $("sensor-nozzle").addEventListener("click", function () { tempPopover(this, "nozzle"); });
    $("sensor-bed").addEventListener("click", function () { tempPopover(this, "bed"); });
    $("sensor-speed").addEventListener("click", function () {
      if (this.disabled) { return; }
      speedPopover(this);
    });
    $("sensor-fan").addEventListener("click", function () { fanPopover(this); });

    // Mini controles
    $("btn-z").addEventListener("click", function () { zOffsetPopover(this); });
    $("btn-led").addEventListener("click", function () {
      send({ type: "set-led", on: $("btn-led").textContent === "Encender" });
    });

    // Cámara
    $("btn-cam").addEventListener("click", function () {
      var url = $("cam").getAttribute("data-url");
      if (!url) { toast("No hay cámara configurada", "warn"); return; }
      $("cam").removeAttribute("data-url");
      updateCamera(url);
    });
    $("cam").addEventListener("error", function () {
      $("cam").style.display = "none";
      var empty = $("cam-empty");
      empty.style.display = "flex";
      empty.textContent = "Sin señal de cámara. Revisa que esté activada y acepta el permiso del puerto 8080.";
    });

    // Control de movimiento
    $("e-retract").addEventListener("click", function () { extrude(-1); });
    $("e-extrude").addEventListener("click", function () { extrude(1); });
    document.querySelectorAll(".z-btn").forEach(function (button) {
      button.addEventListener("click", function () {
        if (ui.motionLocked) { return; }
        if (!requireHomed()) { return; }
        send({ type: "move", axis: "z", delta: Number(button.getAttribute("data-z")) });
      });
    });
    $("not-homed-home").addEventListener("click", openHomeModal);
    $("m-cal-level").addEventListener("click", function () { calibrate("level"); });
    $("m-cal-vibr").addEventListener("click", function () { calibrate("vibr"); });
    $("btn-shutdown").addEventListener("click", function () { shutdownPopover(this); });
    $("m-error-clear").addEventListener("click", clearError);

    // Archivos
    $("u-send").addEventListener("click", sendUpload);
    $("u-path").addEventListener("keydown", function (event) {
      if (event.key === "Enter") { sendUpload(); }
    });
    $("f-refresh").addEventListener("click", requestFiles);
    $("panel-files").addEventListener("toggle", function () {
      if ($("panel-files").open && !ui.filesLoaded) { requestFiles(); }
    });

    // Config
    $("printer-select").addEventListener("change", function () {
      send({ type: "set-active-printer", index: Number($("printer-select").value) });
    });
    $("p-add").addEventListener("click", addPrinter);
    $("p-discover").addEventListener("click", function () {
      $("discover-results").hidden = false;
      $("discover-results").textContent = "Buscando impresoras (2 s)…";
      send({ type: "discover" });
    });
    $("printer-list").addEventListener("click", function (event) {
      var target = event.target;
      var button = target && target.closest ? target.closest("[data-remove]") : null;
      if (button) { removePrinter(Number(button.getAttribute("data-remove"))); }
    });
    $("f-save").addEventListener("click", saveForm);
    $("f-close").addEventListener("click", closeDrawer);

    // Modal
    $("modal-cancel").addEventListener("click", closeModal);
    $("modal-confirm").addEventListener("click", function () {
      var action = ui.confirmAction;
      closeModal();
      if (typeof action === "function") { action(); }
    });
    $("modal-ack").addEventListener("change", function () {
      $("modal-confirm").disabled = !this.checked;
    });
    $("modal-backdrop").addEventListener("click", function (event) {
      if (event.target === $("modal-backdrop")) { closeModal(); }
    });
    $("modal-extra").addEventListener("click", function () {
      var action = ui.extraAction;
      closeModal();
      if (typeof action === "function") { action(); }
    });
    // Foco atrapado dentro del modal (sin trampas: Tab cicla).
    $("modal-backdrop").addEventListener("keydown", function (event) {
      if (event.key !== "Tab") { return; }
      var focusables = Array.prototype.slice
        .call($("modal-backdrop").querySelectorAll("button, input"))
        .filter(function (element) { return !element.disabled && element.offsetParent !== null; });
      if (!focusables.length) { return; }
      var first = focusables[0];
      var last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });

    // Popover: cerrar con el fondo o Escape
    $("pop-backdrop").addEventListener("click", closePopover);
    window.addEventListener("resize", repositionPopover);
    document.addEventListener("scroll", repositionPopover, true);

    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") { return; }
      if (ui.modalOpen) { closeModal(); return; }
      if (ui.popOpen) { closePopover(); return; }
      closeDrawer();
    });

    // Gráfica
    setInterval(drawChart, 1000);
    $("panel-temps").addEventListener("toggle", function () {
      if ($("panel-temps").open) { drawChart(); }
    });

    fillForm();
    fillPrinterSelect();

    if (!bridge || typeof bridge.onMessage !== "function") {
      document.body.classList.remove("booting");
      setConn("off", "Puente no disponible");
      showBanner("warn", "No se detectó window.orca. Abre esta pestaña desde OrcaSlicer.", null, null, true);
      return;
    }

    bridge.onMessage(onHostMessage);
    setConn("wait", "Conectando…");
    send({ type: "ready" });

    setInterval(function () {
      if (!document.hidden) { send({ type: "get-config" }); }
    }, 20000);
    setInterval(watchdog, 2000);
  }

  function sendUpload() {
    var path = $("u-path").value.trim();
    if (!path) { toast("Escribe la ruta del archivo", "warn"); $("u-path").focus(); return; }
    var printNow = $("u-print").checked;

    function submit() {
      if (!send({ type: "upload", path: path, print_now: printNow, leveling: true })) { return; }
      $("u-send").disabled = true;
      $("u-progress").hidden = false;
      $("u-progress-text").textContent = "Subiendo…";
      $("u-bar").style.width = "0%";
    }

    if (printNow) {
      openModal({
        title: "Subir e imprimir",
        text: "Se subirá «" + path + "» y la impresión empezará al terminar la subida.",
        confirmLabel: "Subir e imprimir",
        onConfirm: submit
      });
    } else {
      submit();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
</script>
</body>
</html>
"""


_CONFIG_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>FlashForge Monitor · Configuración</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #ffffff; --fg: #1d1f24; --muted: #6b7280;
    --border: #d9dce2; --surface: #f5f6f8; --surface-2: #eceef2;
    --accent: #2563eb; --accent-fg: #ffffff;
    --ok: #1f9d61; --bad: #d94a45; --warn: #b7791f;
    --shadow: 0 6px 18px rgba(15, 23, 42, .10);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #1b1c20; --fg: #e8e8ec; --muted: #9aa0a8;
      --border: #3a3d45; --surface: #232429; --surface-2: #2b2d33;
      --accent: #3b82f6; --accent-fg: #ffffff;
      --ok: #34c47c; --bad: #e05a55; --warn: #e0a02a;
      --shadow: 0 8px 22px rgba(0, 0, 0, .35);
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    padding: 16px 16px 96px; background: var(--bg); color: var(--fg);
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    font-size: 13px; line-height: 1.45;
  }
  ::-webkit-scrollbar { width: 9px; }
  ::-webkit-scrollbar-thumb { background: var(--surface-2); border-radius: 99px; }
  h1, h2 { margin: 0; font-weight: 650; }
  button, input { font-family: inherit; }
  button { cursor: pointer; }
  input[type=text], input[type=password], input[type=number] {
    background: var(--surface); border: 1px solid var(--border); border-radius: 9px;
    color: var(--fg); padding: 8px 10px; font-size: 13px; width: 100%;
  }
  input:focus-visible, button:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 2px;
  }
  .header { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
  .logo {
    width: 38px; height: 38px; border-radius: 11px; background: var(--accent); color: var(--accent-fg);
    display: flex; align-items: center; justify-content: center; box-shadow: 0 4px 12px rgba(59, 130, 246, .35);
  }
  .logo svg { width: 21px; height: 21px; }
  .header h1 { font-size: 15px; }
  .header .sub { font-size: 12px; color: var(--muted); }

  .section { margin-bottom: 18px; }
  .section-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
  .section-title { font-size: 11px; letter-spacing: .07em; text-transform: uppercase; color: var(--muted); font-weight: 650; }
  .section-actions { display: flex; gap: 6px; }

  .printer {
    border: 1px solid var(--border); border-radius: 12px; padding: 10px;
    background: var(--surface); display: grid; grid-template-columns: 1fr 1fr; gap: 7px;
    margin-bottom: 8px;
  }
  .printer .wide { grid-column: 1 / -1; }
  .printer-head { grid-column: 1 / -1; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .printer-head label { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--fg); }
  .empty { padding: 16px; text-align: center; color: var(--muted); border: 1px dashed var(--border); border-radius: 12px; font-size: 12.5px; }

  .check { display: flex; align-items: center; gap: 8px; font-size: 12.5px; margin-top: 9px; }
  .field { display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; }
  .field label { font-size: 11.5px; color: var(--muted); }
  .hint { font-size: 11.5px; color: var(--muted); margin-top: 6px; }
  .hint.warn { color: var(--warn); }

  .btn {
    display: inline-flex; align-items: center; gap: 6px; justify-content: center;
    border: 1px solid var(--border); background: var(--surface); color: var(--fg);
    border-radius: 9px; padding: 8px 13px; font-size: 12.5px; font-weight: 550;
    transition: background .15s, transform .15s, opacity .15s;
  }
  .btn:hover:not(:disabled) { background: var(--surface-2); transform: translateY(-1px); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .btn.primary { background: var(--accent); border-color: transparent; color: var(--accent-fg); }
  .btn.ghost { background: transparent; }
  .btn.sm { padding: 5px 9px; font-size: 12px; border-radius: 8px; }
  .btn.danger { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 55%, transparent); background: transparent; }

  .footer-actions {
    position: fixed; left: 0; right: 0; bottom: 0; padding: 12px 16px;
    background: color-mix(in srgb, var(--bg) 92%, transparent);
    border-top: 1px solid var(--border);
    display: flex; align-items: center; gap: 8px; justify-content: flex-end;
    backdrop-filter: blur(6px);
  }
  #status { font-size: 12px; color: var(--muted); margin-right: auto; }
  #status.err { color: var(--bad); }
  #status.ok { color: var(--ok); }

  .note {
    border: 1px solid var(--border); background: var(--surface); border-radius: 10px;
    padding: 10px 12px; font-size: 11.5px; color: var(--muted); margin-top: 14px;
  }
</style>
</head>
<body>
  <div class="header">
    <div class="logo" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
           stroke-linecap="round" stroke-linejoin="round">
        <path d="M6 9V3h12v6"/><rect x="3" y="9" width="18" height="7" rx="2"/>
        <path d="M7 16h10v5H7z"/><path d="M17 12h1"/>
      </svg>
    </div>
    <div>
      <h1>FlashForge Monitor</h1>
      <div class="sub">Impresoras y preferencias del plugin.</div>
    </div>
  </div>

  <div class="section">
    <div class="section-head">
      <span class="section-title">Impresoras</span>
      <span class="section-actions">
        <button id="add" class="btn sm" type="button">Añadir</button>
      </span>
    </div>
    <div id="printers"></div>
    <div class="hint">
      La impresora <strong>activa</strong> es la que se monitorea; las demás quedan guardadas.
    </div>
  </div>

  <div class="section">
    <div class="section-head"><span class="section-title">Preferencias</span></div>
    <div class="field">
      <label for="interval">Intervalo de sondeo (segundos)</label>
      <input type="number" id="interval" min="1" max="120" step="1" value="5" />
    </div>
    <label class="check" for="mock">
      <input type="checkbox" id="mock" checked />
      <span>Modo demo (datos simulados, sin impresora)</span>
    </label>
    <label class="check" for="notify_finish">
      <input type="checkbox" id="notify_finish" checked />
      <span>Avisar al terminar un trabajo</span>
    </label>
    <label class="check" for="notify_error">
      <input type="checkbox" id="notify_error" checked />
      <span>Avisar de errores de la impresora</span>
    </label>
  </div>

  <div class="note">
    El descubrimiento UDP («Buscar impresoras») está en el panel <strong>Configurar</strong>
    del dashboard: este formulario está aislado y solo puede leer y escribir la configuración.
    Tras actualizar el plugin, recuerda reactivarlo en el diálogo <strong>Plugins</strong> de OrcaSlicer.
  </div>

  <div class="footer-actions">
    <span id="status"></span>
    <button id="reload" class="btn ghost" type="button">Recargar</button>
    <button id="save" class="btn primary" type="button">Guardar</button>
  </div>

<script>
(function () {
  "use strict";

  var bridge = (typeof window !== "undefined" && window.orca) ? window.orca : null;
  var saveBtn = document.getElementById("save");
  var statusEl = document.getElementById("status");
  var current = { printers: [], active: 0 };

  function setStatus(kind, text) {
    statusEl.className = kind || "";
    statusEl.textContent = text || "";
  }
  function $(id) { return document.getElementById(id); }

  function makePrinterRow(printer, index, activeIndex) {
    var row = document.createElement("div");
    row.className = "printer";
    row.setAttribute("data-index", String(index));

    var head = document.createElement("div");
    head.className = "printer-head";
    var label = document.createElement("label");
    var radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "active";
    radio.value = String(index);
    radio.checked = index === activeIndex;
    label.appendChild(radio);
    label.appendChild(document.createTextNode(" Activa"));
    head.appendChild(label);
    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn sm danger";
    remove.setAttribute("data-remove", String(index));
    remove.textContent = "Quitar";
    head.appendChild(remove);
    row.appendChild(head);

    function addField(fieldName, placeholder, type, wide) {
      var input = document.createElement("input");
      input.type = type || "text";
      input.setAttribute("data-field", fieldName);
      input.placeholder = (fieldName === "check_code" && printer.has_check_code)
        ? "•••••••• (guardado)" : placeholder;
      if (wide) { input.className = "wide"; }
      if (fieldName !== "check_code") { input.value = printer[fieldName] || ""; }
      row.appendChild(input);
      return input;
    }
    addField("name", "Nombre (AD5X taller)", "text", true);
    addField("ip", "IP (192.168.1.50)");
    addField("serial", "Número de serie");
    var code = addField("check_code", "Printer ID (8 caracteres)", "password", true);
    var warning = document.createElement("div");
    warning.className = "hint warn";
    warning.style.gridColumn = "1 / -1";
    warning.hidden = true;
    warning.textContent = "El Printer ID suele tener 8 caracteres entre 0-9 y a-f.";
    row.appendChild(warning);
    code.addEventListener("input", function () {
      var value = code.value.trim();
      warning.hidden = !(value && !/^[a-fA-F0-9]{8}$/.test(value));
    });
    return row;
  }

  function readPrinters(keepEmpty) {
    var rows = document.querySelectorAll("#printers .printer");
    var printers = [];
    for (var i = 0; i < rows.length; i++) {
      var entry = {};
      ["name", "ip", "serial", "check_code"].forEach(function (name) {
        var input = rows[i].querySelector('[data-field="' + name + '"]');
        var value = input ? input.value.trim() : "";
        if (name === "check_code") { if (value) { entry.check_code = value; } }
        else { entry[name] = value; }
      });
      if (keepEmpty || entry.name || entry.ip || entry.serial) { printers.push(entry); }
    }
    return printers;
  }

  function activeIndex() {
    var radios = document.querySelectorAll('input[name="active"]');
    for (var i = 0; i < radios.length; i++) { if (radios[i].checked) { return Number(radios[i].value); } }
    return 0;
  }

  function render(printers, active) {
    var host = $("printers");
    host.innerHTML = "";
    if (!printers.length) {
      host.innerHTML = '<div class="empty">No hay impresoras. Pulsa «Añadir».</div>';
      return;
    }
    printers.forEach(function (printer, index) {
      host.appendChild(makePrinterRow(printer, index, active));
    });
  }

  function fromConfig(cfg) {
    var printers = (cfg.printers || []).map(function (printer) {
      return {
        name: printer.name || "",
        ip: printer.ip || "",
        serial: printer.serial || "",
        check_code: "",
        has_check_code: !!(printer.has_check_code || printer.check_code)
      };
    });
    return { printers: printers, active: Number(cfg.active_printer) || 0 };
  }

  async function loadConfig() {
    if (!bridge || typeof bridge.getConfig !== "function") {
      setStatus("err", "Puente de configuración no disponible.");
      return;
    }
    try {
      var raw = await bridge.getConfig();
      var cfg = {};
      if (raw) { cfg = (typeof raw === "string") ? JSON.parse(raw) : raw; }
      if (!cfg || typeof cfg !== "object") { cfg = {}; }

      current = fromConfig(cfg);
      render(current.printers, current.active);
      $("interval").value = String(Math.round(Number(cfg.poll_interval || cfg.interval) || 5));
      $("mock").checked = (cfg.mock === undefined) ? true : !!cfg.mock;
      $("notify_finish").checked = cfg.notify_on_finish !== false;
      $("notify_error").checked = cfg.notify_on_error !== false;
      setStatus("", "");
    } catch (err) {
      setStatus("err", "No se pudo leer la configuración: " + err);
    }
  }

  async function saveConfig() {
    if (!bridge || typeof bridge.saveConfig !== "function") {
      setStatus("err", "Puente de configuración no disponible.");
      return;
    }
    var interval = Number($("interval").value);
    if (!isFinite(interval)) { interval = 5; }
    interval = Math.round(Math.min(120, Math.max(1, interval)));

    var printers = readPrinters();
    var active = activeIndex();
    if (active >= printers.length) { active = Math.max(0, printers.length - 1); }

    var cfg = {
      printers: printers,
      active_printer: active,
      poll_interval: interval,
      mock: $("mock").checked,
      notify_on_finish: $("notify_finish").checked,
      notify_on_error: $("notify_error").checked
    };
    saveBtn.disabled = true;
    setStatus("", "Guardando…");
    try {
      await bridge.saveConfig(JSON.stringify(cfg));
      await loadConfig();
      setStatus("ok", "Guardado ✔");
    } catch (err1) {
      try {
        await bridge.saveConfig(cfg);
        await loadConfig();
        setStatus("ok", "Guardado ✔");
      } catch (err2) {
        setStatus("err", "No se pudo guardar: " + err2);
      }
    } finally {
      saveBtn.disabled = false;
    }
  }

  $("add").addEventListener("click", function () {
    var printers = readPrinters(true);
    printers.push({ name: "", ip: "", serial: "", check_code: "" });
    render(printers, Math.min(activeIndex(), printers.length - 1));
  });

  $("printers").addEventListener("click", function (ev) {
    var target = ev.target;
    var button = target && target.closest ? target.closest("[data-remove]") : null;
    if (!button) { return; }
    var printers = readPrinters(true);
    var index = Number(button.getAttribute("data-remove"));
    printers.splice(index, 1);
    render(printers, Math.min(activeIndex(), Math.max(0, printers.length - 1)));
  });

  saveBtn.addEventListener("click", saveConfig);
  $("reload").addEventListener("click", loadConfig);
  loadConfig();
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Lógica de la pestaña
# ---------------------------------------------------------------------------

#: Mensajes de la página que se ejecutan en el hilo de sondeo (nunca en UI).
ACTION_KINDS: frozenset[str] = frozenset({
    "set-temps",
    "set-speed",
    "set-zoffset",
    "set-fan",
    "set-led",
    "list-files",
    "start-file",
    "upload",
    "home",
    "move",
    "extrude",
    "calibrate",
    "clear-errors",
    "clear-platform",
    "set-shutdown",
    "ms-load",
    "ms-unload",
    "discover",
})

#: Texto por defecto del acuse de cada acción (el cliente puede sobrescribirlo).
_ACK_TEXT: dict[str, str] = {
    "set-temps": "Temperaturas aplicadas.",
    "set-speed": "Velocidad aplicada.",
    "set-zoffset": "Z-offset aplicado.",
    "set-fan": "Ventiladores actualizados.",
    "set-led": "LED actualizado.",
    "start-file": "Impresión iniciada.",
    "home": "Homing iniciado (la impresora se moverá).",
    "move": "Movimiento enviado.",
    "extrude": "Extrusión enviada.",
    "calibrate": "Calibración iniciada (la impresora se moverá sola).",
    "clear-errors": "Error limpiado.",
    "clear-platform": "Plataforma liberada. La impresora vuelve a ready.",
    "set-shutdown": "Auto-apagado actualizado.",
    "ms-load": "Cargando filamento (la impresora se moverá).",
    "ms-unload": "Descargando filamento (la impresora se moverá).",
}


class _FlashForgePageLogic:
    """Comportamiento de la capability `pages`.

    Se mezcla con ``orca.pages.PagesPluginCapabilityBase`` para poder importar
    este archivo fuera de OrcaSlicer (ver ``test_mock.py``). Nunca importa
    toolkits GUI: toda la UI es HTML servido por ``get_ui()``.
    """

    #: Etiqueta de la pestaña. Sin ';' (Orca lo prohíbe).
    PAGE_NAME = "FlashForge"

    # -- estado perezoso ----------------------------------------------------

    def _ensure_state(self) -> None:
        """Inicializa atributos sin depender del constructor del host."""
        if getattr(self, "_ff_ready", False):
            return
        self._ff_ready = True
        self._requests: queue.Queue[tuple[str, Any] | None] = queue.Queue()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._client: FlashForgeClient | None = None
        self._interval = DEFAULT_INTERVAL
        self._next_poll = 0.0
        self._last_job_name = ""       # para la miniatura getThum del trabajo actual
        self._last_job_active = False  # si ese trabajo sigue imprimiéndose
        self._last_upload_sent = 0     # control de mensajes de progreso de subida
        # Notificaciones (memoria del plugin, no se persisten).
        self._cfg: dict[str, Any] = dict(DEFAULT_CONFIG)
        self._prev_state = ""
        self._prev_job = ""
        self._prev_error = ""
        # Miniatura del trabajo actual: máximo una petición cada 30 s.
        self._job_thumb = ""
        self._job_thumb_name = ""
        self._job_thumb_at = 0.0
        # Mapeo tool→slot del trabajo en curso (una petición al empezar).
        self._job_tools: list[dict[str, Any]] = []
        self._job_tools_name = ""
        # Homing real por ~M119: primera consulta al conectar y luego cada 30 s.
        self._homed_next_at = 0.0
        self._homed_recheck_until = 0.0
        self._homed_last: bool | None = None
        self._homed_error_logged = False
        self._current_state = ""
        self._last_calibration = ""      # "level" | "vibr" | "" (disparo local)
        self._last_filament_action = ""  # "load" | "unload" | ""
        self._last_completed_job = ""
        self._last_completed_elapsed = 0
        self._consecutive_failures = 0   # backoff de red
        self._local_intent: dict[str, Any] | None = None
        self._intent_suppress_until = 0.0
        self._rescue_attempted = False

    # -- contrato de la capability -----------------------------------------

    def get_name(self) -> str:
        return self.PAGE_NAME

    def get_ui(self) -> str:
        """HTML del dashboard (se pide la primera vez que se muestra la pestaña)."""
        self._ensure_state()
        return _DASHBOARD_HTML.replace("{{VERSION}}", PLUGIN_VERSION)

    # -- configuración ------------------------------------------------------

    def get_default_config(self) -> dict[str, Any]:
        return dict(DEFAULT_CONFIG)

    def get_config_version(self) -> str:
        return CONFIG_VERSION

    def _data_dir_candidates(self) -> list[pathlib.Path]:
        """data_dir() por accessor documentado o, si no existe, por rutas conocidas."""
        found: list[pathlib.Path] = []
        # Accesor documentado: orca.host.plugin.storage() devuelve
        # data_dir()/orca_plugins/plugin_data/<plugin_key>.
        try:
            host = getattr(orca, "host", None)
            plugin = getattr(host, "plugin", None)
            storage = getattr(plugin, "storage", None)
            if callable(storage):
                path = pathlib.Path(str(storage())).expanduser()
                if path.parent.name == "plugin_data" and path.parent.parent.name == "orca_plugins":
                    found.append(path.parent.parent.parent)
        except Exception as exc:
            _log(f"rescate: plugin.storage() no disponible ({exc!r})")
        # Posibles accessors futuros/alternativos.
        for owner, attr in ((orca, "data_dir"),
                            (getattr(orca, "host", None), "data_dir"),
                            (getattr(orca, "paths", None), "data_dir")):
            try:
                getter = getattr(owner, attr, None)
                if callable(getter):
                    found.append(pathlib.Path(str(getter())).expanduser())
            except Exception:
                pass
        if not found:
            found.extend(_fallback_data_dirs())
        unique: list[pathlib.Path] = []
        for item in found:
            try:
                resolved = item.resolve()
            except OSError:
                resolved = item
            if resolved not in unique:
                unique.append(resolved)
        return unique

    def _rescue_orphan_config(self) -> dict[str, Any] | None:
        """Adopta la entrada 'FlashForge/pages' más completa de config.json."""
        best: tuple[tuple[Any, ...], dict[str, Any], str, str] | None = None
        for data_dir in self._data_dir_candidates():
            config_path = data_dir / "orca_plugins" / "config.json"
            try:
                raw = config_path.read_text(encoding="utf-8")
                data = json.loads(raw)
            except (OSError, ValueError, UnicodeError):
                continue
            entries = data.get("config") if isinstance(data, dict) else data
            if isinstance(entries, dict):
                entries = list(entries.values())
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("capability_type") or "") != "pages":
                    continue
                if str(entry.get("capability") or "") != "FlashForge":
                    continue
                cap = entry.get("cap_config")
                if not isinstance(cap, dict):
                    continue
                printers = cap.get("printers")
                if not isinstance(printers, list) or not printers:
                    continue
                version = entry.get("plugin_version") or entry.get("version") or ""
                score = (_version_key(version), len(printers), bool(cap.get("active_printer")))
                if best is None or score > best[0]:
                    best = (score, cap, str(config_path), str(entry.get("plugin_key") or ""))
        if best is None:
            return None
        _score, cap, source, key = best
        migrated, _changed = _migrate_config(dict(cap))
        cleaned = _sanitize_config(migrated)
        _log(f"rescate de config: adoptada {key!r} desde {source} "
             f"({len(cleaned.get('printers') or [])} impresora(s))")
        return cleaned

    def get_config(self) -> str:
        """Config v2 (defaults + guardada), siempre como JSON string.

        Si el host aún tiene el schema v1 (ip/serial/check_code en la raíz), se
        migra a ``printers[0]`` y se persiste, sin perder credenciales. Incluye
        los ``check_code`` porque el host los usa para persistir el formulario;
        lo que llega a la página pasa por :func:`_public_config`, que no los
        expone.
        """
        stored: dict[str, Any] = {}
        try:
            raw = super().get_config()  # type: ignore[misc]
        except Exception as exc:  # host sin config todavía / base sin método
            _log(f"get_config(): usando defaults ({exc!r})")
            raw = "{}"
        try:
            parsed = json.loads(raw) if raw else {}
            if isinstance(parsed, dict):
                stored = parsed
        except (TypeError, ValueError):
            _log("get_config(): la config guardada no es JSON válido; se ignoran sus valores")

        migrated, changed = _migrate_config(stored)
        if changed:
            _log("config v1 migrada a v2 (impresoras) conservando credenciales")
            try:
                super().save_config(json.dumps(migrated))  # type: ignore[misc]
            except Exception as exc:
                _log(f"no se pudo persistir la migración: {exc!r}")

        # Una sola vez por carga: rescata configs huérfanas del hub (Bug 2).
        if not getattr(self, "_rescue_attempted", False):
            self._rescue_attempted = True
            if not migrated.get("printers"):
                rescued = self._rescue_orphan_config()
                if rescued and rescued.get("printers"):
                    migrated = rescued
                    try:
                        super().save_config(json.dumps(migrated))  # type: ignore[misc]
                        _log("rescate de config: guardada en la capability actual")
                    except Exception as exc:
                        _log(f"rescate de config: no se pudo guardar ({exc!r})")
                elif rescued is None:
                    _log("rescate de config: no hay config huérfana de FlashForge")
        return json.dumps(migrated)

    def save_config(self, config: str) -> bool:
        """Valida y persiste la config v2. Devuelve False si el host falla.

        Acepta también el schema v1 por compatibilidad con formularios antiguos.
        """
        try:
            parsed = json.loads(config) if isinstance(config, str) else dict(config or {})
        except (TypeError, ValueError):
            _log("save_config(): JSON inválido recibido")
            return False
        if not isinstance(parsed, dict):
            return False

        incoming, _legacy = _migrate_config(parsed)
        current = _load_config_json(self.get_config())

        merged = dict(current)
        for key, value in incoming.items():
            if key == "printers":
                continue
            merged[key] = value
        merged["printers"] = _merge_printer_secrets(
            incoming.get("printers") or [], current.get("printers") or []
        )
        cleaned = _sanitize_config(merged)

        try:
            result = super().save_config(json.dumps(cleaned))  # type: ignore[misc]
        except Exception as exc:
            _log(f"save_config(): el host rechazó el guardado ({exc!r})")
            return False
        return bool(result) if result is not None else True

    def has_config_ui(self) -> bool:
        return True

    def get_config_ui(self) -> str:
        return _CONFIG_HTML

    # -- ciclo de vida ------------------------------------------------------

    def on_load(self) -> None:
        """Arranca el hilo de sondeo y le pasa la config actual.

        Si el host no invocara este hook, el worker arranca igualmente de forma
        perezosa con el primer ``{type:'ready'}`` de la página.
        """
        self._ensure_state()
        self._ensure_worker()
        try:
            # get_config() se lee aquí (hilo de UI) y el worker solo recibe datos.
            self._enqueue(("config", _load_config_json(self.get_config())))
        except Exception as exc:
            _log(f"on_load(): no se pudo enviar la config al worker ({exc!r})")

    def on_unload(self) -> None:
        """Detiene el hilo de sondeo."""
        self._ensure_state()
        self._stop_worker()

    # -- mensajes desde la página (hilo de UI: rápido) ----------------------

    def on_message(self, data: Any) -> None:
        self._ensure_state()
        try:
            if isinstance(data, (bytes, bytearray)):
                data = data.decode("utf-8", "replace")
            if isinstance(data, str):
                data = json.loads(data)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return

        msg_type = str(data.get("type") or "").strip()
        try:
            if msg_type == "ready":
                self._on_ready()
            elif msg_type == "refresh":
                self._on_refresh()
            elif msg_type == "get-config":
                self._on_get_config()
            elif msg_type == "save-config":
                self._on_save_config(data)
            elif msg_type == "control":
                self._on_control(data)
            elif msg_type == "set-slot":
                self._on_set_slot(data)
            elif msg_type == "set-active-printer":
                self._on_set_active_printer(data)
            elif msg_type == "discover":
                self._ensure_worker()
                self._enqueue(("discover", {}))
            elif msg_type in ACTION_KINDS:
                self._on_action(msg_type, data)
            else:
                _log(f"on_message(): tipo desconocido {msg_type!r}")
        except Exception as exc:  # nunca propagar al hilo de UI
            _log(f"on_message({msg_type!r}) falló: {exc!r}")
            self._post({"type": "error", "message": f"Error interno: {exc}"})

    # -- handlers concretos -------------------------------------------------

    def _on_ready(self) -> None:
        self._ensure_worker()
        cfg = _load_config_json(self.get_config())
        self._post({"type": "config", "config": _public_config(cfg)})
        self._enqueue(("config", cfg))

    def _on_refresh(self) -> None:
        cfg = _load_config_json(self.get_config())
        self._post({"type": "config", "config": _public_config(cfg)})
        self._ensure_worker()
        self._enqueue(("config", cfg))

    def _on_get_config(self) -> None:
        cfg = _load_config_json(self.get_config())
        self._post({"type": "config", "config": _public_config(cfg)})
        # Reenvía la config al worker por si cambió desde los ajustes nativos.
        if self._worker is not None and self._worker.is_alive():
            self._enqueue(("config", cfg))

    def _on_save_config(self, data: dict[str, Any]) -> None:
        raw_cfg = data.get("config")
        payload = raw_cfg if isinstance(raw_cfg, dict) else data
        ok = self.save_config(json.dumps(payload))
        cfg = _load_config_json(self.get_config())
        message: dict[str, Any] = {"type": "config-saved", "ok": ok}
        if ok:
            message["config"] = _public_config(cfg)
            self._enqueue(("config", cfg))
        else:
            message["message"] = "No se pudo guardar la configuración."
        self._post(message)

    def _on_control(self, data: dict[str, Any]) -> None:
        action = str(data.get("action") or "").strip()
        if action not in ("pause", "resume", "cancel"):
            self._post({"type": "control-ack", "action": action, "ok": False,
                        "message": f"Orden no válida: {action!r}"})
            return
        self._ensure_worker()
        self._enqueue(("control", action))

    def _on_set_slot(self, data: dict[str, Any]) -> None:
        try:
            slot = int(data.get("slot"))
        except (TypeError, ValueError):
            self._post({"type": "slot-saved", "ok": False, "message": "Slot inválido."})
            return
        material = str(data.get("material") or "").strip()[:32]
        color = str(data.get("color") or "").strip()[:16]
        if not material:
            self._post({"type": "slot-saved", "ok": False,
                        "message": "El material no puede estar vacío."})
            return
        self._ensure_worker()
        self._enqueue(("slot", {"slot": slot, "material": material, "color": color}))

    def _on_set_active_printer(self, data: dict[str, Any]) -> None:
        """Cambia la impresora activa y re-apunta el worker (un solo hilo)."""
        try:
            index = int(data.get("index"))
        except (TypeError, ValueError):
            self._post({"type": "active-printer", "ok": False,
                        "message": "Índice de impresora no válido."})
            return

        cfg = _load_config_json(self.get_config())
        printers = list(cfg.get("printers") or [])
        if not printers and cfg.get("mock"):
            # En demo sin impresoras configuradas, al elegir una se materializan
            # las de demostración para poder alternar entre ellas.
            printers = [dict(printer) for printer in MOCK_PRINTERS]
            cfg["printers"] = printers
        if not 0 <= index < len(printers):
            self._post({"type": "active-printer", "ok": False,
                        "message": "Esa impresora no existe."})
            return

        cfg["active_printer"] = index
        ok = self.save_config(json.dumps(cfg))
        fresh = _load_config_json(self.get_config())
        name = _clean_label((printers[index] or {}).get("name"), f"Impresora {index + 1}")
        self._post({
            "type": "active-printer",
            "ok": bool(ok),
            "index": int(fresh.get("active_printer") or 0),
            "message": f"Impresora activa: {name}" if ok else "No se pudo cambiar de impresora.",
        })
        if ok:
            self._post({"type": "config", "config": _public_config(fresh)})
            self._ensure_worker()
            self._enqueue(("config", _effective_config(fresh)))

    def _on_action(self, msg_type: str, data: dict[str, Any]) -> None:
        """Valida en el hilo de UI y encola la acción para el worker."""
        payload: dict[str, Any] = {}
        try:
            if msg_type == "set-temps":
                nozzle = data.get("nozzle")
                bed = data.get("bed")
                if nozzle in (None, "") and bed in (None, ""):
                    raise ValueError("indica la temperatura de boquilla o de cama")
                payload = {
                    "nozzle": None if nozzle in (None, "") else float(nozzle),
                    "bed": None if bed in (None, "") else float(bed),
                }
            elif msg_type == "set-speed":
                payload = {"percent": float(data.get("percent"))}
            elif msg_type == "set-zoffset":
                payload = {"offset": float(data.get("offset"))}
            elif msg_type == "set-fan":
                part = data.get("part")
                aux = data.get("aux")
                if part in (None, "") and aux in (None, ""):
                    raise ValueError("indica al menos un ventilador")
                payload = {
                    "part": None if part in (None, "") else float(part),
                    "aux": None if aux in (None, "") else float(aux),
                }
            elif msg_type == "set-led":
                payload = {"on": bool(data.get("on"))}
            elif msg_type == "list-files":
                payload = {"with_thumbs": bool(data.get("with_thumbs", True))}
            elif msg_type == "start-file":
                name = str(data.get("file") or "").strip()
                if not name:
                    raise ValueError("falta el nombre del archivo")
                payload = {"file": name[:255],
                           "leveling": bool(data.get("leveling", True)),
                           "clear_first": bool(data.get("clear_first", False))}
            elif msg_type == "upload":
                path = str(data.get("path") or "").strip()
                if not path:
                    raise ValueError("indica la ruta del archivo")
                payload = {
                    "path": path[:4096],
                    "print_now": bool(data.get("print_now")),
                    "leveling": bool(data.get("leveling", True)),
                }
            elif msg_type == "home":
                payload = {}
            elif msg_type == "move":
                payload = {"axis": str(data.get("axis") or "x").lower(),
                           "delta": float(data.get("delta"))}
            elif msg_type == "extrude":
                payload = {"delta": float(data.get("delta"))}
            elif msg_type == "calibrate":
                payload = {"leveling": bool(data.get("leveling", True)),
                           "vibration": bool(data.get("vibration", False))}
            elif msg_type == "clear-errors":
                payload = {"code": str(data.get("code") or "").strip()[:32]}
            elif msg_type == "clear-platform":
                payload = {}
            elif msg_type == "set-shutdown":
                payload = {"minutes": float(data.get("minutes") or 0)}
            elif msg_type in ("ms-load", "ms-unload"):
                slot = int(data.get("slot"))
                if not 0 <= slot < 4:
                    raise ValueError("slot fuera de rango (0-3)")
                payload = {"slot": slot}
        except (TypeError, ValueError) as exc:
            self._post({"type": "command-ack", "command": msg_type, "ok": False,
                        "message": f"Datos no válidos: {exc}"})
            return

        self._ensure_worker()
        self._enqueue((msg_type, payload))

    # -- hilo de sondeo -----------------------------------------------------

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            # Cada hilo recibe su propio Event y su propia cola: si un hilo
            # antiguo tarda en salir, no comparte estado con el nuevo.
            stop = threading.Event()
            requests: queue.Queue[tuple[str, Any] | None] = queue.Queue()
            self._stop = stop
            self._requests = requests
            self._next_poll = 0.0
            self._worker = threading.Thread(
                target=self._worker_loop,
                args=(stop, requests),
                name="FlashForgeMonitorPoller",
                daemon=True,
            )
            self._worker.start()

    def _stop_worker(self) -> None:
        try:
            self._stop.set()
            self._requests.put(None)
        except Exception:
            pass
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=3.0)
        self._worker = None

    def _enqueue(self, request: tuple[str, Any]) -> None:
        try:
            self._requests.put(request)
        except Exception as exc:
            _log(f"no se pudo encolar {request[0]}: {exc!r}")

    def _worker_loop(
        self,
        stop: threading.Event,
        requests: "queue.Queue[tuple[str, Any] | None]",
    ) -> None:
        """Bucle del hilo daemon: solo HTTP + post_message, nunca toca el host."""
        try:
            while not stop.is_set():
                # El timeout es la cadencia calculada: un 'refresh' o el stop
                # (None en la cola) despiertan el wait de inmediato.
                wait_for = max(0.05, self._next_poll - time.monotonic())
                try:
                    request = requests.get(timeout=wait_for)
                except queue.Empty:
                    request = None

                if stop.is_set():
                    break
                if request is None:
                    if time.monotonic() >= self._next_poll:
                        try:
                            self._poll_once()
                        except Exception as exc:  # el hilo nunca debe morir
                            _log(f"fallo inesperado en el sondeo: {exc!r}")
                        self._next_poll = time.monotonic() + self._poll_delay()
                    continue

                kind, payload = request
                try:
                    if kind == "config":
                        self._configure(payload)
                        self._poll_once()
                    elif kind == "poll":
                        self._poll_once()
                    elif kind == "control":
                        self._run_control(str(payload))
                        self._poll_once()
                    elif kind == "slot":
                        self._run_set_slot(payload)
                        self._poll_once()
                    elif kind in ACTION_KINDS:
                        if self._run_action(kind, payload):
                            self._poll_once()
                except Exception as exc:
                    _log(f"error procesando {kind}: {exc!r}")
                self._next_poll = time.monotonic() + self._poll_delay()
        except Exception as exc:  # el hilo no debe morir silenciosamente
            _log(f"el hilo de sondeo terminó por un error inesperado: {exc!r}")

    def _configure(self, cfg: dict[str, Any]) -> None:
        """(Re)crea el cliente para la impresora activa. Solo desde el worker."""
        effective = _effective_config(cfg)
        self._cfg = effective
        self._interval = clamp_interval(
            effective.get("poll_interval", DEFAULT_INTERVAL)
        )
        printer = _active_printer(effective)
        host = str(printer.get("ip") or "")
        serial = str(printer.get("serial") or "")
        check_code = str(printer.get("check_code") or "")
        mock = bool(effective.get("mock"))

        current = self._client
        if current is not None and current.matches(
            host=host, serial=serial, check_code=check_code, mock=mock
        ):
            return  # misma impresora: conservar estado (p. ej. progreso del demo)

        name, port = split_host_port(host, API_PORT)
        self._client = FlashForgeClient(
            host=name,
            port=port,
            serial=serial,
            check_code=check_code,
            mock=mock,
            timeout=REQUEST_TIMEOUT,
        )
        # Al cambiar de impresora no debe dispararse un "Terminó" falso.
        self._prev_state = ""
        self._prev_job = ""
        self._prev_error = ""
        self._job_thumb = ""
        self._job_thumb_name = ""
        self._job_thumb_at = 0.0
        self._job_tools = []
        self._homed_next_at = 0.0
        self._homed_recheck_until = 0.0
        self._homed_last = None
        self._homed_error_logged = False
        self._current_state = ""
        self._last_calibration = ""
        self._last_filament_action = ""
        self._last_completed_job = ""
        self._last_completed_elapsed = 0
        self._consecutive_failures = 0
        self._intent_suppress_until = 0.0
        if self._local_intent is not None:
            self._local_intent = None
            self._post({"type": "intent", "intent": None, "outcome": "printer-changed"})
        self._job_tools_name = ""

    def _intent_public(self) -> dict[str, Any] | None:
        intent = self._local_intent
        if not intent:
            return None
        return {
            "kind": intent["kind"],
            "slot": intent.get("slot"),
            "timeout": intent["timeout"],
            "started": intent["started_wall"],
        }

    def _set_intent(self, kind: str, slot: int | None = None) -> None:
        """Registra una intención local tras despachar el comando con éxito."""
        timeout = INTENT_TIMEOUTS.get(kind, 5 * 60)
        self._local_intent = {
            "kind": kind,
            "slot": slot,
            "started": time.monotonic(),
            "started_wall": time.time(),
            "timeout": timeout,
            "seen_active": False,
        }
        self._homed_next_at = 0.0  # fuerza un M119 inmediato
        self._post({"type": "intent", "intent": self._intent_public()})

    def _intent_label(self, kind: str) -> str:
        return {
            "leveling": "Nivelado",
            "input_shaper": "Input shaper",
            "load": "Carga de filamento",
            "unload": "Descarga de filamento",
            "homing": "Homing",
        }.get(kind, "Operación")

    def _finish_intent(self, outcome: str, error: str = "") -> None:
        """Cierra la intención con notificación (éxito/error) o en silencio."""
        intent = self._local_intent
        if not intent:
            return
        kind = str(intent.get("kind") or "")
        slot = intent.get("slot")
        self._local_intent = None
        self._last_calibration = ""
        self._last_filament_action = ""
        self._intent_suppress_until = time.monotonic() + 5.0
        self._homed_next_at = 0.0
        self._post({"type": "intent", "intent": None, "outcome": outcome})

        if outcome == "success":
            if kind == "leveling":
                text = "✓ Nivelado completado"
            elif kind == "input_shaper":
                text = "✓ Input shaper completado"
            elif kind == "load":
                text = f"✓ Filamento cargado (slot {slot})" if slot else "✓ Filamento cargado"
            elif kind == "unload":
                text = f"✓ Filamento descargado (slot {slot})" if slot else "✓ Filamento descargado"
            elif kind == "homing":
                text = "✓ Homing completado"
            else:
                text = "✓ Operación completada"
            self._notify("info", text)
        elif outcome == "error":
            suffix = f" ({error})" if error else ""
            if kind in ("leveling", "input_shaper"):
                text = f"Calibración terminó con error{suffix}"
            elif kind in ("load", "unload"):
                text = f"Carga/descarga de filamento terminó con error{suffix}"
            else:
                text = f"Homing terminó con error{suffix}"
            self._notify("warning", text)
        elif outcome == "timeout":
            activity = {
                "leveling": "la nivelación",
                "input_shaper": "el input shaper",
                "load": "la carga de filamento",
                "unload": "la descarga de filamento",
                "homing": "el homing",
            }.get(kind, "la operación")
            self._notify("info", f"No se confirmó el fin de {activity}; revisa la impresora.")
        # outcome "disconnect"/"printer-changed": sin aviso.

    def _update_intent_from_status(self, state: str, error: str) -> None:
        """Cierra la intención con /detail; M119 puede cerrarla antes."""
        intent = self._local_intent
        if not intent:
            return
        elapsed = time.monotonic() - float(intent["started"])
        if error:
            self._finish_intent("error", error)
            return
        if state in TRANSIENT_STATES or state == "printing":
            intent["seen_active"] = True
            return
        if state in ("ready", "completed", "finish", "idle"):
            if intent["seen_active"] or elapsed >= INTENT_GRACE:
                self._finish_intent("success")
        elif state in ("cancel", "cancelled"):
            self._finish_intent("disconnect")

    def _poll_delay(self) -> float:
        """Cadencia adaptativa: backoff > transitorio/impresión > idle."""
        return next_delay(self._current_state, self._consecutive_failures, self._interval)

    def _poll_once(self) -> None:
        client = self._client
        if client is None:
            self._post({
                "type": "error",
                "needs_setup": True,
                "message": "Configura tu impresora para empezar a monitorear.",
            })
            return

        started = time.monotonic()
        try:
            # Una sola petición a /detail: estado y IFS salen del mismo JSON.
            snapshot = client.get_snapshot()
        except FlashForgeError as exc:
            if exc.kind in ("network", "timeout", "tcp"):
                self._consecutive_failures += 1
                if self._local_intent is not None:
                    self._finish_intent("disconnect")
            else:
                # La impresora respondió (auth/parámetros): no es un fallo de red.
                self._consecutive_failures = 0
            self._post_error(exc, client)
            return
        except Exception as exc:  # defensa: el hilo no debe caer
            _post_unexpected(self, "get_snapshot", exc)
            return

        status = snapshot.get("status")
        if not isinstance(status, dict):
            _post_unexpected(self, "get_snapshot", TypeError("respuesta no es un dict"))
            return
        self._consecutive_failures = 0
        raw_state = str(status.get("state") or "")
        state = normalize_state(raw_state)
        if state != raw_state:
            status["raw_state"] = raw_state
            status["state"] = state
        self._current_state = state
        self._update_intent_from_status(
            state, str(status.get("error_code") or "").strip()
        )

        job_name = str((status.get("job") or {}).get("name") or "")
        if job_name and job_name != "—":
            self._last_job_name = job_name
        if state in ("completed", "cancel", "cancelled"):
            completed_name = job_name if job_name and job_name != "—" else self._last_job_name
            if completed_name:
                self._last_completed_job = completed_name
            elapsed = _to_int((status.get("job") or {}).get("elapsed_s"))
            if elapsed > 0:
                self._last_completed_elapsed = elapsed
        self._last_job_active = str(status.get("state") or "") in (
            "printing", "pausing", "pause", "paused", "resuming", "heating",
        )
        self._maybe_fetch_thumb(client, status)
        self._maybe_fetch_job_tools(client, job_name)

        payload = dict(status)
        payload.update({
            "type": "status",
            "connected": True,
            "mock": client.mock,
            "host": client.host,
            "camera_url": snapshot.get("camera_url") or client.get_camera_url(),
            # Miniatura del trabajo en curso (data URI, se refresca cada 30 s).
            "job_thumb": self._job_thumb if self._last_job_active else "",
            "job_tools": self._job_tools if self._last_job_active else [],
            "last_calibration": self._last_calibration,
            "last_filament_action": self._last_filament_action,
            "intent": self._intent_public(),
            "completed_job": self._last_completed_job,
            "completed_elapsed_s": self._last_completed_elapsed,
            "ts": time.time(),
            "latency_ms": int((time.monotonic() - started) * 1000),
        })
        self._post(payload)
        self._maybe_check_homing(client)
        self._maybe_notify(status)

        ifs = snapshot.get("ifs")
        if not isinstance(ifs, dict):
            return
        self._post({
            "type": "ifs",
            "slots": ifs.get("slots", []),
            "active_slot": ifs.get("active_slot"),
            "has_station": ifs.get("has_station"),
            "slot_count": ifs.get("slot_count"),
            "palette": [{"name": name, "hex": hex_value} for name, hex_value in PALETTE],
            "materials": list(MATERIALS),
            "mock": client.mock,
            "ts": time.time(),
        })

    def _maybe_check_homing(self, client: FlashForgeClient) -> None:
        """Consulta ~M119 como máximo cada 30 s (o cada 5 s tras un home)."""
        now = time.monotonic()
        rechecking = now < self._homed_recheck_until
        intent = self._local_intent
        if intent is not None and now - float(intent["started"]) >= intent["timeout"]:
            self._finish_intent("timeout")
            intent = None
        if now < self._homed_next_at:
            return
        result = client.get_endstops()
        self._homed_next_at = now + (2.0 if intent is not None
                                     else (5.0 if rechecking else 30.0))
        homed = result.get("homed") if result.get("ok") else None
        if homed is None and not self._homed_error_logged:
            self._homed_error_logged = True
            _log(f"~M119 no disponible; homing real desactivado ({result.get('error')})")
        if homed is not None:
            self._homed_error_logged = False
        if intent is not None and result.get("ok"):
            machine = str(result.get("machine_status") or "").upper()
            move = str(result.get("move_mode") or "").upper()
            if machine not in ("", "READY") or move not in ("", "READY"):
                intent["seen_active"] = True
            elif intent["seen_active"]:
                self._finish_intent("success")
        self._homed_last = homed
        self._post({
            "type": "homing",
            "homed": homed,
            "x": result.get("x"),
            "y": result.get("y"),
            "z": result.get("z"),
            "machine_status": result.get("machine_status") or "",
            "move_mode": result.get("move_mode") or "",
            "source": "mock" if result.get("mock") else "m119",
            "ts": time.time(),
        })

    # -- notificaciones y miniatura ---------------------------------------

    def _notify(self, level: str, text: str) -> None:
        """Aviso al dashboard y, si el host lo expone, notificación nativa.

        El host de OrcaSlicer hace el marshal a la UI thread por nosotros; si
        ``orca.host.ui.push_notification`` no existe en esta versión, se degrada
        a solo banner (try/except silencioso en el log).
        """
        self._post({"type": "notification", "level": level, "message": text,
                    "ts": time.time()})
        if orca is None:  # fuera de OrcaSlicer (tests)
            return
        try:
            host_ui = getattr(getattr(orca, "host", None), "ui", None)
            push = getattr(host_ui, "push_notification", None)
            if callable(push):
                push(level, text)
        except Exception as exc:
            _log(f"push_notification() no disponible: {exc!r}")

    def _maybe_notify(self, status: dict[str, Any]) -> None:
        """Compara el ciclo anterior con el actual (avisos de fin y de error)."""
        cfg = self._cfg or {}
        state = str(status.get("state") or "")
        job = str((status.get("job") or {}).get("name") or "")
        error = str(status.get("error_code") or "").strip()

        working = {"printing", "pausing", "pause", "paused", "resuming"}
        previous = self._prev_state
        calibration_error = False
        intent_suppressed = time.monotonic() < self._intent_suppress_until

        if previous and previous != state and previous in working and state not in working:
            if state != "error" and cfg.get("notify_on_finish", True) and job:
                if state in ("ready", "completed", "finish"):
                    self._notify("info", f"Terminó: {job}")
                elif state in ("cancel", "cancelled"):
                    self._notify("info", f"Trabajo cancelado: {job}")

        # Transitorios: completado/error se avisan SIEMPRE (núcleo de la UX).
        if previous == "calibrate_doing" and state != previous and not intent_suppressed:
            if state == "error":
                calibration_error = True
                suffix = f" ({error})" if error else ""
                self._notify("warning", f"Calibración terminó con error{suffix}")
            elif state in ("ready", "completed", "finish"):
                label = {"level": "Nivelado completado",
                         "vibr": "Input shaper completado"}.get(
                             self._last_calibration, "Calibración completada")
                self._notify("info", f"✓ {label}")

        if previous == "heating" and state in ("ready", "completed", "finish"):
            self._notify("info", "Temperatura lista")

        if previous == "loading" and state in ("ready", "completed", "finish") \
                and not intent_suppressed:
            label = {"load": "Filamento cargado",
                     "unload": "Filamento descargado"}.get(
                         self._last_filament_action, "Carga/descarga completada")
            self._notify("info", f"✓ {label}")

        if error and error != self._prev_error and cfg.get("notify_on_error", True) \
                and not calibration_error and not intent_suppressed:
            self._notify("warning", f"Error de la impresora: {error}")

        if state != "calibrate_doing":
            self._last_calibration = ""
        if state != "loading":
            self._last_filament_action = ""

        self._prev_state = state
        self._prev_job = job
        self._prev_error = error

    def _maybe_fetch_job_tools(self, client: FlashForgeClient, job: str) -> None:
        """Mapeo tool→slot del trabajo en curso (una petición al cambiar)."""
        if not self._last_job_active or not job:
            self._job_tools = []
            self._job_tools_name = ""
            return
        if job == self._job_tools_name:
            return
        try:
            self._job_tools = client.get_file_tools(job)
        except FlashForgeError as exc:
            _log(f"sin mapping de tools para {job!r}: {exc}")
            self._job_tools = []
        self._job_tools_name = job

    def _maybe_fetch_thumb(self, client: FlashForgeClient, status: dict[str, Any]) -> None:
        """Miniatura del trabajo en curso vía GET /getThum (máx. cada 30 s)."""
        job = str((status.get("job") or {}).get("name") or "")
        if not self._last_job_active or not job:
            self._job_thumb = ""
            self._job_thumb_name = ""
            return
        now = time.monotonic()
        if job == self._job_thumb_name and (now - self._job_thumb_at) < 30.0:
            return
        try:
            thumb = client.get_current_thumb()
        except FlashForgeError as exc:
            _log(f"getThum: {exc}")
            thumb = ""
        self._job_thumb = thumb
        self._job_thumb_name = job
        self._job_thumb_at = now

    def _run_discovery(self) -> None:
        """Descubrimiento UDP de un intento (no requiere credenciales)."""
        self._post({"type": "discovering", "ts": time.time()})
        try:
            printers = discover_printers(DISCOVERY_TIMEOUT)
        except FlashForgeError as exc:
            self._post({"type": "discovered", "ok": False, "printers": [],
                        "message": str(exc), "kind": exc.kind})
            return
        except Exception as exc:
            _post_unexpected(self, "discover_printers", exc)
            return
        if printers:
            message = f"{len(printers)} impresora(s) encontrada(s)."
        else:
            message = ("No se encontraron impresoras. Si estás en Flatpak, el "
                       "broadcast puede estar bloqueado: introduce la IP manualmente.")
        self._post({"type": "discovered", "ok": True, "printers": printers,
                    "message": message})

    def _run_control(self, action: str) -> None:
        client = self._client
        if client is None:
            self._post({"type": "control-ack", "action": action, "ok": False,
                        "message": "Configura la impresora antes de enviar órdenes."})
            return
        try:
            result = client.send_control(action)
        except FlashForgeError as exc:
            self._post({"type": "control-ack", "action": action, "ok": False,
                        "message": str(exc)})
            return
        except Exception as exc:
            _post_unexpected(self, "send_control", exc)
            return
        self._post({"type": "control-ack", "action": action,
                    "ok": bool(result.get("ok", True)),
                    "message": result.get("message") or "Orden enviada."})

    def _run_set_slot(self, payload: dict[str, Any]) -> None:
        client = self._client
        if client is None:
            self._post({"type": "slot-saved", "ok": False,
                        "message": "Configura la impresora antes de editar slots."})
            return
        try:
            result = client.set_slot_material(
                int(payload.get("slot")),
                str(payload.get("material") or ""),
                str(payload.get("color") or ""),
            )
        except FlashForgeError as exc:
            self._post({"type": "slot-saved", "ok": False, "message": str(exc)})
            return
        except Exception as exc:
            _post_unexpected(self, "set_slot_material", exc)
            return

        message = "Slot actualizado."
        if result.get("in_palette") is False:
            message = ("Slot actualizado, pero el color no está en la paleta de 24: "
                       "la pantalla de la impresora lo dibujará blanco.")
        self._post({"type": "slot-saved", "ok": True,
                    "slot": result.get("slot"), "message": message})

    # -- acciones de los módulos 1–3 (se ejecutan en el worker) -------------

    @staticmethod
    def _wait_until_ready(client: FlashForgeClient, timeout: float) -> bool:
        """Espera a que /detail vuelva a ``ready`` (sondeo cada 2 s, tope dado)."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                status = client.get_status()
            except FlashForgeError:
                pass
            else:
                if normalize_state(status.get("state")) == "ready":
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))

    def _run_action(self, kind: str, payload: dict[str, Any]) -> bool:
        """Ejecuta una acción de la página. Devuelve True si conviene re-sondear."""
        if kind == "discover":
            self._run_discovery()
            return False

        client = self._client
        if client is None:
            self._post({"type": "command-ack", "command": kind, "ok": False,
                        "message": "Configura la impresora antes de enviar órdenes."})
            return False

        if kind == "list-files":
            self._run_list_files(client, payload)
            return False
        if kind == "upload":
            self._run_upload(client, payload)
            return False

        try:
            result: dict[str, Any]
            if kind == "set-temps":
                result = client.set_temperatures(payload.get("nozzle"), payload.get("bed"))
            elif kind == "set-speed":
                result = client.set_print_speed(payload["percent"])
            elif kind == "set-zoffset":
                result = client.set_z_offset(payload["offset"])
            elif kind == "set-fan":
                result = client.set_fans(payload.get("part"), payload.get("aux"))
            elif kind == "set-led":
                result = client.set_led(payload["on"])
            elif kind == "start-file":
                if payload.get("clear_first"):
                    client.clear_platform()
                    if not self._wait_until_ready(client, 10.0):
                        raise FlashForgeError(
                            "busy",
                            "La impresora no volvió a ready tras limpiar la plataforma "
                            "(10 s). Confirma en la pantalla de la impresora e inténtalo de nuevo.",
                        )
                result = client.start_file(payload["file"], leveling=payload["leveling"])
            elif kind == "home":
                result = client.home_axes()
            elif kind == "clear-platform":
                result = client.clear_platform()
            elif kind == "move":
                result = client.move_axis(payload["axis"], payload["delta"])
            elif kind == "extrude":
                result = client.extrude_filament(payload["delta"])
            elif kind == "calibrate":
                self._last_calibration = ("level" if payload.get("leveling")
                                          else ("vibr" if payload.get("vibration") else ""))
                result = client.start_calibration(payload["leveling"], payload["vibration"])
            elif kind == "clear-errors":
                result = client.clear_errors(payload.get("code") or None)
            elif kind == "set-shutdown":
                result = client.set_auto_shutdown(payload["minutes"])
            elif kind == "ms-load":
                self._last_filament_action = "load"
                result = client.load_filament(payload["slot"])
            elif kind == "ms-unload":
                self._last_filament_action = "unload"
                result = client.unload_filament(payload["slot"])
            else:
                raise FlashForgeError("invalid", f"Acción desconocida: {kind}")
        except FlashForgeError as exc:
            self._post({"type": "command-ack", "command": kind, "ok": False,
                        "message": str(exc)})
            return False
        except Exception as exc:
            _post_unexpected(self, kind, exc)
            return False

        if kind == "home":
            self._set_intent("homing")
            # El home es asíncrono: refresca ~M119 cada 5 s durante 30 s.
            self._homed_recheck_until = time.monotonic() + 30.0
            self._homed_next_at = 0.0
        elif kind == "calibrate":
            self._set_intent("leveling" if payload.get("leveling") else "input_shaper")
        elif kind == "ms-load":
            self._set_intent("load", int(payload["slot"]) + 1)
        elif kind == "ms-unload":
            self._set_intent("unload", int(payload["slot"]) + 1)
        elif kind in ("move", "extrude"):
            self._homed_next_at = 0.0
        message = str(result.get("message") or _ACK_TEXT.get(kind, "Orden enviada."))
        self._post({"type": "command-ack", "command": kind,
                    "ok": bool(result.get("ok", True)), "message": message})
        return True

    def _run_list_files(self, client: FlashForgeClient, payload: dict[str, Any]) -> None:
        try:
            result = client.list_files(with_thumbs=bool(payload.get("with_thumbs", True)))
        except FlashForgeError as exc:
            self._post({"type": "files", "ok": False, "files": [], "message": str(exc)})
            return
        except Exception as exc:
            _post_unexpected(self, "list_files", exc)
            return

        files = result.get("files") or []
        # Respaldo documentado: GET /getThum solo sirve para el trabajo actual.
        for entry in files:
            if entry.get("thumb") or entry.get("name") != self._last_job_name:
                continue
            try:
                entry["thumb"] = client.get_current_thumb()
            except FlashForgeError as exc:
                _log(f"getThum sin miniatura: {exc}")
            break

        self._post({
            "type": "files",
            "ok": True,
            "files": files,
            "current_job": self._last_job_name if self._last_job_active else "",
            "mock": client.mock,
            "ts": time.time(),
        })

    def _run_upload(self, client: FlashForgeClient, payload: dict[str, Any]) -> None:
        path = str(payload.get("path") or "")
        name = os.path.basename(path)
        self._last_upload_sent = 0
        self._post({"type": "upload-progress", "file": name, "sent": 0, "total": 0,
                    "message": "Subiendo…"})

        def progress(sent: int, total: int) -> None:
            if sent - self._last_upload_sent < 4 * 1024 * 1024 and sent != total:
                return
            self._last_upload_sent = sent
            self._post({"type": "upload-progress", "file": name,
                        "sent": sent, "total": total, "message": "Subiendo…"})

        try:
            result = client.upload_file(
                path,
                print_now=bool(payload.get("print_now")),
                leveling=bool(payload.get("leveling", True)),
                progress=progress,
            )
        except FlashForgeError as exc:
            self._post({"type": "upload", "ok": False, "file": name, "message": str(exc)})
            return
        except Exception as exc:
            _post_unexpected(self, "upload_file", exc)
            return

        self._post({
            "type": "upload",
            "ok": True,
            "file": result.get("file") or name,
            "size": result.get("size"),
            "print_now": bool(result.get("print_now")),
            "message": "Archivo subido" + (" e impresión iniciada." if result.get("print_now") else "."),
        })

    def _post_error(self, exc: FlashForgeError, client: FlashForgeClient) -> None:
        payload: dict[str, Any] = {
            "type": "error",
            "message": str(exc),
            "kind": exc.kind,
            "mock": client.mock,
            "ts": time.time(),
        }
        if exc.code is not None:
            payload["code"] = exc.code
        if exc.raw_message:
            payload["raw_message"] = exc.raw_message
        if self._consecutive_failures and exc.kind in ("network", "timeout", "tcp"):
            payload["failures"] = self._consecutive_failures
            payload["retry_in"] = int(round(self._poll_delay()))
        # Los fallos que se arreglan en el formulario abren el panel de config.
        if exc.kind in ("no-host", "auth-serial", "auth-code", "parameters"):
            payload["needs_setup"] = True
        self._post(payload)

    # -- envío a la página --------------------------------------------------

    def _post(self, data: dict[str, Any]) -> None:
        """Envía JSON a la página. Nunca lanza (se descarta si no existe)."""
        try:
            self.post_message(data)
        except Exception as exc:
            _log(f"post_message() falló: {exc!r}")


# ---------------------------------------------------------------------------
# Helpers de configuración (módulo)
# ---------------------------------------------------------------------------


def _clean_label(value: Any, default: str = "") -> str:
    """Texto de display: sin caracteres de control, espacios colapsados."""
    text = "".join(ch for ch in str(value or "") if ch >= " " and ch != "\x7f")
    text = " ".join(text.split())
    return (text or default)[:48]


def _sanitize_printer(entry: dict[str, Any], index: int) -> dict[str, str]:
    """Normaliza una entrada de impresora del schema v2."""
    default_name = f"Impresora {index + 1}"
    name = _clean_label(entry.get("name"), default_name) or default_name
    return {
        "name": name,
        "ip": normalize_host(entry.get("ip", entry.get("host", "")))[:253],
        "serial": str(entry.get("serial") or "").strip()[:64],
        "check_code": str(entry.get("check_code") or "").strip()[:32],
    }


def _sanitize_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Normaliza el schema v2; nunca confía en lo que llega del HTML."""
    cleaned: dict[str, Any] = {
        "printers": [],
        "active_printer": 0,
        "poll_interval": DEFAULT_INTERVAL,
        "notify_on_finish": True,
        "notify_on_error": True,
        "mock": True,
    }
    if not isinstance(cfg, dict):
        return cleaned

    printers: list[dict[str, str]] = []
    for index, entry in enumerate(cfg.get("printers") or []):
        if not isinstance(entry, dict):
            continue
        printers.append(_sanitize_printer(entry, index))
        if len(printers) >= MAX_PRINTERS:
            break
    cleaned["printers"] = printers

    try:
        active = int(cfg.get("active_printer", 0))
    except (TypeError, ValueError):
        active = 0
    cleaned["active_printer"] = max(0, min(len(printers) - 1, active)) if printers else 0

    interval = cfg.get("poll_interval", cfg.get("interval", DEFAULT_INTERVAL))
    cleaned["poll_interval"] = clamp_interval(interval)
    cleaned["notify_on_finish"] = bool(cfg.get("notify_on_finish", True))
    cleaned["notify_on_error"] = bool(cfg.get("notify_on_error", True))
    cleaned["mock"] = bool(cfg.get("mock", True))
    return cleaned


def _migrate_config(parsed: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Migra el schema v1 (ip/serial/check_code en la raíz) al v2.

    Devuelve ``(config_v2, hubo_migración)``. Nunca pierde credenciales: los
    valores antiguos pasan a ``printers[0]``.
    """
    if not isinstance(parsed, dict):
        return _sanitize_config({}), False
    if isinstance(parsed.get("printers"), list):
        return _sanitize_config(parsed), False

    legacy_keys = ("host", "serial", "check_code", "interval")
    if not any(key in parsed for key in legacy_keys):
        return _sanitize_config(parsed), False

    host = normalize_host(parsed.get("host"))
    serial = str(parsed.get("serial") or "").strip()
    check_code = str(parsed.get("check_code") or "").strip()

    printers: list[dict[str, str]] = []
    if host or serial or check_code:
        printers.append({
            "name": host or "Mi impresora",
            "ip": host,
            "serial": serial,
            "check_code": check_code,
        })

    migrated = {
        "printers": printers,
        "active_printer": 0,
        "poll_interval": parsed.get("interval", DEFAULT_INTERVAL),
        "notify_on_finish": True,
        "notify_on_error": True,
        # v1 traía mock=True por defecto; respétalo, pero sin impresora el
        # dashboard debe seguir arrancando en demo.
        "mock": bool(parsed.get("mock", not printers)),
    }
    return _sanitize_config(migrated), True


def _merge_printer_secrets(
    printers: list[Any], previous: list[Any]
) -> list[dict[str, Any]]:
    """Conserva el check code guardado si el formulario no lo reenvía.

    La página nunca recibe ``check_code`` (solo ``has_check_code``), así que al
    editar nombre/IP/serial hay que recuperar el secreto por (ip, serial), por
    IP o, en último caso, por posición si la IP coincide.
    """
    by_pair: dict[tuple[str, str], str] = {}
    by_ip: dict[str, str] = {}
    for entry in previous or []:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("check_code") or "")
        if not code:
            continue
        ip = normalize_host(entry.get("ip"))
        if ip:
            by_ip[ip] = code
            by_pair[(ip, str(entry.get("serial") or ""))] = code

    merged: list[dict[str, Any]] = []
    for index, entry in enumerate(printers or []):
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        if not str(item.get("check_code") or "").strip():
            ip = normalize_host(item.get("ip"))
            code = by_pair.get((ip, str(item.get("serial") or "")))
            if not code:
                code = by_ip.get(ip)
            if not code and index < len(previous or []):
                old = previous[index]
                if isinstance(old, dict) and normalize_host(old.get("ip")) == ip:
                    code = str(old.get("check_code") or "")
            if code:
                item["check_code"] = code
        merged.append(item)
    return merged


def _effective_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Config efectiva: si ``mock`` y no hay impresoras, usa las de demo."""
    if not isinstance(cfg, dict):
        return _sanitize_config({})
    if cfg.get("mock") and not cfg.get("printers"):
        demo = dict(cfg)
        demo["printers"] = [dict(printer) for printer in MOCK_PRINTERS]
        demo["active_printer"] = 0
        return demo
    return cfg


def _active_printer(cfg: dict[str, Any]) -> dict[str, Any]:
    """Impresora activa del config (dict vacío si no hay ninguna)."""
    printers = cfg.get("printers") or []
    try:
        index = int(cfg.get("active_printer") or 0)
    except (TypeError, ValueError):
        index = 0
    if 0 <= index < len(printers):
        return printers[index]
    return printers[0] if printers else {}


def _fallback_data_dirs() -> list[pathlib.Path]:
    """Rutas habituales de data_dir() cuando el host no expone un accessor."""
    home = pathlib.Path.home()
    candidates: list[pathlib.Path] = []
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        candidates.append(pathlib.Path(xdg) / "OrcaSlicer")
    candidates.append(home / ".config" / "OrcaSlicer")
    candidates.append(home / ".var" / "app" / "com.orcaslicer.OrcaSlicer"
                      / "config" / "OrcaSlicer")
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(pathlib.Path(appdata) / "OrcaSlicer")
    candidates.append(home / "Library" / "Application Support" / "OrcaSlicer")
    return candidates


def _version_key(value: Any) -> tuple[int, ...]:
    parts = [int(part) for part in re.findall(r"\d+", str(value or ""))[:4]]
    return tuple(parts) if parts else (0,)


def _load_config_json(raw: str) -> dict[str, Any]:
    """Convierte el JSON del host en un dict v2 saneado (migrando si hace falta)."""
    try:
        parsed = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    migrated, _changed = _migrate_config(parsed)
    return migrated


def _public_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Config apta para la página: nunca expone los check codes."""
    effective = _effective_config(cfg)
    demo = bool(cfg.get("mock")) and not cfg.get("printers")
    printers = [
        {
            "name": entry.get("name", ""),
            "ip": entry.get("ip", ""),
            "serial": entry.get("serial", ""),
            "has_check_code": bool(str(entry.get("check_code") or "").strip()),
            "demo": demo,
        }
        for entry in effective.get("printers") or []
    ]
    interval = effective.get("poll_interval", DEFAULT_INTERVAL)
    return {
        "version": CONFIG_VERSION,
        "printers": printers,
        "active_printer": int(effective.get("active_printer") or 0),
        "poll_interval": interval,
        "interval": interval,  # alias: compatibilidad con el dashboard v0.2
        "notify_on_finish": bool(effective.get("notify_on_finish")),
        "notify_on_error": bool(effective.get("notify_on_error")),
        "mock": bool(effective.get("mock")),
        "demo_printers": demo,
    }


def _post_unexpected(page: "_FlashForgePageLogic", where: str, exc: Exception) -> None:
    """Reporta un fallo inesperado sin matar el hilo ni el handler de UI."""
    _log(f"{where}() lanzó una excepción inesperada: {exc!r}")
    page._post({
        "type": "error",
        "kind": "internal",
        "message": f"Error interno en {where}: {exc}",
    })


# ---------------------------------------------------------------------------
# Registro en OrcaSlicer
# ---------------------------------------------------------------------------

if orca is not None:  # pragma: no cover - solo dentro de OrcaSlicer
    class FlashForgePage(_FlashForgePageLogic, orca.pages.PagesPluginCapabilityBase):  # type: ignore[misc]
        """Pestaña "FlashForge" con el dashboard de la impresora."""

    @orca.plugin
    class FlashForgeMonitorPlugin(orca.base):  # type: ignore[misc]
        """Clase paquete del plugin. Instanciable sin argumentos."""

        def get_name(self) -> str:
            return "FlashForge Monitor"

        def register_capabilities(self) -> None:
            # Una sola capability: la pestaña con el dashboard.
            # Los permisos de red NO se declaran aquí a propósito: la IP es
            # configurable y el audit hook de Orca preguntará en el primer uso.
            orca.register_capability(FlashForgePage)
else:  # pragma: no cover - importado fuera de OrcaSlicer
    # Sin host no hay clases de capability; FlashForgeClient sigue disponible
    # para test_mock.py.
    FlashForgePage = None  # type: ignore[assignment]
    FlashForgeMonitorPlugin = None  # type: ignore[assignment]
