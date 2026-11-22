# FlashForge Monitor

**Local dashboard & control for FlashForge printers, inside OrcaSlicer.**

OrcaSlicer plugin that turns the app into a full FlashForge command center for
the **AD5X** and **Adventurer 5M / 5M Pro** series: live status, IFS material
station, remote files, a Bambu-style axis jog dial, calibration and filament
control — **all local, no cloud, no account, no telemetry.**

> Published on the [Orca Cloud Plugin Hub](https://cloud.orcaslicer.com) —
> open **File → Plugins → Browse plugins** in OrcaSlicer, search
> *FlashForge Monitor*, subscribe and activate.

## Features

- **Live dashboard** — state, nozzle/bed temperatures, job progress, ETA,
  layer count, model thumbnail
- **IFS material station** — 4 slots with material/color, Bambu-AMS-style
  cards, edit material & color from the popover palette
- **Axis control** — authentic Bambu Studio 3-ring jog dial (step encoded by
  radius: outer ±10 mm, inner ±1 mm, center Home), Z strip, extruder with
  cold-nozzle guard; keyboard: arrows / Shift / Enter
- **Real homing state** via the printer's TCP `~M119` protocol; jog is
  blocked with a Bambu-style dialog until homed
- **Remote file manager** — list the printer's stored jobs with thumbnails,
  start a stored print, upload G-code/3MF
- **Calibration & filament** — remote leveling / input shaper with
  intent-based progress cards and completion notifications; filament
  load/unload per slot (`ms_cmd`)
- **Multi-printer** — manage a fleet, per-printer credentials, active
  printer selector
- **Auto-discovery** — UDP broadcast finds FlashForge printers on your LAN
  (outbound only, **no local ports opened**)
- **Notifications** — print finished/error, calibration done, filament
  loaded; temperature history chart
- **Adaptive polling** — 2 s while printing/calibrating, idle interval
  configurable, exponential backoff on connection loss

## Requirements

- OrcaSlicer **2.5.0+** (Python plugin system)
- FlashForge **AD5X / Adventurer 5M / 5M Pro** with **LAN-only mode enabled**
  (Settings → Network on the printer), plus its serial number and the 8-char
  **Printer ID / check code** shown on that same screen
- Any OS — the plugin is pure Python (stdlib only, zero dependencies)

Tested on Linux (Fedora, OrcaSlicer 2.5.1 Flatpak) against a real AD5X.

## Install

### From the Plugin Hub (recommended)

1. Open **File → Plugins → Browse plugins** (sign in to Orca Cloud)
2. Search **FlashForge Monitor**, subscribe, then **Activate**
3. In the new *FlashForge* tab: press **Configurar** and enter IP, serial and
   check code. Accept the network permission prompt.

### Locally (no account needed)

1. Download `flashforge_monitor_any.whl` from the
   [releases](https://github.com/heberdiafet/flashforge-monitor/releases)
2. OrcaSlicer → **File → Plugins → Install local plugin** → pick the `.whl`
3. Check the plugin's **Activate** box, restart, configure. Done.

## How it talks to the printer

The plugin speaks the printers' **local HTTP API** (port 8898) and the raw
**TCP status protocol** (port 8899, `~M119` endstop/MachineStatus), both
reverse-engineered and documented in [`docs/api-reference.md`](docs/api-reference.md)
with hardware-verified request/response examples. All traffic is outbound
from your machine to the printer; nothing leaves your LAN.

## Repository layout

```
docs/                Investigation notes (API reference, Bambu Studio dial
                     design from its real source code, UX rules, field notes)
plugin/orca_plugins/flashforge_monitor/   Plugin source (single .py)
plugin/dist/         Package source used to build the wheel
plugin/test_mock.py  Offline test suite (fake HTTP + TCP + UDP printers)
plugin/smoke_test.py  Read-only smoke test against a real printer
plugin/build_wheel.py / validate_wheel.py  Reproducible wheel build & checks
plugin/release/      Release kit (wheel, changelog, hub listing texts, icon)
```

## Development

```bash
cd plugin
python3 test_mock.py                     # offline suite, no printer needed
python3 validate_wheel.py               # wheel format checks (24 rules)
python3 smoke_test.py --host <ip> \
    --serial <sn> --code <printer-id>    # read-only checks against hardware
python3 build_wheel.py --release         # reproducible .whl build
```

Install for development: copy `plugin/orca_plugins/flashforge_monitor/` into
your OrcaSlicer data dir (`~/.config/OrcaSlicer/orca_plugins/` on Linux,
`~/.var/app/com.orcaslicer.OrcaSlicer/config/OrcaSlicer/orca_plugins/` for the
Flatpak) and restart OrcaSlicer.

## Safety & privacy

- Movement, heating and filament commands act on the real printer — the UI
  uses confirmations and cold-nozzle warnings where the firmware needs them.
- The plugin **opens no listening sockets** (v0.8.2+): HTTP/TCP/UDP are all
  outbound to the printer. OrcaSlicer's plugin permission system gates every
  network access.
- No telemetry, no analytics, no accounts. Your printer credentials stay in
  your OrcaSlicer config.

## License

MIT — see [LICENSE](LICENSE). © 2026 Heberdiafet
