# FlashForge Monitor

**Local dashboard & control for FlashForge printers, inside OrcaSlicer — no cloud, no account, no telemetry.**

Turn OrcaSlicer into a full command center for the FlashForge **AD5X** and **Adventurer 5M / 5M Pro**: everything Flash Studio does on desktop, running locally on any OS, with a Bambu Studio-inspired interface.

> 🔗 **Source code & releases:** https://github.com/heberdiafet/flashforge-monitor

## Features

- **English & Spanish UI** with auto-detection from OrcaSlicer (requested by @Meechi91).

- **Live dashboard** — printer state, nozzle & bed temperatures, job progress, ETA, layer count and model thumbnail, updated every 2 seconds while printing
- **IFS material station** — all 4 slots with material and color in Bambu-AMS-style cards; edit any slot from the plugin with the official 24-color palette
- **Axis control** — authentic Bambu Studio 3-ring jog dial (step encoded by radius: ±10 mm outer ring, ±1 mm inner ring, Home at the center), Z strip and extruder with cold-nozzle protection. Fully keyboard operable
- **Real homing state** — the printer's `~M119` protocol reports true endstop status; movement is gated with a Bambu-style dialog until homed
- **Post-print completion flow** — when a print finishes, a card mirrors the printer's dialog: **Confirme** unlocks the printer remotely and **Volver a imprimir** clears the platform and restarts the same file. No more walking to the touchscreen
- **Calibration & filament** — remote leveling and input shaper with live progress cards and finish notifications; per-slot filament load/unload
- **Remote file manager** — browse the printer's stored jobs with thumbnails, start any of them, or upload new G-code/3MF files
- **Multi-printer fleet** — store several printers with their own credentials and switch from the header dropdown; UDP auto-discovery finds FlashForge printers on your LAN (outbound only)
- **Notifications & charts** — print finished/error alerts, live temperature history chart, adaptive polling with offline backoff

## Requirements

- OrcaSlicer **2.5.0 or newer** (Python plugin system)
- FlashForge AD5X / Adventurer 5M / 5M Pro with **LAN-only mode** enabled (Settings → Network on the printer screen)
- Your printer's IP, serial number and 8-character **Printer ID / check code** (shown on the same screen)

Pure Python, zero dependencies — works on Windows, macOS and Linux. Tested against a real AD5X on Fedora Linux (OrcaSlicer 2.5.1).

## Install

**From this hub** (recommended): subscribe, then open OrcaSlicer → **File → Plugins**, check the plugin's **Activate** box and open the new *FlashForge* tab. Press **Configurar**, enter IP / serial / check code and accept the network permission prompt.

**Locally**: grab `flashforge_monitor_any.whl` from the [GitHub releases](https://github.com/heberdiafet/flashforge-monitor/releases), then **File → Plugins → Install local plugin**, Activate, restart.

## Privacy & safety

- Everything stays on your LAN: the plugin talks **only to your printer** (local HTTP API, port 8898, and the status TCP protocol, port 8899). No cloud, no accounts, no telemetry, no analytics
- The plugin **opens no listening ports** — all network access is outbound, and every connection is gated by OrcaSlicer's plugin permission system
- Movement, heating and filament commands act on the real machine, so the UI uses confirmation dialogs and cold-nozzle warnings wherever the firmware needs them

## Source & license

Open source under the **MIT license** — code, tests, build scripts and the full hardware-verified API documentation live at:

🔗 https://github.com/heberdiafet/flashforge-monitor

© 2026 Heberdiafet
