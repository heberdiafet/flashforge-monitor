# Referencia de la API local — FlashForge Adventurer 5M / 5M Pro / AD5X

Documento técnico para implementar un cliente en Python (sin dependencias) del
**HTTP REST API local** de la impresora **FlashForge AD5X** con firmware stock y
**LAN-only mode** activado.

> **Alcance verificado**: este documento se basa en (a) la especificación OpenAPI
> volcada de firmware real en `Parallel-7/flashforge-api-docs`, (b) el cliente
> TypeScript de referencia `@ghosttypes/ff-api` v2.1.0 (el que usa FlashForgeUI-
> Electron), y (c) dos implementaciones independientes (extensión Chrome
> `ad5m-panel` y la librería Python `aioflashforge`). Además, el **2026-11-20** se
> validaron contra la AD5X real (firmware `v3.1.8`) el enrutado HTTP, el envelope
> de error, las reglas de autenticación y todo el protocolo TCP 8899 (§2.2). Lo
> que sigue sin observarse en hardware está marcado **⚠️ NO VERIFICADO**, y lo
> que no aparece en ninguna fuente está marcado como no documentado (§12).

---

## Índice

1. [Aviso importante: 8898 ≠ 8899](#1-aviso-importante-8898--8899)
2. [Base URL, puertos y transporte](#2-base-url-puertos-y-transporte)
3. [Autenticación exacta (check code)](#3-autenticación-exacta-check-code)
4. [Envelope de respuesta y códigos de error](#4-envelope-de-respuesta-y-códigos-de-error)
5. [Endpoints, uno por uno](#5-endpoints-uno-por-uno)
   - [/checkCode](#51-post-checkcode) · [/detail](#52-post-detail) · [/product](#53-post-product)
   - [/control](#54-post-control) · [/gcodeList](#55-post-gcodelist) · [/gcodeThumb](#56-post-gcodethumb)
   - [/getThum](#57-get-getthum) · [/printGcode](#58-post-printgcode) · [/uploadGcode](#59-post-uploadgcode)
   - [/notifyWanBind](#510-post-notifywanbind)
6. [Comandos de `/control`](#6-comandos-de-control)
7. [Estación de materiales IFS (CRÍTICO)](#7-estación-de-materiales-ifs-crítico)
8. [Cámara MJPEG (puerto 8080)](#8-cámara-mjpeg-puerto-8080)
9. [Descubrimiento en red local (UDP)](#9-descubrimiento-en-red-local-udp)
10. [Protocolo TCP 8899 (complemento)](#10-protocolo-tcp-8899-complemento)
11. [Notas de compatibilidad y limitaciones](#11-notas-de-compatibilidad-y-limitaciones)
12. [Lo que NO está documentado](#12-lo-que-no-está-documentado)
13. [Mini cliente Python (urllib)](#13-mini-cliente-python-urllib)
14. [Checklist de verificación en hardware](#14-checklist-de-verificación-en-hardware)
15. [Fuentes](#15-fuentes)

---

## 1. Aviso importante: 8898 ≠ 8899

`PLAN.md` de este proyecto dice *"cliente de la API local HTTP … (puerto 8899)"*.
**Eso es incorrecto.**

| Puerto | Protocolo | Qué habla | ¿Necesita check code? |
|--------|-----------|-----------|------------------------|
| **8898** | HTTP/1.1 + JSON | La API REST de control y estado | **Sí** (serial + check code) |
| 8899 | TCP crudo, texto | G-code/M-code (`~M601 S1`, `~M105`, `~M27`…) | **No** |
| 8080 | HTTP MJPEG | Stream de cámara | **No** |

Fuentes: `F1`/`F2` (los paths del spec viven sobre 8898), `F9` (`FiveMClient`:
`this.PORT = options?.httpPort ?? 8898`), `F11` (`aioflashforge`: `port: int = 8898`),
`F10` (`const API_PORT = 8898`), `F3` (tabla *Overview*: `Port | 8898`), `F12`
(tabla de puertos: 8899 = TCP crudo).

Un cliente Python correcto usa:

```
http://<IP>:8898/<endpoint>
```

---

## 2. Base URL, puertos y transporte

| Propiedad | Valor | Fuente |
|-----------|-------|--------|
| Base URL | `http://<IP>:8898` | `F3`, `F9`, `F10`, `F11` |
| Métodos | `POST` en todo, **excepto** `GET /getThum` | `F1`, `F2`, `F7` |
| `Content-Type` petición | `application/json` (salvo `/uploadGcode`, que es `multipart/form-data`) | `F1`, `F3` |
| `Content-Type` respuesta | `application/json` (salvo `/getThum` → `image/bmp`) | `F1` |
| Codificación | UTF-8, JSON plano (sin gzip requerido) | `F1` |
| HTTP status | **Siempre 200**, incluso en error. Hay que mirar `code` del cuerpo | `F3`, `F4` |
| Timeout recomendado | 5 s por petición (ff-api usa 5000 ms) | `F9` |
| CORS | **No hay CORS**: `OPTIONS /detail` → `404` sin cabeceras `Access-Control-*`, y un `POST` con `Origin` no devuelve `Access-Control-Allow-Origin` (medido en hardware, §2.2). `ad5m-panel` funciona porque una página de extensión con `host_permissions` no está sujeta a CORS | `F10` + medición |
| `Content-Type` de error | El firmware escribe **`appliation/json`** (typo, falta la `c`) en las respuestas de error. No valides el Content-Type de forma estricta | medición §2.2 |
| Comandos concurrentes | El firmware atiende de una en una; ff-api serializa todo en una cola FIFO (`runCommandExclusive`) | `F9` |

No hay HTTPS, ni tokens, ni cookies, ni sesión. La autenticación es un **secreto
compartido estático** enviado en cada petición.

### 2.1 Evidencia de campo en la unidad de este proyecto

Sondeo directo a la AD5X local (`docs/local-findings.md`, 2026-11-20):

| Puerto | Estado | Interpretación |
|--------|--------|----------------|
| **8898** | **ABIERTO, habla HTTP** | Es la API de este documento. Rutas desconocidas → `404`; las reales son las de §5 |
| 8899 | ABIERTO, TCP | Acepta conexión; un `GET` HTTP plano se corta → es el protocolo de texto `~M…` (§10), **no** HTTP |
| 8080 | CERRADO | El stream MJPEG no responde hasta **activar la cámara** en la pantalla de la impresora (§8) |
| 80, 3000 | cerrados | — |

Esto confirma empíricamente el punto de §1: **el cliente va a 8898**.

### 2.2 Verificación en hardware (2026-11-20)

Todo lo siguiente está **medido** contra la AD5X local (firmware `v3.1.8`,
serial `SNAD5X000001`, IP `192.168.1.50`), no solo documentado.

**a) Las rutas existen y hablan el envelope documentado.** Con un check code
inválido (`88888888`, el que tenía OrcaSlicer):

```console
$ curl -s -X POST http://192.168.1.50:8898/product \
       -H 'Content-Type: application/json' \
       -d '{"serialNumber":"SNAD5X000001","checkCode":"88888888"}'
{"code":1,"message":"Access code is different"}      # HTTP 200
```

Idéntico resultado en `/detail`, `/gcodeList`, `/product`, `/checkCode` → **las
cuatro rutas y el envelope `{code,message}` están confirmados**, no son 404.

> **Hallazgo operativo**: `88888888` **NO es** el check code de esta impresora.
> El valor que OrcaSlicer tiene como *access code* no sirve para la API LAN. El
> plugin **debe pedir al usuario el "Printer ID" de 8 caracteres** que muestra la
> pantalla (LAN-only mode), no reutilizar la config de Orca.

**b) Semántica de errores, exacta:**

| Petición | Respuesta observada |
|----------|---------------------|
| `/detail` con `checkCode` incorrecto | `{"code":1,"message":"Access code is different"}` |
| `/detail` con `serialNumber` inventado + `checkCode` malo | `{"code":1,"message":"SN is different"}` → **comprueba el serial primero** |
| `/detail` sin `checkCode` | `{"code":-1,"message":"Parameters is error"}` |
| `/checkCode` sin `serialNumber` | `{"code":-1,"message":"Parameters is different"}` → **mensaje distinto, tal como documenta `F4`** |
| `GET /` | `404`, `Content-Length: 0` |
| `OPTIONS /detail` | `404`, sin cabeceras CORS |

**c) `/getThum` sin credenciales:**

```console
$ curl -s -D- http://192.168.1.50:8898/getThum
HTTP/1.1 200 OK
Connection: close
Content-Length: 40
Content-Type: appliation/json

{"code":-1,"message":"thumbnail error"}
```

→ La ruta GET existe, **no pide credenciales** y devuelve un error JSON propio
(`thumbnail error`) cuando no hay trabajo con thumbnail. Mensaje **no documentado**
en las fuentes.

**d) TCP 8899 responde sin auth y sin `M601`:**

```
$ printf '~M115\r\n' | nc 192.168.1.50 8899
CMD M115 Received.
Machine Type: Flashforge AD5X
Machine Name: AD5X
Firmware: v3.1.8
SN: SNAD5X000001
X: 220 Y: 220 Z: 220
Tool Count: 1
Mac Address: 24:A1:0D:B2:F7:21
ok
```

**e) Cámara**: `http://192.168.1.50:8080/?action=stream` → `HTTP 000`
(conexión rechazada). Coincide con `local-findings.md`: hay que **activar la
cámara en la pantalla** de la impresora; el módulo puede no estar instalado.

---

## 3. Autenticación exacta (check code)

### 3.1 Credenciales

| Campo | Descripción | Dónde se obtiene |
|-------|-------------|------------------|
| `serialNumber` | Nº de serie de la impresora (13 caracteres alfanuméricos mayúsculas/dígitos según `F11`) | Pantalla: *Settings → Device Info / About* |
| `checkCode` | Código estático de **8 caracteres** en `[a-f0-9]` (minúsculas) | Pantalla de modo de red, junto al interruptor **LAN-only mode**. Flash Studio / Orca-Flashforge lo llaman **"Printer ID"** |

`F4`: *"the printer makes the check code itself. The code has 8 characters. It can
contain the digits 0–9 and the letters a–f, for example `3f9a0c1e`. If the user
makes a new code on the printer, the old code stops working."*

Regex real de una implementación Python independiente (`F11`, `regex.py`):

```python
CHECK_CODE_REGEX = r"^([a-f]|[0-9]){8}$"
SERIAL_REGEX     = r"^([A-Z]|[0-9]){13}$"
```

> **Formatos reales observados** (`docs/local-findings.md`): serial
> `SNAD5X000001` (13 caracteres, **no** tiene por qué empezar por `SNADVA5X`) y
> access code `88888888` (8 dígitos, sin letras). Los ejemplos de este documento
> usan valores ficticios tipo `SNADVA5X00XXXX` / `3f9a0c1e`: **no asumas el
> prefijo ni que el código lleve letras**. Valida con `^[a-f0-9]{8}$` y
> `^[A-Z0-9]{13}$`, pero **no rechaces** un serial de otra longitud sin probar
> primero: manda lo que el usuario copie de la pantalla.

### 3.2 Método A — JSON en el cuerpo (todos los endpoints menos `/uploadGcode`)

```http
POST /detail HTTP/1.1
Host: 192.168.1.50:8898
Content-Type: application/json

{"serialNumber":"SNADVA5X00XXXX","checkCode":"3f9a0c1e"}
```

El cliente TypeScript construye exactamente ese objeto en cada llamada (`F9`,
`Info.getDetailResponse`, `Control.sendControlCommand`):

```js
const payload = { serialNumber: this.client.serialNumber,
                  checkCode:    this.client.checkCode };
```

No hay hash, ni nonce, ni firma: el check code viaja en claro. Es un modelo de
seguridad "LAN de confianza".

### 3.3 Método B — cabeceras (solo `/uploadGcode`)

```http
POST /uploadGcode HTTP/1.1
Host: 192.168.1.50:8898
serialNumber: SNADVA5X00XXXX
checkCode: 3f9a0c1e
fileSize: 1234567
printNow: true
levelingBeforePrint: true
Content-Type: multipart/form-data; boundary=----X
```

Cabeceras exactas que envía `ff-api` (`F9`, `JobControl.uploadFileAD5X`):

```js
{ serialNumber, checkCode, fileSize: String(size),
  printNow: 'true'|'false',
  levelingBeforePrint: 'true'|'false',
  flowCalibration: 'true'|'false',
  firstLayerInspection: 'true'|'false',
  timeLapseVideo: 'true'|'false',
  useMatlStation: 'true',
  gcodeToolCnt: String(materialMappings.length),
  materialMappings: <base64 de JSON>,
  Expect: '100-continue' }
```

> Los booleanos son las palabras `true`/`false` en minúsculas, **sin comillas**
> (`F1`, `F3`). `F10` usa `'1'`/`'0'` y al parecer el firmware también lo acepta,
> pero la forma documentada y usada por la librería de referencia es
> `true`/`false`. Usa `true`/`false`.
>
> **⚠️ Discrepancia menor**: ¿`materialMappings: []` como literal o base64 de
> `[]` (`"W10="`)? `F10` manda `[]` crudo y funciona; `F9` manda base64 siempre.
> Si `useMatlStation` es `"false"`, el firmware ignora el campo.

### 3.4 Endpoint de validación: `/checkCode`

Sirve para comprobar credenciales sin pedir estado. Reglas especiales (`F4`):

* `{code: 0, message: "Success"}` si el **check code** coincide, **aunque el
  serial sea incorrecto**.
* `{code: 1, message: "SN is different"}` / `"Access code is different"` si no.
* `{code: -1, message: "Parameters is different"}` si falta un campo.
* Cuerpo no-JSON → respuesta HTTP 200 **vacía** (sin cuerpo).

Por eso `F9` valida credenciales con `/product` en `initControl()`, no con
`/checkCode`: `/product` sí exige ambos campos correctos. **Recomendación**:
usa `/product` (o `/detail`) como sonda de emparejamiento; usa `/checkCode` solo
si quieres replicar el comportamiento de Flash Studio.

### 3.5 Errores de autenticación

| `code` | `message` | Significado |
|--------|-----------|-------------|
| `0` | `Success` | OK |
| `1` | `SN is different` | Serial incorrecto |
| `1` | `Access code is different` | Check code incorrecto |
| `-1` | `Parameters is error` | Falta campo / tipos mal / JSON inválido |
| `-2` | `Lan mode error` | LAN-only mode desactivado (según firmware/versión) |

`F4` es explícita: *"The printer checks the serial number first, then the check
code. A field that is not a string counts as an empty string."* → envía siempre
strings, nunca números, para `serialNumber`/`checkCode`.

---

## 4. Envelope de respuesta y códigos de error

Todas las respuestas (salvo `/getThum` y el binario del thumbnail) usan:

```json
{ "code": 0, "message": "Success", "...": "campos específicos del endpoint" }
```

Códigos (`F1` → `CodeMessageResponse`, `F3`):

| `code` | `message` típico | Cuándo |
|--------|------------------|--------|
| `0` | `Success` | Operación aceptada |
| `-1` | `Parameters is error` | Campo requerido ausente / tipo erróneo / body no-JSON |
| `-2` | `Lan mode error` | LAN-only mode apagado (solo algunas versiones de firmware) |
| `1` | `SN is different` | Serial no coincide |
| `1` | `Access code is different` | Check code no coincide |
| `2` | `Printer is busy` / `Printer is Busy.` | `/printGcode` con trabajo activo |
| `3` | `File does not exist.` | `/printGcode` con `fileName` inexistente |
| `-1` | `thumbnail error` | `/getThum` sin trabajo/thumbnail (medido en hardware, §2.2c; no está en las fuentes) |

Notas de `F3` que importan al cliente:

* `/control` responde **siempre** `{code:0,message:"Success"}` una vez pasada la
  autenticación: **confirma el despacho, no el efecto**. Un `cmd` desconocido se
  ignora y responde `Success`. → **Verifica siempre el efecto releyendo `/detail`.**
* `/uploadGcode` comprueba "busy" (`code: 2`) **antes** que las credenciales y
  nunca devuelve `Parameters is error`.

`ff-api` considera éxito solo `code === 0 && message === "Success"` (`F9`,
`NetworkUtils.isOk`).

---

## 5. Endpoints, uno por uno

Rutas confirmadas en el spec del AD5X (`F1`) y del 5M (`F2`). El conjunto es
idéntico en ambos salvo campos extra del AD5X:

```
/checkCode  /detail  /product  /control  /gcodeList  /gcodeThumb
/printGcode  /getThum  /notifyWanBind  /uploadGcode
```

---

### 5.1 `POST /checkCode`

Valida `serialNumber` + `checkCode`.

**Petición**

```json
{ "serialNumber": "SNADVA5X00XXXX", "checkCode": "3f9a0c1e" }
```

**Respuesta**

```json
{ "code": 0, "message": "Success" }
```

Fuente: `F1` → `paths./checkCode`; reglas especiales en `F4`.

---

### 5.2 `POST /detail`

**El endpoint principal de estado.** Devuelve estado, temperaturas, progreso y
(AD5X) la estación de materiales.

**Petición**

```json
{ "serialNumber": "SNADVA5X00XXXX", "checkCode": "3f9a0c1e" }
```

**Respuesta** (ejemplo compuesto; estructura y campos de `F1`→`Details`, valores
de ejemplo de `F3`)

```json
{
  "code": 0,
  "message": "Success",
  "detail": {
    "status": "printing",
    "printProgress": 0.45,
    "printLayer": 50,
    "targetPrintLayer": 100,
    "printDuration": 1800,
    "estimatedTime": 3600,
    "printFileName": "Multicolor.3mf",
    "printFileThumbUrl": "http://192.168.1.50:8898/getThum",
    "currentPrintSpeed": 100,
    "printSpeedAdjust": 100,
    "rightTemp": 209,
    "rightTargetTemp": 210,
    "leftTemp": 0,
    "leftTargetTemp": 0,
    "platTemp": 58,
    "platTargetTemp": 60,
    "chamberTemp": 0,
    "chamberTargetTemp": 0,
    "coolingFanSpeed": 100,
    "coolingFanLeftSpeed": 0,
    "chamberFanSpeed": 0,
    "lightStatus": "open",
    "doorStatus": "close",
    "errorCode": "",
    "firmwareVersion": "1.2.1",
    "name": "AD5X",
    "pid": 38,
    "ipAddr": "192.168.1.50",
    "macAddr": "00:11:22:33:44:55",
    "nozzleCnt": 1,
    "nozzleModel": "0.4mm",
    "nozzleStyle": 0,
    "measure": "220X220X220",
    "zAxisCompensation": 0,
    "cumulativeFilament": 120.5,
    "cumulativePrintTime": 1234,
    "remainingDiskSpace": 1024,
    "autoShutdown": "close",
    "autoShutdownTime": 30,
    "cameraStreamUrl": "",
    "camera": 0,
    "hasMatlStation": true,
    "matlStationInfo": {
      "slotCnt": 4,
      "currentSlot": 1,
      "currentLoadSlot": 0,
      "stateAction": 0,
      "stateStep": 0,
      "slotInfos": [
        { "slotId": 1, "hasFilament": true,  "materialName": "PLA", "materialColor": "#FFFFFF" },
        { "slotId": 2, "hasFilament": true,  "materialName": "PLA", "materialColor": "#FF0000" },
        { "slotId": 3, "hasFilament": false, "materialName": "",    "materialColor": "" },
        { "slotId": 4, "hasFilament": false, "materialName": "",    "materialColor": "" }
      ]
    },
    "indepMatlInfo": { "stateAction": 0, "stateStep": 0, "materialName": "", "materialColor": "" }
  }
}
```

> ⚠️ **Campos omitidos a propósito**: `F1` **no** lista `serialNumber` dentro de
> `Details` (el serial se obtiene del emparejamiento o del paquete UDP de
> descubrimiento, §9); si tu firmware lo devuelve, ignóralo. Tampoco incluimos
> `model` (nombre de fábrica): `F9` lo documenta como campo de la serie Creator 5
> y puede faltar en modelos anteriores. El resto de campos del ejemplo está en
> `F1`/`F3`.

**Campos clave**

| Campo | Tipo | Notas |
|-------|------|-------|
| `status` | string | Ver tabla de estados en §11.2 |
| `printProgress` | float | **Ratio 0.0–1.0**, no porcentaje → `×100` (`F3`) |
| `printLayer` / `targetPrintLayer` | int | Capa actual / total |
| `estimatedTime` | float (s) | Tiempo **restante** (cuenta atrás). `0.0` en idle. **No** restarle `printDuration` |
| `printDuration` | int (s) | Tiempo **transcurrido** del trabajo. `0` en idle |
| `rightTemp` / `rightTargetTemp` | float (°C) | Extrusor principal (único en 5M/AD5X) |
| `leftTemp` / `leftTargetTemp` | float (°C) | Extrusor izquierdo (solo bi-extrusor) |
| `platTemp` / `platTargetTemp` | float (°C) | Cama |
| `chamberTemp` / `chamberTargetTemp` | float | `0` en 5M/AD5X (sin cámara calefactada) |
| `coolingFanSpeed` | int (%) | Ventilador de pieza; `0` sin impresión |
| `coolingFanLeftSpeed` | int | Solo AD5X/5M Pro (segundo ventilador) |
| `chamberFanSpeed` | int (%) | Ventilador de cámara |
| `lightStatus` | `"open"`/`"close"` | LED |
| `doorStatus` | `"open"`/`"close"` | En 5M/AD5X sin sensor: valor cosmético (`F9`) |
| `errorCode` | string | `""` si no hay error; p.ej. `E0100` (IFS), `E0048` |
| `pid` | int | **38 = AD5X**, 35 = 5M, 36 = 5M Pro (`F8`). El firmware lo manda como *string hex* (`"0026"`) en algunas versiones → parsea base 16 (`F1`) |
| `cameraStreamUrl` | string | Vacío si no hay cámara; si hay: `http://<ip>:8080/?action=stream` |
| `camera` | int | 1 si hay cámara (ff-api: `detail.camera === 1 \|\| cameraStreamUrl !== ''`) |
| `hasMatlStation` | bool/int | Puede venir `true` o `1`. Comprueba también `matlStationInfo.slotCnt`/`slotInfos` (`F9`) |
| `printFileThumbUrl` | string | `http://<ip>:8898/getThum`; vacío si no hay thumb |

**Reglas de tiempo** (`F3`, sección *Print Time Fields*):

* `estimatedTime` = restante. **No** hacer `estimatedTime - printDuration`.
* `completion = now() + estimatedTime` **solo** si `status == "printing"`; en
  pausa/error/calentando el firmware **congela** `estimatedTime` y la ETA se
  desplazaría en cada sondeo.

**Polling**: 1–2 s es razonable. `ff-api` usa 5 s de timeout por petición y una
cola FIFO; el `FlashForgeUI-Electron` sondea `/detail` en bucle.

---

### 5.3 `POST /product`

Flags de capacidad/control. Compáralo con cuidado: **no es fiable** (`F3`).

**Petición**

```json
{ "serialNumber": "SNADVA5X00XXXX", "checkCode": "3f9a0c1e" }
```

**Respuesta**

```json
{
  "code": 0,
  "message": "Success",
  "product": {
    "nozzleTempCtrlState": 1,
    "chamberTempCtrlState": 0,
    "platformTempCtrlState": 1,
    "lightCtrlState": 0,
    "internalFanCtrlState": 0,
    "externalFanCtrlState": 0
  }
}
```

`1` = disponible, `0` = no disponible (`F3`). Advertencia textual de `F3`:
*"The `/product` `*CtrlState` flags are **NOT a reliable** indicator of hardware
capabilities … Do not use these flags as the source of truth. Instead, identify
the model via `/detail` `pid`."*

Caso concreto: en el AD5X `lightCtrlState` puede ser `0` y aun así
`lightControl_cmd` funciona con LED de postventa (`F3`, `F8` Capability-Matrix).
Y `ff-api` deriva `hasLed` de este flag → si lo copias tal cual, **no podrás
encender el LED** en un AD5X sin LED de fábrica. Recomendación: intenta el
comando y comprueba el efecto en `/detail.lightStatus`.

---

### 5.4 `POST /control`

Un único endpoint para **todos** los comandos. Estructura:

```json
{
  "serialNumber": "SNADVA5X00XXXX",
  "checkCode": "3f9a0c1e",
  "payload": { "cmd": "<nombre_cmd>", "args": { } }
}
```

**Respuesta**

```json
{ "code": 0, "message": "Success" }
```

`payload.args` puede omitirse en `homingCtrl_cmd` (`F1`). Catálogo completo de
`cmd` en §6.

---

### 5.5 `POST /gcodeList`

Lista de archivos imprimibles en `/data` (extensiones `.g`, `.gx`, `.gcode`,
`.3mf`).

**Petición**

```json
{ "serialNumber": "SNADVA5X00XXXX", "checkCode": "3f9a0c1e" }
```

**Respuesta en AD5X** (`F3`, `F1`→`GcodeListResponse`)

```json
{
  "code": 0,
  "message": "Success",
  "gcodeList": ["Multicolor.3mf", "Benchy.gcode"],
  "gcodeListDetail": [
    {
      "gcodeFileName": "Multicolor.3mf",
      "printingTime": 3600,
      "totalFilamentWeight": 150.5,
      "useMatlStation": true,
      "gcodeToolCnt": 4,
      "gcodeToolDatas": [
        { "toolId": 0, "materialName": "PLA", "materialColor": "#FFFFFF",
          "filamentWeight": 50.2, "slotId": 0 },
        { "toolId": 1, "materialName": "PLA", "materialColor": "#FF0000",
          "filamentWeight": 50.2, "slotId": 0 }
      ]
    }
  ]
}
```

* `gcodeList` = nombres (siempre presente).
* `gcodeListDetail` = **solo AD5X** (y builds third-party del Creator 5). Aporta
  requisitos del archivo: `printingTime` (s), `totalFilamentWeight` (g),
  `useMatlStation`, `gcodeToolCnt`, `gcodeToolDatas[]`.
* En `gcodeToolDatas`, `slotId` es **0** cuando no aplica o alimentación directa
  (no confundir con el 1–4 de la estación).
* **No hay thumbnails aquí**: usa `/gcodeThumb` o `/getThum`.
* `printingTime` es la estimación estática del slicer, **no** el tiempo restante
  en vivo.

---

### 5.6 `POST /gcodeThumb`

Thumbnail de un archivo concreto, en base64.

**Petición**

```json
{ "serialNumber": "SNADVA5X00XXXX", "checkCode": "3f9a0c1e", "fileName": "Benchy.gcode" }
```

**Respuesta**

```json
{ "code": 0, "message": "Success", "imageData": "iVBORw0KGgoAAA..." }
```

`imageData` es base64 (PNG normalmente; `F9` hace `Buffer.from(imageData,'base64')`).
En Python: `base64.b64decode(imageData)` → bytes de imagen.

---

### 5.7 `GET /getThum`

Miniatura **del trabajo actual** como imagen BMP. Es la **única ruta GET**.

```http
GET /getThum HTTP/1.1
Host: 192.168.1.50:8898
```

* `200` → `Content-Type: image/bmp`, cuerpo binario.
* Error → JSON `{code, message}` (`F1`).
* No lleva parámetros ni credenciales (`F7`: *"`/getThum` takes no request
  parameters. It works only in LAN mode, and it needs an active print job with a
  thumbnail file"*). **Verificado**: sin credenciales responde
  `{"code":-1,"message":"thumbnail error"}` (§2.2c).
* Si hay LAN-only off, se aplica igualmente la comprobación de modo
  (`F4`: *"`/getThum` also does this check, although it needs no credentials"*).
* Equivale a `detail.printFileThumbUrl`.

---

### 5.8 `POST /printGcode`

Arranca la impresión de un archivo que **ya está** en `/data`.

**Petición mínima (5M / 5M Pro)**

```json
{
  "serialNumber": "SNADVA5X00XXXX",
  "checkCode": "3f9a0c1e",
  "fileName": "Benchy.gcode",
  "levelingBeforePrint": true
}
```

**Petición multi-material (AD5X)** — `F3`, `F5`

```json
{
  "serialNumber": "SNADVA5X00XXXX",
  "checkCode": "3f9a0c1e",
  "fileName": "Multicolor.3mf",
  "levelingBeforePrint": true,
  "firstLayerInspection": false,
  "flowCalibration": false,
  "timeLapseVideo": false,
  "useMatlStation": true,
  "gcodeToolCnt": 2,
  "materialMappings": [
    { "toolId": 0, "slotId": 1, "materialName": "PLA",
      "toolMaterialColor": "#FFFFFF", "slotMaterialColor": "#FFFFFF" },
    { "toolId": 1, "slotId": 2, "materialName": "PLA",
      "toolMaterialColor": "#FF0000", "slotMaterialColor": "#FF0000" }
  ]
}
```

| Campo | Tipo | Obligatorio | Notas |
|-------|------|-------------|-------|
| `serialNumber`, `checkCode` | string | sí | auth |
| `fileName` | string | sí | debe existir en `/data` |
| `levelingBeforePrint` | bool | sí | nivelado previo |
| `firstLayerInspection` | bool | no (AD5X) | inspección de primera capa |
| `flowCalibration` | bool | no (AD5X) | calibración de flujo |
| `timeLapseVideo` | bool | no (AD5X) | timelapse |
| `useMatlStation` | bool | no (AD5X) | `true` para multi-material |
| `gcodeToolCnt` | int | no (AD5X) | nº de tools (1–4) = longitud de `materialMappings` |
| `materialMappings` | array | no (AD5X) | ver §7.5 |

**Respuesta**

```json
{ "code": 0, "message": "Success" }
```

Errores: `-1` si falta `fileName`/`levelingBeforePrint`; `3` si el archivo no
existe; `2` si la impresora no está idle (`F3`).

`ff-api` distingue payload "nuevo" (≥ 3.1.3, con `flowCalibration`,
`useMatlStation`, `gcodeToolCnt`, `materialMappings`) y "viejo" (solo `fileName`
+ `levelingBeforePrint`), y para AD5X **siempre** usa el nuevo (`F9`,
`JobControl.printLocalFile` / `startAD5XMultiColorJob`).

---

### 5.9 `POST /uploadGcode`

Sube un `.gcode` / `.3mf` y opcionalmente imprime.

* `Content-Type: multipart/form-data`
* Campo del archivo: **`gcodeFile`**
* Credenciales y opciones: **en cabeceras** (§3.3)

**Petición**

```http
POST /uploadGcode HTTP/1.1
Host: 192.168.1.50:8898
serialNumber: SNADVA5X00XXXX
checkCode: 3f9a0c1e
fileSize: 1234567
printNow: true
levelingBeforePrint: true
flowCalibration: false
firstLayerInspection: false
timeLapseVideo: false
useMatlStation: true
gcodeToolCnt: 2
materialMappings: W3sidG9vbElkIjowLCJzbG90SWQiOjEsIm1hdGVyaWFsTmFtZSI6IlBMQSIsInRvb2xNYXRlcmlhbENvbG9yIjoiI0ZGRkZGRiIsInNsb3RNYXRlcmlhbENvbG9yIjoiI0ZGRkZGRiJ9XQ==
Expect: 100-continue
Content-Type: multipart/form-data; boundary=----X

------X
Content-Disposition: form-data; name="gcodeFile"; filename="Multicolor.3mf"
Content-Type: application/octet-stream

<bytes>
------X--
```

**Respuesta**

```json
{ "code": 0, "message": "Success" }
```

Notas:
* `materialMappings` aquí va **base64 de un JSON array** (con `#` en los colores);
  en `/printGcode` va como **array JSON normal**.
* `fileSize` en bytes; necesario para el progreso y para el firmware.
* `Expect: 100-continue` lo usa `F9`; con `urllib` no hace falta (y puede
  complicar el handler), pero si lo omites el firmware acepta igualmente.
* Tras subir con `printNow: false`, lanza con `/printGcode` para poder pasar
  `materialMappings` explícitos.
* Para Python: `urllib` no construye multipart solo. Ver §13.3 (helper manual con
  `uuid` como boundary).

---

### 5.10 `POST /notifyWanBind`

Notificación de binding a la nube. **No se usa en LAN-only.**

```json
{ "serialNumber": "SNADVA5X00XXXX" }
```

Único endpoint que **no** aplica la comprobación de "LAN mode" → en modo nube
responde normal (`F1`, `F7`). Para el plugin: ignorar.

---

## 6. Comandos de `/control`

Todos con la misma envoltura de §5.4. Enum del spec `F1` → `payload.oneOf` y
constantes de `F9` → `Commands.js`.

### 6.1 Resumen

| `cmd` | Args | Disponible en | Efecto |
|-------|------|---------------|--------|
| `lightControl_cmd` | `status` | 5M, 5M Pro, AD5X* | LED on/off |
| `jobCtl_cmd` | `jobID`, `action` | todos | pausa/reanuda/cancela |
| `printerCtl_cmd` | `speed`, `zAxisCompensation`, `chamberFan`, `coolingFan`, `coolingLeftFan` | todos | ajustes en impresión |
| `temperatureCtl_cmd` | `rightNozzle`, `leftNozzle`, `platform`, `chamber` | 5M/5M Pro/AD5X (ver nota) | setpoints |
| `calibration_cmd` | `levelingDetection`, `vibrationCompensation` | todos | calibración pre-print |
| `stateCtrl_cmd` | `action: "setClearPlatform"` | todos | cerrar diálogo de fin de print |
| `delayClose_cmd` | `automaticShutdown`, `shutdownAfterTime` | todos | apagado automático |
| `reName_cmd` | `name` | todos | renombrar |
| `streamCtrl_cmd` | `action` | 5M Pro (y 5M con cámara) | abrir/cerrar stream |
| `circulateCtl_cmd` | `internal`, `external` | 5M Pro | filtración |
| `userProfile_cmd` | `name`, `avatar` | todos | perfil local |
| `msConfig_cmd` | `slot`, `mt`, `rgb` | **AD5X** | metadatos de slot IFS |
| `ms_cmd` | `action`, `slot` | **AD5X** | load/unload/cancel IFS |
| `ipdMsConfig_cmd` | `mt`, `rgb` | **AD5X** | metadatos alimentación directa |
| `ipdMs_cmd` | `action` | **AD5X** | load/unload/cancel directo |
| `moveCtrl_cmd` | `axis`, `delta` | **AD5X** | jog X/Y/Z |
| `extrudeCtrl_cmd` | `axis`, `delta` | **AD5X** | extruir/retraer |
| `homingCtrl_cmd` | — | **AD5X** | home de ejes |
| `errorCodeCtrl_cmd` | `action`, `errorCode` | **AD5X** | limpiar error |

\* `lightControl_cmd` en AD5X: aceptado y funcional con LED de postventa, aunque
`/product.lightCtrlState` diga `0` (`F3`, `F8`).

### 6.2 `lightControl_cmd` — LED

```json
{ "cmd": "lightControl_cmd", "args": { "status": "open" } }
```

`status`: `"open"` | `"close"` (`F1`, `F3`, `F9` `setLedOn/setLedOff`).

### 6.3 `jobCtl_cmd` — pausa / reanudar / cancelar

```json
{ "cmd": "jobCtl_cmd", "args": { "jobID": "", "action": "pause" } }
```

`action`: `"pause"` | `"continue"` | `"cancel"`. `jobID` va vacío (`""`); el
firmware no lo valida para estas acciones (`F9`, `sendJobControlCmd`).

Semántica de estados resultante (`F3`): `printing → pausing → pause`,
`→ canceling → cancel`, `→ completed`.

### 6.4 `printerCtl_cmd` — velocidad, Z-offset, ventiladores

```json
{ "cmd": "printerCtl_cmd", "args": { "speed": 120 } }
```

| Arg | Rango | Sentinel "no cambiar" | Mapeo TCP |
|-----|-------|----------------------|-----------|
| `speed` | 50–150 (5M/Pro) / 50–500 (AD5X) | `-200` | `M220 S<val>` |
| `zAxisCompensation` | −5.0 … +5.0 (mm) | `-200.0` | `SET_GCODE_OFFSET Z=<val> MOVE=1` |
| `chamberFan` | 0–100 (%) | `-200` | ventilador de cámara |
| `coolingFan` | 0–100 (%) | `-200` | ventilador de pieza |
| `coolingLeftFan` | 0–100 (%) | `-200` | segundo ventilador (Pro/AD5X) |

**Actualización parcial**: omite los campos que no quieras tocar (o manda
`-200`). **No mandes `0` por defecto**: apagarías el ventilador o borrarías el
Z-offset (`F1`, `F3`, `F7`).

`ff-api` además fuerza `chamberFan=0`/`coolingFan=0` en las dos primeras capas y
lanza error si no hay trabajo activo (`F9`, `sendPrinterControlCmd`).

### 6.5 `temperatureCtl_cmd` — temperaturas

```json
{ "cmd": "temperatureCtl_cmd", "args": { "rightNozzle": 210, "platform": 60 } }
```

| Arg | Rango | Off | No cambiar |
|-----|-------|-----|-----------|
| `rightNozzle` | 0–265 | `-100` | `-200` |
| `leftNozzle` | 0–265 (bi-extrusor) | `-100` | `-200` |
| `platform` | 0–100 | `-100` | `-200` |
| `chamber` | 0–60 (Creator 5: hasta 80) | `-100` | `-200` |

Fuente: `F3`, `F2`, `F1`. `ff-api` define las constantes `TEMP_NO_CHANGE = -200`
y `TEMP_OFF = -100` (`F9`, `TempControl.js`).

> **⚠️ Discrepancia importante para el plugin**: `F3` documenta
> `temperatureCtl_cmd` por HTTP para 5M/AD5X, pero `ff-api` v2.1.0 **usa TCP**
> (`M104`/`M140`) en los modelos con canal TCP, y reserva
> `temperatureCtl_cmd` para los HTTP-only (Creator 5). Es decir: el comando HTTP
> existe y está en el spec, pero la librería de referencia, probada en hardware,
> prefiere TCP. **Plan**: implementa `temperatureCtl_cmd` y, si no ves efecto en
> `/detail` en ~2 s, cae a TCP `~M104 S<temp>` / `~M140 S<temp>`.

### 6.6 `calibration_cmd` — nivelado / input shaper

```json
{ "cmd": "calibration_cmd",
  "args": { "levelingDetection": "open", "vibrationCompensation": "open" } }
```

Ambos campos **obligatorios**; solo el string exacto `"open"` activa. Se ignora
en silencio durante impresión (`F3`).

### 6.7 `stateCtrl_cmd` — limpiar diálogo de plataforma

```json
{ "cmd": "stateCtrl_cmd", "args": { "action": "setClearPlatform" } }
```

Útil tras `completed` o `cancel` para volver a `ready` (`F3`, `F9`
`clearPlatform()`). **No** es "clear nozzle".

### 6.8 `delayClose_cmd` — apagado automático

```json
{ "cmd": "delayClose_cmd",
  "args": { "automaticShutdown": "open", "shutdownAfterTime": 30 } }
```

`automaticShutdown`: `"open"`/`"close"`; `shutdownAfterTime` en minutos
(la unidad es la que muestra la pantalla — `F3`).

### 6.9 `reName_cmd` / `userProfile_cmd`

```json
{ "cmd": "reName_cmd", "args": { "name": "Taller" } }
```

```json
{ "cmd": "userProfile_cmd", "args": { "name": "Taller", "avatar": "avatar_03" } }
```

`avatar`: string libre; catálogo de IDs válidos **⚠️ NO VERIFICADO**.

### 6.10 `streamCtrl_cmd` — cámara

```json
{ "cmd": "streamCtrl_cmd", "args": { "action": "open" } }
```

`"open"`/`"close"`. `F3`: aceptado en toda la serie 5M; solo actúa si hay cámara.
`F10` lo llama **antes** de cargar el `<img>` del stream (patrón recomendado).

> En AD5X, `F8` (Capability-Matrix) marca `streamCtrl_cmd` como **No**. Como el
> AD5X no tiene cámara de fábrica, tiene sentido: si tu unidad sirve MJPEG en
> 8080, el stream ya está activo y el comando es innecesario (o inofensivo).
> **No bloquees la cámara** si `streamCtrl_cmd` devuelve `Success` pero no cambia
> nada.

### 6.11 `circulateCtl_cmd` — filtración (5M Pro)

```json
{ "cmd": "circulateCtl_cmd", "args": { "internal": "open", "external": "close" } }
```

Solo un modo a la vez: manda `"close"` al que no quieras. Ambos `"open"` ≡ modo
externo (`F8`, 5M-Pro-Features). En AD5X **no** aplica.

### 6.12 Comandos AD5X (movimiento, homing, errores)

```json
{ "cmd": "homingCtrl_cmd" }
```

```json
{ "cmd": "moveCtrl_cmd", "args": { "axis": "x", "delta": 10.0 } }
```

```json
{ "cmd": "extrudeCtrl_cmd", "args": { "axis": "e", "delta": 10.0 } }
```

```json
{ "cmd": "errorCodeCtrl_cmd", "args": { "action": "clearErrorCode", "errorCode": "E0048" } }
```

* `homingCtrl_cmd`: `args` opcional e ignorado; hace home de **todos** los ejes.
* `moveCtrl_cmd`: `axis` ∈ `"x"|"y"|"z"` (minúsculas), `delta` float en mm.
* `extrudeCtrl_cmd`: `axis` se **descarta**; solo importa `delta` (mm; positivo
  extruye, negativo retrae).
* `errorCodeCtrl_cmd`: limpia **solo** si `action == "clearErrorCode"` **y**
  `errorCode` coincide exactamente con `detail.errorCode` del momento.
  Si no coincide, no hace nada y responde `Success`.

Fuentes: `F1` (schemas `MoveCtrlCmd`/`ExtrudeCtrlCmd`/`HomingCtrlCmd`/
`ErrorCodeCtrlCmd`), `F5`, `F7`. `homingCtrl_cmd` y compañía existen desde
firmware 1.1.6; en 1.0.2 no están (`F7`).

> **Para el 5M/5M Pro no hay homing por HTTP**: `ff-api` lo hace por TCP
> (`G28`) porque `Control.homeAxes()` exige canal TCP (`F9`). En AD5X usa
> `homingCtrl_cmd`.

---

## 7. Estación de materiales IFS (CRÍTICO)

Modelo: **AD5X únicamente**. 4 slots. Estado en `/detail`, control en `/control`.

### 7.1 Estado: `detail.matlStationInfo`

```jsonc
// fragmento del objeto "detail" de la respuesta /detail
"hasMatlStation": true,
"matlStationInfo": {
  "slotCnt": 4,
  "currentSlot": 1,
  "currentLoadSlot": 0,
  "stateAction": 0,
  "stateStep": 0,
  "slotInfos": [
    { "slotId": 1, "hasFilament": true,  "materialName": "PLA", "materialColor": "#FFFFFF" },
    { "slotId": 2, "hasFilament": true,  "materialName": "PETG","materialColor": "#FF0000" },
    { "slotId": 3, "hasFilament": false, "materialName": "",    "materialColor": "" },
    { "slotId": 4, "hasFilament": false, "materialName": "",    "materialColor": "" }
  ]
}
```

| Campo | Tipo | Significado |
|-------|------|-------------|
| `slotCnt` | int | **Siempre 4** (hardcodeado en firmware) |
| `currentSlot` | int | Slot activo/imprimiendo (1–4; 0 = ninguno) |
| `currentLoadSlot` | int | Slot de la operación de carga/descarga en curso (0 = ninguna) |
| `stateAction` | int | Modo de operación: 0 idle, 1 load, 2 unload, 3 cancel |
| `stateStep` | int | Paso: 0 idle/cancel, 2–3 load, 4–5 unload, 6 completo |
| `slotInfos[]` | array | Un objeto por slot |

`slotInfos[]`:

| Campo | Tipo | Notas |
|-------|------|-------|
| `slotId` | int | **1-based (1–4)**; coincide con `slot` de `msConfig_cmd`/`ms_cmd` |
| `hasFilament` | bool | Detección física de filamento |
| `materialName` | string | Texto configurado (`"PLA"`, `"PETG"`…) |
| `materialColor` | string | `#RRGGBB`; el firmware guarda "color antes del primer `;`" |

Además, `indepMatlInfo` describe la alimentación **directa** (sin estación):
`stateAction`, `stateStep`, `materialName`, `materialColor` (`F1`).

**`stateAction` = `ms_cmd.action` + 1.** Es la forma recomendada de confirmar que
la operación arrancó: mandas `action: 0` (load) → debes ver `stateAction: 1`
(`F5`).

### 7.2 Configurar un slot: `msConfig_cmd`

```json
{
  "serialNumber": "SNADVA5X00XXXX",
  "checkCode": "3f9a0c1e",
  "payload": {
    "cmd": "msConfig_cmd",
    "args": { "slot": 1, "mt": "PLA", "rgb": "#FF0000" }
  }
}
```

| Arg | Tipo | Notas |
|-----|------|-------|
| `slot` | int | 1–4 |
| `mt` | string | Tipo de material; el firmware **no valida** |
| `rgb` | string | Color `#RRGGBB` **en mayúsculas**; ver discrepancia abajo |

Respuesta: `{ "code": 0, "message": "Success" }`. Persiste tras reinicios (`F5`).

> **⚠️ Discrepancia en el `#`**: `F5` (wiki) muestra `"rgb": "FF0000"` **sin** `#`.
> `F9` (`ff-api` 2.1.0, `ad5xPalette.js`) documenta explícitamente lo contrario:
> *"The `rgb` field is sent as uppercase `#RRGGBB`, exactly as the printer's own UI
> stores it — the `#` is part of the value and must not be stripped."*
> **Usa `#RRGGBB` en mayúsculas** (coincide con la paleta que el firmware
> compara byte a byte). Si el icono sale blanco, prueba sin `#`.
> El nombre de material y el color se aceptan como strings arbitrarios, pero la
> **UI solo dibuja el icono de color** si el valor coincide exactamente con una de
> las 24 entradas de la paleta.

### 7.3 Cargar / descargar / cancelar: `ms_cmd`

```json
{ "cmd": "ms_cmd", "args": { "slot": 1, "action": 0 } }
```

| Arg | Valores | Significado |
|-----|---------|-------------|
| `slot` | 1–4 | Slot objetivo (ignorado en `cancel`) |
| `action` | 0 = **load**, 1 = **unload**, 2 = **cancel** | |

* Operación **asíncrona**: responde `Success` al despachar. Monitorea
  `matlStationInfo.stateAction`/`stateStep` en `/detail` hasta `0`.
* `load` requiere `hasFilament: true` en el slot.
* `unload` requiere extrusor caliente para retraer limpio.
* `ipdMs_cmd` hace lo mismo para la alimentación directa (sin slot).

### 7.4 Paleta y materiales que la UI reconoce

Paleta de 24 colores del AD5X (`F9`→`ad5xPalette.js`, idéntica a `F5`):

| # | Color | Hex | # | Color | Hex |
|---|-------|-----|---|-------|-----|
| 0 | White | `#FFFFFF` | 12 | Magenta | `#F330F9` |
| 1 | Yellow | `#FEF043` | 13 | Pink | `#D4B0DC` |
| 2 | Light Green | `#DCF478` | 14 | Coral | `#F95D73` |
| 3 | Green | `#0ACC38` | 15 | Red | `#F72224` |
| 4 | Dark Green | `#067749` | 16 | Brown | `#7C4B00` |
| 5 | Teal | `#0C6283` | 17 | Orange | `#F98D33` |
| 6 | Cyan | `#0DE2A0` | 18 | Cream | `#FDEBD5` |
| 7 | Light Blue | `#75D9F3` | 19 | Tan | `#D3C4A3` |
| 8 | Blue | `#45A8F9` | 20 | Dark Brown | `#AF7836` |
| 9 | Dark Blue | `#2750E0` | 21 | Gray | `#898989` |
| 10 | Purple | `#46328E` | 22 | Light Gray | `#BCBCBC` |
| 11 | Violet | `#A03CF7` | 23 | Black | `#161616` |

Cualquier otro valor → el firmware lo guarda pero la UI cae a **White**.
(Si implementas selector de color en el plugin, haz *snap* a esta lista.)

Materiales que la UI renderiza (`F5`): `PLA`, `PLA-CF`, `ABS`, `PETG`, `PETG-CF`,
`TPU`, `SILK`. Aceptados sin icono: `PA-CF`, `PC`, `PC-ABS`, `PET-CF`, `PPS-CF`,
`PA`, `PAHT-CF`.

### 7.5 Mapeo tool → slot (para imprimir)

```json
[
  { "toolId": 0, "slotId": 1, "materialName": "PLA",
    "toolMaterialColor": "#FFFFFF", "slotMaterialColor": "#FFFFFF" },
  { "toolId": 1, "slotId": 2, "materialName": "PLA",
    "toolMaterialColor": "#FF0000", "slotMaterialColor": "#FF0000" }
]
```

| Campo | Tipo | Rango | Notas |
|-------|------|-------|-------|
| `toolId` | int | **0-based** (0–3) | índice de tool/color en el gcode |
| `slotId` | int | **1-based** (1–4) | slot físico del IFS |
| `materialName` | string | | tipo declarado |
| `toolMaterialColor` | string | `#RRGGBB` | color que pide el gcode |
| `slotMaterialColor` | string | `#RRGGBB` | color real del slot |

`gcodeToolCnt` debe ser **igual** a `materialMappings.length`.

**⚠️ El error clásico**: `toolId` empieza en 0 y `slotId` en 1. No los iguales.

### 7.6 Flujo completo multi-material

1. `POST /detail` → comprobar `hasMatlStation`, `slotInfos[].hasFilament`,
   `materialName`/`materialColor`.
2. `POST /gcodeList` → `gcodeListDetail[].gcodeToolDatas[]` para saber qué pide el
   archivo (solo AD5X).
3. `POST /control` `msConfig_cmd` por cada slot que quieras reetiquetar.
4. (Opcional) `ms_cmd` load/unload; esperar `stateAction → 0`.
5. Construir `materialMappings` (toolId 0-based, slotId 1-based).
6. `POST /printGcode` con `useMatlStation: true`, `gcodeToolCnt`,
   `materialMappings` — o `POST /uploadGcode` con los mismos datos en cabeceras
   (`materialMappings` en base64).
7. Poll `/detail`: `status`, `printProgress`, `matlStationInfo.currentSlot`.

Fuente: `F5` §*Multi-Material Printing Workflow*.

### 7.7 Errores IFS (`detail.errorCode`)

| Código | Causa |
|--------|-------|
| `E0100`–`E0103` | Timeout de alimentación, canal 1–4 |
| `E0104`–`E0107` | Timeout de retracción, canal 1–4 |
| `E0108` | Fallo de alimentación al extrusor (sensor) |
| `E0109` | Atasco: odómetro estancado (> 200) |
| `E0114` | Error de homing del selector IFS |

Para limpiar: `errorCodeCtrl_cmd` con el `errorCode` exacto (§6.12).

---

## 8. Cámara MJPEG (puerto 8080)

```
http://<IP>:8080/?action=stream
```

| Propiedad | Valor | Fuente |
|-----------|-------|--------|
| Puerto | 8080 | `F8`, `F9` (`Endpoints.CAMERA_STREAM_PORT = 8080`) |
| Ruta | `/?action=stream` | `F9`, `F10`, `F8` |
| Formato | MJPEG (`multipart/x-mixed-replace; boundary=...`) | `F8` |
| Auth | **Ninguna** (sin check code, sin cabeceras) | `F9` (probe HEAD/GET sin auth), `F10` (`<img src>` directo) |
| Resolución (5M Pro) | 1280×720 @ 30 fps, MJPEG | `F8` |
| Detección | `detail.cameraStreamUrl` no vacío, o `detail.camera == 1` | `F9` |
| Fallback de URL | `http://<IP>:8080/?action=stream` si `cameraStreamUrl` viene vacío | `F10` |

**En el AD5X**: no hay cámara de fábrica (`F7`, `F8`), pero el firmware incluye el
servidor de stream y las notas de PID dicen *"camera/LED optional"* (`F8`
Printer-PIDs). Si hay módulo de cámara, el stream **no** requiere autenticación;
`cameraStreamUrl` debería venir relleno y, si no, usa la URL fallback.

> **⚠️ Estado en la unidad de este proyecto** (`docs/local-findings.md`): el
> puerto **8080 estaba cerrado** en el sondeo inicial. Antes de dar la cámara por
> rota, el usuario debe **activarla en la pantalla de la impresora**
> (Ajustes → cámara ON). El dashboard debe:
> 1. leer `detail.cameraStreamUrl` y `detail.camera`;
> 2. si no hay URL o el `<img>` da error, mostrar "cámara no disponible" y un
>    aviso de cómo activarla — **nunca** romper la pestaña;
> 3. reintentar al reconectar, no en bucle agresivo.

**Cómo consumirlo en el plugin**:

* En el dashboard HTML: `<img src="http://IP:8080/?action=stream">` (lo que hace
  `F10`). Es la vía más simple y no consume CPU de Python.
* En Python puro: `urllib.request.urlopen(url)` y leer el cuerpo en streaming
  parseando los boundaries `--<boundary>` + `Content-Length` para extraer JPEGs.
  ⚠️ **NO VERIFICADO** que este firmware incluya `Content-Length` por frame; si no
  lo incluye, hay que buscar los marcadores `\xff\xd8` … `\xff\xd9`.
* Snapshot suelto (`/?action=snapshot`): **⚠️ NO VERIFICADO**. Solo está
  documentado `?action=stream`. No lo asumas.

**Consejo de implementación**: `ff-api` prueba primero `HEAD` y luego un `GET`
corto con `responseType: 'stream'` y aborta; algunos servidores MJPEG rechazan
`HEAD` (`F9`, `detectCameraStream`). Replica esa estrategia si haces detección.

---

## 9. Descubrimiento en red local (UDP)

Fuente principal: `F6` y `F9` (`PrinterDiscovery.js`). Coinciden.

### 9.1 Sondas

| Tipo | Dirección | Puerto |
|------|-----------|--------|
| Multicast | `225.0.0.9` | `19000` |
| Broadcast | `255.255.255.255` (y broadcast de cada interfaz) | `48899` |
| Multicast legacy | `225.0.0.9` | `8899` |

* **Payload de la sonda: cualquiera; el firmware lo ignora.** `F9` envía un
  datagrama **vacío** (`Buffer.alloc(0)`). `F6`: *"Send any UDP packet (payload
  ignored)"*.
* Socket UDP con `SO_BROADCAST` y `SO_REUSEADDR`, bind a puerto efímero (`0`).
* `F9` además sondea `127.0.0.1` (simuladores locales) — inofensivo.

### 9.2 Respuesta (protocolo moderno, 276 bytes / 280 en firmware 5.x)

Big-endian. Acepta **≥ 276** (nunca compares por igualdad exacta).

| Offset | Tamaño | Tipo | Campo |
|--------|--------|------|-------|
| `0x00` | 128 | char[] | Nombre de máquina (UTF-8, NUL-terminado) |
| `0x80` | 4 | bytes | Padding |
| `0x84` | 2 | uint16 BE | **Puerto de comandos TCP** (8899) |
| `0x86` | 2 | uint16 BE | Vendor ID (`0x2B71`) |
| `0x88` | 2 | uint16 BE | **PID** (modelo): `0x0026`=AD5X, `0x0023`=5M, `0x0024`=5M Pro |
| `0x8A` | 2 | uint16 BE | Status code (estado de sesión TCP, **no** estado de impresora) |
| `0x8C` | 2 | uint16 BE | Product type (`0x5A02` = familia 5M) |
| `0x8E` | 2 | uint16 BE | **Puerto HTTP/Event = 8898** |
| `0x90` | 1 | uint8 | Connect mode: `0` = LAN-only, `1` = nube |
| `0x91` | 1 | uint8 | Bind status: `1` = conectado a la nube |
| `0x92` | 128 | char[] | **Número de serie** (UTF-8, NUL-terminado) |

Detección de protocolo (`F9`): `len >= 276` → moderno; `len >= 140` → legacy
(140 bytes exactos para A3/A4 Pro).

### 9.3 Ejemplo Python mínimo

```python
import socket, struct

def discover(timeout=2.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.settimeout(timeout)
    s.bind(("", 0))
    s.sendto(b"", ("225.0.0.9", 19000))
    s.sendto(b"", ("255.255.255.255", 48899))
    found = []
    try:
        while True:
            data, (ip, _port) = s.recvfrom(2048)
            if len(data) >= 276:
                name = data[0x00:0x84].split(b"\0")[0].decode("utf-8", "replace")
                pid  = struct.unpack(">H", data[0x88:0x8A])[0]
                http = struct.unpack(">H", data[0x8E:0x90])[0]
                sn   = data[0x92:0x92+128].split(b"\0")[0].decode("utf-8", "replace")
                lan  = data[0x90]
                found.append(dict(ip=ip, name=name, pid=pid, http_port=http,
                                  serial=sn, lan_only=(lan == 0)))
    except socket.timeout:
        pass
    finally:
        s.close()
    return found
```

Notas:
* El **serial** del paquete de descubrimiento es la forma limpia de rellenar el
  campo `serialNumber` del emparejamiento sin pedírselo al usuario.
* `0x90` **no** debe usarse para decidir si la API está disponible: hay firmware
  que la mantiene abierta en modo nube (`F6`).
* Un mismo equipo puede responder a varias sondas → deduplica por IP o serial.

---

## 10. Protocolo TCP 8899 (complemento)

No es la API principal, pero **el plugin lo necesitará** para lo que el HTTP no
cubre en 5M/AD5X (homing en 5M, temperaturas si el HTTP falla, G-code crudo).

| Propiedad | Valor |
|-----------|-------|
| Puerto | 8899 |
| Formato | Texto, comandos con prefijo `~`, terminador `\r\n` |
| Auth | **Ninguna** (solo sesión de control) |
| Timeout de inactividad | ~30 s (5M) → manda algo cada ≤ 20 s |

Handshake:

```
→ ~M601 S1
← CMD M601 Received.
  Control Success V2.1.
  ok
```

Al cerrar: `~M602`. Solo **una** sesión de control a la vez; si otro cliente la
tiene, `M601` responde `Control Failed`.

> **Verificado en hardware (v3.1.8)**: las consultas de solo lectura (`M115`,
> `M105`, `M27`, `M119`) responden **sin** `M601` previo. Aun así, `M601 S1`
> antes y `M602` al final es la práctica correcta si vas a mandar movimiento o
> G-code. Transcripción real:

```
→ ~M601 S1
← CMD M601 Received. / Control Success V2.1. / ok
→ ~M105
← CMD M105 Received.
  T0:36.9/0.0 T1:0.0/0.0 B:37.3/0.0
  ok
→ ~M27
← CMD M27 Received.
  SD printing byte 0/100
  Layer: 0/0
  ok
→ ~M119
← CMD M119 Received.
  Endstop: X-max: 110 Y-max: 110 Z-min: 0
  MachineStatus: READY
  MoveMode: READY
  Status: S:0 L:0 J:0 F:0
  LED: 1
  CurrentFile:
  ok
→ ~M602
← CMD M602 Received. / Control Release. / ok
```

`~M661` devuelve, **después** del `ok`, un blob binario con cabecera propia
(observado: bytes `D\xNN\xNN...D`); hay que parsearlo aparte, no como texto.

Comandos útiles:

| Comando | Efecto |
|---------|--------|
| `~M115` | Info: modelo, firmware, SN, volumen |
| `~M27` | Progreso: `SD printing byte x/y`, `Layer: a/b` |
| `~M105` | Temperaturas: `T0:239.9/240.0 T1:0.0/0.0 B:69.9/70.0` |
| `~M114` | Posición XYZ |
| `~M119` | Endstops / estado |
| `~G28` | Home |
| `~M104 S<temp>` | Temp. extrusor |
| `~M140 S<temp>` | Temp. cama |
| `~M106` / `~M107` | Ventilador on/off (escala 0–255) |
| `~M25` / `~M24` / `~M26` | Pausa / reanudar / parar |
| `~M146 r<R> g<G> b<B> F0` | LED RGB (preferido sobre M144/M145, que escriben EEPROM) |
| `~M661` | Lista de archivos (datos **después** de `ok`) |
| `~M662 <ruta>` | Thumbnail PNG con cabecera custom `0xa2a22a2a` |

Fuente: `F12` (TCP-Protocol, M-Code-Reference). Implementación de referencia
mínima en Python: `IgorZyktin/FlashForgeAdventurer5MAPI` (`transport.py` conecta a
8899, `DEFAULT_PORT = 8899`, envía `~M27\r\n`, etc.).

---

## 11. Notas de compatibilidad y limitaciones

### 11.1 Firmware mínimo por endpoint

| Endpoint | 5M / 5M Pro | AD5X |
|----------|-------------|------|
| `/checkCode`, `/detail`, `/product`, `/control`, `/uploadGcode`, `/getThum` | desde 2.6.x | desde 1.0.2 |
| `/gcodeList`, `/gcodeThumb`, `/printGcode`, `/notifyWanBind` | **desde 3.1.3** (en 2.6.x → HTTP 404) | desde 1.0.2 |
| `stateCtrl_cmd` | desde 3.1.3 | sí |
| `moveCtrl_cmd`, `extrudeCtrl_cmd`, `homingCtrl_cmd`, `errorCodeCtrl_cmd` | — | **desde 1.1.6** (en 1.0.7/1.0.8 hay `firstLayerDetectControl_cmd`/`plateDetectControl_cmd`; en 1.0.2 ninguna) |
| `msConfig_cmd`, `ms_cmd` | — | desde 1.0.2 |

Fuente: `F8` (Adventurer-5M-Series), `F7` (AD5X).

> **Firmware medido en esta unidad**: `v3.1.8` (vía `~M115`). Está por encima de
> 3.1.5, así que aplica la fila "3.1.5+" de la tabla de LAN-only (§11.3): el 8898
> **se cierra** si arranca en modo nube, pero sigue funcionando hasta el siguiente
> reinicio si cambias a nube estando en LAN-only.
> Es decir: el conjunto completo de endpoints de §5 está disponible aquí.

### 11.2 Estados de `detail.status`

| `status` | Significado |
|----------|-------------|
| `ready` | idle, acepta comandos |
| `busy` | operación no-print (homing, etc.) |
| `calibrate_doing` | calibrando |
| `heating` | calentando |
| `printing` | imprimiendo |
| `pausing` → `pause` | pausando / pausada |
| `canceling` → `cancel` | cancelando / cancelada |
| `completed` | terminada |
| `error` | error (ver `errorCode`) |
| `downloading` | recibiendo archivo |
| `cloud_slicing`, `sending`, `unzipping` | solo firmware 5.x del 5M |
| `loading` | carga/descarga de filamento (**⚠️ no verificado en hardware**) |

`F3` es tajante: *"The paused state is spelled `pause`, and the cancelled state
is spelled `cancel`. No model sends `paused`, `cancelled`, `working` or
`building`. Treat any unknown value as busy and keep polling."*

### 11.3 LAN-only mode

* **Es obligatorio.** Sin él: o el puerto 8898 está cerrado, o cada endpoint
  (salvo `/notifyWanBind`) devuelve `{code:-2,"Lan mode error"}`.
* Comportamiento exacto por firmware (`F7`, tabla para AD5X):

| Firmware AD5X | Arranca en modo nube | Tras cambiar LAN-only → nube |
|---------------|----------------------|------------------------------|
| 1.0.x | `code:-2` en todo salvo `/notifyWanBind` | igual |
| 1.1.6 – 3.1.4 | puerto 8898 **cerrado** | `code:-2` |
| 3.1.5+ | puerto **cerrado** | funciona **hasta el siguiente reinicio** |

→ Si no conecta: pedir al usuario LAN-only **y reiniciar** la impresora.

### 11.4 Limitaciones conocidas

* **`/control` responde `Success` aunque el comando no exista o no aplique.**
  Verifica siempre con `/detail`.
* **`/product` miente** sobre capacidades (LED/ventiladores/cámara). Usa `pid` y
  prueba empírica.
* **AD5X sin cámara ni LED de fábrica**: `cameraStreamUrl` puede venir vacío
  aunque el hardware opcional exista. No bloquees la pestaña de cámara por eso.
* **`printProgress` es 0–1**, no 0–100.
* **`estimatedTime` se congela** en pausa/error → no recalcules ETA sin *gate*.
* **Solo un cliente de control TCP** a la vez (puerto 8899). Si Orca/Flash Studio
  está conectado por TCP, tu `M601` puede fallar.
* **Conexiones**: el firmware limita a ~10 conexiones TCP concurrentes, cierra
  inactivas a los ~30 s.
* **`/uploadGcode` y nombres**: sube a `/data`; nombres con espacios/acentos
  aparecen tal cual en `gcodeList`. Escapa correctamente en multipart.
* **`.3mf`**: el firmware lista `.g`, `.gx`, `.gcode`, `.3mf`. **⚠️ NO VERIFICADO**
  qué variantes de `.3mf` acepta (probablemente el `.3mf` con gcode embebido que
  exporta Orca-Flashforge / Flash Studio). Un `.3mf` "solo geometría" de
  OrcaSlicer vanilla podría no imprimirse. Si dudas, sube `.gcode`.
* **CORS/permisos**: en OrcaSlicer el *audit hook* pedirá permiso para
  `http://IP:8898` (y para el `<img>` de `8080` si el WebEngine lo bloquea).
  El puerto correcto a declarar es **8898**.

---

## 12. Lo que NO está documentado

Se listan explícitamente para que **no se inventen**:

| Función | Estado | Lo más cercano |
|---------|--------|----------------|
| **"Clear nozzle" / limpiar boquilla** | **No existe** en la API HTTP. No aparece en `F1`, `F2`, `F3`, `F5`, `F7`, `F8`, `F9`, `F12`. | `extrudeCtrl_cmd` (extruir/retraer manual), `homingCtrl_cmd`, o G-code `~G28`/`~M104` por TCP. La limpieza real es una función de la pantalla táctil (consola de mantenimiento, `F8` AD5X-Maintenance-Console) que no expone endpoint. **⚠️ NO VERIFICADO** que exista un `cmd` oculto. |
| **Homing por HTTP en 5M/5M Pro** | No documentado | `homingCtrl_cmd` es **solo AD5X**. En 5M usa TCP `~G28`. |
| **Set temperatura por HTTP en 5M/AD5X** | Documentado pero con disputa | `temperatureCtl_cmd` está en el spec, pero `ff-api` usa TCP en estos modelos (§6.5). |
| **Snapshot de cámara** (`?action=snapshot`) | **⚠️ NO VERIFICADO** | Solo `?action=stream`. |
| **Auth de la cámara** | Confirmado **sin** auth por implementaciones que funcionan (`F9`, `F10`), pero sin captura de cabeceras publicada | — |
| **Catálogo de `avatar`** de `userProfile_cmd` | **⚠️ NO VERIFICADO** | `F3`: *"the exact catalog of valid IDs is not yet confirmed"*. |
| **`serialNumber` en `/detail`** | No listado en el spec | Obtenerlo del descubrimiento UDP o del usuario. |
| **WebSocket / eventos push** | No existe en esta API | Polling de `/detail`. |
| **Borrar archivos** | No hay endpoint de delete | **⚠️ NO VERIFICADO**; probablemente solo por FTP/SSH (root) o pantalla. |
| **`GET` en rutas que no sean `/getThum`** | No soportado | Usa POST. |
| **Codificación/hash del check code** | No hay: va en claro | — |
| **Respuestas autenticadas de `/detail`, `/gcodeList`, `/control` (incl. IFS)** | **⚠️ NO OBSERVADAS aún**: el check code real de esta impresora no está disponible todavía (`88888888` fue rechazado). La *forma* viene del spec (`F1`) y de la wiki (`F3`, `F5`), pero los valores concretos hay que confirmarlos con el código bueno | §2.2a, §14 |

---

## 13. Mini cliente Python (urllib)

Sin dependencias. Cubre emparejamiento, estado, control, IFS y subida.

### 13.1 Cliente

```python
"""Cliente mínimo de la API local FlashForge (AD5X / 5M). Solo stdlib."""

from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.request
import uuid

DEFAULT_PORT = 8898          # HTTP JSON.  (8899 = TCP G-code, NO es esta API)
DEFAULT_TIMEOUT = 5.0

ERROR_MESSAGES = {
    -1: "Parámetros incorrectos",
    -2: "LAN-only mode desactivado en la impresora",
    1: "Serial o check code incorrectos",
    2: "Impresora ocupada",
    3: "El archivo no existe en la impresora",
}


class PrinterError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class FlashForgeClient:
    def __init__(self, ip: str, serial: str, check_code: str,
                 port: int = DEFAULT_PORT, timeout: float = DEFAULT_TIMEOUT):
        self.ip = ip
        self.serial = serial
        self.check_code = check_code
        self.port = port
        self.timeout = timeout
        self.base = f"http://{ip}:{port}"

    # ---------- transporte ----------

    def _post(self, path: str, extra: dict | None = None) -> dict:
        body = {"serialNumber": self.serial, "checkCode": self.check_code}
        if extra:
            body.update(extra)
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._read_json(req)

    def _read_json(self, req: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:          # el firmware casi nunca usa != 200
            raise PrinterError(-1, f"HTTP {exc.code} en {req.full_url}") from exc
        except urllib.error.URLError as exc:
            raise PrinterError(-1, f"Sin conexión a {self.ip}:{self.port}: {exc.reason}") from exc

        if not raw:                                    # /checkCode puede devolver vacío
            return {"code": -1, "message": "Respuesta vacía"}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PrinterError(-1, f"Respuesta no-JSON: {raw[:120]!r}") from exc

        if data.get("code") != 0:
            code = data.get("code", -1)
            raise PrinterError(code, data.get("message")
                               or ERROR_MESSAGES.get(code, "Error desconocido"))
        return data

    # ---------- info ----------

    def check_code_ok(self) -> bool:
        try:
            self._post("/checkCode")
            return True
        except PrinterError:
            return False

    def product(self) -> dict:
        return self._post("/product")["product"]

    def detail(self) -> dict:
        return self._post("/detail")["detail"]

    def status(self) -> str:
        return self.detail().get("status", "")

    def temperatures(self) -> dict:
        d = self.detail()
        return {
            "nozzle":     d.get("rightTemp", 0.0),
            "nozzle_set": d.get("rightTargetTemp", 0.0),
            "bed":        d.get("platTemp", 0.0),
            "bed_set":    d.get("platTargetTemp", 0.0),
        }

    def progress(self) -> dict:
        d = self.detail()
        return {
            "status": d.get("status"),
            "ratio": d.get("printProgress", 0.0),        # 0.0-1.0
            "percent": round(d.get("printProgress", 0.0) * 100, 1),
            "layer": d.get("printLayer"),
            "layers": d.get("targetPrintLayer"),
            "remaining_s": d.get("estimatedTime"),       # YA es el restante
            "elapsed_s": d.get("printDuration"),
            "file": d.get("printFileName"),
        }

    # ---------- archivos ----------

    def files(self) -> list[dict]:
        data = self._post("/gcodeList")
        detail = data.get("gcodeListDetail")
        if detail:
            return detail
        return [{"gcodeFileName": n, "printingTime": 0} for n in data.get("gcodeList", [])]

    def thumbnail(self, filename: str) -> bytes:
        import base64
        data = self._post("/gcodeThumb", {"fileName": filename})
        return base64.b64decode(data.get("imageData", ""))

    # ---------- control ----------

    def control(self, cmd: str, args: dict | None = None) -> dict:
        payload = {"cmd": cmd}
        if args is not None:
            payload["args"] = args
        return self._post("/control", {"payload": payload})

    def pause(self):    return self.control("jobCtl_cmd", {"jobID": "", "action": "pause"})
    def resume(self):   return self.control("jobCtl_cmd", {"jobID": "", "action": "continue"})
    def cancel(self):   return self.control("jobCtl_cmd", {"jobID": "", "action": "cancel"})

    def set_led(self, on: bool):
        return self.control("lightControl_cmd", {"status": "open" if on else "close"})

    def set_temps(self, nozzle: int | None = None, bed: int | None = None):
        args = {}
        if nozzle is not None:
            args["rightNozzle"] = nozzle
        if bed is not None:
            args["platform"] = bed
        return self.control("temperatureCtl_cmd", args)

    def clear_platform(self):
        return self.control("stateCtrl_cmd", {"action": "setClearPlatform"})

    def home(self):                    # AD5X; en 5M usar TCP ~G28
        return self.control("homingCtrl_cmd")

    # ---------- IFS ----------

    def material_station(self) -> dict | None:
        d = self.detail()
        info = d.get("matlStationInfo") or {}
        if not info.get("slotCnt") and not info.get("slotInfos"):
            return None
        return info

    def set_slot(self, slot: int, material: str, color: str):
        """color en '#RRGGBB' mayúsculas (paleta de 24 colores)."""
        return self.control("msConfig_cmd", {"slot": slot, "mt": material, "rgb": color})

    def slot_action(self, slot: int, action: str):
        """action: 'load' (0) | 'unload' (1) | 'cancel' (2)."""
        codes = {"load": 0, "unload": 1, "cancel": 2}
        return self.control("ms_cmd", {"slot": slot, "action": codes[action]})

    # ---------- impresión ----------

    def print_file(self, filename: str, *, leveling: bool = True,
                   use_station: bool = False,
                   mappings: list[dict] | None = None) -> dict:
        body = {
            "fileName": filename,
            "levelingBeforePrint": leveling,
            "flowCalibration": False,
            "firstLayerInspection": False,
            "timeLapseVideo": False,
            "useMatlStation": bool(use_station),
            "gcodeToolCnt": len(mappings or []) if use_station else 0,
            "materialMappings": mappings or [],
        }
        return self._post("/printGcode", body)

    def upload(self, path: str, *, print_now: bool = False,
               leveling: bool = True, mappings: list[dict] | None = None) -> dict:
        import base64
        size = os.path.getsize(path)
        name = os.path.basename(path)
        boundary = "----ff" + uuid.uuid4().hex
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"

        with open(path, "rb") as fh:
            blob = fh.read()

        pre = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="gcodeFile"; filename="{name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode()
        post = f"\r\n--{boundary}--\r\n".encode()
        body = pre + blob + post

        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "serialNumber": self.serial,
            "checkCode": self.check_code,
            "fileSize": str(size),
            "printNow": "true" if print_now else "false",
            "levelingBeforePrint": "true" if leveling else "false",
            "flowCalibration": "false",
            "firstLayerInspection": "false",
            "timeLapseVideo": "false",
            "useMatlStation": "true" if mappings else "false",
            "gcodeToolCnt": str(len(mappings or [])),
            "materialMappings": base64.b64encode(
                json.dumps(mappings or []).encode()
            ).decode(),
        }
        req = urllib.request.Request(self.base + "/uploadGcode", data=body,
                                     headers=headers, method="POST")
        return self._read_json(req)
```

### 13.2 Uso

```python
cli = FlashForgeClient("192.168.1.50", "SNADVA5X00XXXX", "3f9a0c1e")

# 1) Emparejamiento (mejor con /product: /checkCode aprueba con SN erróneo)
cli.product()

# 2) Estado
print(cli.status())            # 'ready' | 'printing' | 'pause' | ...
print(cli.temperatures())      # {'nozzle': 209, 'nozzle_set': 210, 'bed': 58, ...}
print(cli.progress())          # ratio, percent, capa, restante, transcurrido

# 3) Slots IFS
st = cli.material_station()
for slot in (st or {}).get("slotInfos", []):
    print(slot["slotId"], slot["materialName"], slot["materialColor"],
          "OK" if slot["hasFilament"] else "vacío")

# 4) Editar un slot y cargar filamento
cli.set_slot(1, "PLA", "#FF0000")
cli.slot_action(1, "load")

# 5) Pausar / reanudar / cancelar
cli.pause(); cli.resume(); cli.cancel()

# 6) Imprimir multi-material local
cli.print_file("Multicolor.3mf", use_station=True, mappings=[
    {"toolId": 0, "slotId": 1, "materialName": "PLA",
     "toolMaterialColor": "#FFFFFF", "slotMaterialColor": "#FFFFFF"},
    {"toolId": 1, "slotId": 2, "materialName": "PLA",
     "toolMaterialColor": "#FF0000", "slotMaterialColor": "#FF0000"},
])
```

### 13.3 Detalles que importan al integrar en OrcaSlicer

* **Sondeo en hilo aparte**: `on_message` corre en el hilo de UI. El `urlopen`
  bloquea (hasta 5 s con la impresora caída) → usa un `threading.Thread(daemon=True)`
  y publica con `post_message()`.
* **Audit hook**: declara/permite `http://IP:8898`. La cámara va aparte
  (`http://IP:8080/?action=stream`), normalmente cargada por el WebView.
* **No hagas `fetch()` desde la página del dashboard**: la impresora **no envía
  cabeceras CORS** y responde `404` a `OPTIONS` (§2.2b). Un `fetch` desde el HTML
  embebido fallará por CORS. **Todas las llamadas deben pasar por Python**
  (`urllib`) y llegar a la página vía `post_message()`. El `<img>` del stream sí
  funciona porque las imágenes no están sujetas a CORS.
* **Errores recuperables**: `PrinterError` con `code == -1` o `-2` →
  `PluginResult.RecoverableError` (la impresora está apagada o sin LAN-only).
* **No martillees**: 1–2 s entre sondeos de `/detail`; no sondees `/gcodeList`
  más de cada 5–10 s.
* **Un solo `/control` a la vez**: si haces varias acciones seguidas, serializa
  (el firmware atiende de una en una y `ff-api` usa una cola FIFO).

---

## 14. Checklist de verificación en hardware

Antes de dar por buena la integración, comprueba en tu AD5X:

0. **El check code real**: el valor `88888888` que OrcaSlicer tiene guardado
   **fue rechazado** por esta impresora (`Access code is different`). Pide al
   usuario el "Printer ID" de 8 caracteres de la pantalla (LAN-only mode).
1. `POST /product` con credenciales correctas → `code: 0`. Con check code malo →
   `code: 1, "Access code is different"` (ya reproducido, §2.2).
2. `POST /detail` → `pid` = **38** (o `"0026"`), `hasMatlStation` presente,
   `matlStationInfo.slotCnt` = 4.
3. `/detail` con un rollo cargado → `slotInfos[].hasFilament` y
   `materialName`/`materialColor` coherentes con la pantalla.
4. `msConfig_cmd` con `"#FF0000"` → releer `/detail` y ver `materialColor`
   `"#FF0000"`; comprobar si el icono se pinta rojo (si sale blanco, probar
   `"FF0000"` — ver §7.2).
5. `ms_cmd` load → `stateAction` pasa a 1 y `currentLoadSlot` = slot; al terminar
   vuelve a 0 y `stateAction` 0.
6. `lightControl_cmd` con `lightStatus: "close"` → si `/product.lightCtrlState`
   era 0, confirmar que aun así cambia `detail.lightStatus`.
7. `temperatureCtl_cmd` con `platform: 60` → ver si `platTargetTemp` sube; si no,
   probar TCP `~M140 S60`.
8. `GET /getThum` durante una impresión → `image/bmp` (guardar y abrir).
9. `http://IP:8080/?action=stream` en el navegador → MJPEG sin login. Si no
   responde, comprueba que la cámara está **activada en la pantalla** de la
   impresora (en esta unidad el 8080 estaba cerrado al principio).
10. `POST /uploadGcode` de un `.gcode` pequeño con `printNow: false` → aparece en
    `/gcodeList`; luego `/printGcode`.
11. Con LAN-only apagado → `code: -2` (o puerto cerrado, según firmware).
12. Descubrimiento UDP → aparece tu IP, SN correcto y `http_port` 8898.

Anota en el repo el resultado de cada punto: es la diferencia entre "documentado"
y "verificado".

---

## 15. Fuentes

| ID | Fuente | URL |
|----|--------|-----|
| **F1** | `Parallel-7/flashforge-api-docs` — OpenAPI volcado de **AD5X 1.2.1** (idéntico a 1.1.7 salvo la nota de firmware) | `https://raw.githubusercontent.com/Parallel-7/flashforge-api-docs/main/endpoints/endpoints_ad5x_1.2.1.yaml` |
| **F2** | Ídem — **5M / 5M Pro 3.2.7** | `https://raw.githubusercontent.com/Parallel-7/flashforge-api-docs/main/endpoints/endpoints_5m_3.2.7.yaml` |
| **F3** | Wiki — **HTTP REST API** (endpoints, campos, estados, tiempos) | `https://github.com/Parallel-7/flashforge-api-docs/wiki/HTTP-REST-API` |
| **F4** | Wiki — **Authentication** (check code, errores, LAN mode) | `https://github.com/Parallel-7/flashforge-api-docs/wiki/Authentication` |
| **F5** | Wiki — **AD5X IFS Material Station** (slots, msConfig, ms_cmd, paleta, workflow) | `https://github.com/Parallel-7/flashforge-api-docs/wiki/AD5X-IFS-Material-Station` |
| **F6** | Wiki — **Discovery Protocol** (UDP, offsets) | `https://github.com/Parallel-7/flashforge-api-docs/wiki/Discovery-Protocol` |
| **F7** | Wiki — **AD5X** (modelo, endpoints, comandos, firmwares) | `https://github.com/Parallel-7/flashforge-api-docs/wiki/AD5X` |
| **F8** | Wiki — **Adventurer 5M Series**, **5M Pro Features**, **Capability Matrix**, **Printer PIDs** | `.../wiki/Adventurer-5M-Series`, `.../Adventurer-5M-Pro-Features`, `.../Capability-Matrix`, `.../Printer-PIDs` |
| **F9** | `@ghosttypes/ff-api` **v2.1.0** (npm) — cliente TypeScript de referencia (usado por FlashForgeUI-Electron). Archivos citados: `dist/api/server/Endpoints.js`, `dist/api/server/Commands.js`, `dist/api/controls/{Info,Control,TempControl,JobControl,Files}.js`, `dist/api/controls/ad5xPalette.js`, `dist/api/PrinterDiscovery.js`, `dist/FiveMClient.js`, `dist/models/{MachineInfo,ff-models}.js` | `https://registry.npmjs.org/@ghosttypes/ff-api/-/ff-api-2.1.0.tgz` |
| **F10** | `JECaballeroR/ad5m-panel` — extensión Chrome que habla con la API LAN del 5M Pro (implementación independiente, puerto 8898, upload, cámara) | `https://raw.githubusercontent.com/JECaballeroR/ad5m-panel/master/dashboard.js` |
| **F11** | `Chicchi7393/aioflashforge` — wrapper Python async (puerto 8898, JSON body, regex de serial/check code) | `https://github.com/Chicchi7393/aioflashforge` (`aioflashforge/client.py`, `regex.py`) |
| **F12** | Wiki — **TCP Protocol** + **M-Code Reference** (8899, handshake, M-codes) | `.../wiki/TCP-Protocol`, `.../wiki/M-Code-Reference` |
| **F13** | `IgorZyktin/FlashForgeAdventurer5MAPI` — cliente Python del puerto 8899 (`DEFAULT_PORT = 8899`, `~M27`) | `https://github.com/IgorZyktin/FlashForgeAdventurer5MAPI` |
| **F14** | `docs/local-findings.md` (este repo) — sondeo real de la AD5X: 8898 HTTP abierto, 8899 TCP, 8080 cerrado; serial y access code reales | `flashforge-plugin/docs/local-findings.md` |

### Nota sobre las fuentes que pedía el encargo

* **`GhostTypes/ff-5mp-api-ts`**: ese repositorio **no existe** (404 en GitHub;
  el usuario `GhostTypes` no tiene repos públicos). El código real de GhostTypes
  es el paquete npm **`@ghosttypes/ff-api`** (autor del `package.json` de
  FlashForgeUI-Electron), que es lo que se ha usado aquí como `F9`. Es mejor
  fuente que un README: es el cliente que corre en producción.
* **`Parallel-7/FlashForgeUI-Electron`**: existe y usa `@ghosttypes/ff-api`. Sus
  capas `AD5XBackend.ts`, `MaterialStationBackend.ts` y `ad5x/*` son envoltorios
  de ese paquete, así que se cita el paquete (más directo).
* **`flashforge-python-api`**: no existe un repo con ese nombre exacto; el
  equivalente mantenido es **`aioflashforge`** (`F11`). El artículo de Medium
  "I Gave My AI Agent My 3D Printer" no aporta detalle de protocolo que no esté ya
  en `F1`–`F9`.

---

*Documento generado a partir de especificaciones de firmware y código fuente real.
Todo lo marcado **⚠️ NO VERIFICADO** debe confirmarse contra la impresora antes de
confiar en ello. Última revisión: 2026-11-20.*
