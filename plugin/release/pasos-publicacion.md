# Publicación en cloud.orcaslicer.com — pasos para Heberdiafet

Artefacto listo: `release/flashforgemonitor-0.8.1-py3-none-any.whl`
SHA-256: `3c0038d50451bb96b1fde4345db1309610ac649cc93f4042c645b61c75af5370`

## Antes de subir

1. Verifica el wheel y su hash:
   ```bash
   cd plugin
   python3 validate_wheel.py release/flashforgemonitor-0.8.1-py3-none-any.whl
   sha256sum release/flashforgemonitor-0.8.1-py3-none-any.whl
   ```
   Debe salir **24/24 OK** y el hash de arriba.
2. No modifiques el `.whl` después de validarlo; si cambias código, reconstruye:
   ```bash
   python3 build_wheel.py --check --release
   ```
   y actualiza el hash en este documento y en el listing.

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
5. Sube el archivo **`flashforgemonitor-0.8.1-py3-none-any.whl`** como artefacto/versión del plugin.
6. Versión: escribe **`0.8.1`** exactamente, sin `v`, sin sufijos (`-beta`, `+build`, etc.). El formato estricto es `X.Y.Z`.
7. Revisa la vista previa: nombre, descripción, tags, SO y compatibilidad.
8. Activa **Public Plugin** (o el interruptor equivalente de visibilidad pública).
9. Publica/guarda y comprueba que la página pública muestra la versión **0.8.1** y permite descargar el `.whl`.

## Después de publicar

- Descarga el `.whl` desde la página pública y compara el SHA-256 con el de arriba.
- Instálalo en un OrcaSlicer limpio (o en el Flatpak del usuario) con **File → Plugins → Install local plugin** para el dogfood final.
- Si el hub rechaza la versión, revisa que sea exactamente `0.8.1` y que el `Name` del wheel (`FlashForgeMonitor`) coincida con el del listing.
- Si el hub rechaza el wheel, ejecuta `validate_wheel.py` y revisa: un solo `*.dist-info`, `METADATA`/`WHEEL`/`RECORD`, tag `py3-none-any` y `top_level.txt` con `flashforge_monitor`.

> NOTA (21-nov): el hub exige sufijo de SO en el nombre del archivo.
> El wheel universal Python puro se sube como:
>   flashforge_monitor_any.whl  (sufijo "any" = universal, ya en release/)
> Contenido identico al flashforgemonitor-0.8.1-py3-none-any.whl (mismo sha256:
> 3c0038d50451bb96b1fde4345db1309610ac649cc93f4042c645b61c75af5370).

> ACTUALIZACIÓN (21-nov): v0.8.2 elimina el bind() del descubrimiento UDP
> (perfil solo-salida, sin aviso del escáner). En Edit plugin: reemplaza el
> archivo por flashforge_monitor_any.whl (0.8.2) y actualiza la versión.
