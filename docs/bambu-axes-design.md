# Bambu Studio — anatomía de los controles de ejes (Device ▸ Control)

**Objetivo:** documentar con precisión cómo Bambu Studio diseña y hace funcionar el bloque de
movimiento de ejes (jog) de la pestaña *Device*, para replicarlo en un dashboard web.

**Fuente primaria:** código fuente de `github.com/bambulab/BambuStudio`, rama `master`,
commit `8e524a3` (`Add optional per-object coloring in Preview`, 2026-11-15). Todos los
números de línea citados corresponden a ese commit.

**Hallazgo clave de partida:** el control de ejes **no es un webview**. En esta versión el panel
*Control* sigue siendo wxWidgets nativo y auto-dibujado (`AxisCtrlButton`, `Button`, `StaticBox`).
En el repo existe además un prototipo React **no cableado** (`features/printer/motion/*.tsx`)
que rediseña el bloque como popup; se documenta aparte (§7) porque anticipa hacia dónde va
Bambu, pero **no es lo que se envía**.

---

## 1. Estructura general de la página

### 1.1 Contenedor: Device ▸ Status

La pestaña *Device* es un `Tabbook` con barra vertical izquierda y varias páginas:
`Status`, `Print queue`, `Storage`, `Assistant(HMS)`… (`Monitor.cpp:202-218`).

La página `Status` (`StatusPanel`) se divide en **dos columnas** (`StatusPanel.cpp:1996-2056`):

```
┌ Device ▸ Status ─────────────────────────────────────────────────────────────────────┐
│ ┌ Camera ──────────────────────────────────────────┐  ┌ Control ──────────────────┐ │
│ │  barra "Camera" (36 DIP, fondo #F8F8F8)           │  │  [Printer Parts] (verde)  │ │
│ │  LiveView / vídeo                                │  ├───────────────────────────┤ │
│ ├──────────────────────────────────────────────────┤  │  ┌ TARJETA 586 DIP ──────┐ │ │
│ │  PrintingTaskPanel                               │  │  │ TEMPS │ XY │ EXTRUSOR│ │ │
│ │  miniatura · % · tiempo · pausa/stop             │  │  └───────────────────────┘ │ │
│ │                                                  │  │  ┌ AMS / filamentos ─────┐ │ │
│ │                                                  │  │  └───────────────────────┘ │ │
│ │                                                  │  │  [Filament loading...]    │ │
│ └──────────────────────────────────────────────────┘  └───────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

- Columna izquierda: flexible (`bSizer_left`, proportion 1).
- Columna derecha: **ancho fijo 586 DIP** (`box->SetMinSize(wxSize(FromDIP(586), -1))`,
  `StatusPanel.cpp:2716-2719`), sin scroll propio.
- Fondo de página `#EEEEEE` (`STATUS_PANEL_BG`), títulos de sección sobre `#F8F8F8`
  (`STATUS_TITLE_BG`) con texto 14 pt `#6B6B6B` y 36 DIP de alto (`StatusPanel.cpp:100-120, 586`).

### 1.2 Orden vertical del panel derecho

`create_machine_control_page()` (`StatusPanel.cpp:2559-2691`) construye, en este orden:

1. **Fila de título "Control"** (36 DIP): a la izquierda el texto `Control`; a la derecha,
   botones verdes de 26 DIP de alto: `Printer Parts` (128×26) y `Calibration`; además existen
   `Print Options` y `Safety Options`, que el código **oculta** al construir la página
   (`StatusPanel.cpp:2636-2637`).
2. **Tarjeta "temperaturas + ejes + extrusor"** (`create_temp_axis_group`, 586 DIP de ancho).
3. **Tarjeta AMS / filamentos** (586 DIP).
4. **Caja plegable "Filament loading..."** (586 DIP).

La tarjeta del punto 2 es la relevante aquí. Su layout interno es **horizontal de 3 columnas**
separadas por líneas de 1 px `#EEEEEE` (`StatusPanel.cpp:2693-2765`):

| Columna | Contenido (arriba → abajo) |
|---|---|
| **Izquierda** (~145 DIP) | Temperatura de boquilla (`145×48`), línea, cama (`125×52`), línea, cámara (`125×52`), luego fila **Speed (66×51) \| Lamp (66×51)** y panel **Fan (136×55)** |
| **Centro** (`AXIS_MIN_SIZE` = 258 DIP) | **Dial XY circular** (258×258) → **tira "Bed"** (Z) → *(solo H2D)* rack de hotends |
| **Derecha** (143 DIP) | **Columna extrusor**: switch `Left/Right` (126×26, solo doble boquilla), botón ▲ 40×40, ilustración 45×112, botón ▼ 40×40, estado, etiqueta `Extruder` |

Constantes exactas: `AXIS_MIN_SIZE (258,258)`, `Z_BUTTON_SIZE (44,40)`,
`TEMP_CTRL_MIN_SIZE_ALIGN_TWO_ICON (145,48)`, `..._ONE_ICON (125,52)`,
`MISC_BUTTON_2FAN_SIZE (66,51)`, `MISC_BUTTON_PANEL_SIZE (136,55)`
(`StatusPanel.cpp:602-609`). Esquinas de la tarjeta: **radio 5**, fondo blanco, borde `#EEEEEE`
(`StatusPanel.cpp:2704-2723`).

> **Nota de jerarquía:** el bloque de ejes **no tiene título propio** dentro de la tarjeta. Las
> letras `X/-X/Y/-Y` del dial y la palabra `Bed` son toda la rotulación. El extrusor sí lleva
> etiqueta `Extruder` debajo.

---

## 2. El widget de ejes, al detalle

### 2.1 Dial XY: un solo control circular de 3 anillos

`AxisCtrlButton` (`src/slic3r/GUI/Widgets/AxisCtrlButton.cpp`) es **un único wxWindow
auto-dibujado** (no son 9 botones): se pinta un SVG-like y se hace *hit-testing* geométrico.

Radios en DIP (`AxisCtrlButton.cpp:24-28`):

```cpp
#define OUTER_SIZE   FromDIP(105)   // radio exterior  -> disco de 210 DIP
#define INNER_SIZE   FromDIP(58)    // borde anillo interior
#define HOME_SIZE    FromDIP(23)    // círculo Home
#define BLANK_SIZE   FromDIP(24)    // hueco entre Home y anillo interior (1 DIP)
#define GAP_SIZE     FromDIP(4)     // grosor de la cruz blanca separadora
```

Se dibuja centrado dentro de un cuadro de **258×258 DIP** (`SetMinSize(AXIS_MIN_SIZE)`),
es decir: disco de 210 DIP con **24 DIP de aire** a cada lado.

```
             ┌──────────── 258 DIP ────────────┐
             │            Y  (bold)            │
             │        ╱           ╲            │
             │   +10 ╱   anillo     ╲          │
             │      ╱   exterior     ╲         │
             │ -X  │   (+10 / -10)   │  X      │
             │      ╲   ┌───────┐   ╱          │
             │   +1  ╲  │  🏠   │  ╱  (Home)   │
             │        ╲ │ 46 DIP│ ╱            │
             │   -1    ╲└───────┘╱             │
             │      anillo interior (+1 / -1)  │
             │            -Y                   │
             └─────────────────────────────────┘
```

**Geometría y etiquetas**

- 4 sectores de 90° **separados por una cruz blanca rotada 45°** (no horizontal/vertical):
  los cortes caen en las diagonales, de modo que cada sector queda centrado en un punto
  cardinal (`AxisCtrlButton.cpp:194-202`). Visualmente son "cuatro porciones de tarta".
- Etiquetas de eje, en negrita 12 pt (`Label::Head_12`), negro normal / `#6B6B6B` deshabilitado:
  `Y` arriba, `-X` izquierda, `X` derecha, `-Y` abajo (`AxisCtrlButton.cpp:254-267`).
- Etiquetas numéricas **rotadas 45°** sobre la diagonal NE, regular 12 pt gris `#898989`:
  `+10` (anillo exterior arriba), `+1` (anillo interior arriba), `-1` (anillo interior abajo),
  `-10` (anillo exterior abajo) (`AxisCtrlButton.cpp:269-281`).
- El centro es un **círculo Home de 46 DIP** (r=23) con el icono `monitor_axis_home`
  (casa **verde `#00AE42`**, SVG 32×32) y 1 DIP de aire blanco respecto al anillo interior.

**Colores de reposo** (`AxisCtrlButton.cpp:9-14`):

| Zona | Fill |
|---|---|
| Anillo exterior (±10) | `#EEEEEE` |
| Anillo interior (±1) | `#CECECE` |
| Home | `#EEEEEE` |
| Separadores (cruz) | blanco `#FFFFFF` |

**Hit-testing** (`AxisCtrlButton.cpp:306-364`): según el radio y el signo de `y±x` (con umbral
`gap`) se resuelve una de 9 zonas:

```
enum CurrentPos {
  OUTER_UP=0, OUTER_LEFT=1, OUTER_DOWN=2, OUTER_RIGHT=3,
  INNER_UP=4, INNER_LEFT=5, INNER_DOWN=6, INNER_RIGHT=7,
  INNER_HOME=8, UNDEFINED=9 };
```

**Mapeo zona → movimiento** (`StatusPanel.cpp:5440-5487`):

| Zona | Comando | Paso |
|---|---|---|
| 0 OUTER_UP | `Ctrl_Axis("Y", +10, F3000)` | 10 mm |
| 1 OUTER_LEFT | `Ctrl_Axis("X", -10, F3000)` | 10 mm |
| 2 OUTER_DOWN | `Ctrl_Axis("Y", -10, F3000)` | 10 mm |
| 3 OUTER_RIGHT | `Ctrl_Axis("X", +10, F3000)` | 10 mm |
| 4 INNER_UP | `Ctrl_Axis("Y", +1, F3000)` | 1 mm |
| 5 INNER_LEFT | `Ctrl_Axis("X", -1, F3000)` | 1 mm |
| 6 INNER_DOWN | `Ctrl_Axis("Y", -1, F3000)` | 1 mm |
| 7 INNER_RIGHT | `Ctrl_Axis("X", +1, F3000)` | 1 mm |
| 8 HOME | `Ctrl_GoHome()` previa confirmación | — |

> El paso **no se elige**: está codificado en el radio. Anillo interior = 1 mm, anillo
> exterior = 10 mm. **No hay dropdown ni pills de paso en toda la página de control.**

> Semántica: `+Y` = arriba en el dial, `+X` = derecha; son **coordenadas de impresora**, no
> "dirección en pantalla". En máquinas no CoreXY el firmware recibe `Y`/`Z` invertidos
> (`DevAxisCtrl.cpp:33-38, 42-47`), de modo que la UI mantiene siempre el mismo significado.

### 2.2 Tira "Bed" (eje Z): 5 celdas en horizontal

No es una cruz ni un dial: es **una fila** (`create_bed_control`, `StatusPanel.cpp:3090-3155`).

```
[ 10 ▲ ] [ 1 ▲ ]   Bed   [ 1 ▼ ] [ 10 ▼ ]
  44×40    44×40          44×40    44×40
```

- Orden exacto: `10↑`, `1↑`, etiqueta `Bed`, `1↓`, `10↓`; gaps de 2 DIP (`StatusPanel.cpp:3145-3149`).
- Botones **cuadrados**: `radius = 0`, `border-width = 0` (sin borde), 44×40 DIP.
- Icono de flecha 15 DIP (`monitor_bed_up` / `monitor_bed_down`, SVG 16×16 `#323A3D`) que
  incluye una **línea horizontal** (la cama) en la base del triángulo, más el número en 12 pt.
  El label del botón es literalmente `"10"` y `" 1"` (con espacio inicial para alinear el
  dígito bajo el "10").
- **Dos tonos para distinguir el paso**: ±10 sobre `#EEEEEE` (`BUTTON_NORMAL1_COL`), ±1 sobre
  `#CECECE` (`BUTTON_NORMAL2_COL`); pressed `#ACACAC` (`StatusPanel.cpp:3095-3104, 100-105`).
- Semántica: `▲` = cama sube ⇒ comando `Z -10/-1`; `▼` = cama baja ⇒ `Z +10/+1`
  (`StatusPanel.cpp:5501-5536`), a `F900`.
- La etiqueta `Bed` es 12 pt `#6B6B6B`; si el equipo tiene doble boquilla puede cambiar a
  `Left/Right` en el bloque extrusor, pero la tira Z no cambia.

### 2.3 Home: solo en el centro del dial

- Icono casa verde en el centro del disco; **no hay botón "Home all" aparte** ni homes por eje.
- Al hacer clic se abre un `SecondaryCheckDialog` (`StatusPanel.cpp:5475-5484`):
  - Título: **`Auto homing`**
  - Texto: **`Are you sure you want to trigger auto homing?`**
  - Botón primario: **`Homing`** (verde) · Botón secundario: **`Cancel`** (blanco con borde)
- Confirmar ejecuta `Ctrl_GoHome()`: MQTT `print.command = back_to_center` si la impresora lo
  soporta; si no, `G28` (todas las ejes) — y `G28 X` si está imprimiendo (`DevAxisCtrl.cpp:10-25`).
- Es un diálogo flotante reutilizado (se crea una vez y se re-muestra), no modal.

### 2.4 Aviso "not homed": el texto exacto

Antes de mover X o Y, `on_axis_ctrl_xy()` comprueba `IsAxisAtHomeX()/IsAxisAtHomeY()`
(`StatusPanel.cpp:5446-5455`). Si el eje no está referenciado, **aborta el movimiento** y
muestra `RecenterDialog` (`RecenterDialog.cpp`).

- Título de ventana: **`Confirm`**
- Texto (se dibuja con el icono de casa *inline* en medio de la frase):

  > **`Please home all axes (click `** 🏠 **`) to locate the toolhead's position. This prevents device moving beyond the printable boundary and causing equipment wear.`**

  (`RecenterDialog.cpp:20-21`; el icono es `monitor_axis_home_icon`, casa verde con relleno al 20 %.)
- Botones (`RecenterDialog.cpp:33-47`):
  - **`Go Home`** — primario, verde `#00AE42` (hover `#3DCB73`), texto blanco, radio 12, alto 24.
  - **`Close`** — secundario, blanco, borde `#262E30`, texto `#262E30`, radio 12.
- `Go Home` ⇒ `Ctrl_GoHome()` (home **de todos** los ejes). `Close` ⇒ cancela, no mueve.
- Ventana de 475 DIP de ancho, 100 DIP de área de texto, padding 25 DIP.

⚠️ **No existe opción "Move anyway" / "mover de todos modos".** Bambu Studio **bloquea** el jog
y solo ofrece *ir a home* o *cerrar*. La guía de Bambuddy describe un modal "Studio-style" con
`Home Z / Move anyway / Cancel`; eso es **una adaptación propia de Bambuddy**, no el
comportamiento de Bambu Studio (ver §8).

⚠️ **Asimetría en Z:** los handlers de Z ejecutan `Ctrl_Axis(...)` **antes** de llamar a
`check_axis_z_at_home()` (`StatusPanel.cpp:5501-5536`), así que el comando Z ya se despachó
cuando aparece el aviso. Es una inconsistencia (probable bug) y **no debe replicarse**.

⚠️ El aviso **no recuerda** la respuesta: cada intento sobre un eje no referenciado vuelve a
mostrarlo. Tampoco hay ningún indicador permanente de "homed / not homed" en la UI.

### 2.5 Estados habilitado / deshabilitado

Tres capas independientes:

| Situación | Qué pasa | Fuente |
|---|---|---|
| **Imprimiendo** (`is_in_printing() && !can_resume()`, es decir `RUNNING`, `PREPARE` o `SLICING`) | `show_printing_status(false, true)`: se deshabilitan **dial XY, los 4 botones Z y los 2 de extrusor**; las **temperaturas siguen activas** | `StatusPanel.cpp:4150-4156, 4348-4360`; `DeviceManager.cpp:2435-2440, 2454-2466` |
| **Pausado** (`print_status == PAUSE`) | `can_resume()` = true ⇒ **todo el bloque de movimiento se rehabilita** | idem |
| **Desconectado / conectando / sin impresora** | `show_printing_status(false, false)`: movimiento **y** temperaturas deshabilitados; también los botones del título | `StatusPanel.cpp:7019-7035` |
| **Restricción de cuenta** (menores de edad) | Velo blanco alpha 170 sobre la tarjeta + tarjeta flotante "Attention!" al pasar el ratón; además los comandos se rechazan en `Ctrl_Axis`/`Ctrl_GoHome` con `is_monitor_ctrl_blocked(MonitorCtrlId::AxisMove)` | `StatusPanel.cpp:2820-2855, 4417-4448`; `GUI_App.cpp:4967-4984`; `DevAxisCtrl.cpp:12, 27` |

**Cómo se ve "deshabilitado"** (`StatusPanel.cpp:4348-4385`):

- El dial XY **no cambia de forma**: solo las letras pasan a `#ABACAC` (`DISCONNECT_TEXT_COL`)
  y dejan de responder al hover. El anillo conserva `#EEEEEE/#CECECE`.
- Los botones Z y E **sustituyen sus iconos** por variantes `*_disable` (flechas `#ACACAC`) y
  el texto pasa a `#ABACAC`.
- `Bed` y `Extruder` pasan a `#ABACAC`.
- No hay tooltip explicando *por qué* está deshabilitado (los jog no tienen tooltip alguno).

---

## 3. Control de extrusor

`create_extruder_control()` (`StatusPanel.cpp:3164-3230`), columna de **143 DIP** de ancho:

```
        [ Left | Right ]        126×26   (solo doble boquilla)
             ▲  40×40            icono triángulo 22 DIP, borde 2, radio 8
        ┌───────────┐
        │  dibujo   │           45×112 (ExtruderImage: estado lleno/vacío,
        │ extrusor  │                    carga/descarga, 1 o 2 boquillas)
        └───────────┘
             ▼  40×40
        [estado de cambio de extrusor]   (solo durante swap)
           Extruder                etiqueta 13 pt #6B6B6B
```

- **Dos únicos botones**: ▲ y ▼, **paso fijo de 10 mm** (`±10.0f`, `F900`).
  No hay selector de paso ni campo numérico.
- Semántica: **▲ = retraer** (`E-10`), **▼ = extruir** (`E+10`) — la flecha indica el sentido
  del filamento, no el del flujo (`StatusPanel.cpp:5554-5589`).
- **Aviso de boquilla fría**: si `GetCurrentTemp() < 170 °C` (`TEMP_THRESHOLD_ALLOW_E_CTRL`,
  `StatusPanel.cpp:94`), **no se envía nada** y aparece un `MessageDialog`:
  - Título: **`Warning`**
  - Texto: **`Please heat the nozzle to above 170 degree before loading or unloading filament.`**
  - Un solo botón `OK` (`StatusPanel.cpp:5537-5551`).
- No se comprueba si hay filamento cargado; el botón siempre está disponible (si no está
  imprimiendo).
- En impresoras con rack de hotends (H2D), este bloque se sustituye por `wgtRackExtruder`,
  que emite los mismos eventos `EVT_RACK_EXTRUDER_E_UP/DOWN` y reutiliza los mismos handlers
  (`StatusPanel.cpp:3757-3759`).

---

## 4. Detalle visual (tokens medidos en código)

### 4.1 Paleta

| Token | Valor | Uso |
|---|---|---|
| Fondo de página | `#EEEEEE` | `STATUS_PANEL_BG` |
| Fondo de barra de título | `#F8F8F8` | `STATUS_TITLE_BG` |
| Tarjeta | `#FFFFFF`, borde `#EEEEEE`, radio **5** | `create_temp_axis_group` |
| Gris claro (anillo ext. / Z±10) | `#EEEEEE` | `BUTTON_NORMAL1_COL` |
| Gris medio (anillo int. / Z±1) | `#CECECE` | `BUTTON_NORMAL2_COL` |
| Gris pulsado | `#ACACAC` | `BUTTON_PRESS_COL` |
| Verde marca (hover/primario) | `#00AE42` | `BUTTON_HOVER_COL`, `bd` |
| Verde hover claro | `#3DCB73` | hover de `Go Home` |
| Texto normal | `#303A3C` | `NORMAL_TEXT_COL` |
| Texto secundario | `#6B6B6B` | `Bed`, `Extruder`, títulos |
| Texto deshabilitado | `#ABACAC` | `DISCONNECT_TEXT_COL` |
| Números del dial | `#898989` | `text_num_color` |
| Iconos de flecha | `#323A3D` | SVG `monitor_bed_*`, `monitor_extruder_*` |

### 4.2 Formas y tamaños

| Elemento | Forma | Tamaño | Notas |
|---|---|---|---|
| Dial XY | Círculo completo (3 anillos) | 210 DIP de Ø en caja de 258 | auto-dibujado, sin bordes exteriores |
| Sector | Cuña de 90° | — | separación por cruz blanca de **4 DIP** rotada 45° |
| Home | Círculo | 46 DIP (r=23) | icono casa 32×32 |
| Botón Z | **Cuadrado** (radio 0, sin borde) | 44×40 | icono 15 + texto 12 pt, gap 5 |
| Botón E | Cuadrado redondeado (radio 8, borde 2) | 40×40 | icono 22 |
| Switch Left/Right | Píldora segmentada | 126×26 | solo doble boquilla |
| Botones de diálogo | Píldora (radio 12) | alto 24 | verde primario / blanco secundario |

### 4.3 Estados de interacción

- **Hover (dial):** el sector bajo el cursor recibe **contorno verde `#00AE42` de 2 px**
  (`border_color` en estado Hovered, `AxisCtrlButton.cpp:45, 190`). El relleno **no** cambia.
  El contorno sigue la forma exacta de la cuña (arcos + lados rectos).
- **Pressed (dial):** relleno del sector → `#ACACAC`, contorno verde 2 px (`:182-191`).
- **Hover (Z):** *no hay*. `border-width = 0` y el `StateColor` de fondo no define Hovered:
  el botón solo cambia al pulsar (`#ACACAC`). Inconsistencia real del diseño original.
- **Hover (E):** contorno verde 2 px (el borde normal es del mismo color que el fondo, por lo
  que "aparece" al pasar el ratón).
- **Selección de paso:** no existe control de selección; el "paso activo" se comunica por
  **posición + tono** (anillo interior vs exterior; `#CECECE` vs `#EEEEEE`).
- **Foco / teclado:** el dial hace `SetFocus()` al hacer clic pero **no implementa teclado**
  ni dibuja anillo de foco. Todo el bloque es solo-ratón.
- **Tooltips:** ninguno en XY, Z ni E.

### 4.4 Densidad y coherencia

- Espaciados dominantes: 5, 6, 7, 8, 10, 12, 15 DIP. La tarjeta usa 5 DIP de padding interno
  y separadores de 1 px `#EEEEEE` entre columnas.
- Jerarquía tipográfica: 12 pt (números, `Bed`), 13 pt (`Extruder`), 14 pt (título `Control`).
  No hay títulos en negrita dentro de la tarjeta salvo las letras de eje (`Head_12`).
- La composición es **una sola tarjeta blanca** que agrupa temperatura + movimiento; el
  movimiento ocupa el centro geométrico y es el elemento más grande (258×258) de toda la
  columna derecha.

---

## 5. Traducción a un dashboard HTML/CSS/JS (reglas "Bambu")

### 5.1 Replicar tal cual

1. **Un solo disco XY, sin dropdown de paso.** 8 zonas = 4 direcciones × 2 radios
   (interior 1 mm / exterior 10 mm). Si el dashboard necesita más pasos, añádelos como
   *anillos* o como un control aparte, nunca sustituyendo la interacción radial.
2. **Cruz blanca diagonal de 4 px** entre sectores + etiquetas `+10 / +1 / -1 / -10` rotadas
   45° sobre la diagonal, en gris `#898989`. Es lo que hace el control legible sin leyenda.
3. **Home en el centro físico** del disco, icono casa verde `#00AE42`, 46 px de círculo, y
   **una confirmación explícita** con botón primario verde (`Homing`) + `Cancel`.
4. **Bed como tira horizontal de 5 celdas**: `10▲ 1▲ · Bed · 1▼ 10▼`, botones cuadrados
   (sin radio), 44×40, dos tonos de gris para distinguir 10 vs 1.
5. **Extrusor como columna vertical** con dos botones 40×40 separados por una ilustración del
   filamento, etiqueta `Extruder` debajo, y **▲ = retraer / ▼ = extruir**.
6. **Verde solo para hover/acción primaria**; grises para todo lo demás. Nada de azul.
7. **Deshabilitado = gris `#ABACAC` + iconos apagados**, sin animación ni tooltip.
8. **Bloqueo duro del jog sin home**, con mensaje que explica el *por qué* ("prevents device
   moving beyond the printable boundary and causing equipment wear") y CTA `Go Home`.

### 5.2 Adaptar (mejoras recomendadas para web)

| Punto Bambu | Problema | Recomendación |
|---|---|---|
| Sin indicador de homing | El usuario descubre que no está homed al hacer clic | Chip persistente `Homed / Not homed` por eje, junto al dial |
| "Move anyway" inexistente | Bloquea trabajo legítimo (p. ej. bajar cama tras un print) | Añadir tercera opción **`Move 1 mm anyway`** con `M211 S0` y re-armado, al estilo Bambuddy, marcada como avanzada |
| Z mueve antes de avisar | Riesgo real | Validar homing **antes** de emitir el comando, igual que XY |
| Sin hover en Z | Poca affordance | Añadir borde verde 2 px en hover, como E |
| Solo ratón | Accesibilidad | Flechas del teclado (Shift = ×10), `H` = home, foco visible, `aria-label` por sector — el propio prototipo de Bambu ya lo hace (§7) |
| Diálogos nativos | No tematizables | Reproducir el mismo contenido en `<dialog>` con los mismos textos y orden de botones |
| Sin límites visibles | El firmware no limita jog remoto | Mostrar el recorrido restante (X/Y/Z) y deshabilitar el sector si el paso lo excede |

### 5.3 Medidas CSS listas para usar

```css
:root{
  --card:#fff; --card-border:#eee; --card-radius:5px;
  --g1:#eee; --g2:#cecece; --pressed:#acacac;
  --brand:#00ae42; --brand-hover:#3dcb73;
  --text:#303a3c; --text-2:#6b6b6b; --text-off:#abacac; --num:#898989;
  --page:#eee; --title-bg:#f8f8f8;
}
.jog-card{width:586px;background:var(--card);border:1px solid var(--card-border);
          border-radius:var(--card-radius);display:flex}
.jog-xy{width:258px;height:258px}          /* disco real 210px centrado */
.jog-sector{fill:var(--g1)}                /* exterior */
.jog-sector.inner{fill:var(--g2)}          /* interior */
.jog-sector:hover{stroke:var(--brand);stroke-width:2}
.jog-sector:active{fill:var(--pressed);stroke:var(--brand);stroke-width:2}
.jog-gap{stroke:#fff;stroke-width:4}
.jog-home{fill:var(--g1)}                  /* r=23 */
.jog-z{display:flex;align-items:center;gap:2px}
.jog-z button{width:44px;height:40px;border:0;border-radius:0;
              background:var(--g1);color:var(--text);font:12px/1 inherit}
.jog-z button[data-step="1"]{background:var(--g2)}
.jog-z button:active{background:var(--pressed)}
.jog-e button{width:40px;height:40px;border:2px solid var(--g1);
              border-radius:8px;background:var(--g1)}
.jog-e button:hover{border-color:var(--brand)}
.jog-e button:active{background:var(--pressed)}
.jog-label{color:var(--text-2);font-size:12px}
[disabled]{color:var(--text-off)}
```

> Nota de escalado: Bambu usa DIP (1 DIP ≈ 1 px CSS a 96 dpi) y `FromDIP()` en todo. Los
> valores anteriores ya están en esa unidad, así que se pueden trasladar 1:1.

---

## 6. Resumen de comportamiento (tabla de referencia rápida)

| Acción | Comando | Paso / velocidad | Confirmación | Bloqueo |
|---|---|---|---|---|
| XY exterior | `xyz_ctrl` o `G1 X/Y ±10 F3000` | 10 mm | — | requiere home X e Y |
| XY interior | idem | 1 mm | — | requiere home X e Y |
| Home (centro) | `back_to_center` o `G28` | — | "Auto homing" | — |
| Z ±10 | `G1 Z ∓10 F900` | 10 mm | — | ⚠️ comprueba *después* |
| Z ±1 | `G1 Z ∓1 F900` | 1 mm | — | ⚠️ ídem |
| E ▲ | `G0 E-10 F900` | 10 mm | — | boquilla ≥ 170 °C |
| E ▼ | `G0 E+10 F900` | 10 mm | — | boquilla ≥ 170 °C |
| Todo | — | — | — | deshabilitado si RUNNING/PREPARE/SLICING o desconectado |

---

## 7. El prototipo React del propio repo (contexto, **no enviado**)

Existe un rediseño a medio hacer en `src/slic3r/GUI/DeviceWeb/device_page/src/features/printer/motion/`:

- `Motion.tsx` y `AxisControl copy.tsx` implementan un **popup "Motion:XYZ"** de **546×333 DIP**
  (34.125rem × 20.8125rem), cabecera de 46 DIP `#F8F8F8`, cierre 24×24, sombra
  `0 6px 16px rgba(0,0,0,.12)`, radio 8.
- Mantiene la misma semántica: disco de **220 unidades** con anillo exterior ±10 y anillo
  interior ±1, Home central con casa verde `#00AE42`, y tira `Bed` con filas de 52 DIP
  (`h-[3.25rem]`) `+10 / +1 / -1 / -10`.
- **Añade** lo que falta en producción: etiquetas `Toolhead` y `Bed`, **atajos de teclado**
  (`↑↓←→` = 1 mm, `Shift` = 10 mm, `H` = home) y layout de popup en lugar de tarjeta fija.
- `features/printer/control/Motion.tsx` es solo un stub (botón "Motion > XYZ").
- **No hay ruta** que lo monte (`routeTree.gen.ts` no lo incluye): es un prototipo de diseño.
  Sirve como confirmación de intención (popup + teclado) y como referencia de medidas.

---

## 8. Lo que NO está confirmado / discrepancias

- ⚠️ **NO CONFIRMADO:** que el popup "Motion:XYZ" del repo se haya enviado en alguna versión
  pública. En este commit no está cableado; el control real es el nativo descrito en §2.
- ⚠️ **NO CONFIRMADO:** valores de paso distintos de 1/10 mm en el dashboard oficial. La wiki
  de Bambu para pantalla táctil P1 dice "1 cm" para el extrusor (±10 mm), coherente con el
  código; **no** menciona 50 mm ni 100 mm en ningún sitio.
- ⚠️ **NO CONFIRMADO:** que exista home por eje en la UI de escritorio. El código solo expone
  `Ctrl_GoHome()` global.
- **Discrepancia con Bambuddy:** su documentación describe (a) pasos Z de 1/10/50 mm,
  (b) XY por distancia firmada ±200 mm, (c) E ±100 mm, (d) modal con
  `Home Z / Move anyway / Cancel` y bandera "ya avisado" por sesión. **Nada de eso está en
  Bambu Studio**: son decisiones de Bambuddy. Bambu Studio usa 1/10 fijos, un dial de 8
  direcciones, ±10 mm fijos en E, y un modal bloqueante `Go Home / Close` sin "move anyway".
  (Fuente: <https://wiki.bambuddy.cool/features/printer-control/>.)
- **Wiki oficial:** <https://wiki.bambulab.com/en/software/bambu-studio/remote-control>
  solo enumera "X/Y/Z/E move" dentro de los *Control widgets*, sin documentar el modal de
  homing ni los pasos. La guía de pantalla táctil P1
  (<https://wiki.bambulab.com/en/p1/manual/screen-operation>) sí describe "XY-axis control
  dial", "Z-axis lift" y "extruder … 1 cm", consistente con el código.

---

## 9. Fuentes citadas

Commit `8e524a3` (`8e524a39d4c4cd0541801c1c46d2f77debb0bf1d`), rama `master`.

| Tema | Archivo | Líneas | URL |
|---|---|---|---|
| Layout de página (2 columnas) | `src/slic3r/GUI/StatusPanel.cpp` | 1996-2056 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/StatusPanel.cpp#L1996-L2056) |
| Panel Control + orden | `StatusPanel.cpp` | 2559-2691 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/StatusPanel.cpp#L2559-L2691) |
| Tarjeta temps/ejes/extrusor | `StatusPanel.cpp` | 2693-2765 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/StatusPanel.cpp#L2693-L2765) |
| Dial XY (contenedor) | `StatusPanel.cpp` | 3076-3089 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/StatusPanel.cpp#L3076-L3089) |
| Tira Bed (Z) | `StatusPanel.cpp` | 3090-3155 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/StatusPanel.cpp#L3090-L3155) |
| Columna extrusor | `StatusPanel.cpp` | 3164-3230 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/StatusPanel.cpp#L3164-L3230) |
| Constantes de tamaño | `StatusPanel.cpp` | 586-610 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/StatusPanel.cpp#L586-L610) |
| Colores | `StatusPanel.cpp` | 100-120 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/StatusPanel.cpp#L100-L120) |
| Handlers XY / Home | `StatusPanel.cpp` | 5440-5487 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/StatusPanel.cpp#L5440-L5487) |
| Handlers Z y E + aviso frío | `StatusPanel.cpp` | 5488-5589 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/StatusPanel.cpp#L5488-L5589) |
| Aviso not-homed (invocación) | `StatusPanel.cpp` | 4283-4289 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/StatusPanel.cpp#L4283-L4289) |
| Estados habilitado/deshabilitado | `StatusPanel.cpp` | 4348-4448 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/StatusPanel.cpp#L4348-L4448) |
| Velo de force-disable | `StatusPanel.cpp` | 2070-2300, 2820-2855 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/StatusPanel.cpp#L2070-L2300) |
| Dial: tamaños y dibujo | `src/slic3r/GUI/Widgets/AxisCtrlButton.cpp` | 24-28, 149-285 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/Widgets/AxisCtrlButton.cpp#L24-L28) |
| Dial: hit-testing | `AxisCtrlButton.cpp` | 306-364 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/Widgets/AxisCtrlButton.cpp#L306-L364) |
| Enum de zonas | `src/slic3r/GUI/Widgets/AxisCtrlButton.hpp` | 34-45 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/Widgets/AxisCtrlButton.hpp#L34-L45) |
| Modal not-homed (texto y botones) | `src/slic3r/GUI/RecenterDialog.cpp` | 20-21, 33-47 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/RecenterDialog.cpp#L20-L47) |
| Modal de confirmación genérico | `src/slic3r/GUI/ReleaseNote.cpp` | 741-960 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/ReleaseNote.cpp#L741-L960) |
| Comandos de eje / home | `src/slic3r/GUI/DeviceCore/DevAxisCtrl.cpp` | 10-70 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/DeviceCore/DevAxisCtrl.cpp#L10-L70) |
| Gate de comandos | `src/slic3r/GUI/GUI_App.cpp` | 4967-4984 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/GUI_App.cpp#L4967-L4984) |
| Render icono+texto de botón | `src/slic3r/GUI/Widgets/Button.cpp` | 201-289 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/Widgets/Button.cpp#L201-L289) |
| Radio por defecto 8 | `src/slic3r/GUI/Widgets/StaticBox.cpp` | 22, 158-225 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/Widgets/StaticBox.cpp#L158-L225) |
| Pestañas Device | `src/slic3r/GUI/Monitor.cpp` | 200-218 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/Monitor.cpp#L200-L218) |
| Prototipo Motion:XYZ | `src/slic3r/GUI/DeviceWeb/device_page/src/features/printer/motion/Motion.tsx` | 25-88, 248-336 | [link](https://github.com/bambulab/BambuStudio/blob/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/src/slic3r/GUI/DeviceWeb/device_page/src/features/printer/motion/Motion.tsx) |
| Iconos | `resources/images/monitor_axis_home.svg`, `monitor_bed_up.svg`, `monitor_extruder_up.svg` | — | [carpeta](https://github.com/bambulab/BambuStudio/tree/8e524a39d4c4cd0541801c1c46d2f77debb0bf1d/resources/images) |
| Wiki: control remoto | — | — | <https://wiki.bambulab.com/en/software/bambu-studio/remote-control> |
| Wiki: pantalla P1 (dial, Z, E 1 cm) | — | — | <https://wiki.bambulab.com/en/p1/manual/screen-operation> |
| Wiki: UI A1 mini | — | — | <https://wiki.bambulab.com/en/a1-mini/manual/ui-explanation> |
| Bambuddy (adaptación, no Bambu) | — | — | <https://wiki.bambuddy.cool/features/printer-control/> |
