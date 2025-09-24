import os
import asyncio
import argparse
import logging
import websockets
import json
import signal
from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv
from api_client import ApiClient

# --- Configuration ---
# STRATEGY
DEFAULT_SYMBOL = "ASTERUSDT"
DEFAULT_BUY_SPREAD = 0.006   # 0.6% below mid-price for buy orders
DEFAULT_SELL_SPREAD = 0.006  # 0.6% above mid-price for sell orders
DEFAULT_LEVERAGE = 1
DEFAULT_BALANCE_FRACTION = 0.2  # Use fraction of available balance for each order
POSITION_THRESHOLD_USD = 15.0  # Fixed USD value threshold for position closure

# TIMING (in seconds)
ORDER_REFRESH_INTERVAL = 1    # How long to wait before cancelling an unfilled order (seconds).
RETRY_ON_ERROR_INTERVAL = 30    # How long to wait after a major error before retrying.
PRICE_REPORT_INTERVAL = 60      # How often to report current prices and spread to terminal.
BALANCE_REPORT_INTERVAL = 60    # How often to report account balance to terminal.

# ORDER REUSE SETTINGS
DEFAULT_PRICE_CHANGE_THRESHOLD = 0.001  # minimum price change to cancel and replace order

# ORDER CANCELLATION
CANCEL_SPECIFIC_ORDER = True # If True, cancel specific order ID. If False, cancel all orders for the symbol.

# LOGGING
LOG_FILE = 'market_maker.log'
RELEASE_MODE = False  # When True, suppress all non-error logs and prints

# Global variables for price data and rate limiting
price_last_updated = None
last_order_time = 0
MIN_ORDER_INTERVAL = 0.1  # Minimum seconds between order placements


def setup_logging(file_log_level):
    """Configures logging to both console (INFO) and file (specified level)."""
    log_level = getattr(logging, file_log_level.upper(), logging.DEBUG)
    logger = logging.getLogger()  # Get root logger

    if RELEASE_MODE:
        logger.setLevel(logging.ERROR)  # Only errors in release mode
    else:
        logger.setLevel(log_level)

    if logger.hasHandlers():
        logger.handlers.clear()

    # File handler - only add if not in release mode or for errors
    if not RELEASE_MODE:
        file_handler = logging.FileHandler(LOG_FILE)
        file_handler.setLevel(log_level)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    else:
        # In release mode, only log errors to file
        file_handler = logging.FileHandler(LOG_FILE)
        file_handler.setLevel(logging.ERROR)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    # Console handler - only add if not in release mode or for errors
    if not RELEASE_MODE:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    else:
        # In release mode, only show errors on console
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.ERROR)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)


class StrategyState:
    """A simple class to hold the shared state of the strategy."""
    def __init__(self):
        self.bid_price = None
        self.ask_price = None
        self.mid_price = None
        self.active_order_id = None
        self.position_size = 0.0
        # Mode can be 'BUY' or 'SELL'
        self.mode = 'BUY'
        # Track last order details for reuse logic
        self.last_order_price = None
        self.last_order_side = None
        self.last_order_quantity = None
        # Account balance tracking
        self.account_balance = None  # Total USDF + USDT balance
        self.balance_last_updated = None
        self.balance_listen_key = None
        self.usdf_balance = 0.0
        self.usdt_balance = 0.0
        # Queue for order updates from WebSocket
        self.order_updates = asyncio.Queue()
        # [ADDED] WebSocket connection health flags
        self.price_ws_connected = False
        self.user_data_ws_connected = False


async def websocket_price_updater(state, symbol):
    """[MODIFIED] WebSocket-based price updater with exponential backoff and stale connection detection."""
    global price_last_updated
    log = logging.getLogger('WebSocketPriceUpdater')

    websocket_url = f"wss://fstream.asterdex.com/ws/{symbol.lower()}@depth5"
    reconnect_delay = 5  # Initial delay
    max_reconnect_delay = 60 # Maximum wait time

    while not shutdown_requested:
        try:
            log.info(f"Connecting to WebSocket: {websocket_url}")
            state.price_ws_connected = False # Mark as disconnected while attempting

            async with websockets.connect(websocket_url, ping_interval=20, ping_timeout=10) as websocket:
                log.info(f"WebSocket connected for {symbol} depth stream")
                state.price_ws_connected = True # Mark as connected
                reconnect_delay = 5  # Reset reconnect delay on successful connection
                last_message_time = asyncio.get_event_loop().time()

                while not shutdown_requested:
                    try:
                        # [MODIFIED] Wait for a message with a timeout to detect stale connections
                        message = await asyncio.wait_for(websocket.recv(), timeout=30.0)
                        last_message_time = asyncio.get_event_loop().time()

                        try:
                            data = json.loads(message)

                            if data.get('e') == 'depthUpdate' and ('b' in data and 'a' in data):
                                bids = data.get('b', [])
                                asks = data.get('a', [])

                                if bids and asks:
                                    best_bid = float(bids[0][0])
                                    best_ask = float(asks[0][0])
                                    mid_price = (best_bid + best_ask) / 2

                                    state.bid_price = best_bid
                                    state.ask_price = best_ask
                                    state.mid_price = mid_price
                                    price_last_updated = asyncio.get_event_loop().time()

                                    log.debug(f"Updated prices for {symbol}: Bid={best_bid}, Ask={best_ask}, Mid={mid_price:.4f}")

                        except json.JSONDecodeError:
                            log.warning("Failed to decode WebSocket message")
                        except Exception as e:
                            log.error(f"Error processing WebSocket message: {e}")
                    
                    # [ADDED] Stale connection detection logic
                    except asyncio.TimeoutError:
                        time_since_last_msg = asyncio.get_event_loop().time() - last_message_time
                        if time_since_last_msg > 60:
                            log.warning(f"No price messages received for {time_since_last_msg:.1f}s. Connection may be stale. Reconnecting...")
                            break # Exit inner loop to force reconnection
                        else:
                            log.debug(f"Price WebSocket recv timed out ({time_since_last_msg:.1f}s since last message), but connection seems alive.")
                            continue # Continue waiting for messages

        except (websockets.exceptions.ConnectionClosed, websockets.exceptions.InvalidState) as e:
            log.warning(f"Price WebSocket connection issue: {e}")
        except Exception as e:
            log.error(f"Price WebSocket error: {e}")
        finally:
            state.price_ws_connected = False # Mark as disconnected on any error/exit

        if not shutdown_requested:
            log.info(f"Reconnecting to price WebSocket in {reconnect_delay:.1f}s...")
            await asyncio.sleep(reconnect_delay)
            # [MODIFIED] Implement exponential backoff
            reconnect_delay = min(reconnect_delay * 1.5, max_reconnect_delay)

    log.info("WebSocket price updater shutting down")

def is_price_data_valid(state):
    """Check if the price data is valid and recent."""
    global price_last_updated

    if state.mid_price is None or price_last_updated is None:
        return False

    # Check if price data is recent (within 30 seconds)
    current_time = asyncio.get_event_loop().time()
    if current_time - price_last_updated > 30:
        return False

    return True


def is_balance_data_valid(state):
    """Check if the balance data is valid and recent."""
    if state.account_balance is None or state.balance_last_updated is None:
        return False

    return True


async def keepalive_balance_listen_key(state, client):
    """Periodically send keepalive for balance listen key."""
    log = logging.getLogger('BalanceKeepalive')
    apiv1_public = os.getenv('APIV1_PUBLIC_KEY')
    apiv1_private = os.getenv('APIV1_PRIVATE_KEY')

    while not shutdown_requested and state.balance_listen_key:
        try:
            # Sleep for 10 minutes (listen key expires in 60 minutes)
            await asyncio.sleep(600)

            if shutdown_requested or not state.balance_listen_key:
                break

            log.info("Sending keepalive for balance listen key...")
            await client.signed_request(
                "PUT", "/fapi/v1/listenKey", {},
                use_binance_auth=True,
                api_key=apiv1_public,
                api_secret=apiv1_private
            )
            log.info("Balance listen key keepalive sent successfully")

        except asyncio.CancelledError:
            log.info("Balance keepalive task cancelled.")
            break
        except Exception as e:
            log.error(f"Failed to send balance listen key keepalive: {e}")

    log.info("Balance keepalive task shutting down")


async def websocket_user_data_updater(state, client):
    """[MODIFIED] WebSocket-based user data updater for account and order updates."""
    log = logging.getLogger('UserDataUpdater')
    reconnect_delay = 5
    max_reconnect_delay = 60 # Maximum wait time between reconnection attempts
    keepalive_task = None

    while not shutdown_requested:
        try:
            log.info("Getting listen key for user data stream...")
            state.user_data_ws_connected = False # Mark as disconnected
            apiv1_public = os.getenv('APIV1_PUBLIC_KEY')
            apiv1_private = os.getenv('APIV1_PRIVATE_KEY')

            if not all([apiv1_public, apiv1_private]):
                log.error("Missing APIV1_PUBLIC_KEY or APIV1_PRIVATE_KEY for user data stream.")
                await asyncio.sleep(RETRY_ON_ERROR_INTERVAL)
                continue

            response = await client.signed_request(
                "POST", "/fapi/v1/listenKey", {},
                use_binance_auth=True,
                api_key=apiv1_public,
                api_secret=apiv1_private
            )
            state.balance_listen_key = response['listenKey']
            log.info(f"User data listen key obtained: {state.balance_listen_key[:20]}...")

            keepalive_task = asyncio.create_task(keepalive_balance_listen_key(state, client))

            ws_url = f"wss://fstream.asterdex.com/ws/{state.balance_listen_key}"
            log.info(f"Connecting to user data WebSocket: {ws_url}")

            async with websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=10
            ) as websocket:
                log.info("User data WebSocket connected!")
                state.user_data_ws_connected = True # Mark as connected
                reconnect_delay = 5  # Reset reconnect delay on successful connection
                last_message_time = asyncio.get_event_loop().time()

                while not shutdown_requested:
                    try:
                        # Wait for a message with a timeout to detect stale connections
                        message = await asyncio.wait_for(websocket.recv(), timeout=30.0)
                        last_message_time = asyncio.get_event_loop().time()

                        try:
                            data = json.loads(message)
                            event_type = data.get('e')

                            if event_type == 'ACCOUNT_UPDATE':
                                balances = data.get('a', {}).get('B', [])
                                for balance in balances:
                                    if balance.get('a') == 'USDF':
                                        state.usdf_balance = float(balance.get('wb', '0'))
                                    elif balance.get('a') == 'USDT':
                                        state.usdt_balance = float(balance.get('wb', '0'))

                                state.account_balance = state.usdf_balance + state.usdt_balance
                                state.balance_last_updated = asyncio.get_event_loop().time()
                                log.info(f"Balance updated: USDF={state.usdf_balance:.4f}, USDT={state.usdt_balance:.4f}, Total=${state.account_balance:.4f}")
                            
                            elif event_type == 'ORDER_TRADE_UPDATE':
                                order_data = data.get('o', {})
                                log.debug(f"Queueing order update for {order_data.get('i')}: status {order_data.get('X')}")
                                await state.order_updates.put(data)

                            elif event_type == 'listenKeyExpired':
                                log.warning("User data listen key expired! Reconnecting...")
                                break # Exit inner loop to get a new key

                        except json.JSONDecodeError:
                            log.warning("Failed to decode user data WebSocket message")
                        except Exception as e:
                            log.error(f"Error processing user data message: {e}", exc_info=True)

                    except asyncio.TimeoutError:
                        time_since_last_msg = asyncio.get_event_loop().time() - last_message_time
                        if time_since_last_msg > 60:
                            log.warning(f"No user data messages received for {time_since_last_msg:.1f}s. Connection may be stale. Reconnecting...")
                            break # Exit inner loop to force reconnection
                        else:
                            log.debug(f"User data WebSocket recv timed out ({time_since_last_msg:.1f}s since last message), but connection seems alive.")
                            continue # Continue waiting for messages

        except (websockets.exceptions.ConnectionClosed, websockets.exceptions.InvalidState) as e:
            log.warning(f"User data WebSocket connection issue: {e}")
        except Exception as e:
            log.error(f"An unexpected error occurred in user data updater: {e}", exc_info=True)
        finally:
            state.user_data_ws_connected = False # Mark as disconnected on any error/exit
            if keepalive_task and not keepalive_task.done():
                keepalive_task.cancel()
                try:
                    await keepalive_task
                except asyncio.CancelledError:
                    pass # Expected cancellation

        if not shutdown_requested:
            log.info(f"Reconnecting to user data WebSocket in {reconnect_delay:.1f}s...")
            await asyncio.sleep(reconnect_delay)
            # Exponential backoff
            reconnect_delay = min(reconnect_delay * 1.5, max_reconnect_delay)

    log.info("User data updater shutting down")


async def balance_reporter(state):
    global BALANCE_REPORT_INTERVAL
    """Periodically reports current account balance (only when not in release mode)."""
    log = logging.getLogger('BalanceReporter')

    # Only run balance reporter if not in release mode
    if RELEASE_MODE:
        log.info("Balance reporter disabled in release mode")
        return

    while not shutdown_requested:
        try:
            await asyncio.sleep(BALANCE_REPORT_INTERVAL)  # Report every 30 seconds

            if not shutdown_requested and is_balance_data_valid(state):
                log.info(f"Account Balance: USDF={state.usdf_balance:.4f}, USDT={state.usdt_balance:.4f}, Total=${state.account_balance:.4f}")

        except Exception as e:
            log.error(f"Error in balance reporter: {e}")

    log.info("Balance reporter shutting down")


async def price_reporter(state, symbol):
    """Periodically reports current mid-price and bid-ask spread."""
    log = logging.getLogger('PriceReporter')

    while not shutdown_requested:
        try:
            await asyncio.sleep(PRICE_REPORT_INTERVAL)

            if not shutdown_requested and is_price_data_valid(state):
                bid_ask_spread = state.ask_price - state.bid_price
                spread_percentage = (bid_ask_spread / state.mid_price) * 100 if state.mid_price > 0 else 0

                balance_info = ""
                if is_balance_data_valid(state):
                    balance_info = f" | Balance: ${state.account_balance:.2f}"

                log.info(f"{symbol} | Mid-Price: ${state.mid_price:.4f} | Bid-Ask Spread: {spread_percentage:.3f}% | Bid: ${state.bid_price:.4f} | Ask: ${state.ask_price:.4f}{balance_info}")

        except Exception as e:
            log.error(f"Error in price reporter: {e}")

    log.info("Price reporter shutting down")


def round_down(value, precision):
    """Helper to round a value down to a given precision."""
    factor = 10 ** precision
    return (int(value * factor)) / factor


def should_reuse_order(state, new_price, new_side, new_quantity, threshold=DEFAULT_PRICE_CHANGE_THRESHOLD):
    """Check if existing order can be reused based on price change threshold."""
    if (state.active_order_id is None or
        state.last_order_price is None or
        state.last_order_side != new_side or
        abs(state.last_order_quantity - new_quantity) > 0.000000000001):  # Different quantity
        return False

    # Calculate price change percentage
    price_change_pct = abs(new_price - state.last_order_price) / state.last_order_price

    # Reuse if price change is below threshold
    return price_change_pct < threshold


def get_spreads(state):
    """
    Abstracted function to determine bid and ask spreads.
    This can be modified to implement dynamic spread calculations.
    
    :param state: The current strategy state.
    :return: A tuple of (buy_spread, sell_spread).
    """
    return DEFAULT_BUY_SPREAD, DEFAULT_SELL_SPREAD


async def market_making_loop(state, client, args):
    """The main market making logic loop."""
    log = logging.getLogger('MarketMakerLoop')
    log.info(f"Fetching trading rules for {args.symbol}...")
    symbol_filters = await client.get_symbol_filters(args.symbol)
    log.info(f"Filters loaded: {symbol_filters}")

    while not shutdown_requested:
        try:
            # [ADDED] Primary check for WebSocket health before proceeding.
            if not state.price_ws_connected or not state.user_data_ws_connected:
                ws_status = f"Price_WS_Connected={state.price_ws_connected}, User_Data_WS_Connected={state.user_data_ws_connected}"
                log.warning(f"A WebSocket is disconnected ({ws_status}). Pausing trading logic.")

                # [ADDED] Safety measure: Cancel active order if we lose connectivity
                if state.active_order_id:
                    log.warning(f"Attempting to cancel active order {state.active_order_id} due to WebSocket disconnection.")
                    try:
                        if CANCEL_SPECIFIC_ORDER:
                            await client.cancel_order(args.symbol, state.active_order_id)
                        else:
                            await client.cancel_all_orders(args.symbol)
                        state.active_order_id = None
                        state.last_order_price = None
                        state.last_order_side = None
                        state.last_order_quantity = None
                        log.info(f"Successfully cancelled order {state.active_order_id} as a safety measure.")
                    except Exception as cancel_error:
                        log.error(f"Could not cancel order {state.active_order_id} during WS outage: {cancel_error}")
                
                await asyncio.sleep(5) # Wait before checking again
                continue

            # --- Secondary checks for fresh data ---
            if not is_price_data_valid(state):
                log.info("Waiting for valid price data from WebSocket...")
                await asyncio.sleep(2)
                continue

            if not is_balance_data_valid(state):
                log.info("Waiting for valid balance data from WebSocket...")
                await asyncio.sleep(2)
                continue

            # --- Double-check position before entering BUY mode ---
            if state.mode == 'BUY':
                try:
                    log.debug("Double-checking position before placing BUY order...")
                    positions = await client.get_position_risk(args.symbol)
                    if positions:
                        position = positions[0]
                        current_position_size = float(position.get('positionAmt', 0.0))
                        notional_value = abs(float(position.get('notional', 0.0)))

                        if current_position_size > 0 and notional_value > 15.0:
                            log.info(f"Found existing LONG position of size {current_position_size} with notional ${notional_value:.2f} - switching to SELL mode")
                            state.position_size = current_position_size
                            state.mode = 'SELL'
                except Exception as e:
                    log.warning(f"Failed to double-check position, proceeding with current mode: {e}")

            # --- Determine Strategy and Parameters ---
            buy_spread, sell_spread = get_spreads(state)
            if state.mode == 'SELL':
                log.info(f"Position size is {state.position_size}. Entering SELL mode.")
                side, reduce_only = "SELL", True
                quantity_to_trade = abs(state.position_size)
                limit_price = state.mid_price * (1 + sell_spread)
                log.debug(f"SELL mode parameters: side={side}, reduce_only={reduce_only}, quantity_to_trade={quantity_to_trade}, limit_price={limit_price}, spread={sell_spread}")
            else: # BUY Mode
                log.info("No significant position. Entering BUY mode.")
                side, reduce_only = "BUY", False
                order_amount_usd = state.account_balance * DEFAULT_BALANCE_FRACTION
                quantity_to_trade = order_amount_usd / state.mid_price
                limit_price = state.mid_price * (1 - buy_spread)
                log.debug(f"BUY mode parameters: side={side}, reduce_only={reduce_only}, order_amount_usd={order_amount_usd:.2f}, quantity_to_trade={quantity_to_trade}, limit_price={limit_price}, spread={buy_spread}")

            log.info(f"Calculated order parameters: side={side}, quantity={quantity_to_trade:.8f}, price={limit_price:.8f}, reduce_only={reduce_only}")
            current_spread = sell_spread if side == 'SELL' else buy_spread
            log.debug(f"Market data: mid_price={state.mid_price:.8f}, bid={state.bid_price:.8f}, ask={state.ask_price:.8f}, using_spread={current_spread}")

            # --- Adjust order to conform to exchange filters ---
            log.debug(f"Symbol filters: {symbol_filters}")
            rounded_price = round(limit_price / symbol_filters['tick_size']) * symbol_filters['tick_size']
            formatted_price = f"{rounded_price:.{symbol_filters['price_precision']}f}"
            log.debug(f"Price adjustment: {limit_price:.8f} -> {rounded_price:.8f} -> {formatted_price}")

            rounded_quantity = round_down(quantity_to_trade, symbol_filters['quantity_precision'])
            formatted_quantity = f"{rounded_quantity:.{symbol_filters['quantity_precision']}f}"
            log.info(f"Adjusted order: price={formatted_price}, quantity={formatted_quantity}")
            log.debug(f"Quantity adjustment: {quantity_to_trade:.8f} -> {rounded_quantity:.8f} -> {formatted_quantity}")

            if float(formatted_quantity) <= 0:
                log.warning(f"Calculated quantity is zero or negative: {formatted_quantity}. Skipping cycle.")
                await asyncio.sleep(ORDER_REFRESH_INTERVAL)
                continue

            order_notional = float(formatted_price) * float(formatted_quantity)
            min_notional = symbol_filters['min_notional']
            if order_notional < min_notional:
                log.warning(f"Order notional too small: ${order_notional:.2f} < ${min_notional:.2f} (min required). Skipping cycle.")
                log.debug(f"Notional calculation: {formatted_price} * {formatted_quantity} = ${order_notional:.2f}")
                await asyncio.sleep(ORDER_REFRESH_INTERVAL)
                continue

            log.debug(f"Order validation passed: notional=${order_notional:.2f} >= ${min_notional:.2f}")

            # --- Check if we can reuse existing order ---
            if should_reuse_order(state, float(formatted_price), side, float(formatted_quantity)):
                price_change_pct = abs(float(formatted_price) - state.last_order_price) / state.last_order_price * 100
                log.info(f"Reusing existing order {state.active_order_id}: price change {price_change_pct:.4f}% < {DEFAULT_PRICE_CHANGE_THRESHOLD*100:.2f}% threshold")

                # Continue monitoring the existing order
                filled_qty = 0.0
                try:
                    log.debug(f"Continuing to monitor existing order {state.active_order_id} via WebSocket with timeout {ORDER_REFRESH_INTERVAL}s")
                    start_time = asyncio.get_event_loop().time()
                    while True:
                        remaining_timeout = ORDER_REFRESH_INTERVAL - (asyncio.get_event_loop().time() - start_time)
                        if remaining_timeout <= 0:
                            raise asyncio.TimeoutError
                        
                        update = await asyncio.wait_for(state.order_updates.get(), timeout=remaining_timeout)
                        if update.get('e') == 'ORDER_TRADE_UPDATE':
                            order_data = update.get('o', {})
                            if order_data.get('i') == state.active_order_id:
                                status = order_data.get('X')
                                filled_qty = float(order_data.get('z', 0.0))

                                if status == 'PARTIALLY_FILLED':
                                    avg_price = float(order_data.get('ap', 0.0))
                                    if avg_price > 0:
                                        filled_notional = filled_qty * avg_price
                                        if filled_notional > POSITION_THRESHOLD_USD:
                                            log.info(f"Monitored order {state.active_order_id} is PARTIALLY_FILLED with notional ${filled_notional:.2f} > ${POSITION_THRESHOLD_USD}. Treating as FILLED.")
                                            break # Treat as filled
                                
                                if status in ['FILLED', 'CANCELED', 'REJECTED', 'EXPIRED']:
                                    log.info(f"Monitored order {state.active_order_id} reached final state {status}. Filled: {filled_qty}")
                                    break
                    
                    log.info(f"Reused order {state.active_order_id} filled! Quantity: {filled_qty}")

                    # Update state after a fill (same logic as new order)
                    previous_mode = state.mode
                    previous_position = state.position_size

                    if state.mode == 'BUY':
                        state.position_size += filled_qty
                        state.mode = 'SELL'
                        log.info(f"BUY fill processed: {previous_position:.6f} + {filled_qty:.6f} = {state.position_size:.6f}")
                        log.info(f"Mode change: {previous_mode} -> {state.mode}")
                    else: # SELL
                        state.position_size -= filled_qty
                        position_threshold = POSITION_THRESHOLD_USD / state.mid_price
                        log.debug(f"SELL fill: {previous_position:.6f} - {filled_qty:.6f} = {state.position_size:.6f}, threshold: {position_threshold:.6f}")

                        if state.position_size < position_threshold:
                            state.mode = 'BUY'
                            log.info(f"SELL fill processed: {previous_position:.6f} - {filled_qty:.6f} = {state.position_size:.6f}")
                            log.info(f"Position below threshold ({position_threshold:.6f}), mode change: {previous_mode} -> {state.mode}")
                        else:
                            log.info(f"SELL fill processed: {previous_position:.6f} - {filled_qty:.6f} = {state.position_size:.6f} (keeping SELL mode)")

                    # Clear order tracking after fill
                    state.last_order_price = None
                    state.last_order_side = None
                    state.last_order_quantity = None
                    log.debug("Adding 0.1s delay after order fill to avoid API rate limits")
                    await asyncio.sleep(0.1)

                except asyncio.TimeoutError:
                    log.info(f"Reused order {state.active_order_id} not filled within {ORDER_REFRESH_INTERVAL}s. Will evaluate for replacement in next cycle.")
                    await asyncio.sleep(0.1)

                continue  # Skip to next iteration

            # --- Rate Limiting Protection ---
            global last_order_time
            current_time = asyncio.get_event_loop().time()
            time_since_last_order = current_time - last_order_time

            if time_since_last_order < MIN_ORDER_INTERVAL:
                wait_time = MIN_ORDER_INTERVAL - time_since_last_order
                log.info(f"Rate limiting: waiting {wait_time:.1f}s before placing order")
                await asyncio.sleep(wait_time)

            # --- Cancel existing order if we're placing a new one ---
            if state.active_order_id:
                try:
                    log.info(f"Cancelling existing order {state.active_order_id} to place new order")
                    if CANCEL_SPECIFIC_ORDER:
                        await client.cancel_order(args.symbol, state.active_order_id)
                    else:
                        await client.cancel_all_orders(args.symbol)
                    state.active_order_id = None
                except Exception as cancel_error:
                    log.warning(f"Error cancelling existing order: {cancel_error}")

            # --- Place and Monitor Order ---
            percentage_diff = (float(formatted_price) - state.mid_price) / state.mid_price * 100
            log.info(f"Placing {side} order: {formatted_quantity} {args.symbol} @ {formatted_price} ({percentage_diff:+.4f}% from mid-price)")
            log.info(f"Order details: symbol={args.symbol}, price={formatted_price}, quantity={formatted_quantity}, side={side}, reduceOnly={reduce_only}")

            try:
                active_order = await client.place_order(args.symbol, formatted_price, formatted_quantity, side, reduce_only)
                last_order_time = asyncio.get_event_loop().time()
                state.active_order_id = active_order.get('orderId')

                # Track order details for reuse logic
                state.last_order_price = float(formatted_price)
                state.last_order_side = side
                state.last_order_quantity = float(formatted_quantity)

                log.info(f"Order placed successfully: ID={state.active_order_id}")
                log.debug(f"Full order response: {active_order}")
            except Exception as order_error:
                log.error(f"Failed to place order: {order_error}")
                log.error(f"Order parameters: symbol={args.symbol}, price={formatted_price}, quantity={formatted_quantity}, side={side}, reduceOnly={reduce_only}")
                raise

            filled_qty = 0.0
            try:
                log.debug(f"Waiting for WebSocket update for order {state.active_order_id} with timeout {ORDER_REFRESH_INTERVAL}s")
                start_time = asyncio.get_event_loop().time()
                while True:
                    remaining_timeout = ORDER_REFRESH_INTERVAL - (asyncio.get_event_loop().time() - start_time)
                    if remaining_timeout <= 0:
                        raise asyncio.TimeoutError

                    update = await asyncio.wait_for(state.order_updates.get(), timeout=remaining_timeout)
                    if update.get('e') == 'ORDER_TRADE_UPDATE':
                        order_data = update.get('o', {})
                        if order_data.get('i') == state.active_order_id:
                            status = order_data.get('X')
                            filled_qty = float(order_data.get('z', 0.0))

                            if status == 'PARTIALLY_FILLED':
                                avg_price = float(order_data.get('ap', 0.0))
                                if avg_price > 0:
                                    filled_notional = filled_qty * avg_price
                                    if filled_notional > POSITION_THRESHOLD_USD:
                                        log.info(f"Order {state.active_order_id} is PARTIALLY_FILLED with notional ${filled_notional:.2f} > ${POSITION_THRESHOLD_USD}. Treating as FILLED.")
                                        break # Treat as filled

                            if status in ['FILLED', 'CANCELED', 'REJECTED', 'EXPIRED']:
                                log.info(f"Order {state.active_order_id} reached final state {status}. Filled: {filled_qty}")
                                break

                log.info(f"Order {state.active_order_id} filled! Quantity: {filled_qty}")

                # Update state after a fill
                previous_mode = state.mode
                previous_position = state.position_size

                if state.mode == 'BUY':
                    state.position_size += filled_qty
                    state.mode = 'SELL' # Flip to sell mode
                    log.info(f"BUY fill processed: {previous_position:.6f} + {filled_qty:.6f} = {state.position_size:.6f}")
                    log.info(f"Mode change: {previous_mode} -> {state.mode}")
                else: # SELL
                    state.position_size -= filled_qty
                    position_threshold = POSITION_THRESHOLD_USD / state.mid_price
                    log.debug(f"SELL fill: {previous_position:.6f} - {filled_qty:.6f} = {state.position_size:.6f}, threshold: {position_threshold:.6f}")

                    if state.position_size < position_threshold: # If position is mostly closed
                        state.mode = 'BUY' # Flip back to buy mode
                        log.info(f"SELL fill processed: {previous_position:.6f} - {filled_qty:.6f} = {state.position_size:.6f}")
                        log.info(f"Position below threshold ({position_threshold:.6f}), mode change: {previous_mode} -> {state.mode}")
                    else:
                        log.info(f"SELL fill processed: {previous_position:.6f} - {filled_qty:.6f} = {state.position_size:.6f} (keeping SELL mode)")

                # Clear order tracking after fill
                state.last_order_price = None
                state.last_order_side = None
                state.last_order_quantity = None

                # Add a small delay to avoid hammering the API after fills
                log.debug("Adding 0.1s delay after order fill to avoid API rate limits")
                await asyncio.sleep(0.1)

            except asyncio.TimeoutError:
                log.info(f"Order {state.active_order_id} not filled within {ORDER_REFRESH_INTERVAL}s. Cancelling and refreshing.")
                try:
                    if CANCEL_SPECIFIC_ORDER and state.active_order_id:
                        cancel_result = await client.cancel_order(args.symbol, state.active_order_id)
                        log.debug(f"Cancel order result: {cancel_result}")
                    else:
                        cancel_result = await client.cancel_all_orders(args.symbol)
                        log.debug(f"Cancel all orders result: {cancel_result}")
                except Exception as cancel_error:
                    log.warning(f"Error cancelling orders: {cancel_error}")

                # Clear order tracking data
                state.active_order_id = None
                state.last_order_price = None
                state.last_order_side = None
                state.last_order_quantity = None

                log.debug("Adding 0.1s delay after order timeout to avoid API rate limits")
                await asyncio.sleep(0.1)

        except asyncio.TimeoutError:
            log.warning("Timeout in main loop. Continuing...")
            await asyncio.sleep(1)
        except Exception as e:
            log.error(f"An error occurred in the main loop: {e}", exc_info=True)
            log.error(f"Current state: mode={state.mode}, position_size={state.position_size}, active_order_id={state.active_order_id}")
            log.error(f"Market data: mid_price={state.mid_price}, bid={state.bid_price}, ask={state.ask_price}")

            # Try to cancel any outstanding orders
            if state.active_order_id:
                try:
                    log.info(f"Attempting to cancel active order {state.active_order_id} due to error")
                    if CANCEL_SPECIFIC_ORDER:
                        await client.cancel_order(args.symbol, state.active_order_id)
                    else:
                        await client.cancel_all_orders(args.symbol)
                    state.active_order_id = None
                    # Clear tracking data
                    state.last_order_price = None
                    state.last_order_side = None
                    state.last_order_quantity = None
                except Exception as cleanup_error:
                    log.error(f"Failed to cancel orders during error cleanup: {cleanup_error}")

            log.info(f"Waiting for {RETRY_ON_ERROR_INTERVAL} seconds before retrying...")
            await asyncio.sleep(RETRY_ON_ERROR_INTERVAL)



async def fetch_initial_balance(state, client):
    """Fetch initial account balance via REST API."""
    log = logging.getLogger('InitialBalance')

    try:
        log.info("Fetching initial account balance...")
        account_info = await client.signed_request("GET", "/fapi/v3/account", {})
        balances = account_info.get('assets', [])

        for balance in balances:
            asset = balance.get('asset', '')
            wallet_balance = float(balance.get('walletBalance', '0'))

            if asset == 'USDF':
                state.usdf_balance = wallet_balance
                log.info(f"Initial USDF balance: {wallet_balance}")
            elif asset == 'USDT':
                state.usdt_balance = wallet_balance
                log.info(f"Initial USDT balance: {wallet_balance}")

        # Calculate total balance
        state.account_balance = state.usdf_balance + state.usdt_balance
        state.balance_last_updated = asyncio.get_event_loop().time()

        log.info(f"Initial balance loaded: USDF={state.usdf_balance:.4f}, USDT={state.usdt_balance:.4f}, Total=${state.account_balance:.4f}")
        return True

    except Exception as e:
        log.error(f"Failed to fetch initial balance: {e}", exc_info=True)
        return False


async def cleanup_orders(symbol, api_user, api_signer, api_private_key):
    """Cleanup function to cancel all orders"""
    try:
        logging.info(f"Performing final cleanup: Cancelling all orders for {symbol}.")
        async with ApiClient(api_user, api_signer, api_private_key, RELEASE_MODE) as cleanup_client:
            await cleanup_client.cancel_all_orders(symbol)
        logging.info("All open orders cancelled. Shutdown complete.")
    except Exception as e:
        logging.error(f"Error during final order cancellation: {e}")

# Global variables for signal handling
shutdown_requested = False
global_args = None
global_api_creds = None

def signal_handler(signum, frame):
    """Handle SIGTERM and SIGINT signals"""
    global shutdown_requested
    logging.info(f"Signal {signum} received, initiating shutdown...")
    shutdown_requested = True

async def main():
    global global_args, global_api_creds

    parser = argparse.ArgumentParser(description="A market making bot for Aster Finance.")
    parser.add_argument("--symbol", type=str, default=DEFAULT_SYMBOL, help="The symbol to trade.")
    args = parser.parse_args()
    global_args = args

    setup_logging("INFO")
    logging.info(f"Starting market maker with arguments: {args}")

    load_dotenv()
    API_USER = os.getenv("API_USER")
    API_SIGNER = os.getenv("API_SIGNER")
    API_PRIVATE_KEY = os.getenv("API_PRIVATE_KEY")
    global_api_creds = (API_USER, API_SIGNER, API_PRIVATE_KEY)

    # Set up signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    client = None
    tasks = []

    try:
        client = ApiClient(API_USER, API_SIGNER, API_PRIVATE_KEY, RELEASE_MODE)
        state = StrategyState()

        async with client:
            try:
                logging.info(f"Sending initial cancel all orders for {args.symbol} to ensure a clean slate.")
                await client.cancel_all_orders(args.symbol)
            except Exception as e:
                logging.warning(f"Failed to send initial cancel all orders, proceeding anyway: {e}")

            # [IMPROVED] Fetch initial account balance with a timeout
            logging.info("Fetching initial account balance...")
            try:
                balance_success = await asyncio.wait_for(fetch_initial_balance(state, client), timeout=20.0)
                if not balance_success:
                    logging.error("Failed to fetch initial balance. Cannot proceed.")
                    return
            except asyncio.TimeoutError:
                logging.error("Timed out while fetching initial balance. Cannot proceed.")
                return

            try:
                logging.info(f"Checking for existing position for {args.symbol}...")
                positions = await client.get_position_risk(args.symbol)
                logging.debug(f"Position risk response: {positions}")

                position_found = False
                if positions:
                    position = positions[0]
                    position_size = float(position.get('positionAmt', 0.0))
                    notional_value = abs(float(position.get('notional', 0.0)))

                    if position_size > 0 and notional_value > 15.0:
                        logging.info(f"Found existing LONG position of size {position_size} with notional value ${notional_value:.2f}.")
                        state.position_size = position_size
                        state.mode = 'SELL'
                        logging.info(f"Starting in SELL mode to close position.")
                        position_found = True

                if not position_found:
                    logging.info("No significant existing long position found.")
                    try:
                        logging.info(f"Attempting to set leverage for {args.symbol} to {DEFAULT_LEVERAGE}x.")
                        await client.change_leverage(args.symbol, DEFAULT_LEVERAGE)
                        logging.info(f"Successfully set leverage for {args.symbol} to {DEFAULT_LEVERAGE}x.")
                    except Exception as e:
                        logging.error(f"Failed to set leverage: {e}", exc_info=True)
                    logging.info("Starting in default BUY mode.")

            except Exception as e:
                logging.warning(f"Could not check for existing position or set leverage, starting in default BUY mode: {e}", exc_info=True)

            # Start all async tasks
            mm_task = asyncio.create_task(market_making_loop(state, client, args))
            tasks = [
                asyncio.create_task(websocket_price_updater(state, args.symbol)),
                asyncio.create_task(websocket_user_data_updater(state, client)),
                asyncio.create_task(balance_reporter(state)),
                mm_task,
                asyncio.create_task(price_reporter(state, args.symbol)),
            ]

            # Wait for either the market making task to complete or shutdown signal
            while not shutdown_requested and not mm_task.done():
                await asyncio.sleep(0.1)

    except asyncio.CancelledError:
        logging.info("Main task was cancelled.")
    except Exception as e:
        logging.error(f"An unhandled exception occurred in main: {e}", exc_info=True)
    finally:
        logging.info("Shutdown initiated. Cleaning up...")
        for task in tasks:
            if not task.done():
                task.cancel()
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Always perform cleanup
        await cleanup_orders(args.symbol, API_USER, API_SIGNER, API_PRIVATE_KEY)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutdown requested by user (Ctrl+C).")
