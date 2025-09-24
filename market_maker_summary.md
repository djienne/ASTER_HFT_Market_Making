# Market Maker Bot (`market_maker.py`) Code Summary

This document provides a summary of the structure, components, and logic of the `market_maker.py` script.

## 1. Overview

The script is an asynchronous, event-driven market-making bot designed for the Aster Finance platform. Its primary goal is to continuously place buy (bid) and sell (ask) orders around a calculated mid-price for a specific trading symbol. It manages a single position, flipping between "BUY" mode (to open a position) and "SELL" mode (to close it), effectively creating a simple trading cycle.

The bot is built using Python's `asyncio` library for concurrent operations and `websockets` for real-time data feeds.

## 2. File Structure

The script can be broken down into the following logical sections:

1.  **Imports**: Standard libraries (`os`, `asyncio`, `logging`, etc.) and project-specific modules (`api_client`).
2.  **Configuration**: A set of global constants that define the bot's strategy, timing, and operational parameters.
3.  **Logging Setup**: The `setup_logging` function configures how information is logged to the console and a file (`market_maker.log`).
4.  **State Management**: The `StrategyState` class acts as a central container for all dynamic data the bot uses, such as prices, balance, and active orders.
5.  **Core Logic**:
    *   **WebSocket Handlers**: Asynchronous functions (`websocket_price_updater`, `websocket_user_data_updater`) that connect to exchange streams and update the `StrategyState`.
    *   **Market Making Loop**: The `market_making_loop` function, which contains the main decision-making logic.
    *   **Helper Functions**: Utility functions for data validation, order calculations, and strategy adjustments (`is_price_data_valid`, `get_spreads`, `should_reuse_order`).
6.  **Execution Block**: The `main` function and the `if __name__ == "__main__":` block, which handle initialization, task creation, and graceful shutdown.

## 3. Core Components

### Configuration (`# --- Configuration ---`)

This top section contains easily modifiable global variables to tweak the bot's behavior without altering the core logic. Key parameters include:
- **Strategy**: `DEFAULT_SYMBOL`, `DEFAULT_BUY_SPREAD`, `DEFAULT_SELL_SPREAD`, `DEFAULT_BALANCE_FRACTION`.
- **Timing**: `ORDER_REFRESH_INTERVAL`, `RETRY_ON_ERROR_INTERVAL` for managing the speed and resilience of the bot.
- **Logging**: `LOG_FILE`, `RELEASE_MODE` to control the verbosity of logs.

### `StrategyState` Class

This class is crucial for maintaining the bot's state across different asynchronous tasks. It holds:
- **Market Data**: `bid_price`, `ask_price`, `mid_price`.
- **Position & Order Data**: `active_order_id`, `position_size`, `mode` ('BUY' or 'SELL').
- **Account Data**: `account_balance`, `usdf_balance`, `usdt_balance`.
- **Communication**: An `asyncio.Queue` (`order_updates`) for passing order status changes from the user data WebSocket to the main loop.
- **Health Flags**: `price_ws_connected`, `user_data_ws_connected` to monitor the status of WebSocket connections.

### WebSocket Handlers

The bot relies on two primary WebSocket connections for real-time data:

1.  **`websocket_price_updater`**:
    *   Connects to the public depth stream (`@depth5`).
    *   Continuously receives bid/ask prices and calculates the `mid_price`.
    *   Updates the `StrategyState` with the latest market data.
    *   Includes logic for automatic reconnection with exponential backoff and detection of stale connections.

2.  **`websocket_user_data_updater`**:
    *   Connects to a private user data stream using a `listenKey`.
    *   Receives real-time updates on account balance (`ACCOUNT_UPDATE`) and order status (`ORDER_TRADE_UPDATE`).
    *   Puts order updates into the `state.order_updates` queue for the main loop to process.
    *   Manages the `listenKey` by periodically sending keepalive requests.

### `market_making_loop`

This is the heart of the bot. It runs in a continuous loop and performs the following steps:
1.  **Health Checks**: Ensures WebSocket connections are active and data is recent before proceeding.
2.  **Determine Strategy**:
    *   Checks the current `state.mode`.
    *   If in 'BUY' mode, it calculates order parameters to open a long position.
    *   If in 'SELL' mode, it calculates parameters to close the existing position.
3.  **Get Spreads**: Calls the `get_spreads` function to determine the bid/ask spreads. This function is abstracted to allow for more complex spread logic in the future.
4.  **Calculate Order**: Determines the `side`, `quantity`, and `limit_price` for the next order based on the strategy, spreads, and available balance.
5.  **Validate & Adjust**: Rounds the calculated price and quantity to conform to the exchange's trading rules (filters) and checks if the order's notional value meets the minimum requirement.
6.  **Order Reuse Logic**: Checks if the previously placed order is still good enough (i.e., the price hasn't moved much) using `should_reuse_order`. If so, it skips placing a new order and continues monitoring the old one.
7.  **Place/Replace Order**:
    *   If a new order is needed, it first cancels any existing active order.
    *   It then places a new limit order via the `ApiClient`.
8.  **Monitor Order**: After placing an order, it waits for a status update from the `order_updates` queue for a duration defined by `ORDER_REFRESH_INTERVAL`.
9.  **Update State**:
    *   If the order is filled, it updates the `position_size` and flips the `mode` (from 'BUY' to 'SELL' or vice-versa).
    *   If the order is not filled in time, it is cancelled, and the loop repeats.
10. **Error Handling**: The entire loop is wrapped in a `try...except` block to catch errors, log them, and pause before retrying.

## 4. Execution Flow and Shutdown

1.  **Initialization**: The `main` function parses command-line arguments (e.g., `--symbol`), sets up logging, and loads API credentials from a `.env` file.
2.  **Cleanup**: It starts by cancelling any lingering open orders for the symbol to ensure a clean state.
3.  **Initial State**: It fetches the initial account balance and checks for any existing positions to set the starting `mode` correctly.
4.  **Task Creation**: It creates and starts all the main asynchronous tasks: the two WebSocket handlers, the market-making loop, and two simple "reporter" tasks that periodically print price and balance information.
5.  **Running**: The script runs indefinitely, with all tasks operating concurrently.
6.  **Graceful Shutdown**:
    *   A `signal_handler` is set up to listen for `SIGINT` (Ctrl+C) and `SIGTERM`.
    *   When a signal is received, the global `shutdown_requested` flag is set to `True`.
    *   The loops in all tasks see this flag and exit cleanly.
    *   Finally, a `cleanup_orders` function is called to ensure all open orders are cancelled before the script fully exits.
