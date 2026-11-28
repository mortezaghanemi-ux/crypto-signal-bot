"""Trend-following (Donchian breakout) backtest - standard library only.

Usage:
    python backtest_trend.py        # last 5 years of BTC/USDT
    python backtest_trend.py 3      # last 3 years

Strategy (parameters are the classic "Turtle" values, NOT tuned on this data):
- LONG : a candle closes above the highest high of the previous N_IN candles
         -> enter at the next candle's open.
- SHORT: a candle closes below the lowest low of the previous N_IN candles
         -> enter at the next candle's open.
- Exit : candle closes beyond the opposite N_OUT-candle channel (exit next open),
         or the hard stop at 2 x ATR is touched (checked with high/low,
         gap-aware). No fixed take-profit: winners are allowed to run.
- 1R = the 2 x ATR stop distance. Fee = 0.1% per side.
- Long and short are simulated as independent sleeves, then combined.

Variants tested (fixed in advance, to limit cherry-picking):
    4h and 1d  x  20/10 and 55/20
Educational tool, not financial advice.
"""

import sys
from datetime import datetime, timezone

import backtest
import bot

FEE = 0.001
STOP_ATR = 2.0
ATR_PERIOD = 14
WARMUP_DAYS = 60
DAY_MS = 24 * 3600 * 1000

VARIANTS = [
    ("4h", 20, 10),
    ("4h", 55, 20),
    ("1d", 20, 10),
    ("1d", 55, 20),
]


def to_daily(candles):
    """Aggregate 4h candles into complete UTC days."""
    days = {}
    for c in candles:
        days.setdefault(c[0] // DAY_MS, []).append(c)
    out = []
    for d in sorted(days):
        g = days[d]
        if len(g) != 6:
            continue
        out.append(
            (d * DAY_MS, g[0][1], max(x[2] for x in g), min(x[3] for x in g), g[-1][4])
        )
    return out


def run_donchian(candles, n_in, n_out, direction, entry_ok=None):
    """direction: +1 long-only sleeve, -1 short-only sleeve. Returns trade list.

    entry_ok: optional filter. Called as entry_ok(p) with p = index of the last
    closed candle when a breakout is detected; return False to skip the entry.
    With entry_ok=None the behaviour is exactly the original.
    """
    n = len(candles)
    opens = [c[1] for c in candles]
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    closes = [c[4] for c in candles]
    atrs = bot.atr(highs, lows, closes, ATR_PERIOD)

    t0 = candles[0][0] + WARMUP_DAYS * DAY_MS
    start = max(n_in + 2, ATR_PERIOD + 2, n_out + 2)
    while start < n and candles[start][0] < t0:
        start += 1

    trades, pos = [], None

    def close_trade(k, price, reason):
        nonlocal pos
        risk = pos["risk"]
        gross = direction * (price - pos["entry"]) / risk
        fee_r = 2 * FEE * pos["entry"] / risk
        trades.append(
            {"time": pos["time"], "side": "BUY" if direction == 1 else "SELL",
             "r": gross - fee_r, "reason": reason, "bars": k - pos["k"],
             "risk_pct": risk / pos["entry"]}
        )
        pos = None

    def stop_hit(k):
        """Return exit price if the stop is touched in candle k, else None."""
        if direction == 1:
            if opens[k] <= pos["stop"]:
                return opens[k]
            if lows[k] <= pos["stop"]:
                return pos["stop"]
        else:
            if opens[k] >= pos["stop"]:
                return opens[k]
            if highs[k] >= pos["stop"]:
                return pos["stop"]
        return None

    for k in range(start, n):
        p = k - 1  # last closed candle known when candle k opens
        if pos is not None:
            if direction == 1 and closes[p] < min(lows[p - n_out:p]):
                close_trade(k, opens[k], "CHANNEL")
                continue
            if direction == -1 and closes[p] > max(highs[p - n_out:p]):
                close_trade(k, opens[k], "CHANNEL")
                continue
            px = stop_hit(k)
            if px is not None:
                close_trade(k, px, "STOP")
            continue

        a = atrs[p]
        if a is None:
            continue
        if direction == 1 and closes[p] > max(highs[p - n_in:p]):
            pass
        elif direction == -1 and closes[p] < min(lows[p - n_in:p]):
            pass
        else:
            continue
        if entry_ok is not None and not entry_ok(p):
            continue
        entry = opens[k]
        pos = {
            "entry": entry,
            "stop": entry - direction * STOP_ATR * a,
            "risk": STOP_ATR * a,
            "time": candles[k][0],
            "k": k,
        }
        px = stop_hit(k)  # the entry candle itself can hit the stop
        if px is not None:
            close_trade(k, px, "STOP")

    if pos is not None:
        close_trade(n - 1, closes[-1], "END")
    return trades


def stats_line(label, ts):
    if not ts:
        return f"{label}: no trades"
    wins = [t["r"] for t in ts if t["r"] > 0]
    losses = [t["r"] for t in ts if t["r"] <= 0]
    total = sum(t["r"] for t in ts)
    pf = sum(wins) / -sum(losses) if losses and sum(losses) < 0 else float("inf")
    cum = peak = dd = 0.0
    eq = 1.0
    for t in sorted(ts, key=lambda x: x["time"]):
        cum += t["r"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        eq *= 1 + 0.01 * t["r"]
    return (
        f"{label}: {len(ts):3d} tr | win {len(wins) / len(ts) * 100:2.0f}% | "
        f"avg {total / len(ts):+.2f}R | tot {total:+6.1f}R | PF {pf:.2f} | "
        f"DD {dd:.1f}R | eq x{eq:.2f}"
    )


def year_of(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


def evaluate(name, candles, n_in, n_out):
    longs = run_donchian(candles, n_in, n_out, +1)
    shorts = run_donchian(candles, n_in, n_out, -1)
    both = longs + shorts

    print("=" * 46)
    print(f"{name}  Donchian {n_in}/{n_out}")
    print(stats_line("ALL  ", both))
    print(stats_line("LONG ", longs))
    print(stats_line("SHORT", shorts))
    print("year   ALL   LONG  SHORT")
    pos_years = 0
    years = sorted({year_of(t["time"]) for t in both})
    for y in years:
        la = sum(t["r"] for t in longs if year_of(t["time"]) == y)
        sa = sum(t["r"] for t in shorts if year_of(t["time"]) == y)
        pos_years += (la + sa) > 0
        print(f"{y} {la + sa:+6.1f} {la:+6.1f} {sa:+6.1f}")
    return {
        "name": f"{name} {n_in}/{n_out}",
        "all": sum(t["r"] for t in both),
        "long": sum(t["r"] for t in longs),
        "short": sum(t["r"] for t in shorts),
        "pos_years": pos_years,
        "years": len(years),
    }


if __name__ == "__main__":
    yrs = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    raw = backtest.load_history(yrs)[:-1]  # drop the still-forming last candle
    data = {"4h": raw, "1d": to_daily(raw)}

    results = []
    for tf, n_in, n_out in VARIANTS:
        results.append(evaluate(tf, data[tf], n_in, n_out))

    first = data["1d"][0][1]
    last = data["1d"][-1][4]
    print("=" * 46)
    print("SUMMARY (total R | years positive)")
    for r in results:
        print(
            f"{r['name']:12} ALL {r['all']:+6.1f} | LONG {r['long']:+6.1f} | "
            f"SHORT {r['short']:+6.1f} | {r['pos_years']}/{r['years']} yrs"
        )
    print(f"Buy & hold, same data: {(last / first - 1) * 100:+.0f}%")
    print("=" * 46)
