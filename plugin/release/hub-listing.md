# FlashForge Monitor — listing para cloud.orcaslicer.com

## Name

FlashForge Monitor

## Short description (EN)

Local dashboard and device control for FlashForge AD5X / Adventurer 5M / 5M Pro printers inside OrcaSlicer — status, temperatures, IFS, files, axes, calibration, filament and multi-printer, with no cloud.

## Short description (ES)

Dashboard y control local para impresoras FlashForge AD5X / Adventurer 5M / 5M Pro dentro de OrcaSlicer — estado, temperaturas, IFS, archivos, ejes, calibración, filamento y multi-impresora, sin nube.

## Long description

FlashForge Monitor adds a dedicated **FlashForge** tab to OrcaSlicer: a self-contained dashboard that talks directly to your printer on the local network. No account, no cloud relay and no telemetry.

### What it does

- **Live status**: state, current job, progress, remaining time, layer, thumbnail, model and firmware.
- **Temperatures**: nozzle and bed current/target, quick presets, custom values and a live history chart.
- **Material station (IFS)**: four slots with colour, material, `T0/T1` mapping and an editor with the 24-colour palette.
- **Axes control**: XY dial (1 mm, 10 mm and Home), Z strip, extruder, homing, bed levelling and input shaper.
- **Transient states**: shows “Levelling…”, “Heating…”, “Loading filament…” or “Homing…” with a completion toast; on the AD5X this is driven by the command the plugin sent plus `/detail`/`~M119` confirmation, because the firmware only reports a generic `busy` state.
- **Files**: list, thumbnails, metadata, print, upload and print-after-upload.
- **Multi-printer**: several printers in one profile, active-printer switcher and UDP discovery.
- **Extras**: LED, auto-shutdown timer, camera stream (when available), error clearing and notifications.
- **Homing awareness**: real endstop status via `~M119`; movement is intercepted with an “Ir a Home” dialog when the printer has not homed.

### Requirements

- FlashForge **AD5X**, **Adventurer 5M** or **5M Pro** on the same LAN.
- **LAN-only mode enabled on the printer** (and restart it after changing it). Cloud-only mode can close port 8898.
- The printer **IP**, **serial number** and **Printer ID / check code**. The Printer ID is the code shown by the printer/FlashPrint; it is not your account password.
- **OrcaSlicer ≥ 2.5.0** with the plugin system (tested with 2.5.1 Flatpak on Linux).
- Local network access from OrcaSlicer to the printer on HTTP 8898, TCP 8899 and UDP discovery (port 48899, broadcast).

### Safety notice — real machine commands

This plugin sends **real commands** to your printer. Some actions move the machine and/or heat elements:

- homing, jogging X/Y/Z, extruding/retracting;
- bed levelling and input shaper calibration (the printer moves on its own);
- filament load/unload (`ms_cmd`), which moves filament through the extruder;
- temperature changes, LED, fans and auto-shutdown.

Use it at your own risk and keep the printer in sight while moving or calibrating. The plugin blocks motion while printing/paused and warns before cold extrusion, but it cannot replace the printer’s own safety systems.

### Privacy

Everything is local: the plugin connects directly to the printer over your LAN. It does **not** use Orca Cloud, does not send telemetry and does not store credentials outside OrcaSlicer’s plugin configuration. The serial number and Printer ID are used only to authenticate against the printer’s local API.

### Supported OS

- **Linux**: tested (OrcaSlicer 2.5.1 Flatpak, AD5X firmware 3.1.8-1.1.1).
- **Windows / macOS**: should work because the plugin is pure Python stdlib; not tested by the author.

### Compatible OrcaSlicer

**≥ 2.5.0** (plugin system). Tested with 2.5.1.

### Tags

`flashforge`, `ad5x`, `printer-monitor`, `device-dashboard`, `ifs`, `multi-printer`

### Installation (users)

1. Open OrcaSlicer.
2. **File → Plugins → Install local plugin**.
3. Select `flashforgemonitor-0.8.1-py3-none-any.whl`.
4. Activate **FlashForge Monitor** and open the new **FlashForge** tab.

### Configuration

1. Open the **FlashForge** tab → **Configurar** (gear icon).
2. Add a printer with:
   - a name,
   - its **IP** (for example `192.168.8.24`),
   - the **serial number**,
   - the **Printer ID / check code**.
3. Enable **LAN-only mode** on the printer and restart it if you changed it.
4. Save and refresh. Use **Buscar** (UDP discovery) if you do not know the IP.

### Notes

- The wheel is pure Python (`py3-none-any`) and has no third-party dependencies.
- Camera support depends on the printer model/firmware and whether port 8080 is reachable.
- The interface follows OrcaSlicer’s light/dark theme and prefers-reduced-motion settings.
