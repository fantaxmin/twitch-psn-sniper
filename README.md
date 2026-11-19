# Bot de streams: códigos en Kick y chat de Twitch

Dos funciones independientes: detectar y canjear códigos PSN del vídeo de Kick,
y enviar mensajes periódicos al chat de Twitch. Elige el canal en cada comando;
no hace falta editar el código para cambiar de streamer. El modo de Kick se retira
tras un canje exitoso. El bot de chat no busca ni canjea códigos.

## Comandos a mano

[Asistente automático](#asistente-de-configuración-y-arranque) ·
[Kick en Linux](#kick--linux) · [Kick en Windows](#kick--windows-powershell) ·
[Chat de Twitch](#bot-de-chat-de-twitch--linux) ·
[Instalación Linux](#instalación-en-linux) · [Instalación Windows](#instalación-en-windows) ·
[Configuración con IA](#configuración-asistida-con-agentsmd) ·
[Límites y recuperación](#límites-y-recuperación)

Si ya instalaste el proyecto, empieza aquí. Ejecuta los comandos desde la carpeta
del repositorio. Sustituye `tu_canal` por el nombre que aparece en la URL del stream;
por ejemplo, para `https://kick.com/elmune`, el canal es `elmune`.
Pasa siempre `--channel` en Kick o `--chat` en Twitch para elegir el destino.

### Asistente de configuración y arranque

```bash
uv run python -m sniper.setup
```

El asistente pide tus usuarios de Twitch y Kick, el ID público o alias de PSN
(opcionales), el canal del streamer o su URL, y el intervalo de mensajes o la zona
del código. Al terminar, eliges vista previa, arranque real o solo guardar.
El arranque de chat empieza con la cuenta regresiva antes del primer mensaje.
En Kick arranca la captura; el canje real requiere una ROI configurada.

Para reutilizar la configuración:

```bash
# Vista previa con lo guardado:
uv run python -m sniper.setup --run
# Arranque real con las sesiones guardadas:
uv run python -m sniper.setup --run --real
```

En modo real abre automáticamente el Brave dedicado si CDP no está disponible y
reutiliza sus sesiones. Si falta la autenticación de Twitch para el chat o de PSN
para el canje, termina con un error antes de la captura y del primer envío.
No abre un asistente manual de autenticación durante ese arranque. La primera
sesión debe haberse iniciado previamente en el perfil dedicado. La captura pública
de Kick no necesita una cuenta de Kick.

La sesión del perfil dedicado es independiente de tu Brave habitual. Escribir tu
usuario en el asistente no inicia sesión: debe existir una sesión guardada en esa
ventana. Al arrancar, el bot espera hasta 15 segundos a que el chat esté listo,
también si la pestaña ya estaba abierta. Un cuadro ausente o no editable se informa
como chat no disponible; solo un botón visible de inicio de sesión produce el
mensaje específico de autenticación.

Los usuarios y el ID público guardados son referencias: no cambian ni seleccionan
la cuenta autenticada de Brave. Para cambiar la cuenta que envía o canjea, inicia
sesión con esa cuenta en el perfil dedicado. No se guardan contraseñas, correos,
cookies, tokens ni el `accountId` interno. Las preferencias quedan únicamente en
`.sniper-local.json`, excluido de Git; no lo compartas. En Linux se guarda con
permisos de lectura y escritura solo para su propietario.

### Kick · Linux

Cambia `STREAM_CHANNEL` una vez y usa el mismo valor en diagnóstico, captura y canje:

```bash
STREAM_CHANNEL="tu_canal"
STREAM_URL="https://kick.com/$STREAM_CHANNEL"

# Abrir el canal en el perfil dedicado; inicia sesión en PSN manualmente:
scripts/launch_brave.sh --kick "$STREAM_CHANNEL"
# Comprobar dependencias, Brave y registros:
uv run python -m sniper.doctor --kick "$STREAM_CHANNEL"
# Detectar códigos sin canjear:
uv run python -m sniper.kick --channel "$STREAM_CHANNEL" --source "$STREAM_URL" --dry-run
```

Para activar el canje, sustituye `X0 Y0 X1 Y1` por las coordenadas comprobadas del widget:

```bash
uv run python -m sniper.kick --channel "$STREAM_CHANNEL" --source "$STREAM_URL" --redeem --roi X0 Y0 X1 Y1
```

### Kick · Windows (PowerShell)

Con `uv` instalado y las dependencias preparadas con `uv sync`:

```powershell
$streamChannel = "tu_canal"
$streamUrl = "https://kick.com/$streamChannel"

# Abrir el perfil dedicado e iniciar sesión en PSN manualmente:
.\scripts\launch_brave.ps1 -KickStream -Channel $streamChannel
# También puedes abrirlo desde CMD: scripts\launch_brave.bat --kick tu_canal
uv run python -m sniper.doctor --kick $streamChannel
uv run python -m sniper.kick --channel $streamChannel --source $streamUrl --dry-run
```

Canje real, con las coordenadas ajustadas al stream:

```powershell
uv run python -m sniper.kick --channel $streamChannel --source $streamUrl --redeem --roi X0 Y0 X1 Y1
```

### Bot de chat de Twitch · Linux

`--chat` indica el canal del streamer donde se enviarán los mensajes, no tu
nombre de usuario. Tu identidad se obtiene de la sesión de Twitch abierta en el
perfil dedicado de Brave. El canal de `--chat` y el de `--source` deben coincidir:
si miras `https://www.twitch.tv/rubius`, usa `--chat rubius` aunque tu cuenta tenga
otro nombre. `180` segundos equivale a un mensaje cada 3 minutos.

```bash
STREAM_CHANNEL="tu_canal"
STREAM_URL="https://www.twitch.tv/$STREAM_CHANNEL"

# Abrir el perfil dedicado de Brave:
scripts/launch_brave.sh
```

El lanzador sin opciones abre un canal predeterminado. En esa misma ventana,
abre la URL de `STREAM_URL` e inicia sesión en Twitch. El bot selecciona el canal
que indiques con `--chat`, aunque la pestaña inicial fuera de otro streamer.

```bash
# Vista previa: no conecta a Brave ni envía mensajes:
uv run python -m sniper.daemon --chat "$STREAM_CHANNEL" --source "$STREAM_URL" --dry-run
# Envío real de un mensaje cada 5 minutos:
uv run python -m sniper.daemon --chat "$STREAM_CHANNEL" --source "$STREAM_URL" --chat-interval 300
```

`420` equivale a 7 minutos. `Ctrl+C` detiene cualquiera de los procesos.
La ejecución real del bot de chat reemplaza borradores existentes en el cuadro de envío.
No hay bot de chat para Kick; el modo de chat conserva su alcance de Linux.

Si aparece `ECONNREFUSED 127.0.0.1:9333`, el bot no pudo conectarse al Brave
dedicado. Ejecuta `scripts/launch_brave.sh` en otra terminal, inicia sesión en
Twitch en esa ventana y mantenla abierta. Después vuelve a ejecutar el bot.
Un Brave abierto normalmente no expone ese puerto; el lanzador prepara el perfil
y CDP. El bot no inicia Brave automáticamente. `--dry-run` no necesita esa conexión.

### Cambiar de streamer usando su URL

Elige el nombre que sigue al dominio y usa la misma plataforma y canal en todos
los pasos. Ejemplo de detección para `elmune`, sin depender del canal predeterminado:

```bash
uv run python -m sniper.kick --channel elmune --source https://kick.com/elmune --dry-run
```

Si prefieres que la IA prepare los comandos, basta con darle la URL y la función:

> Lee AGENTS.md y prepara la detección de códigos para https://kick.com/elmune. Configura el entorno y prueba sin canjear.

> Lee AGENTS.md y prepara el bot de chat para https://www.twitch.tv/CANAL, cada 5 minutos. Empieza con la vista previa sin enviar mensajes.

El CLI espera el nombre en `--channel`/`--chat` y la URL en `--source`; pasar solo
`--source` no cambia el nombre del canal. No uses URLs de clips o VOD para el modo real.
Cambiar de streamer conserva el límite de un éxito y los resultados pendientes.

## Tus datos: qué hay que configurar

No necesitas escribir tu correo, contraseña, `accountId` de PSN ni tokens en
archivos del proyecto. No hay `.env` ni archivo de credenciales que llenar.
La cuenta se toma de la sesión que abras a mano en un perfil dedicado de Brave:

| Dato | Dónde va | Cómo |
|---|---|---|
| Correo y contraseña de PSN | Perfil dedicado de Brave | Lanza Brave con `scripts/launch_brave.sh` (Linux) o `scripts\launch_brave.ps1` (Windows) e inicia sesión en store.playstation.com. El perfil queda en `~/.local/share/twitch-psn-sniper/brave-profile` o `%LOCALAPPDATA%\twitch-psn-sniper\brave-profile`. |
| Región de la tienda | URL de canje | Por defecto `es-co` (Colombia). Si tu cuenta es de otra región, cambia `STORE_URL` en `sniper/browser.py` (p. ej. `es-mx`, `en-us`). |
| `accountId` / ID en línea | Ninguno | PSN lo obtiene de la sesión; no hace falta escribirlo. |
| Canal a vigilar | Línea de comandos | `--channel CANAL` en `sniper.kick`; `--chat CANAL` en `sniper.daemon`; `--kick CANAL` en `doctor` y el lanzador de Linux, o `-KickStream -Channel CANAL` en PowerShell. |
| Zona del código en pantalla | Línea de comandos | `--roi X0 Y0 X1 Y1` (ver "Preparar el canje"). |

Todo lo que el programa genera (registro de intentos, capturas del modal de PSN,
logs) va a `runs/`. Esa carpeta **contiene tu correo y `accountId`** porque copia
el texto del modal de PSN: está en `.gitignore` y no debe subirse ni compartirse.

## Configuración asistida con AGENTS.md

[`AGENTS.md`](AGENTS.md) es la guía del repositorio para una IA que te ayude a
configurarlo. Explica cómo preparar tanto la alternativa Kick como el bot de chat
de Twitch, qué comprobar y cómo proteger tus datos.

Con acceso a la carpeta y los permisos necesarios, la IA puede automatizar la
comprobación de Python, la instalación de dependencias, el diagnóstico de Kick,
las pruebas y la preparación de los comandos de uso. El archivo no se ejecuta
por sí solo: contiene las instrucciones que la IA debe seguir. El inicio de sesión
en Brave se hace a mano; el canje y el envío real de mensajes se activan cuando los pidas.

Abre el proyecto con tu asistente de código y dale una de estas instrucciones:

> Lee AGENTS.md y configura la alternativa Kick para https://kick.com/CANAL. Prueba la detección sin canjear.

> Lee AGENTS.md y configura el bot de chat de Twitch para https://www.twitch.tv/CANAL, cada 5 minutos. Usa primero la vista previa sin enviar mensajes.

La guía distingue las dos funciones: Kick utiliza PSN y OCR; el bot de chat usa
tu sesión de Twitch y puede reaccionar con emotes sin configurar PSN ni modelos OCR.
También indica que el soporte de Windows corresponde a Kick, `redeem_cli` y `doctor`;
el bot de chat conserva su alcance de Linux.

## Instalación en Windows

Instala Brave y [uv](https://docs.astral.sh/uv/getting-started/installation/).
Con WinGet, puedes instalar `uv` desde PowerShell:

```powershell
winget install --id=astral-sh.uv -e
```

Abre una terminal nueva para que encuentre `uv`. Desde la carpeta del proyecto:

```powershell
uv python install 3.12
uv sync --python 3.12
uv run python -m sniper.doctor --kick CANAL
uv run python -m sniper.kick --channel CANAL --dry-run
```

`uv` instala Python, crea `.venv` e instala las versiones de `uv.lock`.
No necesitas instalar pip ni activar el entorno virtual. Los mismos comandos
funcionan desde PowerShell y CMD. `uv run` también coloca `streamlink.exe` del
entorno en PATH durante la ejecución.

Windows selecciona ONNX Runtime para CPU automáticamente desde `pyproject.toml`;
no requiere una GPU NVIDIA. Linux conserva la dependencia de ONNX Runtime GPU.

Los modelos OCR deben estar disponibles localmente para que `doctor` los pruebe.
En una instalación nueva, prepararlos una vez fuera del diagnóstico:

```powershell
uv run python -m rapidocr.main download_models
```

La preparación puede descargar pesos. `doctor` no los descarga ni escribe archivos de campaña.
No se necesita descargar un navegador de Playwright: el canje se conecta al Brave dedicado.

## Instalación en Linux

Con `uv` y Brave instalados:

```bash
uv python install 3.12
uv sync --python 3.12
uv run python -m sniper.doctor --kick CANAL
uv run python -m sniper.kick --channel CANAL
```

La instalación sigue `pyproject.toml` y `uv.lock`. Linux conserva ONNX Runtime GPU;
Windows selecciona ONNX Runtime CPU. Usa `uv run` para ejecutar los comandos sin
activar el entorno manualmente.

## Comprobar el entorno

```bash
uv run python -m sniper.doctor --kick CANAL
uv run python -m sniper.doctor --kick CANAL --json
```

El diagnóstico muestra `✓`, `!` o `✗`, una sugerencia para cada problema y un resumen.
Devuelve 1 si hay errores. Sin `--kick`, comprueba una URL predeterminada de Twitch; no permite elegir otro canal de Twitch.
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
uv run python -m sniper.kick --channel CANAL --dry-run
uv run python -m sniper.kick --channel CANAL --source video.mp4 --once
```

En real, usar `--roi X0 Y0 X1 Y1` ajustado al widget, excluyendo chat y alertas.
Las coordenadas son relativas a la imagen, de 0 a 1. La detección de toda la pantalla
sirve para explorar el diseño. `--require-label` exige CODIGO/CÓDIGO en la misma
caja OCR que el cupón o en una caja adyacente a la izquierda o justo encima.
Sin cajas OCR válidas no acepta candidatos con esta opción.

Abrir manualmente el perfil dedicado y autenticar PSN:

```bash
# Linux
scripts/launch_brave.sh --kick CANAL
```

```powershell
# Windows PowerShell
.\scripts\launch_brave.ps1 -KickStream -Channel CANAL
# Windows, mediante el lanzador BAT
.\scripts\launch_brave.bat --kick CANAL
```

En Windows el perfil se guarda en
`%LOCALAPPDATA%\twitch-psn-sniper\brave-profile`; los lanzadores buscan Brave en
PATH, Program Files y LOCALAPPDATA. CDP escucha en `127.0.0.1:9333`.

Una vez elegida la zona, activar el canje con las coordenadas comprobadas:

```bash
uv run python -m sniper.kick --channel CANAL --redeem --roi X0 Y0 X1 Y1
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
uv run python scripts/redeem_cli.py --runs-dir runs/kick-CANAL --resolve XXXX-XXXX-XXXX --as already_used
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
uv run python -m sniper.daemon --chat CANAL --dry-run
# Envío real a Twitch, después de iniciar sesión manualmente:
uv run python -m sniper.daemon --chat CANAL --chat-interval 300
```

Esta parte del proyecto conserva su alcance de Linux; no está incluida en la
validación de compatibilidad con Windows. Los logs se guardan en `runs/` y no se publican.

## Pruebas

```bash
uv sync
uv run python -m pytest -q
```

Las pruebas usan navegadores y canjes simulados. El OCR histórico prueba una imagen local.
