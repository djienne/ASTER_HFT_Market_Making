import time
import aiohttp
import hmac
import hashlib
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_abi import encode
import json
import math


def _trim_dict(my_dict):
    """Helper function to convert all dictionary values to strings recursively."""
    for key, value in my_dict.items():
        if isinstance(value, list):
            new_value = [json.dumps(_trim_dict(item)) if isinstance(item, dict) else str(item) for item in value]
            my_dict[key] = json.dumps(new_value)
        elif isinstance(value, dict):
            my_dict[key] = json.dumps(_trim_dict(value))
        else:
            my_dict[key] = str(value)
    return my_dict


class ApiClient:
    """
    An asynchronous client for interacting with the Aster Finance API,
    handling session management and request signing.
    """

    def __init__(self, api_user, api_signer, api_private_key, release_mode=True):
        # Ethereum-style credentials
        if not api_user or not Web3.is_address(api_user):
            raise ValueError("API_USER is missing or not a valid Ethereum address.")
        if not api_signer or not Web3.is_address(api_signer):
            raise ValueError("API_SIGNER is missing or not a valid Ethereum address.")
        if not api_private_key:
            raise ValueError("API_PRIVATE_KEY is missing.")

        self.api_user = api_user
        self.api_signer = api_signer
        self.api_private_key = api_private_key
        self.release_mode = release_mode

        self.base_url = "https://fapi.asterdex.com"
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    def _sign(self, params):
        """Signs the request parameters using the Ethereum signature method."""
        nonce = math.trunc(time.time() * 1000000)
        my_dict = {k: v for k, v in params.items() if v is not None}
        my_dict["recvWindow"] = 50000
        my_dict["timestamp"] = int(round(time.time() * 1000))

        _trim_dict(my_dict)
        json_str = json.dumps(my_dict, sort_keys=True).replace(' ', '').replace("'", '"')

        encoded = encode(['string', 'address', 'address', 'uint256'],
                         [json_str, self.api_user, self.api_signer, nonce])
        keccak_hex = Web3.keccak(encoded).hex()

        signable_msg = encode_defunct(hexstr=keccak_hex)
        signed_message = Account.sign_message(signable_message=signable_msg, private_key=self.api_private_key)

        my_dict['nonce'] = nonce
        my_dict['user'] = self.api_user
        my_dict['signer'] = self.api_signer
        my_dict['signature'] = '0x' + signed_message.signature.hex()

        return my_dict

    async def get_exchange_info(self):
        """Gets exchange information. This is a public endpoint."""
        url = f"{self.base_url}/fapi/v1/exchangeInfo"
        async with self.session.get(url) as response:
            response.raise_for_status()
            return await response.json()

    async def get_symbol_filters(self, symbol: str) -> dict:
        exchange_info = await self.get_exchange_info()
        for sym_data in exchange_info.get('symbols', []):
            if sym_data['symbol'] == symbol:
                filters = {f['filterType']: f for f in sym_data.get('filters', [])}
                price_filter = filters.get('PRICE_FILTER', {})
                tick_size_str = price_filter.get('tickSize', '0.01')
                price_precision = len(tick_size_str.split('.')[1].rstrip('0')) if '.' in tick_size_str else 0
                lot_size_filter = filters.get('LOT_SIZE', {})
                step_size_str = lot_size_filter.get('stepSize', '0.01')
                quantity_precision = len(step_size_str.split('.')[1].rstrip('0')) if '.' in step_size_str else 0
                return {
                    'price_precision': price_precision,
                    'tick_size': float(tick_size_str),
                    'quantity_precision': quantity_precision,
                    'step_size': float(step_size_str),
                    'min_notional': float(filters.get('MIN_NOTIONAL', {}).get('notional', '5.0'))
                }
        raise ValueError(f"Could not find filters for symbol '{symbol}'.")

    async def place_order(self, symbol, price, quantity, side, reduce_only=False):
        """Places a limit post-only order using Ethereum signature auth."""
        url = f"{self.base_url}/fapi/v3/order"
        params = {
            "symbol": symbol, "side": side, "type": "LIMIT",
            "timeInForce": "GTX", "price": price, "quantity": quantity,
            "positionSide": "BOTH"
        }
        if reduce_only:
            params['reduceOnly'] = 'true'

        signed_params = self._sign(params)
        headers = {'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'PythonApp/1.0'}

        # print("📤 Sending order request with params:", params)
        # print("🔐 Signed params keys:", list(signed_params.keys()))
        # print("📋 Full signed params:", signed_params)

        async with self.session.post(url, data=signed_params, headers=headers) as response:
            # print(f"📨 Response status: {response.status}")
            if not response.ok:
                error_body = await response.text()
                # print(f"❌ API Error on order placement: Status={response.status}")
                # print(f"❌ Error body: {error_body}")
                # print(f"📋 Request params that caused error: {params}")
                # print(f"🔐 Signed params that caused error: {signed_params}")
            else:
                # print("✅ Order request successful")
                pass
            response.raise_for_status()
            result = await response.json()
            # print("📨 Order response:", result)
            return result

    async def get_order_status(self, symbol, order_id):
        """Gets order status using Ethereum signature auth."""
        url = f"{self.base_url}/fapi/v3/order"
        params = {"symbol": symbol, "orderId": order_id}
        signed_params = self._sign(params)

        if not self.release_mode:
            print("Sending status request with params:", params)
        async with self.session.get(url, params=signed_params) as response:
            if not response.ok:
                error_body = await response.text()
                print(f"API Error on order status check: Status={response.status}, Body={error_body}")
            response.raise_for_status()
            return await response.json()

    async def cancel_order(self, symbol: str, order_id: int) -> dict:
        """Cancels an order using Ethereum signature auth."""
        url = f"{self.base_url}/fapi/v3/order"
        params = {"symbol": symbol, "orderId": order_id}
        signed_params = self._sign(params)
        headers = {'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'PythonApp/1.0'}

        if not self.release_mode:
            print(f"Cancelling order {order_id} for symbol: {symbol}")
        async with self.session.delete(url, data=signed_params, headers=headers) as response:
            if not response.ok:
                error_body = await response.text()
                print(f"API Error on cancel order: Status={response.status}, Body={error_body}")
            response.raise_for_status()
            return await response.json()

    async def cancel_all_orders(self, symbol: str) -> dict:
        """Cancels all orders for a symbol using Ethereum signature auth."""
        url = f"{self.base_url}/fapi/v3/allOpenOrders"
        params = {"symbol": symbol}
        signed_params = self._sign(params)
        headers = {'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'PythonApp/1.0'}

        if not self.release_mode:
            print(f"Cancelling all open orders for symbol: {symbol}")
        async with self.session.delete(url, data=signed_params, headers=headers) as response:
            if not response.ok:
                error_body = await response.text()
                print(f"API Error on cancel all orders: Status={response.status}, Body={error_body}")
            response.raise_for_status()
            return await response.json()

    async def get_position_risk(self, symbol: str = None):
        """Gets position risk information using Ethereum signature auth."""
        url = f"{self.base_url}/fapi/v3/positionRisk"
        params = {}
        if symbol:
            params["symbol"] = symbol

        signed_params = self._sign(params)

        async with self.session.get(url, params=signed_params) as response:
            if not response.ok:
                error_body = await response.text()
                print(f"API Error on get position risk: Status={response.status}, Body={error_body}")
            response.raise_for_status()
            return await response.json()

    async def signed_request(self, method: str, endpoint: str, params: dict = None, use_binance_auth=False, api_key=None, api_secret=None):
        """Generic method for making signed requests to the API.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint path
            params: Request parameters
            use_binance_auth: If True, use Binance-style HMAC authentication
            api_key: API key for Binance-style auth
            api_secret: API secret for Binance-style auth
        """
        if params is None:
            params = {}

        url = f"{self.base_url}{endpoint}"

        if use_binance_auth and api_key and api_secret:
            # For USER_STREAM endpoints - use Binance-style HMAC authentication
            import time
            import hmac
            import hashlib
            import urllib.parse

            # Add timestamp
            params['timestamp'] = int(time.time() * 1000)
            params['recvWindow'] = 5000

            # Create query string
            query_string = urllib.parse.urlencode(sorted(params.items()))

            # Create signature
            signature = hmac.new(
                api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()

            # Add signature to params
            params['signature'] = signature

            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': 'PythonApp/1.0',
                'X-MBX-APIKEY': api_key
            }
            request_params = params
        else:
            # For TRADE/USER_DATA endpoints - need full signature
            signed_params = self._sign(params)
            headers = {'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'PythonApp/1.0'}
            request_params = signed_params

        if method.upper() == 'GET':
            async with self.session.get(url, params=request_params, headers=headers) as response:
                if not response.ok:
                    error_body = await response.text()
                    if not self.release_mode:
                        print(f"API Error on {method} {endpoint}: Status={response.status}, Body={error_body}")
                response.raise_for_status()
                return await response.json()

        elif method.upper() == 'POST':
            async with self.session.post(url, data=request_params, headers=headers) as response:
                if not response.ok:
                    error_body = await response.text()
                    if not self.release_mode:
                        print(f"API Error on {method} {endpoint}: Status={response.status}, Body={error_body}")
                response.raise_for_status()
                return await response.json()

        elif method.upper() == 'PUT':
            async with self.session.put(url, data=request_params, headers=headers) as response:
                if not response.ok:
                    error_body = await response.text()
                    if not self.release_mode:
                        print(f"API Error on {method} {endpoint}: Status={response.status}, Body={error_body}")
                response.raise_for_status()
                return await response.json()

        elif method.upper() == 'DELETE':
            async with self.session.delete(url, data=request_params, headers=headers) as response:
                if not response.ok:
                    error_body = await response.text()
                    if not self.release_mode:
                        print(f"API Error on {method} {endpoint}: Status={response.status}, Body={error_body}")
                response.raise_for_status()
                return await response.json()

        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

    async def change_leverage(self, symbol: str, leverage: int):
        """Changes the initial leverage for a symbol."""
        url = f"{self.base_url}/fapi/v3/leverage"
        params = {
            "symbol": symbol,
            "leverage": leverage
        }
        signed_params = self._sign(params)
        headers = {'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'PythonApp/1.0'}

        if not self.release_mode:
            print(f"Changing leverage for {symbol} to {leverage}x")
        async with self.session.post(url, data=signed_params, headers=headers) as response:
            if not response.ok:
                error_body = await response.text()
                print(f"API Error on changing leverage: Status={response.status}, Body={error_body}")
            response.raise_for_status()
            return await response.json()