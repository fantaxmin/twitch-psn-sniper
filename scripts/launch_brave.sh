#!/usr/bin/env bash
# Run manually when ready to sign in. No Playwright browser download is needed.
set -euo pipefail
stream_url='https://www.twitch.tv/rubius'
if [[ "${1:-}" == '--kick' && $# -le 2 ]]; then
  channel="${2:-elded}"
  if [[ ! "$channel" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,99}$ ]]; then
    echo 'Canal de Kick inválido' >&2
    exit 2
  fi
  stream_url="https://kick.com/$channel"
elif [[ $# -ne 0 ]]; then
  echo 'Uso: scripts/launch_brave.sh [--kick [canal]]' >&2
  exit 2
fi
umask 077
profile_dir="${HOME}/.local/share/twitch-psn-sniper/brave-profile"
mkdir -p "$profile_dir"
exec /usr/bin/brave-browser \
  --user-data-dir="$profile_dir" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9333 \
  --no-first-run \
  --no-default-browser-check \
  "$stream_url"
