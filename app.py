from gevent import monkey; monkey.patch_all()

"""Flask‑SocketIO front‑end for IBKR connector — revised 2025‑04‑14

Key fixes
----------
* Apply gevent monkey‑patch **before** any other import (SocketIO requires it).
* Remove client‑side `reconnection_*` kwargs from server constructor.
* Accept both JSON and form payloads in `/connect` (and future endpoints).
* Keep ping/pong settings so the browser can detect stale connections.
* Minor cleanup of unused imports.
"""

import os
import json
import time
import requests
import logging
from datetime import datetime

from flask import Flask, request, render_template, jsonify
from flask_socketio import SocketIO, emit

# ---------------------------------------------------------------------------
# Basic app + logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev_secret_key")

# ---------------------------------------------------------------------------
# Socket.IO (gevent) — **no** reconnection_* kwargs here; those live on the JS side
# ---------------------------------------------------------------------------
socketio = SocketIO(
    app,
    async_mode="gevent",
    cors_allowed_origins="*",
    ping_timeout=60,
    ping_interval=25,
)

# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------
CONNECTOR_URL = os.environ.get("CONNECTOR_URL", "http://localhost:8000")
CONNECTOR_API_KEY = os.environ.get("CONNECTOR_API_KEY")
DEFAULT_TIMEOUT = int(os.environ.get("DEFAULT_TIMEOUT", 60))

# Track whether we are connected to the IBKR connector
connection_state = {"connected": False, "last_heartbeat": None}

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def send_backend_request(endpoint: str, *, method: str = "POST", json_data=None, form_data=None, timeout=None):
    """Centralised HTTP helper with consistent error structure."""
    timeout = timeout or DEFAULT_TIMEOUT
    url = f"{CONNECTOR_URL}/{endpoint}"
    headers = {"X-API-KEY": CONNECTOR_API_KEY} if CONNECTOR_API_KEY else {}

    app.logger.info("Sending %s request to backend: %s", method, url)

    try:
        if method.upper() == "POST":
            response = requests.post(
                url,
                json=json_data,
                data=form_data,
                headers=headers,
                timeout=timeout,
            )
        else:
            response = requests.get(url, headers=headers, timeout=timeout)

        response.raise_for_status()
        return response.json(), None

    except requests.exceptions.Timeout:
        msg = f"Request to backend timed out after {timeout}s."
        app.logger.error(msg)
        return None, {"error": msg, "status_code": 504}

    except requests.exceptions.ConnectionError:
        msg = "Failed to connect to backend server. Please check if it's running."
        app.logger.error(msg)
        return None, {"error": msg, "status_code": 502}

    except requests.exceptions.RequestException as exc:
        msg = f"Request failed: {exc}"
        status_code = getattr(exc.response, "status_code", 500) if hasattr(exc, "response") else 500
        app.logger.error(msg)
        return None, {"error": msg, "status_code": status_code}

    except Exception as exc:  # pylint: disable=broad-except
        msg = f"Unexpected error: {exc}"
        app.logger.error(msg, exc_info=True)
        return None, {"error": msg, "status_code": 500}

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Serve the SPA/HTML page with the correct webhook URL injected."""
    base_url = request.host_url.replace("http://", "https://") if "DYNO" in os.environ else request.host_url
    webhook_url = base_url + "webhook"
    app.logger.info("Serving index page. Webhook URL: %s", webhook_url)
    return render_template("index.html", webhook_url=webhook_url)


@app.route("/connect", methods=["POST"])
def connect_route():
    """Connect the UI to the IBKR connector."""
    data = request.get_json(silent=True) or request.form  # <- accepts JSON or form

    payload = {
        "ip": data.get("ip"),
        "user_id": data.get("user_id"),
        "account_type": data.get("account_type"),
    }

    missing = [k for k, v in payload.items() if not v]
    if missing:
        msg = f"Missing required fields: {', '.join(missing)}"
        app.logger.warning(msg)
        socketio.emit("connection_status", {"success": False, "message": msg})
        return jsonify({"success": False, "message": msg}), 400

    result, error = send_backend_request("connect", json_data=payload)

    if error:
        socketio.emit("connection_status", {"success": False, "message": error["error"]})
        return jsonify({"success": False, "message": error["error"]}), error["status_code"]

    if result.get("success"):
        connection_state.update({"connected": True, "last_heartbeat": datetime.now()})
        socketio.emit("connection_status", result)
        socketio.start_background_task(target=heartbeat_check)
    else:
        socketio.emit("connection_status", result)

    return jsonify(result)


@app.route("/disconnect", methods=["POST"])
def disconnect_route():
    app.logger.info("Received request on /disconnect route")
    result, error = send_backend_request("disconnect")

    if error:
        socketio.emit("connection_status", {"success": False, "message": error["error"]})
        return jsonify({"success": False, "message": error["error"]}), error["status_code"]

    connection_state["connected"] = False
    socketio.emit("connection_status", result)
    return jsonify(result)


@app.route("/webhook", methods=["POST"])
def webhook_receiver():
    data = request.get_json(silent=True)
    if not data:
        app.logger.warning("Received invalid data on /webhook")
        socketio.emit("webhook_error", {"message": "Invalid webhook data received"})
        return "Invalid data", 400

    app.logger.info("[WEBHOOK] Received data: %s", json.dumps(data))

    # Notify all clients
    socketio.emit("new_webhook", data, broadcast=True)

    order_id = data.get("ORDER_ID", f"order-{int(time.time())}")
    socketio.emit(
        "order_status",
        {
            "order_id": order_id,
            "status": "received",
            "message": "Order received, forwarding to backend",
            "data": data,
        },
        broadcast=True,
    )

    headers = {"X-API-KEY": CONNECTOR_API_KEY} if CONNECTOR_API_KEY else {}
    backend_order_url = f"{CONNECTOR_URL}/order"
    app.logger.info("[WEBHOOK] Forwarding order to backend: %s", backend_order_url)

    try:
        response = requests.post(backend_order_url, json=data, headers=headers, timeout=60)
        try:
            response_data = response.json()
        except ValueError:
            response_data = {"message": f"Backend response: {response.text}"}

        response_data.setdefault("order_id", order_id)
        app.logger.info("[WEBHOOK] Backend response: Status=%s, Data=%s", response.status_code, response_data)

        socketio.emit(
            "order_status",
            {
                "order_id": response_data["order_id"],
                "status": "processed" if response.ok else "failed",
                "message": response_data.get("message", "Order processed"),
                "details": response_data,
                "data": data,
            },
            broadcast=True,
        )
        return "Webhook received and processed", 200

    except Exception as exc:  # pylint: disable=broad-except
        msg = f"[WEBHOOK] Error processing order: {exc}"
        app.logger.error(msg, exc_info=True)
        socketio.emit(
            "order_status",
            {
                "order_id": order_id,
                "status": "failed",
                "message": msg,
                "data": data,
            },
            broadcast=True,
        )
        return "Error processing webhook", 500


@app.route("/status", methods=["GET"])
def status():
    """Lightweight health check for uptime monitors."""
    if not CONNECTOR_URL:
        return jsonify({
            "frontend": "running",
            "backend_configured": False,
            "message": "Backend URL not configured",
        })

    result, error = send_backend_request("", method="GET", timeout=5)
    backend_status = "running" if not error else "unreachable"

    return jsonify({
        "frontend": "running",
        "backend": backend_status,
        "backend_configured": True,
        "connected_to_ibkr": connection_state["connected"],
        "last_heartbeat": connection_state["last_heartbeat"].isoformat() if connection_state["last_heartbeat"] else None,
    })


@app.route("/heartbeat", methods=["GET"])
def heartbeat():
    return jsonify({"status": "alive", "timestamp": datetime.now().isoformat()})

# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

def heartbeat_check():
    while connection_state["connected"]:
        try:
            result, error = send_backend_request("heartbeat", method="GET", timeout=5)
            if not error and result:
                connection_state["last_heartbeat"] = datetime.now()
                socketio.emit("heartbeat", {"status": "alive", "timestamp": connection_state["last_heartbeat"].isoformat()})
            else:
                socketio.emit("heartbeat", {"status": "failed", "error": error["error"] if error else "Unknown error"})
        except Exception as exc:  # pylint: disable=broad-except
            app.logger.error("Heartbeat check failed: %s", exc)
            socketio.emit("heartbeat", {"status": "error", "error": str(exc)})
        socketio.sleep(30)

# ---------------------------------------------------------------------------
# Socket.IO event handlers
# ---------------------------------------------------------------------------

@socketio.on("connect")
def socket_connect():
    app.logger.info("Socket.IO client connected: %s", request.sid)
    emit(
        "connection_status",
        {
            "success": connection_state["connected"],
            "message": "Connected to IBKR" if connection_state["connected"] else "Not connected to IBKR",
        },
    )


@socketio.on("disconnect")
def socket_disconnect():
    app.logger.info("Socket.IO client disconnected: %s", request.sid)


@socketio.on_error()
def error_handler(exc):
    app.logger.error("Socket.IO error: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

@app.route("/backend_heartbeat", methods=["GET"])
def backend_heartbeat():
    result, error = send_backend_request("heartbeat", method="GET", timeout=5)
    if error:
        return jsonify({"status": "unreachable", "error": error["error"]}), 503
    return jsonify({"status": "reachable", "backend_response": result})


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.logger.info("Starting Flask‑SocketIO app on 0.0.0.0:%s (debug=%s)", port, debug_mode)
    socketio.run(app, host="0.0.0.0", port=port, debug=debug_mode)
