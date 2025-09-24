#!/usr/bin/env python3
"""
Step-by-step test of user data stream functionality
"""

import asyncio
import os
import sys
import websockets
from datetime import datetime
from dotenv import load_dotenv
from api_client import ApiClient

# Fix Windows encoding issues
if sys.platform.startswith('win'):
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
    sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')

# Load environment variables
load_dotenv()

API_USER = os.getenv('API_USER')
API_SIGNER = os.getenv('API_SIGNER')
API_PRIVATE_KEY = os.getenv('API_PRIVATE_KEY')
APIV1_PUBLIC_KEY = os.getenv('APIV1_PUBLIC_KEY')
APIV1_PRIVATE_KEY = os.getenv('APIV1_PRIVATE_KEY')

async def step_by_step_test():
    """Test each step individually."""
    print("🚀 Step-by-step User Data Stream Test")
    print("="*50)

    # Step 1: Get listen key
    print("📋 Step 1: Getting listen key...")
    try:
        client = ApiClient(API_USER, API_SIGNER, API_PRIVATE_KEY, release_mode=False)
        async with client:
            response = await client.signed_request(
                "POST",
                "/fapi/v1/listenKey",
                {},
                use_binance_auth=True,
                api_key=APIV1_PUBLIC_KEY,
                api_secret=APIV1_PRIVATE_KEY
            )
            listen_key = response.get('listenKey')
            print(f"✅ Listen key: {listen_key[:20]}...")
    except Exception as e:
        print(f"❌ Step 1 failed: {e}")
        return

    # Step 2: Connect to WebSocket
    print("\n📋 Step 2: Connecting to WebSocket...")
    try:
        ws_url = f"wss://fstream.asterdex.com/ws/{listen_key}"
        print(f"🔗 URL: {ws_url}")

        websocket = await websockets.connect(ws_url)
        print("✅ WebSocket connected!")

        # Step 3: Listen for a few messages
        print("\n📋 Step 3: Listening for messages...")
        print("👂 Waiting for user data events (10 second timeout)...")

        try:
            message = await asyncio.wait_for(websocket.recv(), timeout=10)
            print(f"📨 Received: {message}")
        except asyncio.TimeoutError:
            print("⏰ No messages received within 10 seconds (this is normal if no trading activity)")

        await websocket.close()
        print("🔌 WebSocket closed")

    except Exception as e:
        print(f"❌ Step 2/3 failed: {e}")

    print("\n✅ Test completed!")

if __name__ == "__main__":
    asyncio.run(step_by_step_test())