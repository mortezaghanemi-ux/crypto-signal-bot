"""Crypto signal bot - step 2.

Fetch BTC/USDT 4h candles from public exchange APIs, compute EMA, RSI and ATR,
generate buy/sell signals with stop-loss and take-profit levels.
Uses only the Python standard library (no pip installs).

This is an educational tool, not financial advice.
"""

import json
import urllib.request
from datetime import datetime, timezone

SYMBOL_LABEL = "BTC/USDT"
TIMEFRAME = "4h"
LIMIT = 300  # enough candles for EMA 200

# ---- Strategy settings (change these to experiment) ----
EMA_FAST = 50
EMA_SLOW = 200
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
ATR_PERIOD = 14
SL_ATR_MULT = 1.5  # stop-loss distance = 1.5 x ATR
TP_ATR_MULT = 3.0  # take-profit distance = 3 x ATR  (risk:reward = 1:2)


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


# Each fetcher returns a list of (time_ms, open, high, low, close), oldest -> newest.
def from_binance(host):
    url = f"https://{host}/api/v3/klines?symbol=BTCUSDT&interval=4h&limit={LIMIT}"
    return [
        (int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]))
        for c in http_get_json(url)
    ]


def from_bybit():
    url = (
        "https://api.bybit.com/v5/market/kline"
        f"?category=spot&symbol=BTCUSDT&interval=240&limit={LIMIT}"
    )
    rows = http_get_json(url)["result"]["list"]  # newest first
    return [
        (int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
        for r in reversed(rows)
    ]


def from_okx():
    url = f"https://www.okx.com/api/v5/market/candles?instId=BTC-USDT&bar=4H&limit={LIMIT}"
    rows = http_get_json(url)["data"]  # newest first
    return [
        (int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
        for r in reversed(rows)
    ]


def from_kucoin():
    url = "https://api.kucoin.com/api/v1/market/candles?type=4hour&symbol=BTC-USDT"
    rows = http_get_json(url)["data"]  # newest first: [time_s, open, close, high, low, ...]
    return [
        (int(r[0]) * 1000, float(r[1]), float(r[3]), float(r[4]), float(r[2]))
        for r in reversed(rows)
    ]


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
            if len(candles) < EMA_SLOW + 10:
                raise ValueError(f"only {len(candles)} candles returned")
            print(f"Data source: {name}")
            return candles
        except Exception as e:  # noqa: BLE001
            last_error = e
            print(f"{name} failed: {e}")
    raise SystemExit(f"All sources failed. Last error: {last_error}")


# ---------------- Indicators ----------------
def ema(values, span):
    alpha = 2 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def rsi(values, period=14):
    """Wilder RSI, aligned with values (None until enough data)."""
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


def atr(highs, lows, closes, period=14):
    """Wilder ATR, aligned with candles (None until enough data)."""
    n = len(closes)
    out = [None] * n
    if n <= period:
        return out
    tr = [highs[0] - lows[0]]
    for i in range(1, n):
        tr.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )
    value = sum(tr[1 : period + 1]) / period
    out[period] = value
    for i in range(period + 1, n):
        value = (value * (period - 1) + tr[i]) / period
        out[i] = value
    return out


# ---------------- Strategy ----------------
def compute(candles):
    closes = [c[4] for c in candles]
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    return {
        "times": [c[0] for c in candles],
        "closes": closes,
        "ema_fast": ema(closes, EMA_FAST),
        "ema_slow": ema(closes, EMA_SLOW),
        "rsi": rsi(closes, RSI_PERIOD),
        "atr": atr(highs, lows, closes, ATR_PERIOD),
    }


def signal_at(d, i):
    """Return a signal dict for candle i, or None.

    BUY : uptrend (EMA fast > EMA slow) and RSI crosses back up through RSI_OVERSOLD.
    SELL: downtrend (EMA fast < EMA slow) and RSI crosses back down through RSI_OVERBOUGHT.
    """
    r_prev, r_now, a = d["rsi"][i - 1], d["rsi"][i], d["atr"][i]
    if r_prev is None or r_now is None or a is None:
        return None
    entry = d["closes"][i]
    uptrend = d["ema_fast"][i] > d["ema_slow"][i]

    if uptrend and r_prev < RSI_OVERSOLD <= r_now:
        return {
            "side": "BUY",
            "entry": entry,
            "sl": entry - SL_ATR_MULT * a,
            "tp": entry + TP_ATR_MULT * a,
        }
    if (not uptrend) and r_prev > RSI_OVERBOUGHT >= r_now:
        return {
            "side": "SELL",
            "entry": entry,
            "sl": entry + SL_ATR_MULT * a,
            "tp": entry - TP_ATR_MULT * a,
        }
    return None


def fmt_time(ms):
    return f"{datetime.fromtimestamp(ms / 1000, tz=timezone.utc):%Y-%m-%d %H:%M} UTC"


def report(candles):
    d = compute(candles)
    i = len(candles) - 2  # last CLOSED candle (the final one is still forming)

    trend = "up" if d["ema_fast"][i] > d["ema_slow"][i] else "down"
    print("-" * 40)
    print(f"{SYMBOL_LABEL} {TIMEFRAME} | candle open: {fmt_time(d['times'][i])}")
    print(f"Close : {d['closes'][i]:.2f}")
    print(f"EMA{EMA_FAST}: {d['ema_fast'][i]:.2f}")
    print(f"EMA{EMA_SLOW}: {d['ema_slow'][i]:.2f}")
    print(f"RSI   : {d['rsi'][i]:.1f}")
    print(f"ATR   : {d['atr'][i]:.2f}")
    print(f"Trend : {trend}")
    print("-" * 40)

    sig = signal_at(d, i)
    if sig:
        risk = abs(sig["entry"] - sig["sl"])
        print(f"*** SIGNAL: {sig['side']} ***")
        print(f"Entry      : {sig['entry']:.2f}")
        print(f"Stop-loss  : {sig['sl']:.2f} ({risk / sig['entry'] * 100:.2f}% away)")
        print(f"Take-profit: {sig['tp']:.2f}")
        print(f"Risk:Reward = 1:{TP_ATR_MULT / SL_ATR_MULT:.1f}")
    else:
        print("No signal on the last closed candle.")

    # Past signals in the loaded data (only where EMA slow has warmed up)
    past = []
    for j in range(EMA_SLOW, len(candles) - 1):
        s = signal_at(d, j)
        if s:
            past.append((j, s))
    print("-" * 40)
    print(f"Past signals in loaded data: {len(past)}")
    for j, s in past[-5:]:
        print(f"  {fmt_time(d['times'][j])}  {s['side']}  @ {s['entry']:.2f}")
    print("-" * 40)


if __name__ == "__main__":
    report(fetch_candles())
