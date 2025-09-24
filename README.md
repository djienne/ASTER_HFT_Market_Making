# Aster DEX Basic High Frequency Trading System

A simple Python high frequency market making bot for the Aster Finance DEX platform with real-time balance tracking.

Referral link to support this work: [https://www.asterdex.com/en/referral/164f81](https://www.asterdex.com/en/referral/164f81) Earn 10% rebate (I put maximum for you).

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Configure API credentials (copy .env.example to .env and edit)
cp .env.example .env

# Run the market maker
python market_maker.py --symbol ETHUSDT
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

### Trading Parameters (`market_maker.py`)
```python
DEFAULT_SYMBOL = "ETHUSDT"              # Trading pair
DEFAULT_BUY_SPREAD = 0.006              # 0.6% below mid-price
DEFAULT_SELL_SPREAD = 0.006             # 0.6% above mid-price
DEFAULT_BALANCE_FRACTION = 0.2          # Use 20% of balance per order
POSITION_THRESHOLD_USD = 15.0           # $15 position closure threshold

# Order Management
ORDER_REFRESH_INTERVAL = 1              # Cancel and replace unfilled orders after 1 second
DEFAULT_PRICE_CHANGE_THRESHOLD = 0.001  # Min price change to cancel and replace order (0.1%)

# Logging
RELEASE_MODE = True                     # True = errors only, False = detailed logs
```

**Important**: This bot uses **fixed spreads** - you must determine your optimal spread values based on:
- Market volatility and liquidity
- Your risk tolerance and capital requirements
- Backtesting and observation of market conditions

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
