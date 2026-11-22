# Plan: Plugin "FlashForge Monitor" para OrcaSlicer

## Objetivo
Plugin Python (sin dependencias externas) para OrcaSlicer 2.5.x que integra el
monitoreo y control de la impresora **FlashForge AD5X** dentro de OrcaSlicer,
replicando lo que hace Flash Studio Desktop: estado en vivo, temperaturas,
progreso, cámara, **slots de la estación de materiales (IFS)** y controles.

## Arquitectura elegida
- **Capability tipo `pages`** → agrega una pestaña "FlashForge" en la ventana
  principal de OrcaSlicer (junto a Preparar/Preview/Device) que aloja un
  dashboard HTML autocontenido.
- **Cliente de la API local HTTP** de la impresora (puerto 8899, emparejamiento
  con serial + check code de 8 caracteres) implementado con `urllib.request`
  de la stdlib → cero dependencias pip, instalación simple.
- **Hilo de sondeo** (daemon thread + flag de paro) porque `on_message` corre en
  el hilo de UI: el trabajo HTTP se descarga al hilo y los resultados se
  envían a la página con `post_message()`.
- **Configuración persistente** con el sistema de config de capabilities
  (`get_config`/`save_config`) + UI HTML propia (`get_config_ui`): IP, serial,
  check code, intervalo de sondeo.
- **Cámara**: `<img>` apuntando al stream MJPEG `http://IP:8080/?action=stream`.
- **Permisos**: el audit hook de Orca pedirá aprobar el acceso de red a
  `http://IP:8899` la primera vez y lo recordará.

## Entorno de instalación (ya verificado)
- OrcaSlicer 2.5.1 (Flatpak `com.orcaslicer.OrcaSlicer`)
- Directorio de datos: `~/.var/app/com.orcaslicer.OrcaSlicer/config/OrcaSlicer/`
- Destino del plugin:
  `~/.var/app/com.orcaslicer.OrcaSlicer/config/OrcaSlicer/orca_plugins/flashforge_monitor/flashforge_monitor.py`
- Logs de Python: `~/.var/app/com.orcaslicer.OrcaSlicer/config/OrcaSlicer/log/python_*.log`

## Fases
1. **Investigación de la API** (agente A) → `docs/api-reference.md`
   Endpoints, autenticación y payloads de la API local AD5X, a partir de
   ff-5mp-api-ts (GhostTypes), FlashForgeUI-Electron (Parallel-7) y
   flashforge-python-api.
2. **Esqueleto del plugin** (agente B) → `plugin/flashforge_monitor/`
   Estructura PEP 723 + capability pages + dashboard + config UI + hilo de
   sondeo + cliente API con stubs `TODO-API`.
3. **Integración**: rellenar los stubs del cliente API con los endpoints reales
   de la fase 1; cablear dashboard ↔ cliente. Nota de terreno (docs/local-findings.md):
   la API HTTP local vive en el puerto **8898** (verificado); 8899 acepta TCP pero
   no HTTP plano; cámara 8080 cerrada hasta activar cámara en la impresora.
4. **Prueba local**: instalar en `orca_plugins/`, reiniciar Orca, aceptar
   permisos, verificar pestaña y logs `python_*.log`.
5. **Iteración (fase 5)** — el usuario eligió TODOS los módulos:
   - ✅ Fase 5a/entrega base: dashboard con estado, temps, trabajo, IFS,
     pausa/reanudar/cancelar — FUNCIONANDO en el Flatpak del usuario
     (activado vía diálogo Plugins el 2026-11-20).
   - 🔄 Ronda 1 (en curso, agente): panel de control completo
     (temps objetivo, velocidad, Z-offset, ventiladores, LED), gestor de
     archivos remoto (gcodeList/gcodeThumb/printGcode/uploadGcode) y
     movimiento/mantenimiento (homing, ejes, extrusión, calibración,
     auto-apagado, limpiar errores E0xxx). → v0.2.0
   - ⏳ Ronda 2 (diseño en docs/round2-design.md): multi-impresora con
     migración de config, autodescubrimiento UDP, notificaciones de fin/
     error, gráfica de temperaturas y miniatura del trabajo actual. → v0.3.0

## Restricciones y buenas prácticas (del wiki oficial)
- Exactamente una clase `@orca.plugin` (subclase de `orca.base`); capability
  con `get_name()` único y sin `;`.
- No importar GUI toolkits propios (ni PyQt/wx/tkinter) — la UI va por el
  puente HTML (`window.orca`).
- `post_message()` antes de que la página exista se descarta → la página pide
  los datos al cargar (`orca.postMessage({type:'ready'})`).
- Página autocontenido (HTML/CSS/JS inline), tematizada con variables
  `--orca-*` para modo claro/oscuro automático.
- Errores anticipados → `ExecutionResult.failure(PluginResult.RecoverableError, msg)`.
