"""Out-of-sample test: run the SAME fixed Donchian rules on several coins.

Usage:
    python backtest_coins.py                   # 5 years, BTC ETH SOL BNB XRP
    python backtest_coins.py 3 ETH SOL         # 3 years, chosen coins

No parameter is re-tuned per coin. If the rules only work on BTC, that is a
warning sign (likely luck); if they work broadly, the case is much stronger.
Educational tool, not financial advice.
"""

import sys

import backtest
import backtest_trend as bt

DEFAULT_COINS = ["BTC", "ETH", "SOL", "BNB", "XRP"]


def profit_factor(ts):
    wins = sum(t["r"] for t in ts if t["r"] > 0)
    loss = -sum(t["r"] for t in ts if t["r"] <= 0)
    return wins / loss if loss > 0 else float("inf")


def analyze(raw):
    """Return (list of per-variant result dicts, buy&hold fraction)."""
    data = {"4h": raw, "1d": bt.to_daily(raw)}
    out = []
    for tf, n_in, n_out in bt.VARIANTS:
        c = data[tf]
        longs = bt.run_donchian(c, n_in, n_out, +1)
        shorts = bt.run_donchian(c, n_in, n_out, -1)
        both = longs + shorts
        years = sorted({bt.year_of(t["time"]) for t in both})
        pos = sum(
            1 for y in years if sum(t["r"] for t in both if bt.year_of(t["time"]) == y) > 0
        )
        out.append(
            {
                "name": f"{tf} {n_in}/{n_out}",
                "all": sum(t["r"] for t in both),
                "long": sum(t["r"] for t in longs),
                "short": sum(t["r"] for t in shorts),
                "pf_long": profit_factor(longs),
                "n_long": len(longs),
                "pos_years": pos,
                "years": len(years),
            }
        )

    daily = data["1d"]
    t0 = daily[0][0] + bt.WARMUP_DAYS * bt.DAY_MS
    start_price = next(c[1] for c in daily if c[0] >= t0)
    bh = daily[-1][4] / start_price - 1
    return out, bh


def main(years, coins):
    results = {}
    for coin in coins:
        print(f"--- {coin} ---")
        try:
            raw = backtest.load_history(years, coin)[:-1]  # drop forming candle
            res, bh = analyze(raw)
        except SystemExit as e:
            print(f"{coin}: skipped ({e})")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"{coin}: skipped ({e})")
            continue
        results[coin] = res
        print(f"{coin}: buy&hold {bh * 100:+.0f}% over the tested period")
        for r in res:
            print(
                f"{r['name']:9} ALL {r['all']:+6.1f} | "
                f"LONG {r['long']:+6.1f} (PF {r['pf_long']:.2f}, {r['n_long']}tr) | "
                f"SHORT {r['short']:+6.1f} | {r['pos_years']}/{r['years']}y"
            )

    if not results:
        print("No coin could be tested.")
        return results

    print("=" * 46)
    print(f"OVERALL ({len(results)} coins)")
    print("variant    coins>0: ALL LONG | pooled R: ALL   LONG")
    for idx, (tf, n_in, n_out) in enumerate(bt.VARIANTS):
        rs = [results[c][idx] for c in results]
        print(
            f"{tf} {n_in}/{n_out:<3}   "
            f"{sum(r['all'] > 0 for r in rs):>4}/{len(rs)} {sum(r['long'] > 0 for r in rs):>3}/{len(rs)}"
            f"   | {sum(r['all'] for r in rs):+8.1f} {sum(r['long'] for r in rs):+7.1f}"
        )
    print("=" * 46)
    return results


if __name__ == "__main__":
    yrs = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    selected = [a.upper() for a in sys.argv[2:]] or DEFAULT_COINS
    main(yrs, selected)
