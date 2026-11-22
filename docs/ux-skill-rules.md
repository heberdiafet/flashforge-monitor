# Reglas UX/UI (skill ui-ux-pro-max) — ronda de mejora de controles

Fuente: skill ui-ux-pro-max (búsquedas: touch targets, confirmation modals,
icon accessibility, dashboard density, keyboard operation + pro-rules.md
Pre-Delivery Checklist). Aplicar sobre el dashboard v0.4.0.

## Reglas para el pad de ejes / jog (prioridad CRITICAL/HIGH)

1. **Touch targets**: botones del pad ≥ 44×44 px (web: mínimo absoluto 24 px
   + WCAG, pero en desktop apunta a 44). Gap entre botones adyacentes ≥ 8 px
   (nunca gap-0/1 en elementos clicables).
2. **Todo operable por teclado**: tab order alineado al orden visual; el pad
   direccional idealmente operable con flechas del teclado cuando tiene foco;
   foco visible (focus ring) en CADA control, también dentro de modals.
   Sin keyboard traps.
3. **Semántica de chips/toggles**: usar <button> con aria-pressed/estado
   seleccionado (pasos 1/10/50 mm, presets de velocidad) — nunca divs
   clicables ni estados solo visuales.
4. **Iconos**: un solo family/style coherente; icon-only buttons llevan
   aria-label; iconos decorativos junto a texto → aria-hidden="true".
   Nada de emojis como iconos.
5. **Acciones destructivas/de movimiento**: modal de confirmación con foco
   visible dentro, orden de foco lógico, y feedback de éxito (toast) tras
   la acción. Confirm deshabilitado hasta aceptar riesgo (ya implementado).
6. **Sin hover-only**: el overlay "Imprimir" de las tarjetas de archivos
   debe tener affordance visible sin hover (botón visible siempre en
   pantalla táctil / sin puntero — usar @media (hover: none)).
7. **Estados**: disabled visualmente claros y no interactivos; pressed-state
   sin desplazar layout (sin jitter); feedback de pulso/ripple/opacity en
   cada tappable.
8. **Contraste**: ≥4.5:1 texto primario/secundario en claro Y oscuro;
   scrim del modal medido contra el fondo real; probar ambos temas, no
   inferir. Divisores/estados distinguibles en ambos modos.
9. **Animación**: timing compartido (tokens), 150–250 ms; respetar
   prefers-reduced-motion (desactivar shimmer/aspas giratorias/pulsos);
   transiciones que comunican, no decoran.
10. **Densidad dashboard**: secciones compactas pero con respiración;
    evitar texto recortado en cajas fijas (min(), line-height 1.5, altura
    auto).

## Pre-delivery (resumen del checklist canónico de la skill)

- [ ] Test a 375px de ancho y landscape (el panel del plugin puede ser
      angosto: la pestaña comparte ventana con la sidebar de Orca)
- [ ] Test con reduced-motion
- [ ] Contraste verificado en ambos temas de forma independiente
- [ ] Todos los tappables ≥44px y con pressed feedback
- [ ] Sin emojis como iconos; family consistente
- [ ] Tokens semánticos (--orca-*) en todo, sin colores ad-hoc por pantalla
- [ ] Screen reader order = orden visual; labels descriptivos
