"""Pruebas nativas de scripts PowerShell y BAT en Windows, simulando Get-Command y Start-Process."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Pruebas nativas de scripts PowerShell/BAT solo en Windows",
)

ROOT = Path(__file__).resolve().parent.parent
PS1_SCRIPT = ROOT / "scripts" / "launch_brave.ps1"
BAT_SCRIPT = ROOT / "scripts" / "launch_brave.bat"
MOCK_BRAVE = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"

PS1_HARNESS = r"""
$ErrorActionPreference = 'Stop'
function Get-Command {
    [CmdletBinding()]
    param([Parameter(Position=0)][string]$Name)
    if ($Name -eq 'brave.exe') {
        if ($env:TEST_MOCK_BRAVE) {
            return [PSCustomObject]@{ Source = $env:TEST_MOCK_BRAVE }
        }
        return
    }
    return
}
function Start-Process {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$FilePath,
        [string[]]$ArgumentList
    )
    $payload = [ordered]@{
        FilePath = $FilePath
        ArgumentList = @($ArgumentList)
    }
    $json = $payload | ConvertTo-Json -Depth 4 -Compress
    [System.IO.File]::WriteAllText(
        $env:TEST_CAPTURE_JSON,
        $json,
        (New-Object System.Text.UTF8Encoding($false))
    )
}
$bound = @{}
if ($env:TEST_PARAMS_JSON) {
    $raw = ConvertFrom-Json -InputObject $env:TEST_PARAMS_JSON
    foreach ($prop in $raw.PSObject.Properties) {
        $bound[$prop.Name] = $prop.Value
    }
}
& $env:TEST_SCRIPT_PATH @bound
"""

BAT_SPY_PS1 = r"""
param(
    [Alias('kick')][switch]$KickStream,
    [Alias('twitch')][switch]$TwitchStream,
    [Parameter(Position=0)][string]$Channel = ''
)
$ErrorActionPreference = 'Stop'
function Get-Command {
    [CmdletBinding()]
    param([Parameter(Position=0)][string]$Name)
    if ($Name -eq 'brave.exe') {
        if ($env:TEST_MOCK_BRAVE) {
            return [PSCustomObject]@{ Source = $env:TEST_MOCK_BRAVE }
        }
        return
    }
    return
}
function Start-Process {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$FilePath,
        [string[]]$ArgumentList
    )
    $payload = [ordered]@{
        FilePath = $FilePath
        ArgumentList = @($ArgumentList)
    }
    $json = $payload | ConvertTo-Json -Depth 4 -Compress
    [System.IO.File]::WriteAllText(
        $env:TEST_CAPTURE_JSON,
        $json,
        (New-Object System.Text.UTF8Encoding($false))
    )
}
$bound = @{}
if ($KickStream) { $bound['KickStream'] = $true }
if ($TwitchStream) { $bound['TwitchStream'] = $true }
if ($Channel) { $bound['Channel'] = $Channel }
& $env:TEST_SCRIPT_PATH @bound
"""


def _powershell_exe() -> str:
    exe = shutil.which("powershell.exe") or shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not exe:
        pytest.skip("PowerShell no disponible en PATH")
    return exe


def _run_ps1(
    tmp_path: Path,
    params: dict,
    *,
    mock_brave: str | None = MOCK_BRAVE,
    localappdata: Path | None = None,
    clear_localappdata: bool = False,
    clear_program_files: bool = False,
):
    harness = tmp_path / "run_harness.ps1"
    harness.write_text(PS1_HARNESS, encoding="ascii")
    capture = tmp_path / "captured.json"
    env = os.environ.copy()
    env["TEST_SCRIPT_PATH"] = str(PS1_SCRIPT)
    env["TEST_CAPTURE_JSON"] = str(capture)
    env["TEST_PARAMS_JSON"] = json.dumps(params)
    env["TEST_MOCK_BRAVE"] = mock_brave or ""
    if clear_localappdata:
        for key in [k for k in env if k.upper() == "LOCALAPPDATA"]:
            env.pop(key, None)
    else:
        target_local = localappdata or (tmp_path / "Usuario Con Espacios")
        target_local.mkdir(parents=True, exist_ok=True)
        env["LOCALAPPDATA"] = str(target_local)
    if clear_program_files:
        for key in [k for k in env if k.upper() in {"PROGRAMFILES", "PROGRAMFILES(X86)"}]:
            env.pop(key, None)
    result = subprocess.run(
        [_powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    payload = json.loads(capture.read_text(encoding="utf-8-sig")) if capture.exists() else None
    return result, payload


@pytest.mark.parametrize(
    "params,expected_url",
    [
        ({}, "https://www.twitch.tv/rubius"),
        ({"TwitchStream": True}, "https://www.twitch.tv/rubius"),
        ({"TwitchStream": True, "Channel": "mi_canal"}, "https://www.twitch.tv/mi_canal"),
        ({"twitch": True, "Channel": "canal_dos"}, "https://www.twitch.tv/canal_dos"),
        ({"KickStream": True}, "https://kick.com/elded"),
        ({"KickStream": True, "Channel": "mi-canal"}, "https://kick.com/mi-canal"),
        ({"kick": True, "Channel": "canal_kick"}, "https://kick.com/canal_kick"),
    ],
)
def test_ps1_twitch_and_kick_flags_preserve_spaces_without_launching_brave(tmp_path, params, expected_url):
    localappdata = tmp_path / "Usuario Con Espacios"
    result, payload = _run_ps1(tmp_path, params, localappdata=localappdata)

    assert result.returncode == 0, result.stderr
    assert payload is not None
    assert payload["FilePath"] == MOCK_BRAVE
    expected_profile = localappdata / "twitch-psn-sniper" / "brave-profile"
    assert expected_profile.is_dir()
    args = payload["ArgumentList"]
    assert f'--user-data-dir="{expected_profile}"' in args
    assert "--remote-debugging-address=127.0.0.1" in args
    assert "--remote-debugging-port=9333" in args
    assert "--no-first-run" in args
    assert "--no-default-browser-check" in args
    assert args[-1] == expected_url


def test_ps1_falls_back_to_localappdata_brave_when_get_command_returns_nothing(tmp_path):
    localappdata = tmp_path / "Usuario Con Espacios"
    fake_brave = localappdata / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"
    fake_brave.parent.mkdir(parents=True)
    fake_brave.write_bytes(b"")

    result, payload = _run_ps1(
        tmp_path,
        {"TwitchStream": True, "Channel": "mi_canal"},
        mock_brave=None,
        localappdata=localappdata,
        clear_program_files=True,
    )

    assert result.returncode == 0, result.stderr
    assert payload is not None
    assert payload["FilePath"] == str(fake_brave)
    assert payload["ArgumentList"][-1] == "https://www.twitch.tv/mi_canal"


@pytest.mark.parametrize(
    "params,expected_error",
    [
        ({"KickStream": True, "TwitchStream": True}, "Elige Kick o Twitch"),
        ({"TwitchStream": True, "Channel": "mi-canal"}, "Canal invalido"),
        ({"TwitchStream": True, "Channel": "a" * 26}, "Canal invalido"),
        ({"TwitchStream": True, "Channel": "bad/canal"}, "Canal invalido"),
        ({"KickStream": True, "Channel": "-malo"}, "Canal invalido"),
        ({"KickStream": True, "Channel": "con espacio"}, "Canal invalido"),
    ],
)
def test_ps1_rejects_invalid_channel_and_conflicting_flags_without_starting_process(
    tmp_path, params, expected_error
):
    result, payload = _run_ps1(tmp_path, params)

    assert result.returncode != 0
    assert payload is None
    assert expected_error in (result.stderr + result.stdout)


def test_ps1_fails_clearly_when_brave_or_localappdata_is_missing(tmp_path):
    missing_brave, payload_brave = _run_ps1(
        tmp_path,
        {"TwitchStream": True, "Channel": "mi_canal"},
        mock_brave=None,
        clear_program_files=True,
    )
    assert missing_brave.returncode != 0
    assert payload_brave is None
    assert "Instala Brave" in (missing_brave.stderr + missing_brave.stdout)

    missing_env, payload_env = _run_ps1(
        tmp_path,
        {"TwitchStream": True, "Channel": "mi_canal"},
        clear_localappdata=True,
    )
    assert missing_env.returncode != 0
    assert payload_env is None
    assert "LOCALAPPDATA" in (missing_env.stderr + missing_env.stdout)


def _run_bat(tmp_path: Path, bat_args: list[str]):
    bat_dir = tmp_path / "Dir Con Espacios"
    bat_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BAT_SCRIPT, bat_dir / "launch_brave.bat")
    (bat_dir / "launch_brave.ps1").write_text(BAT_SPY_PS1, encoding="ascii")
    capture = tmp_path / "bat_captured.json"
    localappdata = tmp_path / "Usuario Con Espacios"
    localappdata.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["TEST_SCRIPT_PATH"] = str(PS1_SCRIPT)
    env["TEST_CAPTURE_JSON"] = str(capture)
    env["TEST_MOCK_BRAVE"] = MOCK_BRAVE
    env["LOCALAPPDATA"] = str(localappdata)

    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "launch_brave.bat", *bat_args],
        cwd=bat_dir,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    payload = json.loads(capture.read_text(encoding="utf-8-sig")) if capture.exists() else None
    return result, payload


@pytest.mark.parametrize(
    "bat_args,expected_url",
    [
        ([], "https://www.twitch.tv/rubius"),
        (["--twitch"], "https://www.twitch.tv/rubius"),
        (["--twitch", "mi_canal"], "https://www.twitch.tv/mi_canal"),
        (["--kick"], "https://kick.com/elded"),
        (["--kick", "mi-canal"], "https://kick.com/mi-canal"),
    ],
)
def test_bat_forwards_twitch_and_kick_flags_without_opening_brave(tmp_path, bat_args, expected_url):
    result, payload = _run_bat(tmp_path, bat_args)

    assert result.returncode == 0, result.stderr + result.stdout
    assert payload is not None
    assert payload["FilePath"] == MOCK_BRAVE
    assert payload["ArgumentList"][-1] == expected_url


def test_bat_propagates_invalid_channel_error_without_opening_brave(tmp_path):
    result, payload = _run_bat(tmp_path, ["--twitch", "mi-canal"])

    assert result.returncode != 0
    assert payload is None
    assert "Canal invalido" in (result.stderr + result.stdout)
