# AGENTS.md — guía para una IA que configura este proyecto

Eres un agente (Codex, Claude Code u otro) ayudando a una persona a configurar
la alternativa Kick (OCR del stream -> canje PSN), el bot de chat de Twitch,
o ambos, según lo que haya pedido.
Lee `README.md` y `CONTRACT.md` antes de cambiar código.

## Qué automatiza esta guía

Este archivo reúne las instrucciones que debe seguir la IA para preparar el
proyecto: comprobar el sistema, instalar dependencias, diagnosticar problemas,
ejecutar pruebas y preparar los comandos de uso. No es un programa que se ejecute
por sí solo: la IA realiza esos pasos con las herramientas y permisos disponibles.
La persona inicia sesión manualmente y decide cuándo activar el canje o el envío
real de mensajes. No declares completada una comprobación que no hayas ejecutado.

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
- Para configurar o probar el bot de chat, usa `--dry-run`. Inicia el envío real
  únicamente cuando la persona lo solicite; puede reemplazar borradores del chat.
- Mantén fuera de Git `runs/`, logs, capturas, `tests/fixtures/`, entornos virtuales
  y cachés. El código del bot y sus pruebas sí forman parte del repositorio.

## Elegir qué configurar

Usa el objetivo indicado en la conversación. Si no se conoce, pregunta si la
persona quiere Kick, el bot de chat de Twitch o ambos antes de configurar servicios.
Si proporciona una URL, identifica su plataforma y extrae el canal de la ruta:
`https://kick.com/CANAL` o `https://www.twitch.tv/CANAL`. Usa ese nombre en
`--channel CANAL` (Kick) o `--chat CANAL` (Twitch), y la URL en `--source`.
No basta con cambiar `--source`: no actualiza el nombre del canal. Pasa siempre
el canal explícitamente en los comandos; no asumas los destinos predeterminados.
Mantén el mismo canal en la captura y el comando final. En Kick, úsalo también
en el lanzador y el diagnóstico. El lanzador de Twitch abre un canal predeterminado;
abre la URL elegida en esa misma ventana del perfil dedicado.

- **Kick:** entrada `python -m sniper.kick`; detecta códigos PSN del vídeo y puede
  canjearlos con un máximo de un éxito. Seguir los pasos de Kick que aparecen abajo.
- **Chat de Twitch:** entrada `python -m sniper.daemon`; envía mensajes periódicos
  con cuenta regresiva. No detecta ni canjea códigos y no lee susurros. Seguir la
  sección del bot de chat; no exigir una sesión PSN ni descargar modelos OCR para él.
- La compatibilidad de Windows cubre Kick, `redeem_cli` y `doctor`. El bot de chat
  conserva su alcance de Linux; no presentarlo como validado en Windows.

## Pasos de configuración de Kick

1. **Detecta el sistema** (Linux o Windows) y comprueba Python >= 3.12.
2. **Entorno virtual e instalación**
   - Ambos sistemas: instalar `uv`, ejecutar `uv python install 3.12` y `uv sync --python 3.12`.
   - Windows selecciona ONNX Runtime CPU; Linux conserva ONNX Runtime GPU.
   - Usar `uv run` para ejecutar los comandos; no hace falta activar `.venv` ni instalar pip.
3. **Modelos OCR**: `uv run python -m rapidocr.main download_models` (una vez).
4. **Diagnóstico**: `uv run python -m sniper.doctor --kick CANAL`. Arregla cada `✗`
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
   `uv run python -m sniper.kick --channel CANAL --dry-run` y revisa los candidatos en
   `runs/kick-CANAL/*/events.jsonl`. Propón un `--roi X0 Y0 X1 Y1` (0..1) que cubra
   solo el widget, sin chat ni alertas. Considera `--require-label`.
8. **Pruebas**: `uv run python -m pytest -q`. Las pruebas
   que necesitan capturas del stream se saltan si `tests/fixtures/` no existe.
9. **Entrega**: deja a la persona el comando final
   `uv run python -m sniper.kick --channel CANAL --redeem --roi X0 Y0 X1 Y1` para que **ella** lo ejecute.

## Pasos de configuración del bot de chat de Twitch

1. **Entorno:** en Linux, comprobar Python >= 3.12 e instalar las dependencias
   con `uv sync` según `README.md`.
2. **Canal e intervalo:** usar el canal pedido (`--chat`, por defecto `rubius`).
   El intervalo predeterminado es 300 segundos; `--chat-interval` admite al menos
   30 segundos. Confirmar las preferencias que falten antes de activar envíos.
3. **Reacciones:** la visión con Ollama es local y opcional. Para elegir un modelo
   de visión local instalado se usa `--vision-model NOMBRE`. Si no hay visión
   disponible, el bot usa emotes; esto no impide configurar el bot.
4. **Vista previa:** ejecutar `uv run python -m sniper.daemon --dry-run --chat CANAL`.
   Este modo captura vídeo para preparar mensajes simulados, sin conectar a Brave
   ni enviarlos. Detener la prueba una vez revisada la salida; no dejarla corriendo.
5. **Pruebas offline:** ejecutar
   `uv run python -m pytest tests/test_chat_daemon.py tests/test_console_status.py tests/test_participation.py -q`.
   Cubren el daemon, la cuenta regresiva, las reacciones y el envío simulado.
6. **Sesión de Twitch:** pedir a la persona que abra manualmente
   `scripts/launch_brave.sh` e inicie sesión en Twitch en el perfil dedicado.
   Si eligió otro canal, debe abrirlo en ese mismo perfil. No solicitar ni copiar
   credenciales, cookies o tokens. No hace falta iniciar sesión en PSN.
7. **Entrega:** preparar el comando
   `uv run python -m sniper.daemon --chat CANAL --chat-interval SEGUNDOS`.
   La ejecución real reemplaza borradores existentes en el cuadro del chat.
   Ante restricciones de Twitch, dejar el envío pausado para revisión manual.
   `Ctrl+C` detiene el bot; sus registros se quedan en `runs/`.

Los módulos del bot son `sniper/participation.py`, `sniper/reactions.py`,
`sniper/chat_sender.py` y `sniper/daemon.py`. También usa `sniper/session.py`,
`sniper/console.py` y la captura compartida. No activar el canje de Kick al
configurar únicamente el bot de chat.

## Límites del programa (para explicarlos)

- Un solo canje exitoso en todas las campañas; `--new-campaign` lo reabre a propósito.
- `--max-rejections` (3) corta tras códigos inválidos/usados/de otra región.
- Timeout, CAPTCHA, bloqueo o sesión perdida detienen todo hasta revisión manual.
- Código de salida 3: ya hay otro proceso de canje con el candado.
