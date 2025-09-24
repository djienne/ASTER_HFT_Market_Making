#!/usr/bin/env python3
"""Unified terminal dashboard for balances, positions, orders, and mark prices."""

import argparse
import asyncio
import json
import os
import sys
import signal
import time
from contextlib import suppress
from datetime import datetime
from typing import Dict, Optional

import websockets
from websockets.exceptions import ConnectionClosedOK
from dotenv import load_dotenv

from api_client import ApiClient

STABLE_ASSETS = ("USDT", "USDC", "USDF")
MAX_ORDER_EVENTS = 6
REST_REFRESH_INTERVAL = 15
MARK_STREAM_RETRY = 3

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"

USE_COLOR = os.getenv("NO_COLOR") is None

def colorize(text: str, color: str) -> str:
    return f"{color}{text}{RESET}" if USE_COLOR else text


def to_float(value, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class TerminalDashboard:
    """Maintains shared state for the combined account/order/price dashboard."""

    def __init__(
        self,
        credentials: Dict[str, Optional[str]],
        stop_event: asyncio.Event,
        refresh_interval: int = REST_REFRESH_INTERVAL,
    ) -> None:
        self.credentials = credentials
        self.stop_event = stop_event
        self.refresh_interval = refresh_interval

        self.balances: Dict[str, Dict[str, str]] = {
            asset: {"wallet_balance": "0", "cross_wallet_balance": "0", "last_change": "0"}
            for asset in STABLE_ASSETS
        }
        self.positions: Dict[str, Dict[str, float]] = {}
        self.order_events = []
        self.mark_prices: Dict[str, Dict[str, float]] = {}

        self.account_update_count = 0
        self.order_update_count = 0
        self.trade_count = 0
        self.last_reason = "INIT"
        self.last_event_time = "--"
        self.margin_alerts = []

        self.start_time = datetime.now()
        self.latest_snapshot_time: Optional[datetime] = None

        self.mark_symbols = set()
        self.mark_stream_event = asyncio.Event()
        self.mark_stream_event.set()
        self._first_render = True
        self._last_book_render = 0.0
        self._book_render_interval = 0.3

    def _track_symbol(self, symbol: str) -> None:
        symbol = symbol.upper()
        if symbol and symbol not in self.mark_symbols:
            self.mark_symbols.add(symbol)
            self.mark_stream_event.set()

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------
    def ensure_stable(self) -> None:
        for asset in STABLE_ASSETS:
            self.balances.setdefault(
                asset,
                {"wallet_balance": "0", "cross_wallet_balance": "0", "last_change": "0"},
            )

    def _update_mark_symbols(self) -> None:
        symbols = {symbol for symbol, pos in self.positions.items() if pos.get("amount")}
        if symbols != self.mark_symbols:
            self.mark_symbols = symbols
            self.mark_stream_event.set()
        self._first_render = True

    def _recalc_unrealized(self, symbol: str) -> None:
        symbol = symbol.upper()
        pos = self.positions.get(symbol)
        if not pos:
            return
        mark_info = self.mark_prices.get(symbol)
        mark_price = mark_info.get("mark") if mark_info else None
        if mark_price is None:
            return
        entry_price = pos.get("entry")
        if entry_price is None:
            return
        amount = pos.get("amount", 0.0)
        pos["unrealized"] = (mark_price - entry_price) * amount

    # ------------------------------------------------------------------
    # Data ingestion
    # ------------------------------------------------------------------
    def update_from_snapshot(self, data: Dict[str, object]) -> None:
        for balance in data.get("assets", []):
            asset = balance.get("asset")
            if not asset:
                continue
            self.balances[asset] = {
                "wallet_balance": balance.get("walletBalance", "0"),
                "cross_wallet_balance": balance.get("crossWalletBalance", "0"),
                "last_change": balance.get("lastChangeBalance", "0"),
            }

        self.positions.clear()
        for position in data.get("positions", []):
            amount = to_float(position.get("positionAmt"))
            if amount == 0:
                continue
            symbol = position.get("symbol", "N/A").upper()
            raw_unrealized = (
                position.get("unRealizedProfit")
                or position.get("unrealizedProfit")
                or position.get("unrealizedPnl")
            )
            self.positions[symbol] = {
                "amount": amount,
                "entry": to_float(position.get("entryPrice")),
                "unrealized": to_float(raw_unrealized),
                "side": position.get("positionSide", "BOTH"),
            }
            self._recalc_unrealized(symbol)

        self.ensure_stable()
        self.latest_snapshot_time = datetime.now()
        self.margin_alerts.clear()
        self.last_reason = "REST SNAPSHOT"
        self._update_mark_symbols()

    def handle_account_update(self, payload: Dict[str, object], event_time: int = 0) -> None:
        self.last_reason = payload.get("m", "ACCOUNT_UPDATE")
        self.account_update_count += 1
        timestamp = (
            datetime.fromtimestamp(event_time / 1000).strftime("%H:%M:%S.%f")[:-3] if event_time else "--"
        )
        self.last_event_time = timestamp

        for balance in payload.get("B", []):
            asset = balance.get("a")
            if not asset:
                continue
            self.balances[asset] = {
                "wallet_balance": balance.get("wb", "0"),
                "cross_wallet_balance": balance.get("cw", "0"),
                "last_change": balance.get("bc", "0"),
            }

        for position in payload.get("P", []):
            symbol = position.get("s", "N/A").upper()
            amount = to_float(position.get("pa"))
            if amount == 0:
                self.positions.pop(symbol, None)
                continue
            raw_unrealized = (
                position.get("up")
                or position.get("unRealizedProfit")
                or position.get("unrealizedProfit")
                or position.get("unrealizedPnl")
            )
            self.positions[symbol] = {
                "amount": amount,
                "entry": to_float(position.get("ep")),
                "unrealized": to_float(raw_unrealized),
                "side": position.get("ps", "BOTH"),
            }
            self._recalc_unrealized(symbol)

        self.ensure_stable()
        self.latest_snapshot_time = datetime.now()
        self._update_mark_symbols()

    def handle_order_update(self, order: Dict[str, object]) -> None:
        event_time = order.get("T") or order.get("O") or 0
        timestamp = (
            datetime.fromtimestamp(event_time / 1000).strftime("%H:%M:%S.%f")[:-3] if event_time else "--"
        )
        entry = {
            "time": timestamp,
            "symbol": order.get("s", "N/A"),
            "side": order.get("S", "N/A"),
            "status": order.get("X", "N/A"),
            "exec": order.get("x", "N/A"),
            "qty": to_float(order.get("q")),
            "filled": to_float(order.get("z")),
            "price": to_float(order.get("p")),
            "avg": to_float(order.get("ap")),
            "last_fill_qty": to_float(order.get("l")),
            "last_fill_price": to_float(order.get("L")),
            "realized": to_float(order.get("rp")),
        }
        self._track_symbol(entry["symbol"])
        self.order_events.insert(0, entry)
        del self.order_events[MAX_ORDER_EVENTS:]

        self.order_update_count += 1
        if order.get("x") == "TRADE":
            self.trade_count += 1
        self.last_event_time = timestamp

    def handle_margin_call(self, payload: Dict[str, object], event_time: int = 0) -> None:
        timestamp = (
            datetime.fromtimestamp(event_time / 1000).strftime("%H:%M:%S.%f")[:-3] if event_time else "--"
        )
        self.last_event_time = timestamp
        alerts = []
        for pos in payload.get("p", []):
            symbol = pos.get("s", "N/A")
            side = pos.get("ps", "N/A")
            amount = pos.get("pa", "0")
            pnl = pos.get("up", "0")
            alerts.append(f"{symbol} {side} {amount} (PnL {pnl})")
        self.margin_alerts = alerts or ["Margin call event received"]
        self.last_reason = "MARGIN_CALL"

    # ------------------------------------------------------------------
    # Rendering helpers
    # ------------------------------------------------------------------
    def _prepare_frame(self) -> None:
        if self._first_render:
            if os.name == "nt":
                os.system("")
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            self._first_render = False
        else:
            sys.stdout.write("\033[H")
            sys.stdout.flush()

    def render(self, status: str = "WAITING") -> None:
        now = datetime.now()
        uptime = now - self.start_time
        stable_total = 0.0
        stable_lines = []
        for asset in STABLE_ASSETS:
            bal = to_float(self.balances.get(asset, {}).get("wallet_balance"))
            stable_total += bal
            stable_lines.append(f"  {asset}: {bal:,.4f} {asset}")
        other_balances = []
        for asset, info in sorted(self.balances.items()):
            if asset in STABLE_ASSETS:
                continue
            amount = to_float(info.get("wallet_balance"))
            if abs(amount) < 0.01:
                continue
            other_balances.append(f"  {asset}: {amount:,.4f} {asset}")
        total_unrealized = sum(pos.get("unrealized", 0.0) for pos in self.positions.values())
        total_equity = stable_total + total_unrealized
        snapshot = (
            self.latest_snapshot_time.strftime("%Y-%m-%d %H:%M:%S")
            if self.latest_snapshot_time
            else "--"
        )

        header = colorize("=== ASTER TERMINAL DASHBOARD ===", CYAN + BOLD if USE_COLOR else CYAN)
        self._prepare_frame()
        print(header)
        print(
            f"Snapshot: {snapshot} | Rendered: {now.strftime('%Y-%m-%d %H:%M:%S')} | Status: "
            f"{colorize(status, YELLOW if status not in {'CONNECTED', 'IDLE'} else GREEN)}"
        )
        print(
            f"Uptime: {int(uptime.total_seconds() // 60)}m {int(uptime.total_seconds() % 60)}s | "
            f"Last reason: {self.last_reason}"
        )
        print()

        print(colorize("Account Summary", BOLD))
        print(f"  Total Stablecoins: {stable_total:,.4f} USD")
        print(f"  Total Unrealized PnL: {total_unrealized:,.4f} USD")
        print(f"  Total Equity: {total_equity:,.4f} USD")
        print()

        print(colorize("Stablecoin Breakdown:", BOLD))
        for line in stable_lines:
            print(line)
        print(f"  Total Stablecoins: {stable_total:,.4f} USD")
        if other_balances:
            print()
            print(colorize("Other Balances:", BOLD))
            for line in other_balances:
                print(line)

        print()
        print(colorize("Open Positions:", BOLD))
        if self.positions:
            header_row = (
                f"{'Symbol':<10}{'Side':<6}{'Amount':>12}{'Entry':>12}{'Mark':>12}{'Mid':>12}{'Quote':>14}{'Unreal PnL':>14}{'Funding%':>10}"
            )
            print(header_row)
            for symbol, pos in sorted(self.positions.items()):
                amount = pos['amount']
                side = "LONG" if amount > 0 else "SHORT"
                mark_info = self.mark_prices.get(symbol)
                mark_display = "--"
                mid_display = '--'.rjust(12)
                funding_display = '--'.rjust(10)
                mark_val = None
                mid_val = None
                if mark_info:
                    mark_val = mark_info.get('mark')
                    if mark_val is not None:
                        mark_display = f"{mark_val:,.3f}"
                    mid_val = mark_info.get('mid')
                    if mid_val is not None:
                        mid_display = f"{mid_val:>12.3f}"
                    funding_val = mark_info.get('funding')
                    if funding_val is not None:
                        funding_plain = f"{funding_val:.4f}%".rjust(10)
                        if USE_COLOR:
                            funding_color = GREEN if funding_val >= 0 else RED
                            funding_display = colorize(funding_plain, funding_color)
                        else:
                            funding_display = funding_plain
                amount_plain = f"{amount:>12.4f}"
                pnl_value = pos.get('unrealized', 0.0)
                pnl_plain = f"{pnl_value:>14.2f}"
                entry_price = pos.get('entry')
                entry_display = f"{entry_price:>12.3f}" if entry_price is not None else '--'.rjust(12)
                quote_ref = mid_val if mid_val is not None else mark_val if mark_val is not None else entry_price
                quote_value = None
                quote_plain = '--'.rjust(14)
                if quote_ref is not None:
                    quote_value = amount * quote_ref
                    quote_plain = f"{quote_value:>14.3f}"
                if USE_COLOR:
                    amount_color = GREEN if amount >= 0 else RED
                    amount_text = colorize(amount_plain, amount_color)
                    side_color = GREEN if amount > 0 else RED
                    side_cell = colorize(f"{side:<6}", side_color)
                    quote_text = colorize(quote_plain, GREEN if quote_value is not None and quote_value >= 0 else RED) if quote_value is not None else quote_plain
                    pnl_color = GREEN if pnl_value >= 0 else RED
                    pnl_text = colorize(pnl_plain, pnl_color)
                else:
                    amount_text = amount_plain
                    side_cell = f"{side:<6}"
                    quote_text = quote_plain
                    pnl_text = pnl_plain
                mark_cell = f"{mark_display:>12}"
                print(
                    f"{symbol:<10}{side_cell}{amount_text}{entry_display}"
                    f"{mark_cell}{mid_display}{quote_text}{pnl_text} {funding_display}"
                )
        else:
            print(colorize('  None', DIM))

        print()
        print(colorize("Recent Orders:", BOLD))
        display_events = list(self.order_events[:MAX_ORDER_EVENTS])
        while len(display_events) < MAX_ORDER_EVENTS:
            display_events.append(None)
        for entry in display_events:
            if entry:
                qty = entry["qty"]
                filled = entry["filled"]
                progress = f"{filled:.4f}/{qty:.4f}" if qty else f"{filled:.4f}"
                avg_price = f"{entry['avg']:.3f}" if entry["avg"] else '0.000'
                realized = entry["realized"]
                if abs(realized) < 1e-9:
                    pnl_label = "0.00 USD"
                else:
                    pnl_label = f"{realized:+.4f} USD"
                    if USE_COLOR:
                        pnl_color = GREEN if realized >= 0 else RED
                        pnl_label = colorize(pnl_label, pnl_color)
                time_str = entry['time']
                symbol = entry['symbol']
                side_str = entry['side']
                status_str = entry['status']
                exec_type = entry['exec']
                progress_str = progress
                avg_str = avg_price
                price_value = entry['price']
                price_str = f"{price_value:.3f}" if price_value else '0.000'
                pct_str = '--'
                mark_info = self.mark_prices.get(symbol)
                ref_price = None
                mid_str = '--'
                if mark_info:
                    mid_val = mark_info.get('mid')
                    mark_val = mark_info.get('mark')
                    if mid_val:
                        ref_price = mid_val
                        mid_str = f"{mid_val:.3f}"
                    elif mark_val:
                        ref_price = mark_val
                        mid_str = f"{mark_val:.3f}"
                if price_value and ref_price:
                    pct = (price_value - ref_price) / ref_price * 100
                    pct_str = f"{pct:+.2f}%"
                    if USE_COLOR:
                        pct_color = GREEN if pct <= 0 else RED
                        pct_str = colorize(pct_str, pct_color)
                pnl_str = pnl_label
                print(
                    f"  {time_str:<8} {symbol:<10} {side_str:<5} {status_str:<13} ({exec_type:<8}) "
                    f"qty {progress_str:<18} avg {avg_str:>7} limit {price_str:>8} mid {mid_str:>8} dev {pct_str:<9} pnl {pnl_str:<12}"
                )
            else:
                print(colorize("  -- waiting for order activity --", DIM))

        print()
        print(colorize("Alerts:", BOLD))
        if self.margin_alerts:
            for note in self.margin_alerts[-3:]:
                print(colorize(f"  ! {note}", RED))
        else:
            print(colorize("  None", DIM))

        print()
        print(colorize("Stats:", BOLD))
        print(
            f"  Account updates: {self.account_update_count} | Order updates: {self.order_update_count} | Trades: {self.trade_count}"
        )
        print(f"  Last event time: {self.last_event_time}")
        print()
        print(colorize("Press Ctrl+C to exit.", DIM))
        sys.stdout.flush()

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------
    async def periodic_refresh(self) -> None:
        while not self.stop_event.is_set():
            try:
                async with ApiClient(
                    self.credentials["api_user"],
                    self.credentials["api_signer"],
                    self.credentials["api_private_key"],
                ) as client:
                    snapshot = await client.signed_request("GET", "/fapi/v3/account", {})
                self.update_from_snapshot(snapshot)
                self.render("REST REFRESH")
            except Exception as exc:  # noqa: BLE001
                self.last_reason = f"Refresh error: {exc}"
                self.render("REFRESH ERROR")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=self.refresh_interval)
            except asyncio.TimeoutError:
                continue
        self.last_reason = "Refresh stopped"

    async def mark_price_listener(self) -> None:
        while not self.stop_event.is_set():
            await self.mark_stream_event.wait()
            self.mark_stream_event.clear()
            if self.stop_event.is_set():
                break
            symbols = sorted(self.mark_symbols)
            if not symbols:
                self.mark_prices.clear()
                continue
            stream_parts = []
            for symbol in symbols:
                slug = symbol.lower()
                stream_parts.append(f"{slug}@markPrice@1s")
                stream_parts.append(f"{slug}@bookTicker")
            url = f"wss://fstream.asterdex.com/stream?streams={'/'.join(stream_parts)}"
            try:
                async with websockets.connect(url) as ws:
                    self.last_reason = "MARK STREAM"
                    self.render("MARK STREAM")
                    while not self.stop_event.is_set():
                        recv_task = asyncio.create_task(ws.recv())
                        change_task = asyncio.create_task(self.mark_stream_event.wait())
                        stop_task = asyncio.create_task(self.stop_event.wait())
                        done, pending = await asyncio.wait(
                            {recv_task, change_task, stop_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()
                        if stop_task in done:
                            stop_task.result()
                            break
                        if change_task in done:
                            change_task.result()
                            break
                        message = recv_task.result()
                        data = json.loads(message)
                        payload = data.get("data", data)
                        event_type = payload.get("e")
                        if not event_type:
                            continue
                        symbol = payload.get("s", "").upper()
                        if not symbol:
                            continue
                        info = self.mark_prices.setdefault(symbol, {})
                        if event_type == "markPriceUpdate":
                            info.update({
                                "mark": to_float(payload.get("p")),
                                "index": to_float(payload.get("i")),
                                "funding": to_float(payload.get("r")) * 100,
                                "time": payload.get("E"),
                            })
                            self._recalc_unrealized(symbol)
                            self.render("MARK PRICE")
                            continue
                        if event_type != "bookTicker":
                            continue
                        bid = to_float(payload.get("b"))
                        ask = to_float(payload.get("a"))
                        updated = False
                        if bid > 0:
                            info["bid"] = bid
                            updated = True
                        if ask > 0:
                            info["ask"] = ask
                            updated = True
                        mid = None
                        if bid > 0 and ask > 0:
                            mid = (bid + ask) / 2
                        elif ask > 0:
                            mid = ask
                        elif bid > 0:
                            mid = bid
                        if mid is not None:
                            info["mid"] = mid
                            updated = True
                        if updated:
                            info["book_time"] = payload.get("E")
                            now = time.monotonic()
                            if now - self._last_book_render >= self._book_render_interval:
                                self._last_book_render = now
                                self.render("BOOK TICKER")
            except ConnectionClosedOK:
                self.last_reason = "Mark stream closed"
                self.render("MARK STREAM CLOSED")
                await asyncio.sleep(MARK_STREAM_RETRY)
            except Exception as exc:  # noqa: BLE001
                self.last_reason = f"Mark stream error: {exc}"
                self.render("MARK STREAM ERROR")
                await asyncio.sleep(MARK_STREAM_RETRY)
        self.last_reason = "Mark stream stopped"

    async def stream(self, ws_url: str) -> None:
        try:
            async with websockets.connect(ws_url) as ws:
                self.render("CONNECTED")
                while not self.stop_event.is_set():
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=3)
                    except asyncio.TimeoutError:
                        self.render("IDLE")
                        continue
                    except ConnectionClosedOK:
                        self.last_reason = "User stream closed"
                        self.render("CONNECTION CLOSED")
                        break
                    data = json.loads(message)
                    event_type = data.get("e", "unknown")
                    if event_type == "ACCOUNT_UPDATE":
                        self.handle_account_update(data.get("a", {}), data.get("E", 0))
                        self.render("ACCOUNT UPDATE")
                    elif event_type == "ORDER_TRADE_UPDATE":
                        self.handle_order_update(data.get("o", {}))
                        self.render("ORDER EVENT")
                    elif event_type == "MARGIN_CALL":
                        self.handle_margin_call(data, data.get("E", 0))
                        self.render("MARGIN CALL")
                    elif event_type == "listenKeyExpired":
                        self.last_reason = "listenKeyExpired"
                        self.render("LISTEN KEY EXPIRED")
                        break
                    else:
                        self.last_reason = f"Unhandled {event_type}"
                        self.render("UNHANDLED EVENT")
        except ConnectionClosedOK:
            self.last_reason = "Stream closed"
            self.render("CONNECTION CLOSED")
        except Exception as exc:  # noqa: BLE001
            if not self.stop_event.is_set():
                self.last_reason = f"Stream error: {exc}"
                self.render("STREAM ERROR")


async def run_dashboard(args: argparse.Namespace) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _handle_signal(signum, frame):  # noqa: ARG001
        if not stop_event.is_set():
            loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass

    load_dotenv()

    api_user = os.getenv("API_USER")
    api_signer = os.getenv("API_SIGNER")
    api_private_key = os.getenv("API_PRIVATE_KEY")
    apiv1_public = os.getenv("APIV1_PUBLIC_KEY")
    apiv1_private = os.getenv("APIV1_PRIVATE_KEY")

    if not all([apiv1_public, apiv1_private]):
        print("ERROR: Missing APIV1_PUBLIC_KEY or APIV1_PRIVATE_KEY")
        return

    credentials = {
        "api_user": api_user,
        "api_signer": api_signer,
        "api_private_key": api_private_key,
    }

    dashboard = TerminalDashboard(credentials, stop_event, refresh_interval=args.refresh_interval)

    async with ApiClient(api_user, api_signer, api_private_key) as client:
        snapshot = await client.signed_request("GET", "/fapi/v3/account", {})
        dashboard.update_from_snapshot(snapshot)
        response = await client.signed_request(
            "POST",
            "/fapi/v1/listenKey",
            {},
            use_binance_auth=True,
            api_key=apiv1_public,
            api_secret=apiv1_private,
        )
        listen_key = response["listenKey"]

    ws_url = f"wss://fstream.asterdex.com/ws/{listen_key}"

    refresh_task = asyncio.create_task(dashboard.periodic_refresh())
    mark_task = asyncio.create_task(dashboard.mark_price_listener())
    stream_task = asyncio.create_task(dashboard.stream(ws_url))

    tasks = {refresh_task, mark_task, stream_task}
    if args.duration > 0:
        duration_task = asyncio.create_task(asyncio.sleep(args.duration))
        tasks.add(duration_task)
    else:
        duration_task = None

    signal_task = asyncio.create_task(stop_event.wait())
    tasks.add(signal_task)

    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    if duration_task and duration_task in done:
        dashboard.render("TIMEOUT")
        print(f"\nReached duration limit ({args.duration}s); exiting.")

    stop_event.set()
    dashboard.mark_stream_event.set()

    for task in tasks:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


    
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live account/order dashboard")
    parser.add_argument(
        "--duration",
        type=int,
        default=3600,
        help="Seconds to run before auto exit (<=0 to run until interrupted)",
    )
    parser.add_argument(
        "--refresh-interval",
        type=int,
        default=REST_REFRESH_INTERVAL,
        help="Seconds between REST account refresh calls",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(run_dashboard(args))


if __name__ == "__main__":
    main()
