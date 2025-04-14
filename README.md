# IBKR TradingView Connector

This application connects TradingView webhook alerts to Interactive Brokers for automated trading. It consists of two components:

1. **Frontend UI** (this repository) - Deployed on Heroku
   - Provides a web interface for connecting to IBKR
   - Receives webhook messages from TradingView
   - Forwards orders to the backend API

2. **Backend Service** - Deployed on a DigitalOcean droplet
   - Connects to Interactive Brokers TWS/Gateway API
   - Processes order requests
   - Calculates order sizes based on strategy position and current holdings

## Frontend Setup (Heroku)

### Prerequisites

- Heroku account
- Heroku CLI installed
- Git

### Local Development

1. Clone this repository:
   ```
   git clone <repository-url>
   cd ibkr-tradingview-connector
   ```

2. Create a virtual environment:
   ```
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

4. Create a `.env` file with your configuration (see `.env.example`)

5. Run the development server:
   ```
   python app.py
   ```

### Heroku Deployment

1. Make sure you're logged in to Heroku CLI:
   ```
   heroku login
   ```

2. Create a new Heroku app:
   ```
   heroku create <your-app-name>
   ```

3. Set configuration variables:
   ```
   heroku config:set CONNECTOR_URL=<your-backend-url> --app <your-app-name>
   heroku config:set CONNECTOR_API_KEY=<your-api-key> --app <your-app-name>
   ```

4. Deploy to Heroku:
   ```
   git push heroku main
   ```

Alternatively, use the provided deployment script:
```
chmod +x deploy.sh
./deploy.sh <your-app-name>
```

## Using the Application

1. Access your Heroku application URL
2. Enter your TWS IP address and client ID
3. Connect to IBKR
4. Copy the webhook URL provided
5. Configure TradingView alerts to send JSON messages to your webhook URL

### TradingView Alert Format

```json
{
    "TICKER": "ES!",
    "ACTION": "BUY",
    "NEW_STRATEGY_POSITION": "2",
    "STRATEGY_NAME": "My Strategy"
}
```

## Frontend Features

- Real-time order tracking with Socket.IO
- Connection status monitoring
- Heartbeat checking with backend
- Order execution status updates
- Error handling and recovery

## Troubleshooting

1. **Connection Issues**
   - Ensure TWS/Gateway is running and API connections are enabled
   - Check that the IP address is correct
   - Verify client ID is not in use

2. **Order Execution Problems**
   - Check backend logs for specific errors
   - Ensure your IBKR account is funded and enabled for trading
   - Verify ticker symbols are correct

3. **Socket.IO Disconnections**
   - The application will automatically attempt to reconnect
   - If persistent, try refreshing the page or checking your internet connection

## Architecture

```
┌──────────────────┐      ┌───────────────────┐      ┌──────────────┐
│   TradingView    │─────▶│  Frontend (Heroku)│─────▶│   Backend    │
│   Webhook Alert  │      │  Flask + Socket.IO │      │  (DigitalOcean)│
└──────────────────┘      └───────────────────┘      └───────┬──────┘
                                                            │
                                                            ▼
                                                     ┌──────────────┐
                                                     │ Interactive  │
                                                     │ Brokers (TWS)│
                                                     └──────────────┘
```

## License

MIT
