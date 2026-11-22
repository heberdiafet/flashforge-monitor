# Hallazgos de terreno — AD5X local (2026-11-20)

## Datos de conexión (extraídos de la config de OrcaSlicer del usuario)
- IP: `192.168.1.50` (responde ping, ~4 ms)
- Nota: los valores de este documento son genéricos de ejemplo (los reales no se publican).
- Serial: `SNAD5X000001`
- Access code configurado en Orca: `88888888`
  (Orca lo acepta para envío/IFS — verificar si es el check code real
  mostrado en la pantalla de la impresora o un valor aceptado de todos modos)
- Modelo: Flashforge AD5X, LAN-only mode activado

## Puertos (sondeo directo)
| Puerto | Estado | Nota |
|---|---|---|
| 8898 | ABIERTO | **Habla HTTP** — GET / y rutas comunes devuelven 404; los endpoints reales los define ff-5mp-api-ts (agente A). La API HTTP local está AQUÍ. |
| 8899 | ABIERTO | Conexión TCP aceptada pero GET HTTP plano se cae (HTTP 000) — probablemente otro protocolo (¿websocket / TCP legacy / requiere handshake?) |
| 8080 | CERRADO | Cámara MJPEG: no responde — coincide con issue #6106: hay que activar la cámara en los ajustes de la impresora primero |
| 80, 3000 | CERRADOS | — |

## Implicaciones para el plugin
1. Corrección al PLAN.md: el cliente API debe apuntar a `http://IP:8898`, no 8899.
2. La cámara requiere que el usuario active la cámara en la pantalla de la
   impresora (Ajustes → cámara ON). El dashboard debe detectar stream caído
   y mostrar aviso, no romperse.
3. La autenticación exacta (headers) la documentará docs/api-reference.md
   del agente A a partir del código fuente; no adivinar rutas.
