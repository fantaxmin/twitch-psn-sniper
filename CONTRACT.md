# Contrato entre módulos

## Cambio de alcance vigente

Nueva petición: preparar Kick para elded con un máximo de un canje PSN exitoso.
La entrada independiente es `python -m sniper.kick` (simulación predeterminada);
`--redeem` activa el canje real. No modifica el comportamiento de `sniper.daemon`.
Captura vídeo de Kick con Streamlink y usa OCR general con ROI configurable,
sin depender del diseño/color del widget de rubius. Exige dos lecturas consecutivas.
No lee ni envía mensajes del chat de Kick: esa integración queda pendiente de conocer
la mecánica del sorteo. Se presupone el mismo formato PSN de 12 caracteres.
Los éxitos persistidos del CLI en `runs/redeemer-attempts.json` y de cualquier
campaña en `runs/kick-*/redeemer-attempts.json` impiden continuar, incluso al cambiar
de canal o reiniciar. `--new-campaign` crea un registro nuevo con ID y permite
participar pese a éxitos previos; no borra registros ni ignora timeouts de otros registros.
`--max-rejections` limita los rechazos persistidos de la campaña (3 por defecto).
Un error sin dispatch se devuelve con `t_submit=0`, se quita del diario preventivo y
no entra en dedup definitivo. Kick permite confirmarlo otra vez; tres errores
consecutivos sin envío detienen la ejecución. Un timeout nunca habilita reenvío.
Errores después del envío, CAPTCHA, bloqueo o pérdida de sesión detienen la participación.
`--require-label` exige una etiqueta CODIGO/CÓDIGO en la misma caja OCR o en una
caja adyacente a la izquierda (solape vertical ≥50% de la altura del código,
distancia ≤3 alturas) o justo encima (solape horizontal, distancia ≤1.5 alturas).
Sin cajas OCR válidas falla cerrado. Las etiquetas adyacentes deben superar
el mismo umbral de confianza que el código.
La captura real de Kick y el aspecto de los códigos requieren validación en vivo.

Uso de la alternativa:
```bash
# Detectar y registrar candidatos, sin abrir PSN ni canjear:
uv run python -m sniper.kick
# Preparar Brave e iniciar sesión en PSN manualmente:
scripts/launch_brave.sh --kick elded
# Activar canje, una vez comprobado el formato y la zona del código:
uv run python -m sniper.kick --redeem
```
`--roi X0 Y0 X1 Y1` delimita la zona del código en coordenadas relativas 0–1.
Conviene ajustarla al diseño real para excluir chat y alertas superpuestos.
`--source video.mp4 --once` permite ensayar OCR sobre vídeo local en simulación.
El lector solo consume un candidato por escaneo; después de un intento exige
confirmaciones nuevas. También descarta candidatos que envejecen preparando PSN.
Si hay un timeout, verificar el resultado y usar la resolución manual existente:
`uv run python scripts/redeem_cli.py --runs-dir runs/kick-elded --resolve CODIGO --as ESTADO`.
Esta resolución toma los candados global y de campaña y requiere detener Kick primero.
Un resultado incierto nunca se reenvía automáticamente.

### Candados y portabilidad

`sniper.filelock` usa `fcntl.flock` en POSIX y `msvcrt.locking` sobre el primer byte
en Windows. Los candados son no bloqueantes. El orden siempre es:
`runs/.redeemer.lock`, después `runs/kick-CANAL/.redeemer.lock` cuando hay campaña.
Kick mantiene el global durante toda la ejecución; su Redeemer recibe
`global_lock_held=True` y adquiere solo el de campaña. El CLI y la resolución manual
adquieren ambos cuando `--runs-dir` apunta a una carpeta `kick-*`. El CLI en `runs/`
adquiere solo el global. No se deben borrar los archivos de candado para desbloquearlos.
Kick y el CLI devuelven código 3 con mensaje claro si el candado está ocupado.

Windows se limita a Kick, redeem_cli y doctor. Los lanzadores son
`scripts/launch_brave.ps1` y `.bat`, con perfil en LOCALAPPDATA. La captura localiza
Streamlink en PATH; terminate/kill detienen su subproceso. Las notificaciones son
opcionales: notify-send en Linux y log/sonido con winsound en Windows.
`python -m sniper.doctor --kick elded` comprueba el entorno sin iniciar navegador,
crear registros, descargar modelos ni hacer canjes. Ver README.md para instalación
con `uv sync` y detalles de sus comprobaciones de solo lectura. `pyproject.toml`
selecciona ONNX Runtime CPU en Windows y GPU en Linux mediante marcadores de
plataforma; `uv.lock` fija ambas variantes. Ejecutar los comandos con `uv run`.

Por petición del usuario, `python -m sniper.daemon` ahora solo envía mensajes
periódicos a Twitch. Conserva el contador, los registros y la captura como contexto
visual para las reacciones. Solo necesita la sesión de Twitch en Brave.

Las rifas son opcionales: `--raffle-users` habilita un lector IRC independiente
solo de lectura y un compositor que exige 2 o 3 usuarios distintos de la lista,
con rol moderador/broadcaster, repitiendo una frase literal corta dentro de 60 s.
No lee códigos PSN ni susurros. Se activa en `sniper.setup` y obliga al intervalo
de 180 s: dos turnos de frase y luego uno normal; el lector no llama al remitente.
`runs/raffle-CANAL.json` guarda reservas previas al envío, máximo 2 por frase,
con separación mínima de 180 s y turno normal pendiente entre reinicios.
La simulación usa un registro en memoria. El lector valida canal, login IRC,
rol, ID y fecha del mensaje; al reconectar borra confirmaciones en memoria.
La configuración local pasa a v2 con lista vacía al leer un archivo v1.

El daemon activo no importa ni ejecuta el redeemer, el detector OCR, el lector de
códigos del chat ni el lector de susurros. No abre PlayStation Store y no termina
por códigos, canjes o susurros; se detiene con Ctrl+C o al terminar la captura.
`scripts/launch_brave.sh` abre `https://www.twitch.tv/rubius`.

`uv run python -m sniper.setup` configura cuentas públicas de referencia, canal,
intervalo/ROI y arranque. Las preferencias privadas se guardan atómicamente en
`.sniper-local.json` (ignorado por Git; permisos 0600 en POSIX), sin contraseñas,
correo, tokens, cookies ni accountId interno. No selecciona cuentas: reutiliza las
sesiones guardadas del perfil dedicado de Brave. `--run` usa simulación;
`--run --real` arranca en real y abre Brave si CDP falta. Si falta autenticación,
el daemon/Kick termina antes de iniciar captura o enviar. La captura pública de
Kick no requiere sesión de Kick. El modo chat generado por el asistente usa
`--wait-first` para mostrar la cuenta regresiva antes del primer mensaje;
los comandos directos conservan el primer mensaje inmediato salvo que usen ese flag.

Mientras corre el daemon, su mensaje tiene prioridad en el cuadro del chat:
reemplaza cualquier borrador existente antes del envío, según pidió el usuario.

Las secciones siguientes describen el reparto y las interfaces históricos de PSN,
cuyas utilidades independientes se conservan fuera del flujo activo. No reactivar
esa integración sin una nueva petición del usuario.

Proyecto: daemon que lee el código del widget "CODIGO:" del stream de twitch.tv/rubius
y lo canjea en PS Store (cuenta Colombia). Python 3.12, gestionado con `uv`.

## Reparto
- **Claude** → `sniper/capture.py`, `sniper/detector.py`, `sniper/ocr.py`, `sniper/daemon.py`,
  `sniper/codes.py` (normalización/validación), systemd unit.
- **Codex** → `sniper/redeemer.py`, `sniper/browser.py`, `scripts/launch_brave.sh`,
  `scripts/redeem_cli.py`.
- No editar archivos del otro sin avisar por herdr.

## Navegador
- Brave (`/usr/bin/brave-browser`), perfil dedicado `~/.local/share/twitch-psn-sniper/brave-profile`,
  `--remote-debugging-port=9333` en 127.0.0.1. El usuario inicia sesión en PSN a mano una vez.
- Playwright `chromium.connect_over_cdp("http://127.0.0.1:9333")`.

## Código
- `codes.normalize(raw) -> str | None`: mayúsculas, sin espacios, devuelve `XXXX-XXXX-XXXX`
  o None. Para escribir en el input se usa la versión sin guiones (12 chars) salvo que el
  input acepte guiones (Codex lo verifica).

## Interfaz del redeemer (Codex)
```python
@dataclass
class RedeemResult:
    code: str
    tab: str            # "A" | "B"
    status: str         # "success" | "invalid" | "already_used" | "region" | "blocked" | "captcha" | "session_lost" | "timeout" | "error"
    message: str        # texto que mostró PSN
    t_submit: float     # time.perf_counter() al hacer el primer fill
    t_result: float
    screenshot: str | None

class Redeemer:
    async def start(self) -> None          # conecta CDP, prepara pestañas A y B con modal abierto e input listo
    async def ensure_ready(self) -> bool   # health check; reabre modal / detecta sesión perdida
    async def submit(self, code: str, *, source: str, confidence: float) -> RedeemResult
    async def close(self) -> None
```
Reglas dentro de `submit` (el coordinador vive en el redeemer):
- Dedup global: nunca enviar el mismo código dos veces.
- Primer código → pestaña A inmediatamente (fill + Continuar + confirmaciones hasta éxito explícito).
- Un código distinto llegado después → se prepara en B; se envía solo si A terminó en
  `invalid` (o timeout ya resuelto). Si A tuvo `success` → descartar.
- `blocked`/`captcha`/`session_lost` → no más envíos, notificar (`notify-send`), devolver estado.
- Solo un canje en curso a la vez.

## Daemon (Claude) llama
`await redeemer.submit(code, source="ocr"|"chat", confidence=float)` y loguea a `runs/`.
