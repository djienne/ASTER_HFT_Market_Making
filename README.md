# Aster Finance DEX Simple Market Making strategy with Python

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

# find and copy/paste your API keys (see below)

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
                                        # above it will switch to sell mode (like if it was fully filled).

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

**Important info about Parameters**:
- **`DEFAULT_BALANCE_FRACTION`**: Controls order sizing - with 0.2 (20%), each order uses 20% of your available balance. Lower values = smaller orders, higher values = larger orders but more risk.
- **`POSITION_THRESHOLD_USD`**: When your net position exceeds this USD value ($15 default), the bot will only place orders to reduce the position (no new position building).
- **`LEVERAGE`**: Leverage to use to allow position sizes larger than the account available capital (margin). BE VERY CAREFUL WITH THAT. RECOMMENDED TO ALWAYS LEAVE TO 1.

**Account Recommendation**: Use a **dedicated account** for the bot to avoid conflicts with manual trading and ensure accurate balance calculations.

**Note**: The default ±0.6% spreads worked well for ASTERUSDT on September 24th, 2024, but:
- Market conditions change over time - these values will likely need adjustment
- Different trading pairs require different spread configurations
- Always test and optimize spreads for your specific market and timeframe

*Future updates may include dynamic spread models (Avellaneda-Stoikov, Cartea-Jaimungal, or other alternatives) for automated spread optimization.*

## Other scripts
Python scripts in folder `test`, may need to be together with `api_client.py`, and a `.env` with valid API keys to work.

```bash
# Live order monitoring (detailed view - orders are spammed too quickly to be seen properly on ASTER DEX website)
python websocket_orders.py

# Market data utilities
python get_price.py ETHUSDT
python get_trades.py ETHUSDT --limit 50 # all trades executed, not only you (mostly not you)

# User data monitoring
python websocket_user_data.py

# Testing scripts
python test_balance.py                          # Quick balance check
python test_account_balance.py                  # Extended 3-minute balance monitoring
python test_cancel_order.py                     # Test order cancellation functionality
python websocket_user_data.py                   # Test user data stream connectivity
python test_listen_key.py                       # Test listen key creation/renewal
python test_price_threshold.py                  # Test price change threshold logic
python demo_user_stream.py                      # Extended user stream demo (2 minutes)
python websocket_user_data_simple.py            # Simple user data stream test
and more...
```

## Terminal Dashboard

For a comprehensive, real-time overview of your account, use the `terminal_dashboard.py` script. It provides a unified dashboard displaying:
- Account balances (stablecoins and others)
- Open positions with unrealized PnL
- Recent order activity

```bash
python terminal_dashboard.py
```

## Docker Deployment

```bash
# Build
docker-compose build

# Build and run
docker-compose up --build

# Run specific service
docker-compose up market-maker

# Background mode
docker-compose up -d

# View logs
docker-compose logs -f market-maker
```

**Change the crypto pair to trade**: Edit the `command` line in `docker-compose.yml`
```yaml
market-maker:
  command: python market_maker.py --symbol ASTERUSDT
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
- Test incrementally (e.g., 0.5s → 0.3s → 0.2s) to find your optimal balance. Don't forget to adjust other variables too (`MIN_ORDER_INTERVAL`, `RETRY_ON_ERROR_INTERVAL`, `asyncio.sleep` calls, ...)

---

**⚠️ Risk Warning**: This is trading software that can lose money. Always test with small amounts and understand the risks involved in automated cryptocurrency trading.
