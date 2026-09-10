#!/usr/bin/env sh
# Upload a plugin archive to a running Otari through its API.
#
#   OTARI_URL=http://localhost:8000 OTARI_MASTER_KEY=... scripts/upload_plugin.sh ./otari-agent-gates.zip
#
# The gateway must have plugins.allow_install: true (or OTARI_PLUGINS_ALLOW_INSTALL=true)
# and be restarted after the upload for the plugin to load. On the gateway's own
# machine, `otari plugins install <archive>` does the same without the API.
set -eu

archive="${1:-}"
if [ -z "$archive" ] || [ ! -f "$archive" ]; then
  echo "usage: $0 <plugin.zip|plugin.tar.gz>" >&2
  exit 2
fi
: "${OTARI_URL:=http://localhost:8000}"
if [ -z "${OTARI_MASTER_KEY:-}" ]; then
  echo "OTARI_MASTER_KEY is not set" >&2
  exit 2
fi

curl --fail-with-body -sS -X POST "${OTARI_URL%/}/api/v1/plugins/upload" \
  -H "Authorization: Bearer ${OTARI_MASTER_KEY}" \
  -F "file=@${archive}"
echo
echo "Restart the gateway to load the plugin."
