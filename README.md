# Kuma Bridge (separate service)

This bridge lets WP Ultimo create monitors in Uptime Kuma via Socket.IO, without relying on Kuma REST monitor endpoints.

## Run with Docker

1. Copy `docker-compose.yml.example` to `docker-compose.yml`.
2. Update `BRIDGE_SECRET`, `KUMA_URL`, `KUMA_USERNAME`, `KUMA_PASSWORD`.
3. Start service:

```bash
docker compose up -d --build
```

4. Health check:

```bash
curl -s http://127.0.0.1:8788/healthz
```

## WP Ultimo settings

In `Kuma Integration` settings:

- `Kuma Bridge URL`: `http://127.0.0.1:8788`
- `Kuma Bridge Secret`: same value as `BRIDGE_SECRET`
- keep existing `Kuma Base URL` and `API Key` as fallback

WP Ultimo will try bridge first, then fallback to existing REST/Socket flow if bridge is not configured.
