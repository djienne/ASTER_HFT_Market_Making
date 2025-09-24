#!/usr/bin/env python3
"""
Test script to check if we can get a listen key with the new authentication
"""

import asyncio
import os
import sys
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

async def test_listen_key():
    """Test getting a listen key."""
    print("🧪 Testing listen key authentication...")

    # Check environment variables
    print(f"API_USER: {API_USER[:10] if API_USER else 'None'}...")
    print(f"APIV1_PUBLIC_KEY: {APIV1_PUBLIC_KEY[:10] if APIV1_PUBLIC_KEY else 'None'}...")
    print(f"APIV1_PRIVATE_KEY: {APIV1_PRIVATE_KEY[:10] if APIV1_PRIVATE_KEY else 'None'}...")

    if not all([APIV1_PUBLIC_KEY, APIV1_PRIVATE_KEY]):
        print("❌ Missing APIV1 keys!")
        return

    try:
        client = ApiClient(API_USER, API_SIGNER, API_PRIVATE_KEY, release_mode=False)

        async with client:
            print("📡 Attempting to get listen key...")
            response = await client.signed_request(
                "POST",
                "/fapi/v1/listenKey",
                {},
                use_binance_auth=True,
                api_key=APIV1_PUBLIC_KEY,
                api_secret=APIV1_PRIVATE_KEY
            )

            listen_key = response.get('listenKey')
            if listen_key:
                print(f"✅ Success! Listen key: {listen_key[:20]}...")
                print(f"📝 Full response: {response}")
            else:
                print(f"❌ No listen key in response: {response}")

    except Exception as e:
        print(f"❌ Error: {e}")
        print(f"❌ Error type: {type(e)}")

if __name__ == "__main__":
    asyncio.run(test_listen_key())