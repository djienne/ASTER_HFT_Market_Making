# Aster Finance DEX Simple High Frequency Market Making strategy with Python

A simple Python high frequency market making bot for the Aster Finance DEX platform using websockets and Rest API calls.

Referral link to support this work: [https://www.asterdex.com/en/referral/164f81](https://www.asterdex.com/en/referral/164f81) .
Earn 10% rebate on fees (I put maximum for you).

**How it works**: The bot performs "ping-pong" trading by placing buy and sell limit orders around the current market mid-price using a fraction of your available capital in the perpetual futures account. When one order fills (with a significant amount), it immediately places a new order on the opposite side to capture the spread.
Defaults to +-0.6% bid-ask spreads.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Configure API credentials (copy .env.example to .env and edit)
cp .env.example .env

# Run the market maker
python market_maker.py --symbol ASTERUSDT
```

## Configuration

### API Credentials (`.env`)

You need to create both **API** and **Pro API** credentials on Aster Finance as shown below:

![API Management](APIs.png)

```bash
# API V3 (Ethereum-style) - for trading operations (from "Pro API")
API_USER=0x...           # Main account wallet address
API_SIGNER=0x...         # API wallet address
API_PRIVATE_KEY=12...    # API wallet private key

# API V1 (HMAC-style) - for user data streams (from "API")
APIV1_PUBLIC_KEY=...     # API key for user data streams
APIV1_PRIVATE_KEY=...    # API secret for user data streams
```

### Main Parameters (`market_maker.py`)
```python
DEFAULT_SYMBOL = "ETHUSDT"              # Trading pair
DEFAULT_BUY_SPREAD = 0.006              # 0.6% below mid-price
DEFAULT_SELL_SPREAD = 0.006             # 0.6% above mid-price
DEFAULT_BALANCE_FRACTION = 0.2          # Use 20% of balance per order
POSITION_THRESHOLD_USD = 15.0           # position size threshold in USD; below it will consider it is not really an open position,
                                        # above it will switch to sell mode.

# Order Management
ORDER_REFRESH_INTERVAL = 1              # Cancel and replace unfilled orders after 1 second
DEFAULT_PRICE_CHANGE_THRESHOLD = 0.001  # Min price change to cancel and replace order (0.1%),
                                        # to avoid unecessary cancel/open order API calls

# Logging
RELEASE_MODE = True                     # True = prints errors only, False = prints detailed logs (terminal and log file `market_maker.log`),
                                        # to reduce disk and CPU load, specially critical for VPS
```

**Important**: This bot uses **fixed spreads** - you must determine your optimal spread values based on:
- Market volatility and liquidity
- Your risk tolerance and capital requirements
- Backtesting and observation of market conditions

**Key Parameters**:
- **`DEFAULT_BALANCE_FRACTION`**: Controls order sizing - with 0.2 (20%), each order uses 20% of your available balance. Lower values = smaller orders, higher values = larger orders but more risk.
- **`POSITION_THRESHOLD_USD`**: When your net position exceeds this USD value ($15 default), the bot will only place orders to reduce the position (no new position building). Helps manage inventory risk.

**Account Recommendation**: Use a **dedicated account** for the bot to avoid conflicts with manual trading and ensure accurate balance calculations.

**Note**: The default ±0.6% spreads worked well for ASTERUSDT on September 24th, 2024, but:
- Market conditions change over time - these values will likely need adjustment
- Different trading pairs require different spread configurations
- Always test and optimize spreads for your specific market and timeframe

*Future updates may include dynamic spread models (Avellaneda-Stoikov, Cartea-Jaimungal, or other alternatives) for automated spread optimization.*

## Usage

```bash
# Main trading bot
python market_maker.py --symbol ETHUSDT

# Market data utilities
python get_price.py ETHUSDT
python get_trades.py ETHUSDT --limit 50

# Testing
python test_balance.py
python test_account_balance.py

# Optional monitoring
python data_collector.py
python websocket_user_data.py
```

## Docker Deployment

```bash
# Build and run
docker-compose up --build

# Run specific service
docker-compose up market-maker

# Background mode
docker-compose up -d

# View logs
docker-compose logs -f market-maker
```

**Change trading symbol**: Edit the `command` line in `docker-compose.yml`
```yaml
market-maker:
  command: python market_maker.py --symbol ETHUSDT  # Default: ASTERUSDT
```

## Performance Recommendations

**Latency is critical** for market making success. For optimal performance:

- **Recommended**: AWS Tokyo region (ap-northeast-1) with dedicated t3.small instance
- **Why**: Close proximity to Aster Finance servers reduces order placement latency
- **Alternative regions**: Singapore, Hong Kong for Asia-Pacific trading
- Avoid shared/burstable instances during active trading periods

**Order Update Frequency**: The more frequently you can update limit orders, the better your edge:
- Default: 1 second (`ORDER_REFRESH_INTERVAL = 1`)
- **Optimization**: Try reducing this value to increase update frequency
- **Caution**: Don't go too low or you'll hit AsterDEX's rate limits
- Test incrementally (e.g., 0.5s → 0.3s → 0.2s) to find your optimal balance

## File Structure

```
ASTER/
├── market_maker.py                  # Main trading bot
├── api_client.py                    # API client for Aster Finance
├── .env                            # API credentials (required)
├── requirements.txt                # Python dependencies
├── get_price.py                    # Price utility
├── get_trades.py                   # Trade data utility
├── test_*.py                       # Testing scripts
├── websocket_*.py                  # WebSocket monitoring (optional)
├── data_collector.py               # Data collection (optional)
└── docker-compose.yml             # Docker setup (optional)
```

---

**⚠️ Risk Warning**: This is trading software that can lose money. Always test with small amounts and understand the risks involved in automated cryptocurrency trading.
