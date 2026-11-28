"""1-hour filter experiments on top of version A (long-only Donchian 55/20), 12 coins.

Usage:
    python backtest_1h_filters.py

Variants (all rules FIXED IN ADVANCE, nothing is tuned; same data, period, fee and
slippage as backtest_1h.py):
    A   : breakout above the previous 55-candle high, stop 2xATR, exit below the 20-candle low
    ADX : A, but only when ADX(14) > 25 on the breakout candle (the trend is strong)
    BB  : A, but only when a Bollinger squeeze happened shortly before the breakout: in
          the 5 candles BEFORE the breakout candle the Bollinger bandwidth (20, 2 sigma)
          was at or below the lowest 20% of its previous 120 values
    DOM : A, but for the 11 altcoins only when the BTC-dominance PROXY is falling, meaning
          it is below its own 50-day average (BTC itself is not filtered)
    B   : A + BTC above its 200-day average (reference, tested before)

About the "dominance" proxy: real BTC dominance needs total market data that we cannot
get from KuCoin. The proxy used here is  BTC price / equal-weight basket of the 11 other
coins  (both normalised to 1 at the start). Falling = altcoins are beating BTC.
It is NOT the official dominance number.

A filter passes only if ALL are true:  profit factor (after slippage) improves on >= 75%
of the coins, >= 50% of the baseline trades are kept, and the pooled profit factor
(after slippage) improves.
Educational tool, not financial advice.
"""

import backtest_1h as h
import backtest_trend as bt
import bot

ADX_PERIOD = 14
ADX_MIN = 25.0
BB_N = 20
BB_K = 2.0
BB_LOOK = 5
BB_HIST = 120
BB_PCT = 0.20
DOM_SMA = 1200  # 50 days of 1h candles
MIN_COIN_FRAC = 0.75
MIN_TRADES_KEPT = 0.5
VARIANTS = ["A", "ADX", "BB", "DOM", "B"]
TESTED = ["ADX", "BB", "DOM", "B"]


# ---------------- Indicators ----------------
def adx_series(highs, lows, closes, n=ADX_PERIOD):
    """Wilder ADX aligned with the candles (None until 2n-1 candles are available)."""
    size = len(closes)
    out = [None] * size
    if size < 2 * n + 1:
        return out
    tr = [0.0] * size
    pdm = [0.0] * size
    ndm = [0.0] * size
    for i in range(1, size):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        ndm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))

    def dx_at(a, p, m):
        if a == 0:
            return 0.0
        pdi, ndi = 100 * p / a, 100 * m / a
        s = pdi + ndi
        return 0.0 if s == 0 else 100 * abs(pdi - ndi) / s

    a_s, p_s, n_s = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(ndm[1:n + 1])
    dx = [None] * size
    dx[n] = dx_at(a_s, p_s, n_s)
    for i in range(n + 1, size):
        a_s = a_s - a_s / n + tr[i]
        p_s = p_s - p_s / n + pdm[i]
        n_s = n_s - n_s / n + ndm[i]
        dx[i] = dx_at(a_s, p_s, n_s)
    adx = sum(dx[n:2 * n]) / n
    out[2 * n - 1] = adx
    for i in range(2 * n, size):
        adx = (adx * (n - 1) + dx[i]) / n
        out[i] = adx
    return out


def bandwidth_series(closes, n=BB_N, k=BB_K):
    """Bollinger bandwidth = (upper - lower) / middle, None until n candles exist."""
    out = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        w = closes[i - n + 1:i + 1]
        m = sum(w) / n
        var = sum((x - m) ** 2 for x in w) / n
        out[i] = (2 * k * var ** 0.5) / m if m else None
    return out


def squeeze_ok(bw, p, look=BB_LOOK, hist=BB_HIST, pct=BB_PCT):
    """True if, in the `look` candles BEFORE candle p, the bandwidth was in its lowest `pct`.

    The reference window is the `hist` values before p (the breakout candle itself is not
    used, because a breakout widens the bands by definition).
    """
    if p < hist + look:
        return False
    window = bw[p - hist:p]
    if any(x is None for x in window):
        return False
    threshold = sorted(window)[int(pct * hist)]
    return min(bw[p - look:p]) <= threshold


def dominance_falling_map(data, n=DOM_SMA):
    """{time_ms: True if the BTC-dominance proxy is below its n-candle average}."""
    btc = data["BTC"]
    alts = [c for c in data if c != "BTC"]
    if not alts:
        return {}
    alt_close = {c: {x[0]: x[4] for x in data[c]} for c in alts}
    last, base = {}, None
    btc0 = None
    prox, out, running = [], {}, 0.0
    for candle in btc:
        t, btc_c = candle[0], candle[4]
        for c in alts:
            if t in alt_close[c]:
                last[c] = alt_close[c][t]
        if base is None:
            if len(last) < len(alts):
                continue  # wait until every alt has a price
            base = dict(last)
            btc0 = btc_c
        basket = sum(last[c] / base[c] for c in alts) / len(alts)
        value = (btc_c / btc0) / basket
        prox.append(value)
        running += value
        if len(prox) > n:
            running -= prox[-n - 1]
        if len(prox) >= n:
            out[t] = value < running / n
    return out


# ---------------- Runner ----------------
def run_all(data):
    btc = data["BTC"]
    if len(btc) <= h.BTC_SMA:
        raise SystemExit("Not enough BTC history for the 200-day average.")
    cutoff = btc[h.BTC_SMA][0]
    btc_ok = h.btc_trend_map(btc)
    falling = dominance_falling_map(data)
    res = {}
    for coin, cs in data.items():
        times = [c[0] for c in cs]
        highs = [c[2] for c in cs]
        lows = [c[3] for c in cs]
        closes = [c[4] for c in cs]
        adx = adx_series(highs, lows, closes)
        bw = bandwidth_series(closes)
        filters = {
            "A": None,
            "ADX": lambda p, adx=adx: adx[p] is not None and adx[p] > ADX_MIN,
            "BB": lambda p, bw=bw: squeeze_ok(bw, p),
            "DOM": (lambda p: True) if coin == "BTC"
            else (lambda p, times=times: falling.get(times[p], False)),
            "B": lambda p, times=times: btc_ok.get(times[p], False),
        }
        res[coin] = {}
        for v in VARIANTS:
            trades = bt.run_donchian(cs, 55, 20, +1, entry_ok=filters[v])
            kept = []
            for t in trades:
                if t["time"] >= cutoff:
                    t = dict(t)
                    t["r_slip"] = t["r"] - 2 * h.SLIP / t["risk_pct"]
                    kept.append(t)
            res[coin][v] = kept
    return res, cutoff, btc[-1][0]


# ---------------- Report ----------------
def report(res, cutoff, last_ms):
    coins = list(res)
    weeks = (last_ms - cutoff) / (7 * 24 * 3600 * 1000)
    pf, total = h.pf, h.total
    print("=" * 56)
    print(f"1h filters on A (55/20) | {len(coins)} coins | long only")
    print(f"Period {bot.fmt_time(cutoff)[:10]} -> {bot.fmt_time(last_ms)[:10]} ({weeks / 52.18:.1f} years)")
    print("Per coin: PF after slippage / trades")
    print("coin   A         ADX       BB        DOM       B")
    for c in coins:
        cells = [f"{pf(res[c][v], 'r_slip'):4.2f}/{len(res[c][v]):<4d}" for v in VARIANTS]
        print(f"{c:5}  " + " ".join(cells))

    pooled = {v: [t for c in coins for t in res[c][v]] for v in VARIANTS}
    best2 = sorted(coins, key=lambda c: total(res[c]["A"], "r_slip"), reverse=True)[:2]
    print("-" * 56)
    print("POOLED   n  /wk   PF   PFslip avgR  totR(slip) coins>1")
    better = {}
    for v in VARIANTS:
        ts = pooled[v]
        better[v] = sum(1 for c in coins if pf(res[c][v], "r_slip") > pf(res[c]["A"], "r_slip"))
        if not ts:
            print(f"{v:4} no trades")
            continue
        good = sum(1 for c in coins if res[c][v] and pf(res[c][v], "r_slip") > 1)
        print(
            f"{v:4} {len(ts):5d} {len(ts) / weeks:4.1f} {pf(ts):5.2f} {pf(ts, 'r_slip'):6.2f} "
            f"{total(ts, 'r_slip') / len(ts):+5.2f} {total(ts, 'r_slip'):+8.0f}   {good}/{len(coins)}"
        )
    print(f"Without the 2 best A coins ({', '.join(best2)}), pooled PF after slip:")
    print("  " + " | ".join(
        f"{v} {pf([t for c in coins if c not in best2 for t in res[c][v]], 'r_slip'):.2f}"
        for v in VARIANTS
    ))

    print("-" * 56)
    print("Pooled total R after slip, by year (edges are partial years)")
    print("year     " + " ".join(f"{v:>7}" for v in VARIANTS))
    years = sorted({bt.year_of(t["time"]) for v in VARIANTS for t in pooled[v]})
    for y in years:
        row = [total([t for t in pooled[v] if bt.year_of(t["time"]) == y], "r_slip") for v in VARIANTS]
        print(f"{y}  " + " ".join(f"{x:+7.0f}" for x in row))

    print("=" * 56)
    print(f"VERDICT (>= {MIN_COIN_FRAC * 100:.0f}% coins better, >= {MIN_TRADES_KEPT * 100:.0f}% trades kept, pooled PF up)")
    base_n = len(pooled["A"])
    for v in TESTED:
        frac = better[v] / len(coins)
        kept = len(pooled[v]) / base_n if base_n else 0
        ok = (
            frac >= MIN_COIN_FRAC
            and kept >= MIN_TRADES_KEPT
            and pf(pooled[v], "r_slip") > pf(pooled["A"], "r_slip")
        )
        print(f"{v:4}: {'PASS' if ok else 'FAIL'}  (coins better {frac * 100:.0f}%, trades kept {kept * 100:.0f}%)")
    print("=" * 56)


def main():
    data = {}
    for coin in h.COINS:
        print(f"loading {coin} ...", flush=True)
        try:
            data[coin] = h.load_coin(coin)
        except Exception as e:  # noqa: BLE001
            print(f"{coin}: skipped ({e})")
    if "BTC" not in data:
        raise SystemExit("BTC data is required.")
    res, cutoff, last_ms = run_all(data)
    report(res, cutoff, last_ms)


if __name__ == "__main__":
    main()
