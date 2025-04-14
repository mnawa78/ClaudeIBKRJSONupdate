import os
import json
import time
import requests
import logging
from flask import Flask, request, render_template, jsonify, abort
from flask_socketio import SocketIO, emit
from datetime import datetime

# Configure enhanced logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
)

app = Flask(__name__)
# Use environment variable for secret key with a fallback
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY", 'dev_secret_key')

# Improved SocketIO configuration with reconnection settings
socketio = SocketIO(
    app, 
    async_mode='gevent', 
    cors_allowed_origins='*',
    ping_timeout=60,    # Longer ping timeout
    ping_interval=25,   # More frequent ping to detect connection issues
    reconnection=True,  # Enable reconnection
    reconnection_attempts=5,  # Attempt to reconnect 5 times
    reconnection_delay=1,     # Start with 1 second delay
    reconnection_delay_max=10 # Max 10 seconds between attempts
)

# Configuration from Heroku config vars
CONNECTOR_URL = os.environ.get("CONNECTOR_URL")
CONNECTOR_API_KEY = os.environ.get("CONNECTOR_API_KEY")
# Timeout configuration - can be adjusted via environment variables
DEFAULT_TIMEOUT = int(os.environ.get("DEFAULT_TIMEOUT", 60))

# Global state tracking
connection_state = {"connected": False, "last_heartbeat": None}

@app.route('/')
def index():
    """Serve the index page with proper webhook URL."""
    # Get the base URL, defaulting to HTTPS if we're on Heroku
    is_heroku = 'DYNO' in os.environ
    
    if is_heroku:
        # Force HTTPS on Heroku
        base_url = request.host_url.replace('http://', 'https://')
    else:
        # Use whatever protocol is being used
        base_url = request.host_url
        
    webhook_url = base_url + "webhook"
    app.logger.info(f"Serving index page. Webhook URL: {webhook_url}")
    return render_template('index.html', webhook_url=webhook_url)

def send_backend_request(endpoint, method="POST", json_data=None, form_data=None, timeout=None):
    """Centralized function to handle backend requests with proper error handling"""
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    
    url = f"{CONNECTOR_URL}/{endpoint}"
    headers = {}
    if CONNECTOR_API_KEY:
        headers['X-API-KEY'] = CONNECTOR_API_KEY

    app.logger.info(f"Sending {method} request to backend: {url}")
    
    try:
        if method.upper() == "POST":
            if json_data:
                response = requests.post(url, json=json_data, headers=headers, timeout=timeout)
            elif form_data:
                response = requests.post(url, data=form_data, headers=headers, timeout=timeout)
            else:
                response = requests.post(url, headers=headers, timeout=timeout)
        else:
            response = requests.get(url, headers=headers, timeout=timeout)
            
        response.raise_for_status()
        return response.json(), None
    except requests.exceptions.Timeout:
        error_msg = f"Request to backend timed out after {timeout}s."
        app.logger.error(error_msg)
        return None, {"error": error_msg, "status_code": 504}
    except requests.exceptions.ConnectionError:
        error_msg = "Failed to connect to backend server. Please check if it's running."
        app.logger.error(error_msg)
        return None, {"error": error_msg, "status_code": 502}
    except requests.exceptions.RequestException as e:
        error_msg = f"Request failed: {str(e)}"
        status_code = getattr(e.response, 'status_code', 500) if hasattr(e, 'response') else 500
        app.logger.error(error_msg)
        return None, {"error": error_msg, "status_code": status_code}
    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        app.logger.error(error_msg, exc_info=True)
        return None, {"error": error_msg, "status_code": 500}

@app.route('/connect', methods=['POST'])
def connect_route():
    app.logger.info("Received request on /connect route")
    data = request.form
    payload = {
        "ip": data.get("ip"),
        "user_id": data.get("user_id"),
        "account_type": data.get("account_type")
    }
    
    # Check for missing fields
    missing_fields = [field for field, value in payload.items() if not value]
    if missing_fields:
        error_msg = f"Missing required fields: {', '.join(missing_fields)}"
        app.logger.warning(f"Connection request missing fields: {error_msg}")
        socketio.emit('connection_status', {"success": False, "message": error_msg})
        return jsonify({"success": False, "message": error_msg}), 400
    
    # Make the backend request
    result, error = send_backend_request("connect", json_data=payload)
    
    if error:
        socketio.emit('connection_status', {"success": False, "message": error['error']})
        return jsonify({"success": False, "message": error['error']}), error['status_code']
    
    # If successful, update connection state and start heartbeat
    if result and result.get('success'):
        connection_state["connected"] = True
        connection_state["last_heartbeat"] = datetime.now()
        socketio.emit('connection_status', result)
        # Schedule a heartbeat check
        socketio.start_background_task(target=heartbeat_check)
    else:
        socketio.emit('connection_status', result)
    
    return jsonify(result)

@app.route('/disconnect', methods=['POST'])
def disconnect_route():
    app.logger.info("Received request on /disconnect route")
    
    # Make the backend request
    result, error = send_backend_request("disconnect")
    
    if error:
        socketio.emit('connection_status', {"success": False, "message": error['error']})
        return jsonify({"success": False, "message": error['error']}), error['status_code']
    
    # Update connection state
    connection_state["connected"] = False
    socketio.emit('connection_status', result)
    
    return jsonify(result)

@app.route('/webhook', methods=['POST'])
def webhook_receiver():
    data = request.json
    if not data:
        app.logger.warning("Received invalid data on /webhook")
        socketio.emit('webhook_error', {"message": "Invalid webhook data received"})
        return "Invalid data", 400

    app.logger.info(f"[WEBHOOK] Received data: {json.dumps(data)}")
    socketio.emit('new_webhook', data)

    # Save order in memory in case we need to retry
    order_id = data.get('ORDER_ID', str(time.time()))
    socketio.emit('order_status', {
        "order_id": order_id,
        "status": "received",
        "message": "Order received, forwarding to backend"
    })

    # Forward to backend
    result, error = send_backend_request("order", json_data=data)
    
    if error:
        error_msg = f"Error forwarding order: {error['error']}"
        app.logger.error(error_msg)
        socketio.emit('order_status', {
            "order_id": order_id,
            "status": "failed",
            "message": error_msg
        })
        return error_msg, error['status_code']
    
    # Success
    socketio.emit('order_status', {
        "order_id": order_id,
        "status": "processed",
        "message": "Order successfully processed by backend",
        "details": result
    })
    
    return "Webhook received and processed successfully", 200

@app.route('/status', methods=['GET'])
def status():
    """Check connection status with backend"""
    if not CONNECTOR_URL:
        return jsonify({
            "frontend": "running",
            "backend_configured": False,
            "message": "Backend URL not configured"
        })
    
    try:
        result, error = send_backend_request("", method="GET", timeout=5)
        backend_status = "running" if not error else "unreachable"
        
        return jsonify({
            "frontend": "running",
            "backend": backend_status,
            "backend_configured": True,
            "connected_to_ibkr": connection_state["connected"],
            "last_heartbeat": connection_state["last_heartbeat"].isoformat() if connection_state["last_heartbeat"] else None
        })
    except Exception as e:
        app.logger.error(f"Status check error: {str(e)}")
        return jsonify({
            "frontend": "running",
            "backend": "error",
            "backend_configured": True,
            "connected_to_ibkr": connection_state["connected"],
            "error": str(e)
        })

# Add a heartbeat route to check backend connectivity
@app.route('/heartbeat', methods=['GET'])
def heartbeat():
    """Simple endpoint to check server heartbeat"""
    return jsonify({"status": "alive", "timestamp": datetime.now().isoformat()})

def heartbeat_check():
    """Background task to periodically check backend connectivity"""
    while connection_state["connected"]:
        try:
            result, error = send_backend_request("heartbeat", method="GET", timeout=5)
            if not error and result:
                connection_state["last_heartbeat"] = datetime.now()
                socketio.emit('heartbeat', {
                    "status": "alive",
                    "timestamp": connection_state["last_heartbeat"].isoformat()
                })
            else:
                socketio.emit('heartbeat', {
                    "status": "failed",
                    "error": error['error'] if error else "Unknown error"
                })
        except Exception as e:
            app.logger.error(f"Heartbeat check failed: {str(e)}")
            socketio.emit('heartbeat', {
                "status": "error",
                "error": str(e)
            })
        
        # Wait 30 seconds before next check
        socketio.sleep(30)

# Socket.IO event handlers
@socketio.on('connect')
def socket_connect():
    app.logger.info(f"Socket.IO client connected: {request.sid}")
    # Send initial connection state to newly connected clients
    emit('connection_status', {
        "success": connection_state["connected"],
        "message": "Connected to IBKR" if connection_state["connected"] else "Not connected to IBKR"
    })

@socketio.on('disconnect')
def socket_disconnect():
    app.logger.info(f"Socket.IO client disconnected: {request.sid}")

@socketio.on_error()
def error_handler(e):
    app.logger.error(f"Socket.IO error: {str(e)}", exc_info=True)

# Add a backend heartbeat endpoint
@app.route('/backend_heartbeat', methods=['GET'])
def backend_heartbeat():
    """Check if backend is responsive"""
    try:
        # Try to connect to backend with a short timeout
        result, error = send_backend_request("heartbeat", method="GET", timeout=5)
        if error:
            return jsonify({
                "status": "unreachable",
                "error": error['error']
            }), 503
        return jsonify({
            "status": "reachable",
            "backend_response": result
        })
    except Exception as e:
        app.logger.error(f"Backend heartbeat check failed: {str(e)}")
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    use_debug = os.environ.get("FLASK_DEBUG", "False").lower() == "true"
    app.logger.info(f"Starting Frontend Flask app on 0.0.0.0:{port} (Debug Mode: {use_debug})")
    socketio.run(app, host="0.0.0.0", port=port, debug=use_debug)
