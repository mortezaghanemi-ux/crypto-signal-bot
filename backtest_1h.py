"""1-hour test of the long-only Donchian strategy on the 12 coins.

Usage:
    python backtest_1h.py              # entry 55 / exit 20 candles (the numbers of version A)
    python backtest_1h.py 220 80       # same TIME window as version A (55x4h = 220x1h)

Two variants are run on the SAME period (about the last 2 years):
    A : breakout above the highest high of the previous N_IN candles,
        stop = entry - 2 x ATR(14), exit when a candle closes below the lowest low
        of the previous N_OUT candles. Entry at the next candle's open. Fee 0.1% per side.
    B : A + "BTC 1h close is above its 200-day average (4800 x 1h candles)".

Data: KuCoin 1h candles, 2 years + about 200 days of warm-up for the BTC average
(the warm-up is not traded). Every result is shown before and after an extra 0.1%
slippage per side, because on 1h the costs eat a much larger part of each trade.
Educational tool, not financial advice.
"""

import json
import os
import sys
import time

import backtest_trend as bt
import bot

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "TRX", "LINK", "BCH", "UNI", "XLM"]
STEP = 3600 * 1000  # 1 hour in ms
EVAL_YEARS = 2.0
BTC_SMA = 4800  # 200 days of 1h candles
LOAD_YEARS = EVAL_YEARS + 0.6  # 0.6 years = ~219 days: covers the 200-day warm-up
SLIP = 0.001  # extra slippage per side
VARIANTS = ["A", "B"]
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


# ---------------- Data (KuCoin 1h) ----------------
def _get(url):
    last = None
    for attempt in range(3):
        try:
            return bot.http_get_json(url)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def fetch_kucoin_1h(coin, years):
    """Rows (time_ms, open, high, low, close, volume), oldest -> newest."""
    end_s = int(time.time())
    start_s = end_s - int(years * 365 * 24 * 3600)
    out = {}
    while end_s > start_s:
        url = (
            "https://api.kucoin.com/api/v1/market/candles"
            f"?type=1hour&symbol={coin}-USDT&startAt={start_s}&endAt={end_s}"
        )
        rows = _get(url)["data"]  # newest first: [t_s, open, close, high, low, volume, turnover]
        if not rows:
            break
        for r in rows:
            t = int(r[0]) * 1000
            out[t] = (t, float(r[1]), float(r[3]), float(r[4]), float(r[2]), float(r[5]))
        oldest = int(rows[-1][0])
        if oldest >= end_s:
            break
        end_s = oldest - 1
        time.sleep(0.3)
    return sorted(out.values())


def load_coin(coin, years=LOAD_YEARS):
    path = os.path.join(CACHE_DIR, f"{coin}_1h.json")
    try:
        if time.time() - os.path.getmtime(path) < 6 * 3600:
            with open(path, encoding="utf-8") as f:
                return [tuple(x) for x in json.load(f)]
    except (OSError, ValueError):
        pass
    now_ms = int(time.time() * 1000)
    candles = [c for c in fetch_kucoin_1h(coin, years) if c[0] + STEP <= now_ms]  # closed only
    if len(candles) < 2000:
        raise ValueError(f"only {len(candles)} candles")
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(candles, f)
    except OSError:
        pass
    return candles


# ---------------- Strategy variants ----------------
def btc_trend_map(btc, n=BTC_SMA):
    """{candle_time_ms: BTC close > average of its last n closes (including this one)}."""
    closes = [c[4] for c in btc]
    ok, s = {}, 0.0
    for i, c in enumerate(closes):
        s += c
        if i >= n:
            s -= closes[i - n]
        if i >= n - 1:
            ok[btc[i][0]] = c > s / n
    return ok


def run_variants(data, n_in, n_out):
    btc = data["BTC"]
    if len(btc) <= BTC_SMA:
        raise SystemExit("Not enough BTC history for the 200-day average.")
    btc_ok = btc_trend_map(btc)
    cutoff = btc[BTC_SMA][0]  # first candle where the BTC average is fully available
    res = {}
    for coin, candles in data.items():
        times = [c[0] for c in candles]
        filters = {"A": None, "B": (lambda p, times=times: btc_ok.get(times[p], False))}
        res[coin] = {}
        for v in VARIANTS:
            trades = bt.run_donchian(candles, n_in, n_out, +1, entry_ok=filters[v])
            kept = []
            for t in trades:
                if t["time"] >= cutoff:
                    t = dict(t)
                    t["r_slip"] = t["r"] - 2 * SLIP / t["risk_pct"]
                    kept.append(t)
            res[coin][v] = kept
    return res, cutoff, btc[-1][0]


# ---------------- Stats ----------------
def pf(trades, key="r"):
    wins = sum(t[key] for t in trades if t[key] > 0)
    loss = -sum(t[key] for t in trades if t[key] <= 0)
    if loss > 0:
        return min(wins / loss, 99.0)
    return 99.0 if wins > 0 else 0.0


def total(trades, key="r"):
    return sum(t[key] for t in trades)


def report(res, cutoff, last_ms, n_in, n_out):
    coins = list(res)
    weeks = (last_ms - cutoff) / (7 * 24 * 3600 * 1000)
    print("=" * 54)
    print(f"1h Donchian {n_in}/{n_out} | long only | {len(coins)} coins")
    print(f"Period {bot.fmt_time(cutoff)[:10]} -> {bot.fmt_time(last_ms)[:10]} ({weeks / 52.18:.1f} years)")
    print("Per coin: PF before slip / PF after slip / trades")
    print("coin   A                  B")
    for c in coins:
        cells = []
        for v in VARIANTS:
            ts = res[c][v]
            cells.append(f"{pf(ts):4.2f}/{pf(ts, 'r_slip'):4.2f}/{len(ts):<4d}")
        print(f"{c:5}  " + "   ".join(cells))

    pooled = {v: [t for c in coins for t in res[c][v]] for v in VARIANTS}
    print("-" * 54)
    for v in VARIANTS:
        ts = pooled[v]
        n = len(ts)
        if n == 0:
            print(f"{v}: no trades")
            continue
        good = sum(1 for c in coins if res[c][v] and pf(res[c][v], "r_slip") > 1)
        print(f"{v}: {n} entries = {n / weeks:.1f} per week (plus as many exits)")
        print(
            f"   PF {pf(ts):.2f} | avg {total(ts) / n:+.2f}R | total {total(ts):+.0f}R"
        )
        print(
            f"   after slip: PF {pf(ts, 'r_slip'):.2f} | avg {total(ts, 'r_slip') / n:+.2f}R "
            f"| total {total(ts, 'r_slip'):+.0f}R | coins PF>1: {good}/{len(coins)}"
        )

    print("-" * 54)
    print("Pooled total R after slip, by calendar year (partial years at the edges)")
    years = sorted({bt.year_of(t["time"]) for v in VARIANTS for t in pooled[v]})
    print("year      A       B")
    for y in years:
        row = [total([t for t in pooled[v] if bt.year_of(t["time"]) == y], "r_slip") for v in VARIANTS]
        print(f"{y}  {row[0]:+7.1f} {row[1]:+7.1f}")
    print("=" * 54)


def main(n_in, n_out):
    data = {}
    for coin in COINS:
        print(f"loading {coin} ...", flush=True)
        try:
            data[coin] = load_coin(coin)
        except Exception as e:  # noqa: BLE001
            print(f"{coin}: skipped ({e})")
    if "BTC" not in data:
        raise SystemExit("BTC data is required for variant B.")
    res, cutoff, last_ms = run_variants(data, n_in, n_out)
    report(res, cutoff, last_ms, n_in, n_out)


if __name__ == "__main__":
    n_in = int(sys.argv[1]) if len(sys.argv) > 2 else 55
    n_out = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    main(n_in, n_out)
