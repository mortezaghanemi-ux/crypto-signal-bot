"""What would $100 have become? Portfolio simulation of the long-only Donchian 55/20.

Usage:
    python backtest_equity.py            # 12 coins, same period as backtest_filters.py

Variants:
    A : the live strategy
    B : A + "BTC above its 200-day average" filter
    +slip : an extra 0.1% slippage per side on top of the 0.1% fee already in the backtest

Rules of the simulation (to stay realistic, still simplified):
- Start with $100. Each trade risks a fixed % of CURRENT equity (0.25%, 0.5%, 1%).
- Position size = risk amount / stop distance, so a wide stop means a smaller position.
- Spot only: total open position value can never exceed 100% of equity
  (a trade is shrunk to the remaining room, or skipped when too little room is left).
- Equity changes only when a trade closes. Max drawdown is measured on those closed-trade
  values, so the true drawdown (with open losses) is somewhat deeper.
- Past results do not guarantee future results. Educational tool, not financial advice.
"""

import sys

import backtest_filters as bf
import backtest_trend as bt

START = 100.0
RISKS = [0.0025, 0.005, 0.01]
SLIP = 0.001  # extra slippage per side
MAX_EXPOSURE = 1.0  # spot: open position value <= equity
MIN_FILL = 0.25  # skip a trade if less than 25% of its intended size fits
STEP = bf.STEP


def collect(data, use_btc):
    btc_ok = bf.btc_trend_map(data["BTC"])
    cutoff = data["BTC"][bf.BTC_SMA][0]
    out = []
    for coin, candles in data.items():
        f = bf.make_filters(candles, btc_ok)["btc" if use_btc else "base"]
        for t in bt.run_donchian(candles, bf.N_IN, bf.N_OUT, +1, entry_ok=f):
            if t["time"] >= cutoff:
                out.append(
                    {
                        "coin": coin,
                        "t0": t["time"],
                        "t1": t["time"] + t["bars"] * STEP + 1,  # +1ms: exit sorts after its own entry
                        "r": t["r"],
                        "risk_pct": t["risk_pct"],
                    }
                )
    return out, cutoff


def simulate(trades, risk_frac, slip):
    """Return dict(final, max_dd, skipped, taken, by_year)."""
    events = []
    for i, t in enumerate(trades):
        events.append((t["t0"], 1, i))  # 1 = entry
        events.append((t["t1"], 0, i))  # 0 = exit (exits first when times tie)
    events.sort()

    equity = peak = START
    max_dd = 0.0
    open_pos, skipped, taken = {}, set(), 0
    year_end = {}
    for when, kind, i in events:
        t = trades[i]
        if kind == 1:
            intended = risk_frac * equity / t["risk_pct"]
            room = MAX_EXPOSURE * equity - sum(n for n, _ in open_pos.values())
            notional = min(intended, room)
            if notional < MIN_FILL * intended:
                skipped.add(i)
                continue
            open_pos[i] = (notional, notional * t["risk_pct"])
            taken += 1
        else:
            if i not in open_pos:
                continue
            _, risk_amt = open_pos.pop(i)
            r = t["r"] - 2 * slip / t["risk_pct"]
            equity += risk_amt * r
            peak = max(peak, equity)
            max_dd = max(max_dd, 1 - equity / peak)
            year_end[bt.year_of(when)] = equity
    return {
        "final": equity,
        "max_dd": max_dd,
        "skipped": len(skipped),
        "taken": taken,
        "by_year": year_end,
    }


def buy_and_hold(data, cutoff):
    ratios = {}
    for coin, candles in data.items():
        start = next((c for c in candles if c[0] >= cutoff), None)
        if start:
            ratios[coin] = candles[-1][4] / start[4]
    btc = ratios.get("BTC", float("nan"))
    avg = sum(ratios.values()) / len(ratios)
    return START * btc, START * avg


def main():
    data = {}
    for coin in bf.COINS:
        print(f"loading {coin} ...", flush=True)
        try:
            data[coin] = bf.load_coin(coin)
        except Exception as e:  # noqa: BLE001
            print(f"{coin}: skipped ({e})")
    if "BTC" not in data:
        raise SystemExit("BTC data is required.")

    a_trades, cutoff = collect(data, use_btc=False)
    b_trades, _ = collect(data, use_btc=True)
    bh_btc, bh_all = buy_and_hold(data, cutoff)

    print("=" * 50)
    print(f"Start ${START:.0f} on {bf.bot.fmt_time(cutoff)[:10]} | {len(data)} coins")
    print(f"Hold BTC only: ${bh_btc:,.0f} | hold all coins equally: ${bh_all:,.0f}")
    print("-" * 50)
    print("risk   variant    final $   maxDD   skipped")
    rows = {}
    for risk in RISKS:
        for label, trades, slip in (
            ("A", a_trades, 0.0),
            ("A+slip", a_trades, SLIP),
            ("B", b_trades, 0.0),
            ("B+slip", b_trades, SLIP),
        ):
            r = simulate(trades, risk, slip)
            rows[(risk, label)] = r
            print(
                f"{risk * 100:4.2f}%  {label:8} {r['final']:9,.0f}  "
                f"{r['max_dd'] * 100:4.0f}%   {r['skipped']:4d}/{r['taken'] + r['skipped']}"
            )
        print()

    key = (0.005, "A+slip")
    print("-" * 50)
    print("Year-end value, A+slip, 0.50% risk:")
    for y, v in sorted(rows[key]["by_year"].items()):
        print(f"  {y}: ${v:,.0f}")
    print("=" * 50)
    print("Closed-trade values; no taxes, no withdrawal limits, no")
    print("exchange problems. Real results will differ.")


if __name__ == "__main__":
    main()
