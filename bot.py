"""Crypto signal bot - step 1.

Fetch BTC/USDT 4h candles from public exchange APIs, compute EMA and RSI,
print a basic reading. Uses only the Python standard library (no pip installs).
"""

import json
import urllib.request
from datetime import datetime, timezone

SYMBOL_LABEL = "BTC/USDT"
TIMEFRAME = "4h"
LIMIT = 300  # enough candles for EMA 200


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


# Each fetcher returns a list of (time_ms, close) sorted oldest -> newest.
def from_binance(host):
    url = f"https://{host}/api/v3/klines?symbol=BTCUSDT&interval=4h&limit={LIMIT}"
    return [(int(c[0]), float(c[4])) for c in http_get_json(url)]


def from_bybit():
    url = (
        "https://api.bybit.com/v5/market/kline"
        f"?category=spot&symbol=BTCUSDT&interval=240&limit={LIMIT}"
    )
    rows = http_get_json(url)["result"]["list"]  # newest first
    return [(int(r[0]), float(r[4])) for r in reversed(rows)]


def from_okx():
    url = f"https://www.okx.com/api/v5/market/candles?instId=BTC-USDT&bar=4H&limit={LIMIT}"
    rows = http_get_json(url)["data"]  # newest first
    return [(int(r[0]), float(r[4])) for r in reversed(rows)]


def from_kucoin():
    url = "https://api.kucoin.com/api/v1/market/candles?type=4hour&symbol=BTC-USDT"
    rows = http_get_json(url)["data"]  # newest first: [time_s, open, close, ...]
    return [(int(r[0]) * 1000, float(r[2])) for r in reversed(rows)]


SOURCES = [
    ("binance", lambda: from_binance("api.binance.com")),
    ("binance-mirror", lambda: from_binance("data-api.binance.vision")),
    ("bybit", from_bybit),
    ("okx", from_okx),
    ("kucoin", from_kucoin),
]


def fetch_candles():
    last_error = None
    for name, fn in SOURCES:
        try:
            candles = fn()
            if len(candles) < 210:
                raise ValueError(f"only {len(candles)} candles returned")
            print(f"Data source: {name}")
            return candles
        except Exception as e:  # noqa: BLE001
            last_error = e
            print(f"{name} failed: {e}")
    raise SystemExit(f"All sources failed. Last error: {last_error}")


def ema(values, span):
    alpha = 2 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def rsi(values, period=14):
    """Wilder RSI. Returns a list aligned with values (None until enough data)."""
    out = [None] * len(values)
    if len(values) <= period:
        return out
    gains, losses = [], []
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    def calc(g, l):
        return 100.0 if l == 0 else 100 - 100 / (1 + g / l)

    out[period] = calc(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period
        out[i] = calc(avg_gain, avg_loss)
    return out


def describe(candles):
    times = [c[0] for c in candles]
    closes = [c[1] for c in candles]
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)
    rsi14 = rsi(closes, 14)

    i = len(candles) - 2  # last CLOSED candle (the final one is still forming)
    trend = "up" if ema50[i] > ema200[i] else "down"
    if rsi14[i] >= 70:
        rsi_state = "overbought"
    elif rsi14[i] <= 30:
        rsi_state = "oversold"
    else:
        rsi_state = "neutral"

    t = datetime.fromtimestamp(times[i] / 1000, tz=timezone.utc)
    print("-" * 40)
    print(f"{SYMBOL_LABEL} {TIMEFRAME} | candle open: {t:%Y-%m-%d %H:%M} UTC")
    print(f"Close : {closes[i]:.2f}")
    print(f"EMA50 : {ema50[i]:.2f}")
    print(f"EMA200: {ema200[i]:.2f}")
    print(f"RSI14 : {rsi14[i]:.1f} ({rsi_state})")
    print(f"Trend : {trend} (EMA50 vs EMA200)")
    print("-" * 40)


if __name__ == "__main__":
    describe(fetch_candles())
