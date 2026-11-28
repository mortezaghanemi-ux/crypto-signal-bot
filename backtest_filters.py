"""Filter experiment for the long-only Donchian 55/20 strategy (4h candles).

Usage:
    python backtest_filters.py                 # 12 coins, ~5 years
    python backtest_filters.py ADA XLM BCH     # chosen coins (BTC is always loaded)

Variants (FIXED IN ADVANCE, nothing is tuned):
    base : the live strategy (no filter)
    vol  : breakout candle volume > 1.0 x average volume of the previous 20 candles
    btc  : BTC 4h close is above its 200-day average (1200 x 4h candles)
    both : vol and btc together

Accept rule (also fixed in advance). A filter passes only if ALL are true:
    1. profit factor improves on >= 75% of the coins,
    2. at least 50% of the baseline trades are kept,
    3. pooled profit factor improves.
All variants are compared on the SAME period (the first 200 days are skipped
because the BTC average needs them). No slippage is modelled.
Educational tool, not financial advice.
"""

import json
import os
import sys
import time

import backtest
import backtest_trend as bt
import bot

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "TRX", "LINK", "BCH", "UNI", "XLM"]
YEARS = 5.6  # 5 years evaluated + ~0.6 years of warm-up for the BTC average
STEP = backtest.CANDLE_MS
N_IN, N_OUT = 55, 20
VOL_N = 20
VOL_MULT = 1.0
BTC_SMA = 1200  # 200 days of 4h candles
MIN_COIN_FRAC = 0.75
MIN_TRADES_KEPT = 0.5
VARIANTS = ["base", "vol", "btc", "both"]

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


# ---------------- Data (KuCoin, with volume) ----------------
def _get(url):
    last = None
    for attempt in range(3):
        try:
            return bot.http_get_json(url)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def fetch_kucoin(coin, years):
    """Rows: (time_ms, open, high, low, close, volume), oldest -> newest."""
    end_s = int(time.time())
    start_s = end_s - int(years * 365 * 24 * 3600)
    out = {}
    while end_s > start_s:
        url = (
            "https://api.kucoin.com/api/v1/market/candles"
            f"?type=4hour&symbol={coin}-USDT&startAt={start_s}&endAt={end_s}"
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


def load_coin(coin, years=YEARS):
    path = os.path.join(CACHE_DIR, f"{coin}_4h_vol.json")
    try:
        if time.time() - os.path.getmtime(path) < 6 * 3600:
            with open(path, encoding="utf-8") as f:
                return [tuple(x) for x in json.load(f)]
    except (OSError, ValueError):
        pass
    now_ms = int(time.time() * 1000)
    candles = [c for c in fetch_kucoin(coin, years) if c[0] + STEP <= now_ms]
    if len(candles) < BTC_SMA + 500:
        raise ValueError(f"only {len(candles)} candles")
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(candles, f)
    except OSError:
        pass
    return candles


# ---------------- Filters ----------------
def btc_trend_map(btc):
    """{candle_time_ms: True/False} -> BTC close above its 200-day average."""
    closes = [c[4] for c in btc]
    ok, s = {}, 0.0
    for i, c in enumerate(closes):
        s += c
        if i >= BTC_SMA:
            s -= closes[i - BTC_SMA]
        if i >= BTC_SMA - 1:
            ok[btc[i][0]] = c > s / BTC_SMA
    return ok


def make_filters(candles, btc_ok):
    vols = [c[5] for c in candles]
    times = [c[0] for c in candles]

    def vol_f(p):
        if p < VOL_N:
            return False
        return vols[p] > VOL_MULT * (sum(vols[p - VOL_N:p]) / VOL_N)

    def btc_f(p):
        return btc_ok.get(times[p], False)

    return {
        "base": None,
        "vol": vol_f,
        "btc": btc_f,
        "both": lambda p: vol_f(p) and btc_f(p),
    }


# ---------------- Stats ----------------
def pf(trades):
    wins = sum(t["r"] for t in trades if t["r"] > 0)
    loss = -sum(t["r"] for t in trades if t["r"] <= 0)
    if loss > 0:
        return min(wins / loss, 99.0)
    return 99.0 if wins > 0 else 0.0


def total(trades):
    return sum(t["r"] for t in trades)


def run_all(data, btc_key="BTC"):
    """data: {coin: candles}. Returns ({coin: {variant: trades}}, cutoff_ms)."""
    btc = data[btc_key]
    btc_ok = btc_trend_map(btc)
    cutoff = btc[BTC_SMA][0]
    res = {}
    for coin, candles in data.items():
        filters = make_filters(candles, btc_ok)
        res[coin] = {}
        for v in VARIANTS:
            trades = bt.run_donchian(candles, N_IN, N_OUT, +1, entry_ok=filters[v])
            res[coin][v] = [t for t in trades if t["time"] >= cutoff]
    return res, cutoff


def report(res, cutoff):
    coins = [c for c in res if res[c]["base"]]
    print("=" * 52)
    print(f"Period from {bot.fmt_time(cutoff)[:10]} | {len(coins)} coins | long only")
    print("PF / trades per coin")
    print("coin   base      vol       btc       both")
    for c in coins:
        cells = []
        for v in VARIANTS:
            ts = res[c][v]
            cells.append(f"{pf(ts):4.2f}/{len(ts):<3d}")
        print(f"{c:5}  " + "  ".join(cells))

    print("-" * 52)
    print("POOLED   PF   avgR  trades  totalR  better")
    pooled = {v: [t for c in coins for t in res[c][v]] for v in VARIANTS}
    better = {}
    for v in VARIANTS:
        better[v] = sum(1 for c in coins if pf(res[c][v]) > pf(res[c]["base"]))
        ts = pooled[v]
        avg = total(ts) / len(ts) if ts else 0.0
        extra = "" if v == "base" else f"{better[v]}/{len(coins)}"
        print(f"{v:6}  {pf(ts):5.2f} {avg:+5.2f} {len(ts):6d} {total(ts):+7.1f}  {extra}")

    # robustness: remove the single best baseline coin (one big winner can dominate)
    best = max(coins, key=lambda c: total(res[c]["base"]))
    print(f"Without {best} (best baseline coin) pooled PF:")
    print("  " + " | ".join(
        f"{v} {pf([t for c in coins if c != best for t in res[c][v]]):.2f}" for v in VARIANTS
    ))

    print("-" * 52)
    print("Pooled total R per year")
    years = sorted({bt.year_of(t["time"]) for t in pooled["base"]})
    print("year   " + "  ".join(f"{v:>6}" for v in VARIANTS))
    for y in years:
        row = [total([t for t in pooled[v] if bt.year_of(t["time"]) == y]) for v in VARIANTS]
        print(f"{y}  " + "  ".join(f"{x:+6.1f}" for x in row))

    print("=" * 52)
    print(f"VERDICT (need >={MIN_COIN_FRAC * 100:.0f}% coins better, "
          f">={MIN_TRADES_KEPT * 100:.0f}% trades kept, pooled PF up)")
    base_n = len(pooled["base"])
    for v in VARIANTS[1:]:
        frac = better[v] / len(coins) if coins else 0
        kept = len(pooled[v]) / base_n if base_n else 0
        ok1 = frac >= MIN_COIN_FRAC
        ok2 = kept >= MIN_TRADES_KEPT
        ok3 = pf(pooled[v]) > pf(pooled["base"])
        verdict = "PASS" if (ok1 and ok2 and ok3) else "FAIL"
        why = []
        if not ok1:
            why.append(f"coins {frac * 100:.0f}%")
        if not ok2:
            why.append(f"kept {kept * 100:.0f}%")
        if not ok3:
            why.append("pooled PF not higher")
        print(f"{v:5}: {verdict}" + (f"  ({', '.join(why)})" if why else f"  (coins {frac * 100:.0f}%, kept {kept * 100:.0f}%)"))
    print("=" * 52)


def main(coins):
    wanted = list(dict.fromkeys(["BTC"] + coins))
    data = {}
    for coin in wanted:
        print(f"loading {coin} ...", flush=True)
        try:
            data[coin] = load_coin(coin)
        except Exception as e:  # noqa: BLE001
            print(f"{coin}: skipped ({e})")
    if "BTC" not in data:
        raise SystemExit("BTC data is required for the BTC filter.")
    res, cutoff = run_all(data)
    if "BTC" not in coins:
        res.pop("BTC", None)
    report(res, cutoff)


if __name__ == "__main__":
    selected = [a.upper() for a in sys.argv[1:]] or COINS
    main(selected)
