# Avellaneda-Stoikov Market Making Model Parameter Calculator
# This script implements the optimal market making strategy from
# "High-frequency trading in a limit order book" by Avellaneda & Stoikov (2008)

import numpy as np
import pandas as pd
import scipy.optimize
from scipy.optimize import brentq, fsolve
import sys
import os
import argparse
from pathlib import Path
import warnings
# warnings.filterwarnings('ignore')
from numba import jit
import json
import asyncio
from typing import Tuple, Optional
import logging
import math
from typing import Dict, Any
from arch import arch_model

PARAMS_DIR = os.getenv("PARAMS_DIR", "params")
os.makedirs(PARAMS_DIR, exist_ok=True)

logging.getLogger('numba').setLevel(logging.WARNING)

# Only compute the most recent parameter periods to avoid heavy full-history runs.
RECENT_PARAM_PERIODS = 4
# Gamma backtesting window (period count) used when optimizing the risk aversion.
GAMMA_CALCULATION_WINDOW = 4

def _finite_nonneg(x) -> bool:
    try:
        v = float(x)
        return math.isfinite(v) and v >= 0.0
    except Exception:
        return False

def save_avellaneda_params_atomic(params: Dict[str, Any], symbol: str) -> bool:
    """
    Writes params to PARAMS_DIR/avellaneda_parameters_<SYMBOL>.json
    via a .tmp file + os.replace (atomic) if validation passes.
    Returns True if the final file was updated, False otherwise.
    """
    limit_orders = (params or {}).get("limit_orders") or {}
    da = limit_orders.get("delta_a")
    db = limit_orders.get("delta_b")

    final_path = os.path.join(PARAMS_DIR, f"avellaneda_parameters_{symbol}.json")
    tmp_path = final_path + ".tmp"

    # Strict validation
    if not (_finite_nonneg(da) and _finite_nonneg(db)):
        # Do NOT overwrite a good file with a bad calculation
        return False

    with open(tmp_path, "w") as f:
        json.dump(params, f, indent=4)

    # Atomic replacement
    os.replace(tmp_path, final_path)
    return True

def running_in_docker() -> bool:
    """Return True if running inside a Docker container."""
    return os.path.exists('/.dockerenv')

def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description='Calculate Avellaneda-Stoikov market making parameters')
    parser.add_argument('ticker', nargs='?', default='BNB', help='Ticker symbol (default: BNB)')
    parser.add_argument('--minutes', type=int, default=5,
                        help='Frequency in minutes to recalculate parameters (default: 5)')
    return parser.parse_args()

def get_fallback_tick_size(ticker):
    """Get tick size based on the ticker symbol."""
    if ticker == 'BTC':
        return 0.1
    elif ticker == 'ETH':
        return 0.1
    elif ticker == 'SOL':
        return 0.01
    elif ticker == 'WLFI':
        return 0.0001
    elif ticker == 'PAXG':
        return 0.01
    elif ticker == 'ASTER':
        return 0.0001
    elif ticker == 'BNB':
        return 0.1
    else:
        return 0.01

def load_trades_data(csv_path):
    """Load trades data from a CSV file."""
    df = pd.read_csv(csv_path)
    # Convert unix timestamp in milliseconds to datetime
    df['datetime'] = pd.to_datetime(df['unix_timestamp_ms'], unit='ms')
    df = df.set_index('datetime')
    return df

def load_and_resample_mid_price(csv_path):
    """Load and resample mid-price data from a CSV file."""
    df = pd.read_csv(csv_path)
    # Convert unix timestamp to datetime
    df['datetime'] = pd.to_datetime(df['unix_timestamp'], unit='s')

    # Drop duplicate timestamps before setting the index, keeping the last entry
    if df['datetime'].duplicated().any():
        print("Warning: Duplicate timestamps found in price data. Keeping last entry for each.")
        df.drop_duplicates(subset=['datetime'], keep='last', inplace=True)

    df = df.set_index('datetime')

    # Use the new simplified CSV format with direct bid/ask columns
    df['price_bid'] = df['bid']
    df['price_ask'] = df['ask']

    # Resample and forward fill
    merged = df[['price_bid', 'price_ask']].resample('s').ffill()
    merged['mid_price'] = (merged['price_bid'] + merged['price_ask']) / 2
    merged.dropna(inplace=True)
    return merged

def calculate_garch_volatility(mid_price_df, window_minutes, periods):
    """Calculate volatility using GARCH(1,1) for the requested periods only."""
    print("Calculating GARCH(1,1) Volatility...")

    if not periods:
        return []

    sigma_garch_list = []

    for i, period_start in enumerate(periods):
        period_end = period_start + pd.Timedelta(minutes=window_minutes)

        # Use all history up to the end of this period for a stable fit
        mask = mid_price_df.index <= period_end
        historical_data = mid_price_df.loc[mask]

        if len(historical_data) < 100:
            sigma_garch_list.append(np.nan)
            continue

        scale_fact = 1000.0  # scale factor to avoid warnings.

        returns = historical_data['mid_price'].pct_change().dropna() * 100.0 * scale_fact

        if len(returns) < 100:
            sigma_garch_list.append(np.nan)
            continue

        try:
            am = arch_model(returns, mean='Constant', vol='GARCH', p=1, q=1, dist='t')
            res = am.fit(disp='off', show_warning=False)

            forecasts = res.forecast(horizon=1)
            variance_next = forecasts.variance.iloc[-1, 0]
            volatility_next = variance_next**0.5

            volatility_decimal = volatility_next / 100.0 / scale_fact
            sigma_daily = volatility_decimal * np.sqrt(60 * window_minutes)

            sigma_garch_list.append(sigma_daily)

            if i == len(periods) - 1:
                print("\nGARCH Model Results for latest period:")
                print(f"  Omega (α₀): {res.params['omega']:.6f}")
                print(f"  Alpha (α₁): {res.params['alpha[1]']:.6f}")
                print(f"  Beta (β₁):  {res.params['beta[1]']:.6f}")
                print(f"  Nu (df):    {res.params['nu']:.2f}")
                print(f"  Persistence: {res.params['alpha[1]'] + res.params['beta[1]']:.6f}")

        except Exception:
            sigma_garch_list.append(np.nan)

    valid_sigmas = [s for s in sigma_garch_list if not pd.isna(s)]

    if valid_sigmas:
        print(f"Calculated {len(valid_sigmas)} valid GARCH sigma values.")
        print("Latest GARCH volatility values:")
        for s in sigma_garch_list[-3:]:
            if not pd.isna(s):
                print(f"  - {s:.6f}")
    else:
        print("No valid GARCH sigma values calculated.")

    return sigma_garch_list

def calculate_rolling_volatility(mid_price_df, window_minutes, freq_str, periods):
    """Calculate rolling volatility (sigma) for the requested periods."""
    print("Calculating rolling volatility as fallback...")

    if not periods:
        return []

    window_periods = 6  # Default window from calculate_sigma.py

    log_returns = np.log(mid_price_df.loc[:, 'mid_price']).diff().dropna()
    period_std = log_returns.groupby(pd.Grouper(freq=freq_str)).std()

    num_periods_total = len(period_std)

    if num_periods_total == 0:
        print("Rolling sigma values not available.")
        return [np.nan] * len(periods)

    # Adjust window if we have fewer periods than desired
    if num_periods_total < window_periods:
        actual_window = max(2, num_periods_total // 2) if num_periods_total > 1 else 1
    else:
        actual_window = window_periods

    print(f"Using rolling window of {actual_window} periods ({actual_window * window_minutes} minutes)")

    smoothed_std = period_std.rolling(window=actual_window, min_periods=1).mean()
    sigma_series = smoothed_std * np.sqrt(60 * window_minutes)
    sigma_series = sigma_series.reindex(periods)

    sigma_list = sigma_series.tolist()

    if sigma_list:
        print("Latest rolling sigma values:")
        for s in sigma_list[-3:]:
            if not pd.isna(s):
                print(f"  - {s:.6f}")
    else:
        print("Rolling sigma values not available.")

    return sigma_list

def calculate_volatility(mid_price_df, window_minutes, freq_str, periods=None):
    """Calculate volatility (sigma) for the requested periods."""
    print("\n" + "-"*20)
    print("Calculating volatility (sigma)...")

    all_periods = mid_price_df.index.floor(freq_str).unique().tolist()[:-1]
    target_periods = periods if periods is not None else all_periods

    if not target_periods:
        print("No periods available for volatility calculation.")
        return []

    total_periods = len(all_periods)

    if total_periods < 10:
        print("Fewer than 10 periods available, using rolling volatility only.")
        final_sigma = calculate_rolling_volatility(mid_price_df, window_minutes, freq_str, target_periods)
    else:
        garch_sigma = calculate_garch_volatility(mid_price_df, window_minutes, target_periods)
        rolling_sigma = calculate_rolling_volatility(mid_price_df, window_minutes, freq_str, target_periods)

        final_sigma = []
        print("\nCombining GARCH and rolling volatility...")
        max_multiplier = 5.0
        min_multiplier = 1.0 / max_multiplier
        for i, (g, r) in enumerate(zip(garch_sigma, rolling_sigma)):
            use_garch = pd.notna(g)
            if use_garch and pd.notna(r) and r != 0:
                ratio = g / r
                if ratio > max_multiplier or ratio < min_multiplier:
                    print(f"  - Period {i}: GARCH outlier ({g:.6f} vs rolling {r:.6f}), using rolling value")
                    use_garch = False
            if use_garch:
                final_sigma.append(g)
            else:
                if pd.notna(r):
                    if not pd.notna(g):
                        print(f"  - Period {i}: GARCH failed, using rolling value: {r:.6f}")
                    final_sigma.append(r)
                else:
                    print(f"  - Period {i}: Both GARCH and rolling failed.")
                    final_sigma.append(np.nan)

    final_sigma_series = pd.Series(final_sigma).ffill()
    final_sigma = final_sigma_series.tolist()

    if final_sigma and not all(pd.isna(s) for s in final_sigma):
        print("\nFinal combined sigma values:")
        for s in final_sigma[-3:]:
            if pd.notna(s):
                print(f"  - {s:.6f}")
            else:
                print("  - nan")
    else:
        print("\nCould not calculate any sigma values.")

    return final_sigma

def calculate_intensity_params(periods, window_minutes, buy_orders, sell_orders, deltalist, mid_price_df):
    """Calculate order arrival intensity parameters (A and k)."""
    print("\n" + "-"*20)
    print("Calculating order arrival intensity (A and k)...")

    def exp_fit(x, a, b):
        return a * np.exp(-b * x)

    Alist, klist = [], []

    if not periods:
        return Alist, klist

    for period_start in periods:
        period_end = period_start + pd.Timedelta(minutes=window_minutes)

        mask_buy = (buy_orders.index >= period_start) & (buy_orders.index < period_end)
        period_buy_orders = buy_orders.loc[mask_buy].copy()

        mask_sell = (sell_orders.index >= period_start) & (sell_orders.index < period_end)
        period_sell_orders = sell_orders.loc[mask_sell].copy()

        if period_buy_orders.empty and period_sell_orders.empty:
            Alist.append(float('nan'))
            klist.append(float('nan'))
            continue

        best_bid = period_buy_orders['price'].max() if not period_buy_orders.empty else np.nan
        best_ask = period_sell_orders['price'].min() if not period_sell_orders.empty else np.nan

        if pd.isna(best_bid) or pd.isna(best_ask):
            s_period = mid_price_df.loc[period_start:period_end]
            reference_mid = s_period['mid_price'].mean() if not s_period.empty else np.nan
        else:
            reference_mid = (best_bid + best_ask) / 2

        if pd.isna(reference_mid):
            Alist.append(float('nan'))
            klist.append(float('nan'))
            continue

        deltadict = {}
        for price_delta in deltalist:
            limit_bid = reference_mid - price_delta
            limit_ask = reference_mid + price_delta
            
            bid_hits = []
            if not period_sell_orders.empty:
                sell_hits_bid = period_sell_orders[period_sell_orders['price'] <= limit_bid]
                if not sell_hits_bid.empty:
                    bid_hits = sell_hits_bid.index.tolist()
            
            ask_hits = []
            if not period_buy_orders.empty:
                buy_hits_ask = period_buy_orders[period_buy_orders['price'] >= limit_ask]
                if not buy_hits_ask.empty:
                    ask_hits = buy_hits_ask.index.tolist()
            
            all_hits = sorted(bid_hits + ask_hits)
            
            if len(all_hits) > 1:
                hit_times = pd.DatetimeIndex(all_hits)
                deltas = hit_times.to_series().diff().dt.total_seconds().dropna()
                deltadict[price_delta] = deltas
            else:
                deltadict[price_delta] = pd.Series([window_minutes * 60])

        lambdas = pd.DataFrame({
            "delta": list(deltadict.keys()),
            "lambda_delta": [1 / d.mean() if len(d) > 0 else 1e-6 for d in deltadict.values()]
        }).set_index("delta")

        try:
            paramsB, _ = scipy.optimize.curve_fit(exp_fit, lambdas.index.values, lambdas["lambda_delta"].values, maxfev=5000)
            A, k = paramsB
            Alist.append(A)
            klist.append(k)
        except (RuntimeError, ValueError):
            Alist.append(float('nan'))
            klist.append(float('nan'))

    if Alist and klist:
        print("Latest A and k values:")
        for i in range(max(0, len(Alist) - 3), len(Alist)):
            print(f"  - A: {Alist[i]:.4f}, k: {klist[i]:.6f}")
    else:
        print("A and k values not available.")
    return Alist, klist

def optimize_gamma(list_of_periods, sigma_list, Alist, klist, window_minutes, ma_window, mid_price_df, buy_trades, sell_trades, tick_size):
    """Optimize risk aversion parameter (gamma) via backtesting."""
    print("\n" + "-"*20)
    print("Optimizing risk aversion (gamma) via backtesting...")

    gammalist = []
    gamma_grid_to_test = None

    start_index = max(1, len(list_of_periods) - GAMMA_CALCULATION_WINDOW)
    period_index_range = range(start_index, len(list_of_periods))

    for j in period_index_range:
        if ma_window > 1:
            a_slice = Alist[max(0, j - ma_window):j]
            k_slice = klist[max(0, j - ma_window):j]
            A = pd.Series(a_slice).mean()
            k = pd.Series(k_slice).mean()
        else:
            A = Alist[j-1]
            k = klist[j-1]

        sigma = sigma_list[j-1]

        if pd.isna(sigma) or pd.isna(A) or pd.isna(k):
            gammalist.append(np.nan)
            continue

        period_start = list_of_periods[j]
        period_end = period_start + pd.Timedelta(minutes=window_minutes)
        print(f"\nProcessing period: {period_start} to {period_end}")

        mask = (mid_price_df.index >= period_start) & (mid_price_df.index < period_end)
        s_df = mid_price_df.loc[mask]
        s = s_df.resample('s').asfreq(fill_value=np.nan).ffill()['mid_price']

        if s.empty:
            gammalist.append(np.nan)
            continue

        if gamma_grid_to_test is None:
            gamma_grid_to_test = generate_gamma_grid(s.iloc[-1], sigma, k, window_minutes)

        if gamma_grid_to_test is None:
            print("Could not find a reasonable gamma interval. Aborting.")
            return None

        buy_mask = (buy_trades.index >= period_start) & (buy_trades.index < period_end)
        buy_trades_period = buy_trades.loc[buy_mask]
        sell_mask = (sell_trades.index >= period_start) & (sell_trades.index < period_end)
        sell_trades_period = sell_trades.loc[sell_mask]

        gamma_results = []
        for i, gamma_to_test in enumerate(gamma_grid_to_test):
            if (i + 1) % 8 == 0 or i == 0 or i == len(gamma_grid_to_test) - 1:
                print(f"  - Testing gamma: {gamma_to_test:.5f} ({i+1}/{len(gamma_grid_to_test)})")
            result = evaluate_gamma(gamma_to_test, s, buy_trades_period, sell_trades_period, k, sigma, window_minutes)
            gamma_results.append(result)

        results_df = pd.DataFrame(gamma_results, columns=['gamma', 'pnl', 'spread'])
        valid_results = results_df.dropna(subset=['pnl'])
        
        if valid_results.empty:
            print("Warning: All backtests resulted in NaN or 0 PnL. Using fallback gamma.")
            best_gamma = 0.5
        else:
            positive_pnl_results = valid_results[valid_results['pnl'] > 0]
            if not positive_pnl_results.empty:
                best_gamma = positive_pnl_results.loc[positive_pnl_results['spread'].idxmax()]['gamma']
            else:
                best_gamma = valid_results.loc[valid_results['pnl'].idxmax()]['gamma']
        
        print(f"Best gamma for period: {best_gamma:.5f}")
        gammalist.append(best_gamma)
        
    return gammalist

def evaluate_gamma(gamma, mid_prices_period, buy_trades_period, sell_trades_period, k, sigma, window_minutes):
    """Run backtest for a single gamma value and return results."""
    res = run_backtest(mid_prices_period, buy_trades_period, sell_trades_period, gamma, k, sigma, window_minutes)
    final_pnl = res['pnl'][-1]
    
    if np.isnan(final_pnl) or final_pnl == 0:
        return [round(gamma, 5), np.nan, np.nan]

    spread_base = gamma * sigma**2.0 * 0.5 + (2.0 / gamma) * np.log(1.0 + (gamma / k))
    return [round(gamma, 5), final_pnl, spread_base]

def generate_gamma_grid(s, sigma, k, window_minutes):
    """Generate a grid of gamma values to test."""
    window_days = (window_minutes / 60.0) / 24.0
    time_remaining = window_days / 2.0
    
    def spread(gamma):
        return (gamma * sigma**2 * time_remaining + (2.0 / gamma) * np.log(1.0 + (gamma / k))) / s * 100.0
    
    try:
        gamma_001 = find_gamma(0.01, spread, k)
    except ValueError:
        _, gamma_001 = find_workable_spread(0.01, spread, k, 'up')
        if gamma_001 is None: return None
    
    try:
        gamma_1 = find_gamma(2.0, spread, k)
    except ValueError:
        _, gamma_1 = find_workable_spread(2.0, spread, k, 'down')
        if gamma_1 is None: return None
        
    return np.logspace(np.log10(gamma_1 * 0.99), np.log10(gamma_001 * 1.01), 64)

def find_gamma(target_spread, spread_func, k):
    """Find gamma for a given target spread."""
    # ... (implementation from previous steps, unchanged)
    def equation(gamma):
        if gamma <= 0: return float('inf')
        try: return spread_func(gamma) - target_spread
        except: return float('inf')
    
    def is_valid(gamma, tolerance=1e-6):
        if gamma <= 0: return False
        try: return abs(spread_func(gamma) - target_spread) < tolerance
        except: return False

    try:
        gamma_min, gamma_max = 1e-8, 1000.0
        if equation(gamma_min) * equation(gamma_max) < 0:
            gamma = brentq(equation, gamma_min, gamma_max)
            if is_valid(gamma): return gamma
    except: pass

    for guess in [1.0, k, 0.1, 10.0, k*10, k*0.1]:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                result = fsolve(equation, guess, full_output=True)
                if result[2] == 1 and is_valid(result[0][0]): return result[0][0]
        except: continue
    
    raise ValueError(f"Could not find gamma for target_spread = {target_spread}")

def find_workable_spread(initial_spread, spread_func, k, direction='up', factor=1.05, max_iterations=100):
    """Find a workable spread if the target is not achievable."""
    # ... (implementation from previous steps, unchanged)
    spread = initial_spread
    for i in range(max_iterations):
        try:
            gamma = find_gamma(spread, spread_func, k)
            return spread, gamma
        except ValueError:
            spread *= factor if direction == 'up' else (1/factor)
    return None, None

@jit(nopython=True)
def jit_backtest_loop(s_values, buy_min_values, sell_max_values, gamma, k, sigma, fee, time_remaining, spread_base, half_spread):
    """Core JIT-compiled backtest loop."""
    # ... (implementation from previous steps, unchanged)
    N = len(s_values)
    q, x, pnl, spr, r, r_a, r_b = np.zeros(N + 1), np.zeros(N + 1), np.zeros(N + 1), np.zeros(N + 1), np.zeros(N + 1), np.zeros(N + 1), np.zeros(N + 1)
    gamma_sigma2 = gamma * sigma**2
    for i in range(N):
        r[i] = s_values[i] - q[i] * gamma_sigma2 * time_remaining[i]
        spr[i] = spread_base[i]
        gap = abs(r[i] - s_values[i])
        if r[i] >= s_values[i]:
            delta_a, delta_b = half_spread[i] + gap, half_spread[i] - gap
        else:
            delta_a, delta_b = half_spread[i] - gap, half_spread[i] + gap
        r_a[i], r_b[i] = r[i] + delta_a, r[i] - delta_b
        sell = 1 if not np.isnan(sell_max_values[i]) and sell_max_values[i] >= r_a[i] else 0
        buy = 1 if not np.isnan(buy_min_values[i]) and buy_min_values[i] <= r_b[i] else 0
        q[i+1] = q[i] + (sell - buy)
        sell_net = (r_a[i] * (1 - fee)) if sell else 0
        buy_total = (r_b[i] * (1 + fee)) if buy else 0
        x[i+1] = x[i] + sell_net - buy_total
        pnl[i+1] = x[i+1] + q[i+1] * s_values[i]
    return pnl, x, q, spr, r, r_a, r_b

def run_backtest(mid_prices, buy_trades, sell_trades, gamma, k, sigma, window_minutes, fee=0.00040):
    """Simulate the market making strategy."""
    # ... (implementation from previous steps, unchanged)
    time_index = mid_prices.index
    buy_trades_clean = buy_trades.groupby(level=0).min()
    sell_trades_clean = sell_trades.groupby(level=0).max()
    buy_min = buy_trades_clean['price'].resample('5s').min().reindex(time_index, method='ffill')
    sell_max = sell_trades_clean['price'].resample('5s').max().reindex(time_index, method='ffill')
    mid_prices = mid_prices.resample('5s').first().reindex(time_index, method='ffill')
    N = len(time_index)
    T = window_minutes / 1440.0
    dt = T / N
    s_values = mid_prices.values
    buy_min_values = buy_min.values
    sell_max_values = sell_max.values
    time_remaining = T - np.arange(len(s_values)) * dt
    spread_base = gamma * sigma**2.0 * time_remaining + (2.0 / gamma) * np.log(1.0 + (gamma / k))
    half_spread = spread_base / 2.0
    pnl, x, q, spr, r, r_a, r_b = jit_backtest_loop(s_values, buy_min_values, sell_max_values, gamma, k, sigma, fee, time_remaining, spread_base, half_spread)
    return {'pnl': pnl, 'x': x, 'q': q, 'spread': spr, 'r': r, 'r_a': r_a, 'r_b': r_b}

def calculate_final_quotes(gamma, sigma, A, k, window_minutes, mid_price_df, ma_window):
    """Calculate the final reservation price and quotes."""
    print("\n" + "-"*20)
    print("Calculating final parameters for current state...")
    
    s = mid_price_df.loc[:, 'mid_price'].iloc[-1]
    time_remaining = 0.5
    q = 1.0  # Placeholder for current inventory

    spread_base = gamma * sigma**2.0 * time_remaining + (2.0 / gamma) * np.log(1.0 + (gamma / k))
    half_spread = spread_base / 2.0
    r = s - q * gamma * sigma**2.0 * time_remaining
    gap = abs(r - s)

    if r >= s:
        delta_a, delta_b = half_spread + gap, half_spread - gap
    else:
        delta_a, delta_b = half_spread - gap, half_spread + gap
        
    r_a, r_b = r + delta_a, r - delta_b
    
    return {
        "ticker": TICKER,
        "timestamp": pd.Timestamp.now().isoformat(),
        "market_data": {"mid_price": float(s), "sigma": float(sigma), "A": float(A), "k": float(k)},
        "optimal_parameters": {"gamma": float(gamma)},
        "current_state": {"time_remaining": float(time_remaining), "inventory": int(q), "minutes_window": window_minutes, "ma_window": ma_window},
        "calculated_values": {"reservation_price": float(r), "gap": float(gap), "spread_base": float(spread_base), "half_spread": float(half_spread)},
        "limit_orders": {"ask_price": float(r_a), "bid_price": float(r_b), "delta_a": float(delta_a), "delta_b": float(delta_b),
                         "delta_a_percent": (delta_a / s) * 100.0, "delta_b_percent": (delta_b / s) * 100.0}
    }

def print_summary(results, list_of_periods):
    """Print a summary of the results to the terminal."""
    timestamp = pd.Timestamp.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')

    if not results:
        print()
        print('=' * 80)
        print('AVELLANEDA-STOIKOV MARKET MAKING PARAMETERS')
        print(f'Timestamp: {timestamp}')
        print('⚠️  DATA WARNING: Insufficient data for robust parameter estimation.')
        print('=' * 80)
        return

    ticker = results['ticker']
    window_minutes = results['current_state']['minutes_window']
    ma_window = results['current_state']['ma_window']

    print()
    print('=' * 80)
    print(f'AVELLANEDA-STOIKOV MARKET MAKING PARAMETERS - {ticker}')
    print(f'Timestamp: {timestamp}')
    print(f'Analysis Period: {window_minutes} minutes (~{window_minutes / 60:.2f} hours)')
    if ma_window > 1:
        print(f'Moving Average Window: {ma_window} periods')
    print('=' * 80)

    if len(list_of_periods) <= 1:
        print('⚠️  DATA WARNING: Insufficient data for robust parameter estimation.')
        print('=' * 80)

    print('Market Data:')
    print(f"   Mid Price:                        ${results['market_data']['mid_price']:,.4f}")
    print(f"   Volatility (sigma):               {results['market_data']['sigma']:.6f}")
    print(f"   Intensity (A):                    {results['market_data']['A']:.4f}")
    print(f"   Order arrival rate decay (k):     {results['market_data']['k']:.6f}")

    print()
    print('Optimal Parameters:')
    print(f"   Risk Aversion (gamma): {results['optimal_parameters']['gamma']:.6f}")

    print()
    print('Current State:')
    print(f"   Time Remaining:        {results['current_state']['time_remaining']:.4f} (in days)")
    print(f"   Inventory (q):         {results['current_state']['inventory']:.4f}")

    print()
    print('Calculated Prices:')
    print(f"   Reservation Price:     ${results['calculated_values']['reservation_price']:.4f}")
    print(f"   Ask Price:             ${results['limit_orders']['ask_price']:.4f}")
    print(f"   Bid Price:             ${results['limit_orders']['bid_price']:.4f}")

    print()
    print('Spreads:')
    print(f"   Delta Ask:             ${results['limit_orders']['delta_a']:.6f} ({results['limit_orders']['delta_a_percent']:.6f}%)")
    print(f"   Delta Bid:             ${results['limit_orders']['delta_b']:.6f} ({results['limit_orders']['delta_b_percent']:.6f}%)")
    total_spread_pct = results['limit_orders']['delta_a_percent'] + results['limit_orders']['delta_b_percent']
    print(f'   Total Spread:          {total_spread_pct:.4f}%')

    trade_summary = results.get('trade_summary')
    if trade_summary:
        start_ts = pd.Timestamp(trade_summary['period_start'])
        end_ts = pd.Timestamp(trade_summary['period_end'])
        print()
        print(f"Trades (final {results['current_state']['minutes_window']} minute interval):")
        print(f'   Interval:              {start_ts} → {end_ts}')
        print(f"   Total trades:          {trade_summary['total_count']}")
        print(f"      Buys:               {trade_summary['buy_count']}")
        print(f"      Sells:              {trade_summary['sell_count']}")

    ok = save_avellaneda_params_atomic(results, TICKER)
    out_path = os.path.join(PARAMS_DIR, f'avellaneda_parameters_{TICKER}.json')
    if ok:
        print(f'\nResults saved to: {out_path}')
    else:
        print('\n⚠️ Invalid params (delta_a/delta_b). Keeping previous file.')

    final_timestamp = pd.Timestamp.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    print(f'Timestamp: {final_timestamp}')
    print('=' * 80)

def main():
    """Main execution function."""
    global TICKER  # Make TICKER a global variable
    args = parse_arguments()
    TICKER = args.ticker
    window_minutes = args.minutes

    if window_minutes <= 0:
        print("Error: window size in minutes must be positive.")
        sys.exit(1)
    
    if window_minutes <= 8 * 60:
        ma_window = 3
    elif 8 * 60 < window_minutes < 20 * 60:
        ma_window = 2
    else:
        ma_window = 1

    print("-" * 20)
    print(f"DOING: {TICKER}")
    print(f"Using analysis period of {window_minutes} minutes (~{window_minutes / 60:.2f} hours).")
    if ma_window > 1:
        print(f"Using a {ma_window}-period moving average for parameters.")

    tick_size = get_fallback_tick_size(TICKER)
    print(f"Using fallback price tick size for {TICKER}: {tick_size}")
    delta_list = np.arange(tick_size, 50.0 * tick_size, tick_size)
    
    # Load data
    script_dir = Path(__file__).parent.absolute()
    default_if_not_env = script_dir / 'ASTER_data'
    HL_DATA_DIR = os.getenv('HL_DATA_LOC', default_if_not_env)
    csv_file_path = os.path.join(HL_DATA_DIR, f'prices_{TICKER}USDT.csv')

    if not os.path.exists(csv_file_path):
        print(f"Error: File {csv_file_path} not found!")
        sys.exit(1)

    mid_price_df = load_and_resample_mid_price(csv_file_path)
    trades_df = load_trades_data(os.path.join(HL_DATA_DIR, f'trades_{TICKER}USDT.csv'))
    buy_trades = trades_df[trades_df['side'] == 'buy'].copy()
    sell_trades = trades_df[trades_df['side'] == 'sell'].copy()
    print(f"Loaded {len(mid_price_df)} data points from {mid_price_df.index.min()} to {mid_price_df.index.max()}.")

    # Display sample data for verification
    print(f"\nFirst 3 price data points:")
    print(mid_price_df.head(3))
    print(f"\nFirst 3 trade data points:")
    print(trades_df.head(3))

    freq_str = f'{window_minutes}min'
    list_of_periods = mid_price_df.index.floor(freq_str).unique().tolist()[:-1]

    # Ensure the most recent interval is complete before using it for parameter calculations
    last_start = list_of_periods[-1]
    last_end = last_start + pd.Timedelta(minutes=window_minutes)
    expected_samples = window_minutes * 60
    mask_last_window = (mid_price_df.index >= last_start) & (mid_price_df.index < last_end)
    actual_samples = mask_last_window.sum()
    min_required_samples = int(expected_samples * 0.75)
    if actual_samples < min_required_samples:
        print(
            f"Skipping trailing period starting {last_start}; \n             expected {expected_samples} samples but found {actual_samples}."
        )
        list_of_periods.pop()

    if not list_of_periods:
        print_summary({}, list_of_periods)
        sys.exit()

    target_period_count = min(len(list_of_periods), RECENT_PARAM_PERIODS)
    planned_buffer = ma_window if len(list_of_periods) > target_period_count else 0
    calc_period_count = min(len(list_of_periods), target_period_count + planned_buffer)
    buffer_used = max(0, calc_period_count - target_period_count)

    calc_periods = list_of_periods[-calc_period_count:]
    recent_periods = calc_periods[-target_period_count:]

    if len(list_of_periods) > calc_period_count:
        print(f"Using last {calc_period_count} periods (of {len(list_of_periods)}) for sigma/A/k with a buffer of {buffer_used} period(s).")
    elif buffer_used > 0:
        print(f"Using last {calc_period_count} periods to cover {target_period_count} recent periods with buffer {buffer_used} period(s).")

    sigma_list = calculate_volatility(mid_price_df, window_minutes, freq_str, periods=calc_periods)
    Alist, klist = calculate_intensity_params(calc_periods, window_minutes, buy_trades, sell_trades, delta_list, mid_price_df)
    
    if len(calc_periods) <= 1:
        print_summary({}, calc_periods)
        sys.exit()

    gammalist = optimize_gamma(calc_periods, sigma_list, Alist, klist, window_minutes, ma_window, mid_price_df, buy_trades, sell_trades, tick_size)

    if gammalist is None:
        print("WARNING: Falling back to another gamma estimation method (Hummingbot)")
        s = mid_price_df.loc[:, 'mid_price'].iloc[-1]
        IRA = 1.0
        MAX_spread = 0.1/100*s
        MIN_spread = 0.01/100*s
        gammalist = [IRA * (MAX_spread-MIN_spread)/(2*1*sigma*sigma) for sigma in sigma_list]

    # Final calculations
    if len(gammalist) > 0:
        if ma_window > 1:
            gamma_slice = gammalist[max(0, len(gammalist) - ma_window):]
            gamma = pd.Series(gamma_slice).mean()
        else:
            gamma = gammalist[-1]
        if pd.isna(gamma): gamma = 0.1
    else:
        gamma = 0.1

    if ma_window > 1:
        start_index = max(0, len(Alist) - 1 - ma_window + 1)
        end_index = len(Alist) - 1
        a_slice = Alist[start_index:end_index]
        k_slice = klist[start_index:end_index]
        A = pd.Series(a_slice).mean()
        k = pd.Series(k_slice).mean()
    else:
        A = Alist[-2] if len(Alist) > 1 else Alist[-1]
        k = klist[-2] if len(klist) > 1 else klist[-1]

    if pd.isna(A): A = Alist[-2] if len(Alist) > 1 else Alist[-1]
    if pd.isna(k): k = klist[-2] if len(klist) > 1 else klist[-1]
    
    sigma = sigma_list[-2] if len(sigma_list) > 1 else sigma_list[-1]

    results = calculate_final_quotes(gamma, sigma, A, k, window_minutes, mid_price_df, ma_window)

    final_period_start = recent_periods[-1]
    final_period_end = final_period_start + pd.Timedelta(minutes=window_minutes)
    final_buy_count = int(((buy_trades.index >= final_period_start) & (buy_trades.index < final_period_end)).sum())
    final_sell_count = int(((sell_trades.index >= final_period_start) & (sell_trades.index < final_period_end)).sum())
    final_total_count = int(((trades_df.index >= final_period_start) & (trades_df.index < final_period_end)).sum())

    results["trade_summary"] = {
        "period_start": final_period_start.isoformat(),
        "period_end": final_period_end.isoformat(),
        "buy_count": final_buy_count,
        "sell_count": final_sell_count,
        "total_count": final_total_count,
    }

    print_summary(results, recent_periods)

if __name__ == "__main__":
    main()
