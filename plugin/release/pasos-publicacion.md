# Publicación en cloud.orcaslicer.com — pasos para Heberdiafet

Artefacto estable para subir: `release/flashforge_monitor_any.whl`
Copia canónica: `release/flashforgemonitor-0.10.0-py3-none-any.whl`
SHA-256 (idéntico en ambos): `c57df8016c54e2a7fb1776331b5107209c2760882b1b946b8432a160168c47ab`

## Antes de subir

1. Verifica el wheel estable y su hash:
   ```bash
   cd plugin
   python3 validate_wheel.py release/flashforge_monitor_any.whl
   python3 validate_wheel.py release/flashforgemonitor-0.10.0-py3-none-any.whl
   sha256sum release/flashforge_monitor_any.whl
   ```
   Ambos deben dar **24/24 OK** y el hash de arriba.
2. No modifiques el `.whl` después de validarlo; si cambias código, reconstruye:
   ```bash
   python3 build_wheel.py --check --release
   ```
   Actualiza el hash en este documento si cambia.

## Por qué el nombre estable

OrcaSlicer deriva el `plugin_key` del nombre del archivo de entrada. Cambiar
`flashforgemonitor-0.8.2...whl` por otra versión crea una clave nueva y la
configuración anterior queda huérfana (parece una reinstalación). Por eso el hub
debe recibir **siempre** `flashforge_monitor_any.whl`; el wheel canónico con
versión queda solo para uso local/pip.

## Publicar en Orca Cloud

1. Entra en **https://cloud.orcaslicer.com** con tu cuenta.
2. Ve a **Shared Plugins** (plugins compartidos).
3. Pulsa **Create plugin** (o **Edit** si ya existe el listing de FlashForge Monitor).
4. Rellena el listing copiando desde `release/hub-listing.md`:
   - **Name**: `FlashForge Monitor`.
   - **Short description**: usa la versión EN (y la nota ES si el formulario lo permite).
   - **Long description**: bloque completo de “Long description”.
   - **Tags**: `flashforge`, `ad5x`, `printer-monitor`, `device-dashboard`, `ifs`, `multi-printer`.
   - **Supported OS**: Linux (probado); Windows/macOS (sin probar).
   - **Compatible OrcaSlicer**: `>= 2.5.0`.
5. Sube **`flashforge_monitor_any.whl`** como artefacto/versión del plugin.
6. Versión: escribe **`0.10.0`** exactamente, sin `v`, sin sufijos (`-beta`, `+build`, etc.). Formato estricto `X.Y.Z`.
7. Revisa la vista previa: nombre, descripción, tags, SO y compatibilidad.
8. Activa **Public Plugin** (o el interruptor equivalente de visibilidad pública).
9. Publica/guarda y comprueba que la página pública muestra la versión **0.10.0** y permite descargar el `.whl`.

## Después de publicar

- Descarga el `.whl` desde la página pública y compara el SHA-256 con el de arriba.
- Instálalo en un OrcaSlicer limpio (o en el Flatpak del usuario) con **File → Plugins → Install local plugin** para el dogfood final. Usa siempre el nombre estable.
- Si el hub rechaza la versión, revisa que sea exactamente `0.10.0` y que el `Name` del wheel (`FlashForgeMonitor`) coincida con el del listing.
- Si el hub rechaza el wheel, ejecuta `validate_wheel.py` y revisa: un solo `*.dist-info`, `METADATA`/`WHEEL`/`RECORD`, tag `py3-none-any` y `top_level.txt` con `flashforge_monitor`.
