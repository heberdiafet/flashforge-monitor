# FlashForge Monitor — plugin de OrcaSlicer

Plugin Python para **OrcaSlicer 2.5.x** que agrega una pestaña **“FlashForge”** con un
dashboard HTML para monitorear y controlar impresoras **FlashForge AD5X** por red
local (varias impresoras, descubrimiento UDP, notificaciones y gráficas).

- **Multi-impresora**: lista de impresoras guardadas + selector de activa en el
  topbar. Un solo hilo de sondeo apunta a la impresora activa.
- **Estado**: temperaturas, trabajo (nombre, progreso, restante, capas, miniatura),
  cámara MJPEG opcional y 4 slots **IFS** (material + color editables).
- **Control**: setpoints de boquilla/cama, velocidad en vivo, Z-offset,
  ventiladores y LED.
- **Archivos**: listar con miniaturas, imprimir (con confirmación) y subir
  `.gcode`/`.3mf`.
- **Mantenimiento**: homing, mover ejes, extruir/retraer, calibración, limpiar
  errores y auto-apagado.
- **Notificaciones** al terminar un trabajo o al aparecer un error (host +
  banner del dashboard).
- **Gráfica de temperaturas** en vivo (canvas nativo, ~120 muestras).
- **Descubrimiento UDP** de impresoras en la red (rellena IP y serial).

Sin dependencias externas: solo stdlib de Python (`urllib.request`, `http.client`,
`socket`, `json`, `threading`, `queue`, `time`). Toda la UI es HTML/JS plano
servido por el puente de plugins de OrcaSlicer (nada de PyQt/wx/tkinter).

> **Estado: v0.9.0 — fin de impresión y config estable.** API real autenticada contra la AD5X
> (`192.168.1.50`, fw `3.1.8-1.1.1`), multi-impresora, UDP discovery,
> notificaciones, gráficas y dashboard con sensores integrados bajo el hero,
> control más ancho (70/30) con dial ampliado, IFS a ancho completo, homing real
> por ~M119, carga/descarga de filamento, tarjeta de fin con **Confirme** y
> reimpresión, y config que sobrevive a los cambios de wheel.

---

## Estructura

```
flashforge-plugin/
├── PLAN.md
├── docs/
│   ├── api-reference.md                      # API local verificada
│   ├── local-findings.md                     # sondeo de puertos de la unidad real
│   └── round2-design.md                      # diseño de esta ronda
└── plugin/
    ├── README.md                             # este archivo
    ├── test_mock.py                          # pruebas automáticas (sin impresora)
    ├── smoke_test.py                         # smoke test real (solo lectura)
    └── orca_plugins/
        └── flashforge_monitor/
            └── flashforge_monitor.py         # el plugin completo
```

El plugin es **un único archivo** `.py` con cabecera PEP 723 y exactamente una
clase `@orca.plugin` que registra una capability de tipo `pages`.

---

## Instalación (OrcaSlicer 2.5.1 Flatpak)

Directorio de datos de OrcaSlicer en Flatpak:

```
~/.var/app/com.orcaslicer.OrcaSlicer/config/OrcaSlicer/
```

Destino del plugin:

```
~/.var/app/com.orcaslicer.OrcaSlicer/config/OrcaSlicer/orca_plugins/flashforge_monitor/flashforge_monitor.py
```

```bash
# 1. Crear la carpeta del plugin
mkdir -p ~/.var/app/com.orcaslicer.OrcaSlicer/config/OrcaSlicer/orca_plugins/flashforge_monitor

# 2. Copiar el plugin (la ruta de origen tiene un espacio: comillas)
cp "/home/jfet/Documentos/Proyecto predeterminado/flashforge-plugin/plugin/orca_plugins/flashforge_monitor/flashforge_monitor.py" \
   ~/.var/app/com.orcaslicer.OrcaSlicer/config/OrcaSlicer/orca_plugins/flashforge_monitor/flashforge_monitor.py

# 3. Comprobar sintaxis
python3 -m py_compile \
   ~/.var/app/com.orcaslicer.OrcaSlicer/config/OrcaSlicer/orca_plugins/flashforge_monitor/flashforge_monitor.py

# 4. Reiniciar OrcaSlicer
flatpak run com.orcaslicer.OrcaSlicer
```

> ⚠️ **Al actualizar el plugin hay que volver a activarlo**: OrcaSlicer firma/
> autoriza el contenido del archivo. Tras copiar una versión nueva, abre
> **Plugins** (menú de OrcaSlicer), localiza *FlashForge Monitor* y **vuelve a
> aceptar/reactivarlo** si aparece el aviso; si no, la pestaña puede seguir
> mostrando la versión anterior o no cargar. Reinicia después.

### Permisos

La primera vez que el plugin abra una conexión hacia `IP:8898` (API), hacia
`IP:8080` (cámara, si existe), haga **UDP broadcast** (descubrimiento) o **lea un
archivo local** para subirlo, OrcaSlicer mostrará un diálogo **Sí/No**. Acéptalo;
Orca lo recuerda por destino.

El puerto de la API es **8898** (no 8899: ese es el protocolo TCP crudo de
texto, usado solo como *fallback* de temperatura).

### Logs

```bash
ls -lt ~/.var/app/com.orcaslicer.OrcaSlicer/config/OrcaSlicer/log/python_*.log
tail -f ~/.var/app/com.orcaslicer.OrcaSlicer/config/OrcaSlicer/log/python_*.log
```

Todos los mensajes salen por `stderr` con el prefijo `[FlashForgeMonitor]`.

---

## Configuración multi-impresora (schema v2)

```json
{
  "printers": [
    {"name": "AD5X taller", "ip": "192.168.1.50",
     "serial": "SNAD5X000001", "check_code": "a1b2c3d4"},
    {"name": "AD5M sala", "ip": "192.168.8.31", "serial": "…", "check_code": "…"}
  ],
  "active_printer": 0,
  "poll_interval": 5,
  "notify_on_finish": true,
  "notify_on_error": true,
  "mock": true
}
```

- **Migración automática** desde v0.2 (`ip`/`serial`/`check_code` en la raíz):
  `get_config_version()` devuelve `"2"`; al leer una config v1 se pasa a
  `printers[0]` **conservando el Printer ID** y se persiste sola. No se pierde
  nada de la configuración existente.
- **La UI nunca recibe los check codes** (solo `has_check_code`). Al guardar sin
  reescribir el código, el plugin lo recupera por `(IP, serial)`, por IP o por
  posición: editar el nombre o la IP no borra el secreto.
- **Un solo hilo** de sondeo: al cambiar de impresora (dropdown del topbar o
  `set-active-printer`) el cliente se re-crea con las credenciales nuevas y se
  reinicia el ciclo. Las impresoras inactivas **no** se sondean.
- **Editor**: panel *Configurar* del dashboard (añadir/quitar/editar, activa,
  flags) y los ajustes nativos del plugin en Orca (mismo formulario, sin el
  buscador UDP).
- Nombres de impresora: se limpian caracteres de control y se recortan. El nombre
  de la pestaña sigue siendo `FlashForge` (sin `;`, como exige el host).

### Modo demo con dos impresoras

Con `mock = true` y sin impresoras configuradas aparecen dos impresoras de
demostración (`192.0.2.10/11`). Al elegir una se materializan en la config para
poder alternar; cada serial de demo genera datos distintos (trabajo, progreso y
temperaturas), así se comprueba el cambio de impresora sin hardware.

---

## Descubrimiento UDP

Botón **Buscar impresoras** en el panel *Configurar* → mensaje `discover` → el
worker hace **un** broadcast/multicast (§9): `225.0.0.9:19000` y
`255.255.255.255:48899`, timeout 2 s. Parsea el anuncio de 276 B
(nombre, PID, puerto HTTP y serial) y los muestra como botones; al tocar uno se
añade la impresora con IP y serial ya rellenos: solo falta el **Printer ID**.

- **Probado desde este host (fuera del Flatpak)**: encuentra la AD5X
  (`192.168.1.50`, `SNAD5X000001`, PID `0x0026`, HTTP 8898) en ~2 s.
- **Dentro del Flatpak** el broadcast puede estar bloqueado; en ese caso el
  plugin muestra *“No se encontraron impresoras… introduce la IP manualmente”* y
  el flujo manual sigue disponible. Si el host deniega el socket, el mensaje es
  explícito (permiso de red).
- Sin respuestas no es un error: puede que la impresora esté apagada o en otra
  subred.

---

## Notificaciones

Comparan el estado del ciclo anterior con el actual (en memoria, no se
persisten):

- **Fin de trabajo**: `printing/pause → ready/completed` ⇒ “Terminó: <archivo>”.
- **Cancelación**: ⇒ “Trabajo cancelado: <archivo>”.
- **Error nuevo**: aparición de `error_code` distinto ⇒ “Error de la impresora:
  E0100” (una sola vez por código).

Se entregan por dos vías: **banner/toast del dashboard** (siempre) y
`orca.host.ui.push_notification(level, text)` si el host lo expone. La llamada va
protegida con `try/except`: en Orca 2.5.1, si esa API no existe, degrada en
silencio a solo UI. Los flags `notify_on_finish` / `notify_on_error` del config
se respetan.

## Gráfica de temperaturas y miniatura

- **Histórico** (sección colapsable “Temperaturas”): canvas nativo sin
  librerías, buffer circular de ~120 muestras (10 min a 5 s), autoescala 0–100/
  200/250 °C, objetivos con línea punteada (color `--orca-accent`) y actuales en
  naranja (boquilla) y azul (cama). Redibujo a 1 Hz solo con la sección abierta.
  Se limpia al cambiar de impresora.
- **Miniatura del trabajo actual**: `GET /getThum` (BMP) por Python → data URI
  en la tarjeta de trabajo; máximo una petición cada 30 s y se oculta si hay
  error o no hay trabajo.

---

## Interfaz (rediseño v0.4, estilo Bambu Studio)

**Composición (v0.6.0)** — el tablero se organiza en filas con un gutter único
(`--gap: 14px`) y padding común (`--pad: 14px`):

```
┌───────────────────────────────────────────────┬──────────────────┐
│ HERO del trabajo                              │ CONTROL          │
│ miniatura · nombre · estado · % · restante    │ dial XY · Z · E  │
│ chips (modelo/fw/error) · Pausar / Detener    │ extras (texto)   │
├───────────────────────────────────────────────┤                  │
│ [Boquilla] [Cama] [Velocidad] [Ventilador]    │                  │
└───────────────────────────────────────────────┴──────────────────┘
┌───────────────────────── IFS (ancho completo) ─────────────┐
└────────────────────────────────────────────────────────────┘
   Z-offset / Luz · Temperaturas (histórico) · Archivos
```

- La fila 1 usa `grid-template-columns: minmax(0,70fr) minmax(320px,30fr)` con
  `align-items: stretch`: la columna izquierda (hero + sensores) ocupa ~70 % y
  el control ~30 %, con **mínimo de 320 px** para el control. A 1280 mide
  ~371 px; a 960 el `minmax` se queda en **320 px** y la izquierda cede
  (medido ±0 px de diferencia de altura entre columnas en estado activo).
- Los **sensores viven dentro de la columna izquierda**, bajo el hero, y se
  estiran para llenar la altura del control: 4 columnas legibles a 1280/960/720
  y 2×2 por debajo de 640 px (375 px). Sin recortes de texto.
- **< 880 px** el control pasa debajo (columna única, máx. 480 px centrado). El
  hero conserva miniatura lateral también en móvil para no crecer de más.
  **375 px** sin scroll horizontal.
- El bloque de control **ya no es desplegable**: vive fijo al lado del progreso.
  Los extras (Nivelado, Input shaper, Apagado, Limpiar errores) son botones con
  **icono + texto visible**, borde propio, rejilla de 4 columnas, 10 px de gap,
  56 px de alto y texto de 12 px; el de auto-apagado muestra un punto cuando
  está programado.
- **Hero en reposo (idle)**: se compacta (~154 px, miniatura de 120 px, sin
  barra de progreso, botones deshabilitados con `aria-disabled`) y conserva sus
  chips de modelo/firmware/estado. Con trabajo activo crece y muestra miniatura,
  progreso, chips y acciones.
- El aviso «not homed» es una **línea discreta** con borde izquierdo ámbar (sin
  caja completa), texto de 12 px y botón «Ir a Home».
- Los toasts pasan a la esquina inferior derecha (máximo 3) para no tapar el
  topbar ni la tarjeta de control.

- **Hero del trabajo**: miniatura grande (o placeholder), nombre, chip de estado
  con color (verde imprimiendo, ámbar en pausa, rojo error, gris lista), barra
  gruesa con el % grande, tiempo restante y capa. **Pausar/Reanudar** y
  **Detener** (modal de confirmación, nunca `confirm()` del navegador).
- **Fila de chips**: modelo, firmware y error activo (chip rojo con botón
  *Limpiar*).
- **Tarjetas de sensores**: Boquilla, Cama, Velocidad y Ventilador con icono
  grande y valor actual enorme. Al hacer clic se abre un **popover** con botones
  rápidos (0/170/200/220/250 °C y 0/50/60/80/100 °C), valor personalizado y
  **Apagar**. Sustituyen a los steppers antiguos. La velocidad solo se habilita
  mientras imprime o está en pausa.
- **Presets de velocidad** (Bambu): Silent 50 %, Standard 100 %, Sport 124 %,
  Ludicrous 166 %; se resalta el más cercano al valor real de `/detail`.
- **IFS estilo AMS**: 4 tarjetas con anillo del color, número de slot dentro,
  material, tag **en uso** y badges `T0/T1` del mapping tool→slot del trabajo.
  El editor (paleta de 24 colores + material) se abre como popover. Los slots
  usan `padding: 14px 10px 16px` y **14 px de separación** (también en 2×2);
  la selección es un borde de 1 px + anillo interior de 2 px, sin sombra
  exterior ni cambio de tamaño. En colores muy oscuros (luminancia < 0,16) el
  anillo recibe un outline sutil derivado de `--orca-fg` para no perderse sobre
  el fondo oscuro.
- **Control**: dial XY radial (ver sección propia), tira Z, extrusor, nivelado/
  input shaper, auto-apagado 30/60/120/Off y limpiar error.
- **Archivos**: grid con miniatura, chips de tiempo/gramos/colores IFS y overlay
  **Imprimir** al pasar el ratón (y al enfocar con teclado), modal de
  confirmación, scroll interno y subida con barra de progreso.
- **Pulido**: ~25 iconos SVG inline (currentColor), skeletons *shimmer* durante
  el primer ciclo, toasts abajo a la derecha, banner no intrusivo, popovers que
  se cierran con clic fuera o `Escape`, foco visible, scrollbar tematizada,
  animación de las aspas del ventilador (más rápida con más %) y pulso del punto
  de conexión. Todo con `--orca-*`, así que claro/oscuro es automático.

La UI de configuración (pestaña del plugin) comparte el mismo lenguaje:
tarjetas de impresora, preferencias y barra de acciones fija.

## Control de movimiento (dial estilo Bambu Studio, v0.5)

La tarjeta **Control** (fija, a la derecha del hero) replica el bloque de ejes
del *Device ▸ Control* de Bambu Studio (medidas y comportamiento extraídos de su
código fuente); el dial se escala a ~186 px para encajar en los 320 px de la
tarjeta, con el extrusor al lado:

- **Dial XY único** de 258 px de viewBox escalado a **~217 px** a 1280 (164 px
  a 960, 187 px a 375), con **tres anillos**: exterior ±10 mm, interior ±1 mm y
  **Home en el centro** (casa). El paso está codificado en el radio: no hay
  selector de pasos. Ocho zonas clicables + Home. Las etiquetas numéricas van al
  86 % del color de texto (contraste ~8:1) y a 19 px de SVG (≈16 px efectivos a
  1280 y ≈12,2 px a 960).
- **Cruz separadora diagonal a 45°** y etiquetas `Y / X / -Y / -X` en negrita y
  `+10 / +1 / -1 / -10` en gris rotadas sobre la diagonal, como
  `AxisCtrlButton`.
- **Teclado**: con el dial enfocado, `↑↓←→` = 1 mm, `Shift+flechas` = 10 mm,
  `Enter` = Home. Cada zona expone `aria-label` («Mover X +10 mm») y el foco es
  visible. El dial anuncia la mecánica en un hint debajo.
- **Home** solo en el centro: abre el modal «Auto homing» (botón primario
  `Homing`) y aclara que se mueven **todos** los ejes porque la API solo ofrece
  home completo.
- **Tira Z** `10▲ 1▲ Cama 1▼ 10▼`: botones de **48×48** con dos tonos derivados
  del tema y gap 10, repartidos con `justify-content: space-between`; una sola
  fila a 1280 y 960, sin aire irregular. Hover y `focus-visible`.
- **Extrusor**: columna de 110 px con botones de 44×44 y `▲` = retraer,
  `▼` = extruir, paso fijo ±10 mm e ilustración del hotend; el dial y el
  extrusor mantienen 10 px de separación. Si la boquilla está **< 170 °C** no se
  envía nada: aparece el aviso de Bambu («Calienta la boquilla por encima de
  170 °C…») con `OK` y un atajo **Calentar a 200 °C**.
- **Estados**: imprimiendo o en pausa se deshabilitan **solo** dial, Z y
  extrusor (las temperaturas siguen activas); sin conexión se deshabilita todo
  con un velo y `aria-disabled` real.
- **Homing real (~M119)**: el chip de la cabecera muestra **Homed ✓** (verde) o
  **Sin homing** (ámbar) según los endstops reales; si el TCP falla se oculta y
  se conserva el aviso de sesión como fallback. Sin homing, dial/Z/extrusor se
  interceptan con el diálogo «Ubica el cabezal» (**Ir a Home** / **Cerrar**).
  Con homing, el jog funciona directo, sin casilla de riesgo; esa casilla queda
  solo para calibración.
- **Colores**: el verde de marca de Bambu se sustituye por `--orca-accent`
  reservado a acciones primarias; el resto son grises `color-mix` derivados de
  `--orca-bg`/`--orca-fg`, con contraste ≥ 4.5:1 medido en claro y oscuro.

Las acciones de calibración conservan su modal con casilla de riesgo; el
auto-apagado se elige en un popover (30/60/120/Off) y el botón muestra un punto
cuando hay un apagado programado.

## Estados y calibración (v0.8.0)

El hero tiene vistas propias para los estados transitorios del firmware:

| Estado | Tarjeta | Notificación al terminar |
|---|---|---|
| `calibrate_doing` | Nivelando… / Input shaper… / Calibrando…, ondas animadas y barra indeterminada | ✓ Nivelado/Input shaper completado, o error con código |
| `heating` | Calentando…, termómetro y temps actuales → objetivo | Temperatura lista |
| `loading` | Cargando/Descargando filamento… según el último `ms_cmd` | ✓ Filamento cargado/descargado |
| `busy` o desconocido | Ocupada… (compacta) | — |

- Durante calibración/ocupada/carga se bloquean dial, Z, extrusor y `ms_cmd`
  con `aria-disabled` y la nota «Controles bloqueados mientras la impresora
  trabaja». Las temperaturas siguen activas (el firmware las acepta).
- Los botones Pausar/Detener se ocultan en estas tarjetas.
- Las notificaciones de fin son **siempre** (no dependen de los flags
  `notify_*`), usan toast + `push_notification` y respetan `reduced-motion`
  (icono y barra estáticos).
- **En la AD5X real el firmware no reporta `calibrate_doing` ni `loading`**:
  durante una nivelación `/detail` y `~M119` solo dicen `busy` /
  `MachineStatus: BUSY + MoveMode: MOVING`. Por eso v0.8.1 registra una
  **intención local** al despachar `calibration_cmd`, `ms_cmd` o
  `homingCtrl_cmd`, y la usa como prioridad de visualización (intención > estado
  del firmware > busy genérico). Se cierra al ver `ready`/`printing`, por
  `MachineStatus/MoveMode: READY` de M119 (más rápido) o por error; los timeouts
  de seguridad son 12 min (nivelado), 8 min (input shaper), 5 min (carga/
  descarga) y 3 min (homing), y en ese caso solo avisa «no se confirmó el fin».
  La tarjeta busy sin intención dice «Ocupada — operación iniciada fuera de la
  pestaña».
- **Cadencia adaptativa**: 2 s durante transitorios e impresión; el
  `poll_interval` del usuario en reposo; backoff 5/10/20/40/60 s si la
  impresora no responde (contador de fallos, reset al primer éxito). El banner
  muestra «reintentando… (Xs)» sin repetir toasts. Un `refresh` manual
  interrumpe el backoff.

## Fin de impresión y config estable (v0.9.0)

- Tras `completed`/`cancel`, el hero muestra una tarjeta con tilde verde,
  «Tiempo utilizado: Xh Ymin» (si `/detail` lo trae), el texto «Después de
  limpiar la cama, pulsa para desbloquear la impresora» y dos botones:
  **Confirme** (envía `stateCtrl_cmd` → `setClearPlatform`) y **Volver a
  imprimir** (limpia, espera `ready` hasta 10 s y reenvía `printGcode` del mismo
  archivo). Pausar/Detener se ocultan.
- Si se lanza un archivo desde **Archivos** con la impresora en `completed`/`cancel`,
  se intercepta con un modal que ofrece limpiar la plataforma e imprimir.
- **Nombre estable del wheel**: el hub recibe siempre
  `flashforge_monitor_any.whl`; así el `plugin_key` de OrcaSlicer no cambia entre
  versiones. El build también genera el nombre canónico con versión.
- **Rescate de config**: si `get_config()` arranca sin `printers`, el plugin lee
  `data_dir()/orca_plugins/config.json` (vía `orca.host.plugin.storage()`, con
  fallback a las rutas típicas de Linux/Flatpak/Windows/macOS) y adopta la
  entrada `pages`/`FlashForge` más completa y de versión más alta. Se ejecuta una
  vez por carga y se guarda en la capability actual.

## Homing real por TCP 8899 (v0.7.0)

El plugin consulta `~M601 S1` + `~M119` (solo lectura) al conectar, cada 30 s,
tras un home (cada 5 s durante 30 s) y tras mover o extruir. El parser acepta el
formato moderno de la AD5X/5M (`Endstop: X-max: 110 Y-max: 110 Z-min: 0`) y el
legacy 0/1 de la Adventurer 3.

Smoke real contra la AD5X (v3.1.8), sin mover nada:

```
→ ~M601 S1
← CMD M601 Received. / Control Success V2.1. / ok
→ ~M119
← CMD M119 Received.
  Endstop: X-max: 110 Y-max: 110 Z-min: 0
  MachineStatus: READY
  MoveMode: READY
  Status: S:0 L:0 J:0 F:0
  LED: 0
  CurrentFile:
  ok
→ ~M602
← CMD M602 Received. / Control Release. / ok
```

Parseo: `{x: true, y: true, z: true, homed: true}`. Si el puerto no responde,
`get_endstops()` devuelve `homed: null` y la UI no muestra chip.

## Carga y descarga de filamento (`ms_cmd`, v0.7.0)

El editor de cada slot incluye **Cargar filamento aquí** y **Descargar
filamento**. Ambos respetan la boquilla ≥ 170 °C (si está fría, aviso con atajo
**Calentar a 200 °C**), piden confirmación simple y se deshabilitan con tooltip
mientras la impresora imprime o está en pausa.

Payload exacto (§7.3), con `slot` 1-based en la API:

```json
{ "cmd": "ms_cmd", "args": { "slot": 1, "action": 0 } }   // load
{ "cmd": "ms_cmd", "args": { "slot": 1, "action": 1 } }   // unload
```

La UI envía `ms-load`/`ms-unload` con el índice 0-based del slot; el cliente
suma 1. En modo demo el slot cambia de `present` para reflejar la operación. El
`ms_cmd` real **no se ejecutó** en la impresora durante esta ronda.

## Accesibilidad y pulido (pasada v0.5)

- Pasos y presets son `<button aria-pressed>`; los iconos decorativos van con
  `aria-hidden` y los botones icono con `aria-label`.
- `prefers-reduced-motion`: se desactivan aspas giratorias, shimmer, pulsos y
  transiciones.
- Modal con foco atrapado, `Escape` para cerrar y retorno de foco al disparador.
- En pantallas táctiles (`hover: none`) el botón **Imprimir** de las tarjetas de
  archivos queda siempre visible (no depende del hover).
- Toasts en la esquina inferior derecha; banner no intrusivo.
- Todos los popovers se anclan a su trigger con un helper único (`anchorPopover`):
  a la derecha si cabe, si no a la izquierda, con flip vertical, margen de 8 px,
  clamp al viewport y re-anclaje en `resize` y scroll interno.

## Módulos de la ronda 1 (resumen)

| Módulo | Comandos |
|---|---|
| Control | `temperatureCtl_cmd` (+ fallback TCP 8899), `printerCtl_cmd` (velocidad, Z, ventiladores), `lightControl_cmd` |
| Archivos | `gcodeList`, `gcodeThumb`, `getThum`, `printGcode` (mapeo IFS automático), `uploadGcode` (multipart, auth por cabeceras, ≤ 200 MB) |
| Control | `homingCtrl_cmd`, `moveCtrl_cmd` (pasos por anillo), `extrudeCtrl_cmd` (±10 fijo), `calibration_cmd`, `errorCodeCtrl_cmd`, `delayClose_cmd` |

La API HTTP **no tiene borrado de archivos**: no hay botón de eliminar. Las
acciones peligrosas piden confirmación (las de movimiento, dos veces).

---

## Probar sin impresora (modo demo)

```bash
cd "/home/jfet/Documentos/Proyecto predeterminado/flashforge-plugin/plugin"
python3 test_mock.py          # resumen
python3 test_mock.py -v       # detalle
```

Cuatro secciones:

1. **Cliente en modo demo**: todos los métodos y validaciones.
2. **Cliente contra la API real simulada**: HTTP falso con `/detail`, IFS,
   control, temperaturas + fallback TCP 8899, archivos, subida multipart…
3. **Descubrimiento UDP**: servidor UDP local que responde con el paquete de
   276 B (offsets §9.2), más el caso de permiso denegado.
4. **Capability completa** con un `orca` falso: migración v1→v2, editor
   multi-impresora, cambio de impresora activa, notificaciones (con
   `push_notification` simulado), descubrimiento vía handler, y todos los
   mensajes de la ronda 1.

Salida esperada: `Resultado: 4/4 secciones correctas`, exit `0`.

---

## Smoke test contra la impresora real

`smoke_test.py` es **solo lectura** (no mueve, no calienta, no imprime):

```bash
cd "/home/jfet/Documentos/Proyecto predeterminado/flashforge-plugin/plugin"
python3 smoke_test.py --code <Printer ID>
```

Hace: descubrimiento UDP, `/checkCode` (normal y con serial falso), `/detail`
(normal y con serial falso), `/product`, `/gcodeList`, `/gcodeThumb` y
`GET /getThum`. Sin `--code` usa `88888888` para validar la plomería y el
diagnóstico de credenciales.

Salida observada el 2026-11-20 (`192.168.1.50`, `SNAD5X000001`, fw
`3.1.8-1.1.1`):

```
UDP discovery: 1 respuesta(s)
   AD5X · 192.168.1.50 · SNAD5X000001 · pid 0x0026 · http 8898

POST /checkCode: code=0 message='Success'
POST /checkCode (SN falso): code=0 message='Success'
POST /detail (SN falso): code=1 message='SN is different'
POST /detail: code=0 message='Success'
   estado='ready' pid=38 fw='3.1.8-1.1.1'
   nozzle=29.93/0.0 cama=29.51/0.0 speed=0.0 z=0.0 led='open'
   IFS: slot1=PETG, slot2=PETG, slot3=PETG, slot4=PLA
POST /product: code=0 message='Success'
POST /gcodeList: code=0 · 10 archivo(s)
POST /gcodeThumb: code=0 · 23788 bytes base64
GET /getThum: HTTP 200 · appliation/json · 40 bytes

  ✓ Descubrimiento UDP responde
  ✓ envelope de /checkCode correcto
  ✓ con serial falso, /detail responde 'SN is different'
  ✓ /detail y /product autentican
  ✓ gcodeList / gcodeThumb / getThum responden

Credenciales válidas: la API real responde datos de la impresora.
```

Nota: con check code válido, `/checkCode` acepta un serial falso (regla F4,
§3.4); el serial lo valida `/detail`.

---

## Cómo funciona (resumen técnico)

- **Base URL**: `http://<ip>:8898`, `POST` JSON con
  `{"serialNumber", "checkCode"}`. HTTP 200 siempre; el resultado está en
  `code`/`message`. **Sin CORS** → todo el HTTP pasa por Python y llega a la
  página por `post_message()`.
- **Un solo `/detail` por ciclo** (`get_snapshot()`): estado, temperaturas, IFS
  y valores del panel de control.
- **Hilo daemon + cola**: `on_message()` (hilo de UI) valida y encola; el worker
  hace HTTP/UDP/subidas y publica. `on_load`/`on_unload` lo arrancan y paran.
  Cambiar de impresora re-crea el cliente en el worker.
- **Notificaciones** se calculan en el worker comparando estados y se publican.
- **Tematización**: CSS con `--orca-bg`, `--orca-fg`, `--orca-muted`,
  `--orca-accent`, `--orca-accent-fg`, `--orca-border` y `--orca-font`.

## Problemas típicos

| Síntoma | Causa / solución |
|---|---|
| Tras actualizar no veo los cambios | Vuelve a activar el plugin en el diálogo **Plugins** y reinicia OrcaSlicer. |
| La pestaña no aparece | Ruta incorrecta o error de sintaxis: revisa `python_*.log` y `py_compile`. |
| “No se pudo conectar con IP:8898” | Impresora apagada, IP cambiada o LAN-only desactivado. |
| “Check code incorrecto” | Usa el **Printer ID** de la pantalla, no el `88888888` de Orca. |
| “Serial incorrecto” en el sondeo | El SN guardado en esa impresora no coincide. |
| “Buscar impresoras” no encuentra nada | Broadcast bloqueado (Flatpak) o impresora en otra subred: escribe la IP a mano. |
| La subida falla con “Sin permiso para leer…” | El sandbox no puede leer esa ruta o no aceptaste el diálogo. |
| La gráfica está vacía | La sección está cerrada (solo se dibuja abierta) o aún no hay sondeos. |
| No llegan notificaciones | Mira los flags en Configurar; el host 2.5.1 puede no exponer `push_notification` (queda el banner). |
| Cambio de impresora y no reconecta | Revisa que esa impresora tenga IP/SN/check code; el banner mostrará el diagnóstico. |

## Pendiente para una ronda futura

1. Sondeo en segundo plano de las impresoras inactivas (badge de estado).
2. Notificaciones nativas si el host expone la API en versiones futuras
   (hoy ya se intenta y se degrada).
3. Historial persistente de trabajos/estadísticas (filamento, tiempos).
4. Exportar el histórico de temperaturas a CSV.
5. LED con color (hoy solo encendido/apagado).
