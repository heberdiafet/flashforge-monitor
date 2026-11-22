# Diseño — Ronda 2 (fase 5): multi-impresora, autodescubrimiento, notificaciones, gráficas

Estado: DISEÑO PREPARADO (se ejecuta cuando termine la ronda 1; tocará config
y dashboard, por eso va secuencial y no en paralelo con la ronda 1).

## 1. Refactor de configuración → multi-impresora

Schema nuevo del config de la capability (v0.3.0, con migración desde v0.2.0
usando `get_config_version()`):

```json
{
  "printers": [
    {"name": "AD5X taller", "ip": "192.168.1.50",
     "serial": "SNAD5X000001", "check_code": "a1b2c3d4"},
    {"name": "AD5M Pro", "ip": "192.168.8.31", "serial": "...", "check_code": "..."}
  ],
  "active_printer": 0,
  "poll_interval": 5,
  "notify_on_finish": true,
  "notify_on_error": true
}
```

- Migración: si el config viejo tiene ip/serial/check_code en la raíz,
  moverlos a printers[0] y save_config.
- UI de config: lista de impresoras (añadir/quitar), selector de activa.
- Dashboard: dropdown "impresora activa" en el topbar; al cambiar, el worker
  reinicia el ciclo con las credenciales de esa impresora. Un solo hilo
  worker que apunta a la activa (simple; el plugin no sondea las demás).

## 2. Autodescubrimiento UDP

- Botón "Buscar impresoras" en la UI de config. El worker hace broadcast UDP:
  - Destinos documentados: 225.0.0.9:19000 y/o 255.255.255.255:48899
    (paquete de 276 B; offsets documentados en api-reference § descubrimiento).
- Parsear respuesta → lista {ip, modelo, serial} → el usuario elige y solo
  teclea el check code (el serial ya viene del anuncio).
- Nota Fedora/Flatpak: el broadcast desde dentro del sandbox de Orca puede
  estar bloqueado; si falla, degradar con mensaje claro: "No se encontraron
  impresoras — introduce la IP manualmente" (no romper el flujo manual).
- Timeouts cortos (2 s) y un solo intento por clic.

## 3. Notificaciones

- `on_lifecycle_event` o detección por sondeo: comparar estado del trabajo
  del ciclo anterior vs actual (printing→ready = "Terminó",
  →error_code nuevo = aviso de error).
- Notificar con `orca.host.ui.push_notification(level, text)` — verificar
  en la API del host (host_ui wiki) el signature exacto; si el nivel es
  "info"/"warning", usar esos.
- Flags notify_on_finish / notify_on_error en config (por defecto ON).
- Cuidado: push_notification desde el hilo worker — el host hace marshal
  a la UI thread (según wiki: "These host calls run on the main thread for
  you"); si la API no lo garantiza desde threads, encolarlo por on_message.

## 4. Gráfica de temperaturas en vivo

- JS canvas nativo (sin librerías): buffer circular en JS de ~120 muestras
  (10 min a 5 s) de nozzle actual/target y cama actual/target.
- Cada post_message de 'status' alimenta el buffer; redibujar ~1 Hz.
- Autoescala, líneas de color derivadas de --orca-accent para targets y
  colores cálidos/fríos fijos para actual; leyenda compacta.
- Sección colapsable "Temperaturas (histórico)" para no ocupar el dashboard.

## 5. Miniatura del trabajo actual

- `getThum` (verificado: responde sin credenciales, devuelve error
  "thumbnail error" cuando no hay trabajo) → mostrar la imagen del trabajo
  en curso en la tarjeta de trabajo cuando exista; ocultar si error.
- Como no hay CORS: la imagen NO puede ir como URL directa en <img> si
  requiere auth; obtenerla por Python (bytes → base64) y embeber data URL
  en post_message. Limitar refresco a cada ~30 s para no saturar.

## Orden de implementación sugerido para el agente (ronda 2)
1. Refactor config + migración + UI multi-impresora + dropdown activa
2. Notificaciones (estado previo vs actual)
3. Gráfica canvas
4. getThum por base64
5. UDP discovery (último: es lo más dependiente del entorno de red)
