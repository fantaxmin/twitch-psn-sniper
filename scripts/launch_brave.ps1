param(
    [Alias('kick')][switch]$KickStream,
    [Alias('twitch')][switch]$TwitchStream,
    [Parameter(Position=0)][string]$Channel = ''
)
$ErrorActionPreference = 'Stop'
if ($KickStream -and $TwitchStream) { throw 'Elige Kick o Twitch, no ambos' }
if (-not $Channel) {
    $Channel = if ($KickStream) { 'elded' } else { 'rubius' }
}
$channelPattern = if ($KickStream) { '^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,99}$' } else { '^[a-zA-Z0-9_]{1,25}$' }
if ($Channel -cnotmatch $channelPattern) {
    throw 'Canal invalido para la plataforma elegida'
}
if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA no esta definido' }
$brave = Get-Command brave.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1
if (-not $brave) {
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        if ($base) {
            $candidate = Join-Path $base 'BraveSoftware\Brave-Browser\Application\brave.exe'
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { $brave = $candidate; break }
        }
    }
}
if (-not $brave) { throw 'Instala Brave o agrega brave.exe a PATH' }
$profileDir = Join-Path $env:LOCALAPPDATA 'twitch-psn-sniper\brave-profile'
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null
$streamUrl = "https://www.twitch.tv/$Channel"
if ($KickStream) { $streamUrl = "https://kick.com/$Channel" }
# Las comillas internas conservan rutas con espacios al construir la linea de Windows.
$braveArgs = @(
    "--user-data-dir=`"$profileDir`"",
    '--remote-debugging-address=127.0.0.1',
    '--remote-debugging-port=9333',
    '--no-first-run',
    '--no-default-browser-check',
    $streamUrl
)
Start-Process -FilePath $brave -ArgumentList $braveArgs
