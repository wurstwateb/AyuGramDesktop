#!/usr/bin/env bash
# Installs or updates the TypendruckerGram Sync relay. Run as root on the server
# from the directory with the unpacked sync-server sources:
#   sudo bash deploy/install.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
CADDYFILE=/etc/caddy/Caddyfile
SITE='unionofjackbox.tokenized.name, www.unionofjackbox.tokenized.name {'

id typsync >/dev/null 2>&1 || useradd --system --home /var/lib/typsync --shell /usr/sbin/nologin typsync
install -d -m 755 /opt/typsync
install -d -m 700 -o typsync -g typsync /var/lib/typsync /var/lib/typsync/blobs
install -d -m 750 -g typsync /etc/typsync

rsync -a --delete --exclude '.venv' --exclude '__pycache__' --exclude '*.egg-info' \
    "$SRC/" /opt/typsync/src/
[ -x /opt/typsync/venv/bin/python ] || python3 -m venv /opt/typsync/venv
/opt/typsync/venv/bin/pip install -q --upgrade pip
/opt/typsync/venv/bin/pip install -q "/opt/typsync/src[catcher]"

if [ ! -f /etc/typsync/relay.env ]; then
    ( umask 027
    cat > /etc/typsync/relay.env <<ENV
TYPSYNC_DB=/var/lib/typsync/relay.db
TYPSYNC_BLOB_DIR=/var/lib/typsync/blobs
TYPSYNC_REGISTRATION_SECRET=$(openssl rand -hex 16)
ENV
    )
    chgrp typsync /etc/typsync/relay.env
fi

install -m 644 "$SRC/deploy/typsync-relay.service" /etc/systemd/system/typsync-relay.service
systemctl daemon-reload
systemctl enable --now typsync-relay.service
systemctl restart typsync-relay.service

# The catcher starts only after `python -m typsync.catcher.setup` created its config.
install -m 644 "$SRC/deploy/typsync-catcher.service" /etc/systemd/system/typsync-catcher.service
systemctl daemon-reload
if [ -f /var/lib/typsync/catcher.json ]; then
    systemctl enable typsync-catcher.service
    systemctl restart typsync-catcher.service
fi

if ! grep -q 'handle_path /typsync/\*' "$CADDYFILE"; then
    cp "$CADDYFILE" "$CADDYFILE.bak-typsync-$(date +%Y%m%d%H%M%S)"
    tmp="$(mktemp)"
    awk -v site="$SITE" -v snippet="$SRC/deploy/Caddyfile.snippet" '
        { print }
        $0 == site && !done {
            while ((getline line < snippet) > 0) print line
            done = 1
        }
        END { if (!done) exit 3 }
    ' "$CADDYFILE" > "$tmp"
    caddy validate --config "$tmp" --adapter caddyfile
    install -m 644 "$tmp" "$CADDYFILE"
    rm -f "$tmp"
    systemctl reload caddy
fi

echo "typsync relay installed"
