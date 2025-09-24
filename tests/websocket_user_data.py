#!/usr/bin/env python3
"""
WebSocket User Data Stream Example for Aster Finance
Connects to user data stream to receive real-time trade executions, order updates, and position changes.
"""

import asyncio
import json
import os
import sys
import websockets
from datetime import datetime
from dotenv import load_dotenv
from api_client import ApiClient

# Fix Windows encoding issues - use a safer approach
def setup_encoding():
    if sys.platform.startswith('win'):
        try:
            import codecs
            sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
            sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')
        except Exception:
            # If encoding setup fails, continue without emojis
            pass

setup_encoding()

# Load environment variables
load_dotenv()

API_USER = os.getenv('API_USER')
API_SIGNER = os.getenv('API_SIGNER')
API_PRIVATE_KEY = os.getenv('API_PRIVATE_KEY')

# Binance-style API keys for USER_STREAM endpoints
APIV1_PUBLIC_KEY = os.getenv('APIV1_PUBLIC_KEY')
APIV1_PRIVATE_KEY = os.getenv('APIV1_PRIVATE_KEY')

class UserDataStream:
    def __init__(self):
        self.api_client = ApiClient(API_USER, API_SIGNER, API_PRIVATE_KEY)
        self.listen_key = None
        self.websocket = None
        self.should_reconnect = True

    async def get_listen_key(self):
        """Get a listen key for the user data stream."""
        try:
            async with self.api_client as client:
                # Enable debug mode to see detailed error
                client.release_mode = False
                # Use Binance-style HMAC authentication for USER_STREAM endpoints
                response = await client.signed_request(
                    "POST",
                    "/fapi/v1/listenKey",
                    {},
                    use_binance_auth=True,
                    api_key=APIV1_PUBLIC_KEY,
                    api_secret=APIV1_PRIVATE_KEY
                )
                self.listen_key = response.get('listenKey')
                print(f"🔑 Listen key obtained: {self.listen_key[:20]}...")
                return self.listen_key
        except Exception as e:
            print(f"❌ Error getting listen key: {e}")
            print(f"❌ Exception type: {type(e)}")
            # Try to get more details from the response
            if hasattr(e, 'response'):
                print(f"❌ Response status: {e.response.status}")
                try:
                    error_text = await e.response.text()
                    print(f"❌ Response body: {error_text}")
                except:
                    pass
            return None

    async def keepalive_listen_key(self):
        """Extend the listen key validity by 60 minutes."""
        try:
            async with self.api_client as client:
                await client.signed_request(
                    "PUT",
                    "/fapi/v1/listenKey",
                    {},
                    use_binance_auth=True,
                    api_key=APIV1_PUBLIC_KEY,
                    api_secret=APIV1_PRIVATE_KEY
                )
                print(f"🔄 Listen key keepalive sent at {datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            print(f"❌ Error keeping listen key alive: {e}")

    async def close_listen_key(self):
        """Close the user data stream."""
        try:
            async with self.api_client as client:
                await client.signed_request(
                    "DELETE",
                    "/fapi/v1/listenKey",
                    {},
                    use_binance_auth=True,
                    api_key=APIV1_PUBLIC_KEY,
                    api_secret=APIV1_PRIVATE_KEY
                )
                print("🔒 Listen key closed")
        except Exception as e:
            print(f"❌ Error closing listen key: {e}")

    def format_timestamp(self, timestamp_ms):
        """Convert timestamp to readable format."""
        return datetime.fromtimestamp(timestamp_ms / 1000).strftime('%H:%M:%S.%f')[:-3]

    def print_trade_execution(self, order_data):
        """Print formatted trade execution information."""
        symbol = order_data.get('s', 'N/A')
        side = order_data.get('S', 'N/A')
        exec_type = order_data.get('x', 'N/A')
        order_status = order_data.get('X', 'N/A')
        filled_qty = order_data.get('l', '0')
        filled_price = order_data.get('L', '0')
        total_filled = order_data.get('z', '0')
        avg_price = order_data.get('ap', '0')
        commission = order_data.get('n', '0')
        commission_asset = order_data.get('N', 'N/A')
        realized_pnl = order_data.get('rp', '0')
        order_id = order_data.get('i', 'N/A')
        trade_time = self.format_timestamp(order_data.get('T', 0))

        print("\n" + "="*60)
        print(f"🔥 TRADE EXECUTION - {symbol}")
        print("="*60)
        print(f"📊 Order ID: {order_id}")
        print(f"⏰ Time: {trade_time}")
        print(f"📈 Side: {side}")
        print(f"🎯 Execution Type: {exec_type}")
        print(f"📋 Order Status: {order_status}")

        if float(filled_qty) > 0:
            print(f"💰 Last Fill: {filled_qty} @ ${filled_price}")

        print(f"📊 Total Filled: {total_filled}")

        if float(avg_price) > 0:
            print(f"💵 Average Price: ${avg_price}")

        if float(commission) > 0:
            print(f"💸 Commission: {commission} {commission_asset}")

        if float(realized_pnl) != 0:
            pnl_emoji = "📈" if float(realized_pnl) > 0 else "📉"
            print(f"{pnl_emoji} Realized PnL: ${realized_pnl}")

        print("="*60)

    def print_account_update(self, account_data):
        """Print formatted account and position updates."""
        reason = account_data.get('m', 'N/A')

        print("\n" + "="*60)
        print(f"💼 ACCOUNT UPDATE - {reason}")
        print("="*60)

        # Print balance changes
        balances = account_data.get('B', [])
        if balances:
            print("💰 Balance Changes:")
            for balance in balances:
                asset = balance.get('a', 'N/A')
                wallet_balance = balance.get('wb', '0')
                balance_change = balance.get('bc', '0')
                if float(balance_change) != 0:
                    change_emoji = "📈" if float(balance_change) > 0 else "📉"
                    print(f"  {change_emoji} {asset}: {wallet_balance} (Δ{balance_change})")

        # Print position changes
        positions = account_data.get('P', [])
        if positions:
            print("📊 Position Updates:")
            for position in positions:
                symbol = position.get('s', 'N/A')
                position_amt = position.get('pa', '0')
                entry_price = position.get('ep', '0')
                unrealized_pnl = position.get('up', '0')
                position_side = position.get('ps', 'N/A')

                if float(position_amt) != 0:
                    pnl_emoji = "📈" if float(unrealized_pnl) > 0 else "📉"
                    print(f"  📍 {symbol} ({position_side}): {position_amt} @ ${entry_price}")
                    print(f"    {pnl_emoji} Unrealized PnL: ${unrealized_pnl}")

        print("="*60)

    def print_margin_call(self, margin_data):
        """Print margin call alert."""
        cross_wallet = margin_data.get('cw', '0')
        positions = margin_data.get('p', [])

        print("\n" + "🚨" * 20)
        print("⚠️  MARGIN CALL ALERT  ⚠️")
        print("🚨" * 20)
        print(f"💼 Cross Wallet Balance: ${cross_wallet}")

        for position in positions:
            symbol = position.get('s', 'N/A')
            position_side = position.get('ps', 'N/A')
            position_amt = position.get('pa', '0')
            mark_price = position.get('mp', '0')
            unrealized_pnl = position.get('up', '0')
            maintenance_margin = position.get('mm', '0')

            print(f"⚠️  {symbol} ({position_side}): {position_amt}")
            print(f"   💵 Mark Price: ${mark_price}")
            print(f"   📉 Unrealized PnL: ${unrealized_pnl}")
            print(f"   🛡️  Required Margin: ${maintenance_margin}")

        print("🚨" * 20)

    async def handle_message(self, message):
        """Handle incoming WebSocket messages."""
        try:
            data = json.loads(message)
            event_type = data.get('e')
            event_time = self.format_timestamp(data.get('E', 0))

            if event_type == 'ORDER_TRADE_UPDATE':
                print(f"\n📡 [{event_time}] Order/Trade Update Received")
                order_data = data.get('o', {})

                # Check if this is a trade execution
                exec_type = order_data.get('x', '')
                if exec_type == 'TRADE':
                    self.print_trade_execution(order_data)
                else:
                    # Print basic order update
                    symbol = order_data.get('s', 'N/A')
                    side = order_data.get('S', 'N/A')
                    order_status = order_data.get('X', 'N/A')
                    order_id = order_data.get('i', 'N/A')
                    print(f"📋 Order Update: {symbol} {side} Order #{order_id} -> {order_status}")

            elif event_type == 'ACCOUNT_UPDATE':
                print(f"\n📡 [{event_time}] Account Update Received")
                account_data = data.get('a', {})
                self.print_account_update(account_data)

            elif event_type == 'MARGIN_CALL':
                print(f"\n📡 [{event_time}] Margin Call Received")
                self.print_margin_call(data)

            elif event_type == 'listenKeyExpired':
                print(f"\n⚠️  [{event_time}] Listen key expired!")
                print("🔄 Attempting to reconnect...")
                await self.reconnect()

            else:
                print(f"\n📡 [{event_time}] Unknown event: {event_type}")
                print(f"📄 Raw data: {data}")

        except json.JSONDecodeError as e:
            print(f"❌ Error parsing message: {e}")
        except Exception as e:
            print(f"❌ Error handling message: {e}")

    async def connect(self):
        """Connect to the user data stream."""
        if not self.listen_key:
            if not await self.get_listen_key():
                return False

        try:
            ws_url = f"wss://fstream.asterdex.com/ws/{self.listen_key}"
            print(f"🔗 Connecting to user data stream...")

            self.websocket = await websockets.connect(ws_url)
            print("✅ Connected to user data stream!")
            print("👂 Listening for trade executions, order updates, and position changes...")
            print("🛑 Press Ctrl+C to stop\n")

            return True

        except Exception as e:
            print(f"❌ Connection error: {e}")
            return False

    async def reconnect(self):
        """Reconnect to the user data stream."""
        try:
            if self.websocket:
                await self.websocket.close()

            # Get new listen key
            await self.get_listen_key()

            # Reconnect
            if await self.connect():
                print("✅ Reconnected successfully!")
            else:
                print("❌ Reconnection failed!")

        except Exception as e:
            print(f"❌ Reconnection error: {e}")

    async def start_keepalive_task(self):
        """Start the keepalive task to prevent listen key expiration."""
        while self.should_reconnect:
            await asyncio.sleep(3000)  # 50 minutes
            if self.should_reconnect:
                await self.keepalive_listen_key()

    async def listen(self):
        """Main listening loop."""
        # Start keepalive task
        keepalive_task = asyncio.create_task(self.start_keepalive_task())

        try:
            async for message in self.websocket:
                await self.handle_message(message)
        except websockets.exceptions.ConnectionClosed:
            print("🔌 WebSocket connection closed")
            if self.should_reconnect:
                print("🔄 Attempting to reconnect...")
                await asyncio.sleep(5)
                await self.reconnect()
        except Exception as e:
            print(f"❌ Listening error: {e}")
        finally:
            keepalive_task.cancel()

    async def start(self):
        """Start the user data stream."""
        print("📡 Initializing user data stream...")

        if not await self.connect():
            print("❌ Failed to connect. Exiting...")
            return

        try:
            await self.listen()
        except KeyboardInterrupt:
            print("\n🛑 Shutting down...")
        finally:
            await self.cleanup()

    async def cleanup(self):
        """Clean up resources."""
        self.should_reconnect = False

        if self.websocket:
            await self.websocket.close()
            print("🔌 WebSocket connection closed")

        if self.listen_key:
            await self.close_listen_key()

        print("✅ Cleanup completed")

async def main():
    """Main function."""
    print("🚀 Starting Aster Finance User Data Stream")

    # Validate environment variables
    if not all([API_USER, API_SIGNER, API_PRIVATE_KEY]):
        print("❌ Missing required Web3 authentication variables!")
        print("Please ensure API_USER, API_SIGNER, and API_PRIVATE_KEY are set in your .env file")
        return

    if not all([APIV1_PUBLIC_KEY, APIV1_PRIVATE_KEY]):
        print("❌ Missing required Binance-style API keys!")
        print("Please ensure APIV1_PUBLIC_KEY and APIV1_PRIVATE_KEY are set in your .env file")
        return

    print("✅ All credentials found")
    print("="*50)

    # Create and start the user data stream
    stream = UserDataStream()
    await stream.start()

if __name__ == "__main__":
    asyncio.run(main())