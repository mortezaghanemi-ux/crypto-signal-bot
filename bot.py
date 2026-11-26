"""Crypto signal bot - step 1: fetch candles, compute EMA and RSI, print a basic reading."""

import ccxt
import pandas as pd

SYMBOL = "BTC/USDT"
TIMEFRAME = "4h"
LIMIT = 300  # enough candles for EMA 200
EXCHANGES = ["binance", "bybit", "okx", "kucoin"]  # tried in order


def fetch_candles():
    """Fetch OHLCV candles from the first exchange that answers."""
    last_error = None
    for name in EXCHANGES:
        try:
            exchange = getattr(ccxt, name)({"enableRateLimit": True})
            raw = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=LIMIT)
            df = pd.DataFrame(
                raw, columns=["time", "open", "high", "low", "close", "volume"]
            )
            df["time"] = pd.to_datetime(df["time"], unit="ms")
            print(f"Data source: {name}")
            return df
        except Exception as e:  # noqa: BLE001
            last_error = e
            print(f"{name} failed: {e}")
    raise SystemExit(f"All exchanges failed. Last error: {last_error}")


def add_indicators(df):
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

    # RSI (Wilder smoothing, period 14)
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
    rs = avg_gain / avg_loss
    df["rsi"] = 100 - (100 / (1 + rs))
    return df


def describe(df):
    last = df.iloc[-2]  # last CLOSED candle (the final row is still forming)
    trend = "up" if last["ema50"] > last["ema200"] else "down"

    if last["rsi"] >= 70:
        rsi_state = "overbought"
    elif last["rsi"] <= 30:
        rsi_state = "oversold"
    else:
        rsi_state = "neutral"

    print("-" * 40)
    print(f"{SYMBOL} {TIMEFRAME} | candle: {last['time']}")
    print(f"Close : {last['close']:.2f}")
    print(f"EMA50 : {last['ema50']:.2f}")
    print(f"EMA200: {last['ema200']:.2f}")
    print(f"RSI14 : {last['rsi']:.1f} ({rsi_state})")
    print(f"Trend : {trend} (EMA50 vs EMA200)")
    print("-" * 40)


if __name__ == "__main__":
    data = add_indicators(fetch_candles())
    describe(data)
