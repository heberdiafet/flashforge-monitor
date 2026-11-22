# Changelog — FlashForge Monitor

Formato: una línea por hito relevante. Versión actual: **0.9.0**.

- **0.1.0** — Base: cliente local HTTP 8898, dashboard demo y modelo de datos de la AD5X.
- **0.2.0** — Control de trabajos y archivos: pausar/reanudar/detener, listado, miniaturas y subida.
- **0.3.0** — Multi-impresora, descubrimiento UDP y configuración v2 con credenciales por impresora.
- **0.4.0** — Rediseño de interfaz estilo Bambu Studio: hero, tarjetas, popovers y temas del host.
- **0.5.0** — Control de ejes: dial XY de tres anillos, tira Z, extrusor, home y calibración.
- **0.6.0** — Layout optimizado: sensores bajo el hero, control 70/30 y IFS a ancho completo.
- **0.6.1** — Ajuste de padding/gaps de la estación de materiales y anillo de contraste.
- **0.7.0** — Homing real por `~M119` y carga/descarga de filamento por `ms_cmd`.
- **0.8.0** — Estados transitorios en el hero, notificaciones de calibración y cadencia adaptativa.
- **0.8.1** — Intención local (la AD5X solo reporta `busy`) y confirmación de fin por `/detail` + `~M119`.

## v0.8.2 (21-nov)
- Descubrimiento UDP sin bind(): el plugin ya NO abre ningún puerto local
  (solo conexiones de salida) — elimina el aviso "Ejecuta un servidor" del
  escáner de seguridad de Orca. Verificado en hardware (respuesta de la
  AD5X sin bind, 320 B).

- **0.9.0** — Tarjeta de finalización con **Confirme**/`stateCtrl_cmd` y **Volver a imprimir**; reimpresión tras `completed`/`cancel`; wheel estable `flashforge_monitor_any.whl` y rescate de config huérfana al arrancar.
