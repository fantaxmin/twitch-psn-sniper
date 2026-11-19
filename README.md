# Alternativa Kick para elded

Lee códigos PSN del vídeo con OCR y se retira al confirmar un canje exitoso.
El modo predeterminado solo detecta y registra candidatos; `--redeem` activa
el canje real. No lee ni escribe en el chat de Kick.

## Tus datos: qué hay que configurar

El proyecto **no guarda ni pide** tu correo, contraseña, `accountId` de PSN ni
tokens en ningún archivo. No hay `.env` ni archivo de credenciales que llenar.
La cuenta se toma de la sesión que abras a mano en un perfil dedicado de Brave:

| Dato | Dónde va | Cómo |
|---|---|---|
| Correo y contraseña de PSN | Perfil dedicado de Brave | Lanza Brave con `scripts/launch_brave.sh` (Linux) o `scripts\launch_brave.ps1` (Windows) e inicia sesión en store.playstation.com. El perfil queda en `~/.local/share/twitch-psn-sniper/brave-profile` o `%LOCALAPPDATA%\twitch-psn-sniper\brave-profile`. |
| Región de la tienda | URL de canje | Por defecto `es-co` (Colombia). Si tu cuenta es de otra región, cambia `STORE_URL` en `sniper/browser.py` (p. ej. `es-mx`, `en-us`). |
| `accountId` / ID en línea | Ninguno | PSN lo obtiene de la sesión; no hace falta escribirlo. |
| Canal a vigilar | Línea de comandos | `--channel elded` en `sniper.kick`, o `--kick CANAL` en `doctor` y los lanzadores. |
| Zona del código en pantalla | Línea de comandos | `--roi X0 Y0 X1 Y1` (ver "Preparar el canje"). |

Todo lo que el programa genera (registro de intentos, capturas del modal de PSN,
logs) va a `runs/`. Esa carpeta **contiene tu correo y `accountId`** porque copia
el texto del modal de PSN: está en `.gitignore` y no debe subirse ni compartirse.

Para que una IA (Codex, Claude Code, etc.) te configure el proyecto, ver
[`AGENTS.md`](AGENTS.md).

## Instalación en Windows

Instalar Python 3.12 o posterior y Brave. Desde la carpeta del proyecto, en PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`requirements.txt` instala Streamlink y ONNX Runtime para CPU; no requiere GPU.
Para tener `streamlink.exe` en PATH, activar el entorno:

```powershell
.\.venv\Scripts\Activate.ps1
python -m sniper.doctor --kick elded
python -m sniper.kick
```

Si la política de PowerShell impide activar el entorno, se puede trabajar desde CMD:

```bat
.venv\Scripts\activate.bat
python -m sniper.doctor --kick elded
python -m sniper.kick
```

Los modelos OCR deben estar disponibles localmente para que `doctor` los pruebe.
En una instalación nueva, prepararlos una vez fuera del diagnóstico:

```powershell
python -m rapidocr.main download_models
```

La preparación puede descargar pesos. `doctor` no los descarga ni escribe archivos de campaña.
No se necesita descargar un navegador de Playwright: el canje se conecta al Brave dedicado.

## Instalación en Linux

Con `uv` y Brave instalados:

```bash
uv sync
uv run python -m sniper.doctor --kick elded
uv run python -m sniper.kick
```

También se puede usar un entorno virtual con `pip install -r requirements.txt`
para la variante CPU. La configuración histórica de `uv` utiliza ONNX Runtime GPU.

## Comprobar el entorno

```bash
python -m sniper.doctor --kick elded
python -m sniper.doctor --kick elded --json
```

El diagnóstico muestra `✓`, `!` o `✗`, una sugerencia para cada problema y un resumen.
Devuelve 1 si hay errores. Sin `--kick`, comprueba la URL de Twitch de rubius.
Verifica importaciones y versiones, Streamlink, reconocimiento de un código sintético,
Brave, el endpoint HTTP local de CDP, permisos, registros y disponibilidad de candados.

Es de solo lectura: no inicia Brave, no usa Playwright, no abre pestañas ni canjea.
Los candados existentes se prueban y se liberan; los ausentes no se crean.
La comprobación de Streamlink usa `--can-handle-url` con navegador deshabilitado:
confirma compatibilidad de URL, **no confirma que el canal esté en vivo**.
Los permisos de escritura se revisan sin crear un archivo de prueba.

## Preparar el canje

Primero comprobar el formato del sorteo y probar la detección sin canjear:

```bash
python -m sniper.kick --dry-run
python -m sniper.kick --source video.mp4 --once
```

En real, usar `--roi X0 Y0 X1 Y1` ajustado al widget, excluyendo chat y alertas.
Las coordenadas son relativas a la imagen, de 0 a 1. La detección de toda la pantalla
sirve para explorar el diseño. `--require-label` exige CODIGO/CÓDIGO en la misma
caja OCR que el cupón o en una caja adyacente a la izquierda o justo encima.
Sin cajas OCR válidas no acepta candidatos con esta opción.

Abrir manualmente el perfil dedicado y autenticar PSN:

```bash
# Linux
scripts/launch_brave.sh --kick elded
```

```powershell
# Windows PowerShell
.\scripts\launch_brave.ps1 -KickStream -Channel elded
# Windows, mediante el lanzador BAT
.\scripts\launch_brave.bat --kick elded
```

En Windows el perfil se guarda en
`%LOCALAPPDATA%\twitch-psn-sniper\brave-profile`; los lanzadores buscan Brave en
PATH, Program Files y LOCALAPPDATA. CDP escucha en `127.0.0.1:9333`.

Una vez elegida la zona, activar el canje con las coordenadas comprobadas:

```bash
python -m sniper.kick --redeem --roi X0 Y0 X1 Y1
```

Sustituir `X0 Y0 X1 Y1` por números. El programa exige al menos dos escaneos
consecutivos, descarta imágenes viejas y procesa un candidato por escaneo.
Después de cada intento exige nuevas confirmaciones.

## Límites y recuperación

- Un éxito en `runs/redeemer-attempts.json` o en cualquier carpeta `runs/kick-*`
  termina la participación normal, incluso al cambiar de canal o reiniciar.
- `--new-campaign` permite participar de nuevo pese a éxitos previos y crea una
  carpeta `runs/kick-CANAL-ID`. Conserva los registros anteriores. Usar el mismo
  flag repetidamente crea campañas diferentes; un reinicio normal mantiene el
  registro habitual `runs/kick-CANAL` y sigue comprobando los éxitos globales.
- `--max-rejections 3` limita los resultados `invalid`, `already_used` y `region`.
  Se cuentan desde el registro de esa campaña y se conservan entre reinicios.
- Un error anterior al envío permite volver a confirmar el candidato. Tres errores
  consecutivos sin envío detienen la ejecución. No se guardan como intentos definitivos.
- Timeout, CAPTCHA, bloqueo, sesión perdida o error posterior al envío detienen
  la ejecución. `--new-campaign` tampoco permite ignorar un timeout de otro registro.
- El código de salida 3 indica que hay otro proceso con el candado de canje.

Para resolver un timeout, detener el proceso y comprobar el resultado en la cuenta.
Después actualizar el registro correspondiente, sin reenviar el código:

```bash
python scripts/redeem_cli.py --runs-dir runs/kick-elded --resolve XXXX-XXXX-XXXX --as already_used
```

Usar el estado realmente comprobado y la ruta que aparezca en el registro de ejecución.
La resolución adquiere primero el candado global y después el de la campaña.
El CLI conserva `--inspect`, `--bench` y `--code`; este último realiza un canje real
solo al solicitarlo explícitamente.

La compatibilidad de Windows cubre Kick, `redeem_cli` y `doctor`.
Las pruebas de Windows simulan sus APIs desde Linux; falta probar los lanzadores,
la captura y la conexión a Brave en una máquina Windows real.

## Bot de chat de Twitch

El repositorio también incluye el bot de chat: `participation.py`, `reactions.py`,
`chat_sender.py` y `daemon.py`, junto con sus pruebas. Envía mensajes periódicos
al canal elegido y muestra una cuenta regresiva; no detecta ni canjea códigos.
Usa la sesión de Twitch del perfil dedicado de Brave. Las reacciones pueden usar
visión local con Ollama; si no está disponible, recurre a emotes.

```bash
# Vista previa sin conectar a Brave ni enviar mensajes:
uv run python -m sniper.daemon --dry-run
# Envío real a Twitch, después de iniciar sesión manualmente:
uv run python -m sniper.daemon --chat rubius --chat-interval 300
```

Esta parte del proyecto conserva su alcance de Linux; no está incluida en la
validación de compatibilidad con Windows. Los logs se guardan en `runs/` y no se publican.

## Pruebas

```bash
python -m pip install pytest
python -m pytest -q
```

Las pruebas usan navegadores y canjes simulados. El OCR histórico prueba una imagen local.
