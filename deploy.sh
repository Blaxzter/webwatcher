#!/usr/bin/env bash
# Überträgt den Watcher auf den Server - ohne .venv, Daten und Secrets.
#
#   ./deploy.sh                        # an den Standard-Host, nur übertragen
#   ./deploy.sh hetzner ~/docker/watcher/watcher
#   ./deploy.sh --restart              # danach neu bauen und starten
#   DRY_RUN=1 ./deploy.sh              # nur zeigen, was übertragen würde
#
# Warum tar statt scp: scp kennt kein --exclude und würde die 159 MB große
# .venv mitschleppen. Ausserdem legt "scp -r ordner ziel" beim zweiten Mal
# ein verschachteltes ziel/ordner an, wenn das Ziel schon existiert.

set -euo pipefail

RESTART=0
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --restart) RESTART=1 ;;
        *) ARGS+=("$arg") ;;
    esac
done

REMOTE="${ARGS[0]:-hetzner}"
REMOTE_DIR="${ARGS[1]:-~/docker/watcher/watcher}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# .env bleibt aus: der Server hat seinen eigenen Token. config.yaml geht mit,
# weil du sie lokal mit `webwatcher pick` pflegst.
EXCLUDES=(
    --exclude=.venv
    --exclude=__pycache__
    --exclude='*.pyc'
    --exclude='*.egg-info'
    --exclude=.ruff_cache
    --exclude=data
    --exclude='*.sqlite3*'
    --exclude=.env
    --exclude=.git
)

if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "Würde nach ${REMOTE}:${REMOTE_DIR} übertragen:"
    tar czf - -C "$DIR" "${EXCLUDES[@]}" . | tar tzf - | grep -v '/$' | sed 's|^\./|  |'
    exit 0
fi

echo "Übertrage nach ${REMOTE}:${REMOTE_DIR} ..."
tar czf - -C "$DIR" "${EXCLUDES[@]}" . \
    | ssh "$REMOTE" "mkdir -p ${REMOTE_DIR} && tar xzf - -C ${REMOTE_DIR}"

if [[ "$RESTART" == "1" ]]; then
    echo "Baue und starte neu ..."
    ssh "$REMOTE" "cd ${REMOTE_DIR} && docker compose up -d --build"
    echo "Fertig. Logs:  ssh ${REMOTE} 'cd ${REMOTE_DIR} && docker compose logs -f'"
else
    echo "Übertragen. Auf dem Server aktivieren:"
    echo "  ssh ${REMOTE} 'cd ${REMOTE_DIR} && docker compose up -d --build'"
fi
