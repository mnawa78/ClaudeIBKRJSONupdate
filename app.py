# Improved with Enhanced Connection Handling uploaded to mnawa78/ClaudeFrontendworkingcopy + json loads update2
# Specifically checks for TradingView's JSON format (string wrapped in quotes with double quotes inside)
# Uses a separate parsing path for TradingView format vs regular JSON


import os
import json
import time
import requests
import logging
from flask import Flask, request, render_template, jsonify, abort
from flask_socketio import SocketIO, emit, disconnect
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
# Max consecutive heartbeat failures before considering disconnected
MAX_HEARTBEAT_FAILURES = int(os.environ.get("MAX_HEARTBEAT_FAILURES", 3))
# Store the last connection parameters for potential auto-reconnect
last_connection_params = {}

# Global state tracking
connection_state = {
    "connected": False, 
    "last_heartbeat": None,
    "heartbeat_failures": 0,
    "reconnect_in_progress": False
}

@app.route('/')
def index():
    webhook_url = request.host_url + "webhook"
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
    
    # Store connection parameters for potential auto-reconnect
    global last_connection_params
    last_connection_params = payload.copy()
    
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
        connection_state["heartbeat_failures"] = 0
        connection_state["reconnect_in_progress"] = False
        socketio.emit('connection_status', result)
        # Start the enhanced heartbeat check in a background task
        socketio.start_background_task(target=start_heartbeat_check)
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
    data = None
    content_type = request.headers.get('Content-Type', '')
    
    # Log incoming request details
    app.logger.info(f"Webhook request received. Content-Type: {content_type}")
    
    # Try Flask's built-in JSON parser first
    if 'application/json' in content_type:
        try:
            data = request.json
            app.logger.info("Successfully parsed JSON using Flask's parser")
        except Exception as e:
            app.logger.warning(f"Failed to parse JSON with Flask's parser: {str(e)}")
    
    # Handle raw data, particularly for TradingView
    if not data and request.data:
        raw_data_str = request.data.decode('utf-8').strip()
        app.logger.info(f"Raw data received: {raw_data_str[:100]}...")  # Log first 100 chars
        
        # Check if it looks like a TradingView format (starts with quote, has double quotes inside)
        if raw_data_str.startswith('"') and '""' in raw_data_str:
            app.logger.info("Detected TradingView format - handling specifically")
            try:
                # Remove outer quotes
                if raw_data_str.startswith('"') and raw_data_str.endswith('"'):
                    raw_data_str = raw_data_str[1:-1]
                
                # Replace double quotes with single quotes
                raw_data_str = raw_data_str.replace('""', '"')
                
                # Parse the cleaned string
                data = json.loads(raw_data_str)
                app.logger.info("Successfully parsed TradingView format JSON")
            except Exception as e:
                app.logger.error(f"Failed to parse TradingView format: {str(e)}")
        else:
            # Try parsing the raw data directly
            try:
                data = json.loads(raw_data_str)
                app.logger.info("Successfully parsed raw data as JSON")
            except Exception as e:
                app.logger.error(f"Failed to parse raw data: {str(e)}")
    
    # If we still have no data, report error
    if not data:
        app.logger.warning("Could not parse webhook data as valid JSON")
        socketio.emit('webhook_error', {"message": "Invalid webhook data received"})
        return "Invalid data", 400
    
    # At this point we have valid JSON data
    app.logger.info(f"[WEBHOOK] Processed data: {json.dumps(data)}")
    socketio.emit('new_webhook', data)

    # Continue with your existing code for order handling
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
            "last_heartbeat": connection_state["last_heartbeat"].isoformat() if connection_state["last_heartbeat"] else None,
            "heartbeat_failures": connection_state["heartbeat_failures"],
            "reconnect_in_progress": connection_state["reconnect_in_progress"]
        })
    except Exception as e:
        app.logger.error(f"Status check error: {str(e)}")
        return jsonify({
            "frontend": "running",
            "backend": "error",
            "backend_configured": True,
            "connected_to_ibkr": connection_state["connected"],
            "heartbeat_failures": connection_state["heartbeat_failures"],
            "error": str(e)
        })

# Add a heartbeat route to check server heartbeat
@app.route('/heartbeat', methods=['GET'])
def heartbeat():
    """Simple endpoint to check server heartbeat"""
    return jsonify({"status": "alive", "timestamp": datetime.now().isoformat()})

def heartbeat_check():
    """Background task to periodically check backend connectivity with enhanced error handling"""
    while connection_state["connected"]:
        try:
            result, error = send_backend_request("heartbeat", method="GET", timeout=5)
            if not error and result:
                # Reset failure counter on successful heartbeat
                connection_state["heartbeat_failures"] = 0
                connection_state["last_heartbeat"] = datetime.now()
                socketio.emit('heartbeat', {
                    "status": "alive",
                    "timestamp": connection_state["last_heartbeat"].isoformat()
                })
            else:
                connection_state["heartbeat_failures"] += 1
                app.logger.warning(f"Heartbeat failure #{connection_state['heartbeat_failures']}: {error['error'] if error else 'Unknown error'}")
                
                socketio.emit('heartbeat', {
                    "status": "warning",
                    "error": error['error'] if error else "Unknown error",
                    "consecutive_failures": connection_state["heartbeat_failures"],
                    "max_failures": MAX_HEARTBEAT_FAILURES
                })
                
                # Only mark as disconnected after multiple consecutive failures
                if connection_state["heartbeat_failures"] >= MAX_HEARTBEAT_FAILURES:
                    app.logger.error(f"Maximum heartbeat failures reached ({MAX_HEARTBEAT_FAILURES}). Marking as temporarily disconnected.")
                    # We do NOT set connection_state["connected"] = False here
                    # Instead, we'll let the reconnection logic handle it
                    break
        except Exception as e:
            connection_state["heartbeat_failures"] += 1
            app.logger.error(f"Heartbeat check exception: {str(e)}")
            
            socketio.emit('heartbeat', {
                "status": "error",
                "error": str(e),
                "consecutive_failures": connection_state["heartbeat_failures"],
                "max_failures": MAX_HEARTBEAT_FAILURES
            })
            
            if connection_state["heartbeat_failures"] >= MAX_HEARTBEAT_FAILURES:
                app.logger.error(f"Maximum heartbeat failures reached ({MAX_HEARTBEAT_FAILURES}). Marking as temporarily disconnected.")
                # Again, we do NOT set connection_state["connected"] = False here
                break
        
        # Wait 30 seconds before next check
        socketio.sleep(30)

def start_heartbeat_check():
    """
    Enhanced heartbeat check with auto-recovery capabilities.
    This runs in a continuous loop that handles both initial connection monitoring
    and attempts to recover from temporary disconnections.
    """
    while True:
        # If we're connected, run the regular heartbeat check
        if connection_state["connected"]:
            heartbeat_check()
            
            # If we exit the heartbeat check but still think we're connected,
            # it means we hit the max failures but haven't actually marked as disconnected yet
            if connection_state["connected"] and connection_state["heartbeat_failures"] >= MAX_HEARTBEAT_FAILURES:
                app.logger.info("Heartbeat check failed but connection may still be active. Attempting verification...")
                
                # Instead of immediately marking as disconnected, verify the connection
                connection_state["reconnect_in_progress"] = True
                socketio.emit('connection_status', {
                    "success": True,
                    "message": "Verifying connection status...",
                    "verifying": True
                })
                
                # Try a deeper connection check to see if we're really disconnected
                try:
                    # Check if backend is still alive
                    backend_result, backend_error = send_backend_request("", method="GET", timeout=10)
                    
                    if not backend_error:
                        # Backend is responding, try to check actual connection status
                        verify_result, verify_error = send_backend_request("verify_connection", timeout=10)
                        
                        if not verify_error and verify_result and verify_result.get('connected', False):
                            # We're actually still connected! Reset the heartbeat failure counter
                            app.logger.info("Connection verification successful - we're still connected!")
                            connection_state["heartbeat_failures"] = 0
                            connection_state["last_heartbeat"] = datetime.now()
                            connection_state["reconnect_in_progress"] = False
                            
                            socketio.emit('connection_status', {
                                "success": True,
                                "message": "Connection verified successfully",
                                "verified": True
                            })
                            
                            # Continue with normal heartbeat checks
                            continue
                        else:
                            # Backend says we're not connected
                            app.logger.warning("Backend reports we are not connected. Trying to reconnect...")
                    else:
                        app.logger.warning(f"Backend verification failed with error: {backend_error['error']}")
                        
                except Exception as e:
                    app.logger.error(f"Connection verification failed: {str(e)}")
                
                # If we get here, we need to try reconnecting
                try_reconnect()
        
        # If we're not connected or reconnection is in progress, wait before checking again
        socketio.sleep(30)
        
        # After waiting, check if we should attempt auto-reconnect 
        if not connection_state["connected"] and not connection_state["reconnect_in_progress"]:
            try_reconnect()

def try_reconnect():
    """Attempt to reconnect to backend using stored connection parameters"""
    global last_connection_params
    
    # Mark that we're attempting to reconnect
    connection_state["reconnect_in_progress"] = True
    
    # Notify UI that we're attempting to reconnect
    socketio.emit('connection_status', {
        "success": False,
        "message": "Connection lost. Attempting to reconnect...",
        "reconnecting": True
    })
    
    # Check if backend is accessible before attempting reconnect
    app.logger.info("Checking if backend is accessible before reconnect attempt...")
    try:
        backend_check, error = send_backend_request("", method="GET", timeout=5)
        
        if error:
            app.logger.error(f"Backend not accessible for reconnect: {error['error']}")
            connection_state["reconnect_in_progress"] = False
            socketio.emit('connection_status', {
                "success": False,
                "message": f"Reconnect failed: Backend not accessible ({error['error']})",
                "reconnecting": False
            })
            return
            
        # If we have stored connection parameters, try to reconnect
        if last_connection_params and all(last_connection_params.values()):
            app.logger.info(f"Attempting automatic reconnect with stored parameters: {json.dumps(last_connection_params)}")
            
            # Attempt to reconnect using the stored parameters
            result, error = send_backend_request("connect", json_data=last_connection_params)
            
            if error:
                app.logger.error(f"Auto-reconnect failed: {error['error']}")
                connection_state["reconnect_in_progress"] = False
                socketio.emit('connection_status', {
                    "success": False,
                    "message": f"Auto-reconnect failed: {error['error']}",
                    "reconnecting": False
                })
            elif result and result.get('success'):
                app.logger.info("Auto-reconnect successful!")
                connection_state["connected"] = True
                connection_state["last_heartbeat"] = datetime.now()
                connection_state["heartbeat_failures"] = 0
                connection_state["reconnect_in_progress"] = False
                
                socketio.emit('connection_status', {
                    "success": True,
                    "message": "Auto-reconnect successful",
                    "reconnected": True
                })
            else:
                app.logger.warning(f"Auto-reconnect received unexpected response: {json.dumps(result)}")
                connection_state["reconnect_in_progress"] = False
                socketio.emit('connection_status', {
                    "success": False,
                    "message": "Auto-reconnect failed with unexpected response from backend",
                    "reconnecting": False,
                    "details": result
                })
        else:
            app.logger.warning("No stored connection parameters available for auto-reconnect")
            connection_state["reconnect_in_progress"] = False
            socketio.emit('connection_status', {
                "success": False,
                "message": "Cannot auto-reconnect: No stored connection parameters",
                "reconnecting": False
            })
            
    except Exception as e:
        app.logger.error(f"Auto-reconnect attempt failed with exception: {str(e)}")
        connection_state["reconnect_in_progress"] = False
        socketio.emit('connection_status', {
            "success": False,
            "message": f"Auto-reconnect failed with error: {str(e)}",
            "reconnecting": False
        })

# Socket.IO event handlers
@socketio.on('connect')
def socket_connect():
    app.logger.info(f"Socket.IO client connected: {request.sid}")
    # Send initial connection state to newly connected clients
    emit('connection_status', {
        "success": connection_state["connected"],
        "message": "Connected to IBKR" if connection_state["connected"] else "Not connected to IBKR",
        "reconnect_in_progress": connection_state["reconnect_in_progress"],
        "heartbeat_failures": connection_state["heartbeat_failures"]
    })

@socketio.on('disconnect')
def socket_disconnect():
    app.logger.info(f"Socket.IO client disconnected: {request.sid}")

@socketio.on('force_reconnect')
def force_reconnect():
    """Handle manual reconnect request from client"""
    app.logger.info(f"Manual reconnect requested by client: {request.sid}")
    if not connection_state["reconnect_in_progress"]:
        socketio.start_background_task(target=try_reconnect)
    else:
        emit('connection_status', {
            "success": False,
            "message": "Reconnection already in progress",
            "reconnecting": True
        })

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

@app.route('/reset_heartbeat_failures', methods=['POST'])
def reset_heartbeat_failures():
    """Manually reset heartbeat failures counter"""
    connection_state["heartbeat_failures"] = 0
    app.logger.info("Heartbeat failures counter manually reset")
    return jsonify({
        "success": True,
        "message": "Heartbeat failures counter reset",
        "heartbeat_failures": connection_state["heartbeat_failures"]
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    use_debug = os.environ.get("FLASK_DEBUG", "False").lower() == "true"
    app.logger.info(f"Starting Frontend Flask app on 0.0.0.0:{port} (Debug Mode: {use_debug})")
    socketio.run(app, host="0.0.0.0", port=port, debug=use_debug)
