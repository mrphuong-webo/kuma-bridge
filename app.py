import hashlib
import hmac
import json
import os
import time
from threading import Event
from typing import Any, Dict

import requests
import socketio
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="kuma-bridge", version="1.0.0")

BRIDGE_SECRET = os.getenv("BRIDGE_SECRET", "")
MAX_SKEW_SECONDS = int(os.getenv("BRIDGE_MAX_SKEW_SECONDS", "300"))
KUMA_URL = os.getenv("KUMA_URL", "").rstrip("/")
KUMA_USERNAME = os.getenv("KUMA_USERNAME", "")
KUMA_PASSWORD = os.getenv("KUMA_PASSWORD", "")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))
KUMA_SSL_VERIFY = os.getenv("KUMA_SSL_VERIFY", "1").strip() not in ("0", "false", "False")
KUMA_DEFAULT_NOTIFICATION_IDS = os.getenv("KUMA_DEFAULT_NOTIFICATION_IDS", "").strip()
KUMA_NOTIFY_ON_CREATE = os.getenv("KUMA_NOTIFY_ON_CREATE", "1").strip() == "1"
BRIDGE_ADD_RETRIES = max(1, int(os.getenv("BRIDGE_ADD_RETRIES", "3")))
BRIDGE_RETRY_BASE_DELAY = max(1, int(os.getenv("BRIDGE_RETRY_BASE_DELAY", "2")))
FALLBACK_TELEGRAM_BOT_TOKEN = os.getenv("FALLBACK_TELEGRAM_BOT_TOKEN", "").strip()
FALLBACK_TELEGRAM_CHAT_ID = os.getenv("FALLBACK_TELEGRAM_CHAT_ID", "").strip()
FALLBACK_TELEGRAM_THREAD_ID = os.getenv("FALLBACK_TELEGRAM_THREAD_ID", "").strip()


def _verify_request(timestamp: str, signature: str, raw_body: bytes) -> None:
    if not BRIDGE_SECRET:
        raise HTTPException(status_code=500, detail="Bridge secret is not configured.")

    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="Missing signature headers.")

    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid timestamp.") from exc

    if abs(int(time.time()) - ts) > MAX_SKEW_SECONDS:
        raise HTTPException(status_code=401, detail="Timestamp skew is too large.")

    expected = hmac.new(
        BRIDGE_SECRET.encode("utf-8"),
        f"{timestamp}.{raw_body.decode('utf-8')}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid signature.")


def _normalize_login_ack(data: Any) -> Dict[str, Any]:
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                return item
    if isinstance(data, dict):
        return data
    return {}


def _normalize_notification(notification: Dict[str, Any]) -> Dict[str, Any]:
    config = notification.get("config")
    if isinstance(config, str) and config.strip():
        try:
            parsed = json.loads(config)
        except Exception:
            parsed = {}
        if isinstance(parsed, dict):
            merged = dict(parsed)
            merged.update(notification)
            return merged
    return notification


def _call_with_step(sio: socketio.Client, event: str, data: Any = None, timeout: int = 20, step: str = "") -> Any:
    try:
        if data is None:
            return sio.call(event, timeout=timeout)
        return sio.call(event, data, timeout=timeout)
    except socketio.exceptions.TimeoutError as exc:
        label = step or event
        raise HTTPException(status_code=504, detail=f"Kuma Socket.IO timeout at step: {label}") from exc


def _send_fallback_telegram_on_create(payload: Dict[str, Any], add_ack: Dict[str, Any]) -> None:
    if not FALLBACK_TELEGRAM_BOT_TOKEN or not FALLBACK_TELEGRAM_CHAT_ID:
        return

    name = str(payload.get("name", "")).strip()
    url = str(payload.get("url", "")).strip()
    monitor_id = add_ack.get("monitorID", 0)
    text = (
        "Kuma monitor created\n"
        f"Domain: {name or '(unknown)'}\n"
        f"URL: {url or '(unknown)'}\n"
        f"Monitor ID: {monitor_id}"
    )

    endpoint = f"https://api.telegram.org/bot{FALLBACK_TELEGRAM_BOT_TOKEN}/sendMessage"
    body = {
        "chat_id": FALLBACK_TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if FALLBACK_TELEGRAM_THREAD_ID.isdigit():
        body["message_thread_id"] = int(FALLBACK_TELEGRAM_THREAD_ID)

    try:
        requests.post(endpoint, data=body, timeout=10)
    except Exception:
        pass


def _bridge_add_monitor(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not KUMA_URL or not KUMA_USERNAME or not KUMA_PASSWORD:
        raise HTTPException(status_code=500, detail="Kuma credentials are not fully configured on bridge.")

    payload = dict(payload)
    payload.setdefault("accepted_statuscodes", ["200-299"])
    payload.setdefault("notificationIDList", {})
    payload.setdefault("maxretries", 3)

    sio = socketio.Client(http_session=requests.Session(), ssl_verify=KUMA_SSL_VERIFY, request_timeout=REQUEST_TIMEOUT)
    login_ack: Dict[str, Any] = {}
    add_ack: Dict[str, Any] = {}
    monitor_list_holder: Dict[str, Any] = {}
    notification_list_holder: Dict[str, Any] = {}
    monitor_list_event = Event()
    notification_list_event = Event()

    @sio.on("monitorList")
    def on_monitor_list(data: Any) -> None:
        monitor_list_holder["data"] = data
        monitor_list_event.set()

    @sio.on("notificationList")
    def on_notification_list(data: Any) -> None:
        notification_list_holder["data"] = data
        notification_list_event.set()

    try:
        sio.connect(KUMA_URL, transports=["websocket"], wait_timeout=REQUEST_TIMEOUT)
        login_res = _call_with_step(
            sio,
            "login",
            {"username": KUMA_USERNAME, "password": KUMA_PASSWORD},
            timeout=REQUEST_TIMEOUT,
            step="login",
        )
        login_ack = _normalize_login_ack(login_res)
        if not login_ack.get("ok"):
            raise HTTPException(status_code=401, detail=f"Kuma login failed: {login_ack.get('msg', 'unknown')}")

        token = login_ack.get("token")
        if token:
            token_res = _call_with_step(sio, "loginByToken", token, timeout=REQUEST_TIMEOUT, step="loginByToken")
            token_ack = _normalize_login_ack(token_res)
            if token_ack and not token_ack.get("ok", False):
                raise HTTPException(status_code=401, detail=f"Kuma token login failed: {token_ack.get('msg', 'unknown')}")

        if KUMA_DEFAULT_NOTIFICATION_IDS:
            wanted_ids = []
            for part in KUMA_DEFAULT_NOTIFICATION_IDS.split(","):
                part = part.strip()
                if part.isdigit():
                    wanted_ids.append(int(part))
        else:
            wanted_ids = []

        try:
            _call_with_step(sio, "getSettings", timeout=min(5, REQUEST_TIMEOUT), step="getSettings")
        except Exception:
            pass

        try:
            _call_with_step(sio, "getMonitorList", timeout=min(8, REQUEST_TIMEOUT), step="getMonitorList")
        except Exception:
            pass

        try:
            _call_with_step(sio, "getNotificationList", timeout=min(8, REQUEST_TIMEOUT), step="getNotificationList")
        except Exception:
            pass

        monitor_list_event.wait(timeout=min(2, REQUEST_TIMEOUT))
        existing_monitors = monitor_list_holder.get("data")
        if isinstance(existing_monitors, dict):
            target_name = str(payload.get("name", "")).strip().lower()
            target_url = str(payload.get("url", "")).strip().lower()
            for mid, monitor in existing_monitors.items():
                if not isinstance(monitor, dict):
                    continue
                m_name = str(monitor.get("name", "")).strip().lower()
                m_url = str(monitor.get("url", "")).strip().lower()
                if (target_name and m_name == target_name) or (target_url and m_url == target_url):
                    try:
                        monitor_id = int(monitor.get("id") or mid)
                    except Exception:
                        monitor_id = 0
                    return {"ok": True, "monitorID": monitor_id, "msg": "already-exists"}

        notification_list_event.wait(timeout=min(2, REQUEST_TIMEOUT))
        notifications = notification_list_holder.get("data")
        if isinstance(notifications, list):
            notifications = {
                str(notification.get("id")): _normalize_notification(notification)
                for notification in notifications
                if isinstance(notification, dict) and notification.get("id") is not None
            }
        elif isinstance(notifications, dict):
            notifications = {
                str(notification_id): _normalize_notification(notification)
                for notification_id, notification in notifications.items()
                if isinstance(notification, dict)
            }
        if isinstance(notifications, dict):
            if not wanted_ids:
                wanted_ids = [int(k) for k in notifications.keys() if str(k).isdigit()]
            if wanted_ids:
                payload["notificationIDList"] = {str(i): True for i in wanted_ids}

        add_res = _call_with_step(sio, "add", payload, timeout=REQUEST_TIMEOUT, step="add")
        add_ack = _normalize_login_ack(add_res)
        if not add_ack.get("ok"):
            raise HTTPException(status_code=400, detail=f"Kuma add monitor failed: {add_ack.get('msg', 'unknown')}")

        if KUMA_NOTIFY_ON_CREATE and isinstance(notifications, dict):
            notification_id_list = payload.get("notificationIDList", {})
            if isinstance(notification_id_list, dict):
                for nid, enabled in notification_id_list.items():
                    if not enabled:
                        continue
                    notification = notifications.get(str(nid))
                    if isinstance(notification, dict):
                        try:
                            _call_with_step(sio, "testNotification", notification, timeout=min(8, REQUEST_TIMEOUT), step="testNotification")
                        except Exception:
                            # Non-blocking: monitor creation already succeeded.
                            pass

        # _send_fallback_telegram_on_create(payload, add_ack)  # Disabled: Telegram Alert Testing

        return add_ack
    except socketio.exceptions.ConnectionError as exc:
        raise HTTPException(status_code=502, detail=f"Kuma Socket.IO connection failed: {exc}") from exc
    except socketio.exceptions.TimeoutError as exc:
        raise HTTPException(status_code=504, detail=f"Kuma Socket.IO request timed out: {exc}") from exc
    finally:
        if sio.connected:
            sio.disconnect()


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"ok": "true"}


@app.post("/api/v1/monitors")
async def create_monitor(request: Request) -> JSONResponse:
    raw_body = await request.body()
    timestamp = request.headers.get("X-Kuma-Bridge-Timestamp", "")
    signature = request.headers.get("X-Kuma-Bridge-Signature", "")
    _verify_request(timestamp, signature, raw_body)

    try:
        data = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body.") from exc

    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Missing payload object.")

    last_error = None
    for attempt in range(1, BRIDGE_ADD_RETRIES + 1):
        try:
            result = _bridge_add_monitor(payload)
            return JSONResponse(status_code=200, content=result)
        except HTTPException as exc:
            last_error = exc
            should_retry = exc.status_code in (502, 503, 504)
            if (not should_retry) or attempt >= BRIDGE_ADD_RETRIES:
                raise
            time.sleep(BRIDGE_RETRY_BASE_DELAY * attempt)

    if isinstance(last_error, HTTPException):
        raise last_error

    raise HTTPException(status_code=500, detail="Unexpected bridge failure.")


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("BRIDGE_HOST", "0.0.0.0"), port=int(os.getenv("BRIDGE_PORT", "8788")))
