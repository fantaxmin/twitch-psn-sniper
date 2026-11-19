# AGENTS.md — guía para una IA que configura este proyecto

Eres un agente (Codex, Claude Code u otro) ayudando a una persona a dejar
funcionando la alternativa Kick: OCR del stream -> canje del código en PSN.
Lee `README.md` y `CONTRACT.md` antes de cambiar código.

## Reglas que no se negocian

- **Nunca hagas un canje real** (`python -m sniper.kick --redeem`,
  `scripts/redeem_cli.py --code`, `--bench`) sin que la persona lo pida en ese momento.
  Un canje consume el código: no hay forma de deshacerlo.
- **No pidas, escribas ni guardes** correo, contraseña, `accountId`, cookies ni tokens.
  La persona inicia sesión a mano en el Brave dedicado; tú no tocas ese paso.
- **No subas ni compartas `runs/`**: contiene el texto del modal de PSN con el correo
  y `accountId` de la persona.
- No uses `locator.click(trial=True, force=True)` de Playwright como prueba "en seco":
  `force` hace que el clic sea real.
- No borres ni edites a mano `runs/**/redeemer-attempts.json`; para resolver un
  timeout usa `scripts/redeem_cli.py --resolve CODIGO --as ESTADO` tras verificar
  el resultado con la persona.

## Pasos de configuración

1. **Detecta el sistema** (Linux o Windows) y comprueba Python >= 3.12.
2. **Entorno virtual e instalación**
   - Linux: `uv sync` (GPU) o `python -m venv .venv && .venv/bin/pip install -r requirements.txt` (CPU).
   - Windows: ver "Instalación en Windows" en `README.md` (`py -3.12 -m venv .venv`, `pip install -r requirements.txt`).
3. **Modelos OCR**: `python -m rapidocr.main download_models` (una vez).
4. **Diagnóstico**: `python -m sniper.doctor --kick CANAL`. Arregla cada `✗`
   siguiendo su sugerencia y repite hasta que no queden errores. `--json` da salida
   legible por máquina. Es de solo lectura.
5. **Brave dedicado**: pide a la persona que ejecute
   `scripts/launch_brave.sh --kick CANAL` (Linux) o
   `.\scripts\launch_brave.ps1 -KickStream -Channel CANAL` (Windows) y que inicie
   sesión en store.playstation.com. Espera su confirmación; vuelve a correr `doctor`
   para ver el CDP en `127.0.0.1:9333`.
6. **Región**: pregunta el país de la cuenta PSN. Si no es Colombia, ajusta
   `STORE_URL` en `sniper/browser.py` (`es-co` -> `es-mx`, `en-us`, ...).
7. **Zona del código**: con el stream mostrando el sorteo, corre
   `python -m sniper.kick --dry-run` y revisa los candidatos en
   `runs/kick-CANAL/*/events.jsonl`. Propón un `--roi X0 Y0 X1 Y1` (0..1) que cubra
   solo el widget, sin chat ni alertas. Considera `--require-label`.
8. **Pruebas**: `python -m pip install pytest && python -m pytest -q`. Las pruebas
   que necesitan capturas del stream se saltan si `tests/fixtures/` no existe.
9. **Entrega**: deja a la persona el comando final
   `python -m sniper.kick --redeem --roi X0 Y0 X1 Y1` para que **ella** lo ejecute.

## Límites del programa (para explicarlos)

- Un solo canje exitoso en todas las campañas; `--new-campaign` lo reabre a propósito.
- `--max-rejections` (3) corta tras códigos inválidos/usados/de otra región.
- Timeout, CAPTCHA, bloqueo o sesión perdida detienen todo hasta revisión manual.
- Código de salida 3: ya hay otro proceso de canje con el candado.
