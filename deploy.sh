#!/usr/bin/env bash
# Ship this app to the Hetzner box as https://$DOMAIN.
#
# Same approach as runsheet: rsync the source, then build the venv on the box
# (it's arm64, so a copied venv would break), and install the systemd unit and
# nginx site. Safe to rerun: the unit is reinstalled only when it changes, and
# the nginx site only on first deploy, because certbot edits it afterwards.
#
#   ./deploy.sh              deploy (runs tests first)
#   ./deploy.sh --env        also copy the local .env (API key) to the box
#   ./deploy.sh logs         tail the service log
#   ./deploy.sh down         stop and remove the service and site
set -euo pipefail
cd "$(dirname "$0")"
unset PYTHONPATH

. ./deploy.env
SERVER=root@91.99.162.43
KEY=~/.ssh/hetzner_key
SSH=(ssh -i "$KEY" -o ConnectTimeout=15)
REMOTE=/root/hack/$NAME
UNIT=hack-$NAME
DOMAIN=${DOMAIN:-$NAME.91.99.162.43.sslip.io}

case "${1:-}" in
  logs) exec "${SSH[@]}" "$SERVER" journalctl -u "$UNIT" -f -n 50 ;;
  down)
    "${SSH[@]}" "$SERVER" "systemctl disable --now $UNIT; rm -f /etc/systemd/system/$UNIT.service \
      /etc/nginx/sites-enabled/$UNIT /etc/nginx/sites-available/$UNIT; systemctl daemon-reload; \
      nginx -t && systemctl reload nginx"
    echo "removed $UNIT (files left in $REMOTE)"; exit 0 ;;
esac

# Refuse to clash with another app on the box.
if "${SSH[@]}" "$SERVER" "ss -ltnp | grep -q ':$PORT ' && ! systemctl is-active -q $UNIT"; then
  echo "port $PORT is taken by something else on the box; change PORT in deploy.env" >&2; exit 1
fi

if [ "${SKIP_TESTS:-}" != "1" ]; then
  echo "==> tests"; ./run.sh test >/dev/null || { echo "tests failed; not deploying" >&2; exit 1; }
fi

echo "==> upload"
"${SSH[@]}" "$SERVER" mkdir -p "$REMOTE"
rsync -az --delete -e "ssh -i $KEY" \
  --exclude venv/ --exclude .env --exclude __pycache__/ --exclude .pytest_cache/ \
  --exclude .git/ --exclude 'data/' ./ "$SERVER:$REMOTE/"
# Replays are uploaded separately and never deleted, so a recording made on
# the box survives redeploys.
[ -d data/replays ] && rsync -az -e "ssh -i $KEY" data/replays/ "$SERVER:$REMOTE/data/replays/"
if [ "${1:-}" = "--env" ] && [ -f .env ]; then
  scp -q -i "$KEY" .env "$SERVER:$REMOTE/.env"; echo "    copied .env"
fi

echo "==> server"
"${SSH[@]}" "$SERVER" NAME="$NAME" PORT="$PORT" DOMAIN="$DOMAIN" REMOTE="$REMOTE" UNIT="$UNIT" bash -s <<'REMOTE_SCRIPT'
set -euo pipefail
mkdir -p "$REMOTE/data"
[ -x "$REMOTE/venv/bin/python" ] || python3 -m venv "$REMOTE/venv"
"$REMOTE/venv/bin/pip" install -q --upgrade pip
"$REMOTE/venv/bin/pip" install -q -r "$REMOTE/requirements.txt"

render() { sed -e "s|__NAME__|$NAME|g" -e "s|__PORT__|$PORT|g" -e "s|__REMOTE__|$REMOTE|g" -e "s|__DOMAIN__|$DOMAIN|g" "$1"; }

render "$REMOTE/deploy/app.service" > "/tmp/$UNIT.service"
if ! cmp -s "/tmp/$UNIT.service" "/etc/systemd/system/$UNIT.service"; then
  install -m 644 "/tmp/$UNIT.service" "/etc/systemd/system/$UNIT.service"
  systemctl daemon-reload; systemctl enable -q "$UNIT"
fi
systemctl restart "$UNIT"

if [ ! -f "/etc/nginx/sites-available/$UNIT" ]; then
  render "$REMOTE/deploy/nginx.conf" > "/etc/nginx/sites-available/$UNIT"
  ln -sfn "/etc/nginx/sites-available/$UNIT" "/etc/nginx/sites-enabled/$UNIT"
  nginx -t -q && systemctl reload nginx
  if command -v certbot >/dev/null; then
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect -q \
      || echo "    certbot failed; site is on plain http for now"
  fi
fi

for _ in $(seq 20); do
  curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break; sleep 0.5
done
curl -fsS "http://127.0.0.1:$PORT/api/health" || { journalctl -u "$UNIT" -n 30 --no-pager; exit 1; }
echo
REMOTE_SCRIPT

echo "==> live: https://$DOMAIN"
