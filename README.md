# Kuma Bridge

`kuma-bridge` is a standalone HTTP bridge that lets WP Ultimo create Uptime Kuma monitors through Socket.IO (works well with Kuma v2 setups where direct REST monitor endpoints are limited).

## 1) Requirements

- Docker + Docker Compose
- A running Uptime Kuma instance
- Kuma account credentials (username/password)
- A shared secret used by WP Ultimo and bridge (`BRIDGE_SECRET`)

## 2) Quick Start (Docker)

1. Clone or pull this repo on your server.
2. Create your runtime file:

```bash
cp docker-compose.yml.example docker-compose.yml
```

3. Edit `docker-compose.yml` and set at least:
   - `BRIDGE_SECRET`
   - `KUMA_URL`
   - `KUMA_USERNAME`
   - `KUMA_PASSWORD`

4. Build and start:

```bash
docker compose up -d --build
```

5. Verify health:

```bash
curl -s http://127.0.0.1:8788/healthz
```

Expected response:

```json
{"ok":"true"}
```

## 3) Important Environment Variables

- `BRIDGE_SECRET`: HMAC secret shared with WP Ultimo (required).
- `BRIDGE_MAX_SKEW_SECONDS`: max accepted request timestamp drift, default `300`.
- `KUMA_URL`: Kuma base URL, for example `https://uptime.example.com`.
- `KUMA_USERNAME` / `KUMA_PASSWORD`: Kuma login for Socket.IO flow.
- `KUMA_SSL_VERIFY`: set `1` (default) to verify TLS cert, set `0` only for trusted self-signed/internal labs.
- `REQUEST_TIMEOUT`: timeout per Socket.IO call, default `20`.
- `BRIDGE_ADD_RETRIES`: retry attempts for transient errors, default `3`.
- `BRIDGE_RETRY_BASE_DELAY`: base delay for retry backoff, default `2`.
- `KUMA_DEFAULT_NOTIFICATION_IDS`: optional comma-separated notification IDs to attach.
- `KUMA_NOTIFY_ON_CREATE`: `1` to trigger Kuma test notification after create.
- `FALLBACK_TELEGRAM_*`: optional direct Telegram fallback from bridge.

## 4) Update / Deploy New Version

```bash
git pull
docker compose up -d --build
docker compose logs -f --tail=100
```

## 5) Troubleshooting

- **401 invalid signature**: `BRIDGE_SECRET` mismatch between WP Ultimo and bridge.
- **Socket timeout**: increase `REQUEST_TIMEOUT` and WP Ultimo bridge timeout.
- **Login failed**: verify `KUMA_USERNAME` / `KUMA_PASSWORD`.
- **Cannot reach Kuma**: verify `KUMA_URL`, DNS, firewall, reverse proxy.
- **No notification sent**: check `KUMA_NOTIFY_ON_CREATE`, attached notification IDs, or fallback Telegram envs.

## Repository Layout

- `app.py`, `Dockerfile`, `requirements.txt`: active bridge service.
- `legacy-uptime-kuma/`: legacy Node.js Uptime Kuma source fragments moved out of `wp-ultimo` during repository split.
