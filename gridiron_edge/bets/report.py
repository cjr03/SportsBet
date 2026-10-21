"""Performance report: ROI net of fees, CLV, calibration, drawdown, drift. Plain dicts for the UI.

All money is integer cents. ROI and Brier use graded bets (won/lost); refunds are counted apart.
Drawdown is on settled trading P&L only (payout - stake - fee, in settled_at order): deposits,
withdrawals and open stakes are excluded, and open bets are not marked to market.
"""
import json
import math
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime

Z = 1.96


def _get(row, key, default=None):
    try:
        v = row[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def _legs(row) -> list[dict]:
    try:
        return json.loads(_get(row, "legs_json", "[]"))
    except (TypeError, ValueError):
        return []


def _cost(row) -> int:
    return _get(row, "stake_cents", 0) + _get(row, "fee_cents", 0)


def _profit(row) -> int:
    return _get(row, "payout_cents", 0) - _cost(row)


def _graded(rows) -> list:
    return [r for r in rows if _get(r, "result") in ("won", "lost")]


def segment(row) -> str:
    """'<league> <ml|spread|total|prop|prizepicks>' or 'arb'; the drift and gate unit."""
    kind = _get(row, "kind")
    if kind == "arb":
        return "arb"
    legs = _legs(row)
    leg = legs[0] if legs else {}
    league = (leg.get("game_id") or "nfl").split(":")[0]
    if kind == "pp_entry":
        return f"{league} prizepicks"
    if kind == "kalshi_prop":
        return f"{league} prop"
    if leg.get("line") is None:
        return f"{league} ml"
    return f"{league} {'total' if leg.get('team') is None else 'spread'}"


def roi_stats(rows: Iterable) -> dict:
    """Fee-inclusive ROI = sum(profit)/sum(cost), with a delta-method CI for the ratio."""
    g = _graded(rows)
    cost = sum(_cost(r) for r in g)
    profit = sum(_profit(r) for r in g)
    n = len(g)
    out = {"n": n, "cost_cents": cost, "profit_cents": profit, "roi": None, "se": None,
           "ci_lo": None, "ci_hi": None, "conclusive": False}
    if not cost:
        return out
    roi = profit / cost
    out["roi"] = roi
    if n >= 2:
        resid = sum((_profit(r) - roi * _cost(r)) ** 2 for r in g)
        se = math.sqrt(n / (n - 1) * resid) / cost
        out.update(se=se, ci_lo=roi - Z * se, ci_hi=roi + Z * se,
                   conclusive=se > 0 and (roi - Z * se > 0 or roi + Z * se < 0))
    return out


def clv_stats(values: Iterable[float]) -> dict:
    v = [x for x in values if x is not None]
    n = len(v)
    out = {"n": n, "mean": None, "se": None, "t": None, "lo": None, "hi": None, "pos_share": None}
    if not n:
        return out
    mean = sum(v) / n
    out.update(mean=mean, pos_share=sum(x > 0 for x in v) / n)
    if n >= 2:
        se = math.sqrt(sum((x - mean) ** 2 for x in v) / (n - 1)) / math.sqrt(n)
        out.update(se=se, t=mean / se if se > 0 else None, lo=mean - 2 * se, hi=mean + 2 * se)
    return out


def _won_event(row) -> int:
    """Outcome of the event p_win forecasts: for PrizePicks that is a profit (mult > 1), not any payout."""
    if _get(row, "kind") == "pp_entry":
        return int(_get(row, "payout_cents", 0) > _get(row, "stake_cents", 0))
    return int(_get(row, "result") == "won")


def brier_by_kind(rows: Iterable) -> dict[str, dict]:
    """Brier score per bet kind (arbs excluded: p_win=1 by construction)."""
    acc: dict[str, list[float]] = defaultdict(list)
    for r in _graded(rows):
        p = _get(r, "p_win")
        if p is None or _get(r, "kind") == "arb":
            continue
        acc[_get(r, "kind")].append((p - _won_event(r)) ** 2)
    return {k: {"n": len(v), "brier": sum(v) / len(v)} for k, v in acc.items()}


def brier(rows: Iterable) -> float | None:
    pts = [(_get(r, "p_win") - _won_event(r)) ** 2 for r in _graded(rows)
           if _get(r, "p_win") is not None and _get(r, "kind") != "arb"]
    return sum(pts) / len(pts) if pts else None


def drawdown(rows: Iterable, starting_cents: int = 0) -> dict:
    """Max and current drawdown of cumulative settled P&L; pct is of peak (starting + P&L) equity."""
    settled = sorted((r for r in rows if _get(r, "result") in ("won", "lost", "refund")),
                     key=lambda r: _get(r, "settled_at", ""))
    pnl = peak = 0
    max_dd = 0
    max_pct = 0.0
    for r in settled:
        pnl += _profit(r)
        peak = max(peak, pnl)
        dd = peak - pnl
        max_dd = max(max_dd, dd)
        eq = starting_cents + peak
        if eq > 0:
            max_pct = max(max_pct, dd / eq)
    eq = starting_cents + peak
    return {"pnl_cents": pnl, "peak_cents": peak, "max_dd_cents": max_dd, "current_dd_cents": peak - pnl,
            "max_dd_pct": max_pct, "current_dd_pct": (peak - pnl) / eq if eq > 0 else 0.0}


# -- database report -----------------------------------------------------------
_LEAGUE_WORD = {"nfl": "NFL", "cfb": "college football"}
_TYPE_WORD = {"ml": "game-winner bets", "spread": "point-spread bets", "total": "total-points bets",
              "prop": "player-prop bets", "prizepicks": "PrizePicks entries"}
MIN_CAL_N = 10
CALIBRATION_WARNING = ("Warning: lately our chances have come true less often than they did when we tested "
                       "them on past games. Bets are not paused for this; keep an eye on it.")


def segment_label(seg: str) -> str:
    """'nfl spread' -> 'NFL point-spread bets' (plain English)."""
    if seg == "arb":
        return "locked-profit bets"
    lg, _, typ = seg.partition(" ")
    return f"{_LEAGUE_WORD.get(lg, lg.upper())} {_TYPE_WORD.get(typ, typ + ' bets')}"


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def _pct(x: float | None, signed: bool = True) -> str:
    if x is None:
        return "not enough data"
    return f"{x * 100:+.1f}%" if signed else f"{x * 100:.1f}%"


def _settled_rows(conn) -> list:
    return conn.execute(
        "SELECT b.*, r.p_win, r.p_mkt, r.leg_key FROM bets b LEFT JOIN recommendations r ON r.id = b.rec_id "
        "WHERE b.state != 'PLACED'").fetchall()


def _signal_rows(conn) -> list:
    """One CLV-measured signal per contract set (leg_key): the first one issued, placed or not."""
    rows = conn.execute("SELECT * FROM recommendations WHERE clv_close IS NOT NULL ORDER BY id").fetchall()
    seen, out = set(), []
    for r in rows:
        if r["leg_key"] in seen:
            continue
        seen.add(r["leg_key"])
        out.append(r)
    return out


def _leg_rows(conn) -> list:
    return conn.execute(
        "SELECT l.clv, l.stale, l.ts, r.kind, r.legs_json FROM leg_clv l "
        "JOIN recommendations r ON r.id = l.rec_id ORDER BY l.ts, l.rec_id, l.leg_idx").fetchall()


def _clv_series(conn) -> dict[str, list[tuple[str, float]]]:
    """Per segment, (time, CLV) of every measured signal (Kalshi recs and PrizePicks legs)."""
    out: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in _signal_rows(conn):
        out[segment(r)].append((r["clv_at"] or "", r["clv_close"]))
    for r in _leg_rows(conn):
        out[segment(r)].append((r["ts"], r["clv"]))
    for v in out.values():
        v.sort(key=lambda t: t[0])
    return out


def _after(ts: str, cutoff: str) -> bool:
    try:
        return datetime.fromisoformat(ts) > datetime.fromisoformat(cutoff)
    except (TypeError, ValueError):
        return ts > cutoff


def drift(conn, window: int = 50, since: dict[str, str] | None = None) -> dict[str, dict]:
    """Rolling-window CLV per segment; alarm when the window is full and mean + 2 SE is below 0.

    since: segment -> ISO time of its last reset; only CLV recorded after it counts, so a reset
    window has to refill with new data before it can alarm again.
    """
    out = {}
    for seg, series in _clv_series(conn).items():
        if (cut := (since or {}).get(seg)) is not None:
            series = [t for t in series if _after(t[0], cut)]
        s = clv_stats(c for _, c in series[-window:])
        alarm = s["n"] >= window and s["hi"] is not None and s["hi"] < 0
        out[seg] = {**s, "window": window, "alarm": alarm, "label": segment_label(seg)}
    return out


def _calibration(rows) -> dict:
    from gridiron_edge.models.blend import reliability

    pts = [(_get(r, "p_win"), _won_event(r), _get(r, "p_mkt")) for r in _graded(rows)
           if _get(r, "kind") != "arb" and _get(r, "p_win") is not None]
    both = [(p, y, m) for p, y, m in pts if m is not None]
    b_model = sum((p - y) ** 2 for p, y, _ in both) / len(both) if both else None
    b_mkt = sum((m - y) ** 2 for _, y, m in both) / len(both) if both else None
    return {
        "n": len(pts), "brier": sum((p - y) ** 2 for p, y, _ in pts) / len(pts) if pts else None,
        "brier_by_kind": brier_by_kind(rows), "n_with_market": len(both),
        "brier_model_same_bets": b_model, "brier_market": b_mkt,
        "skill_vs_market": (1 - b_model / b_mkt) if both and b_mkt else None,
        "reliability": reliability([p for p, _, _ in pts], [y for _, y, _ in pts]),
    }


def _calibration_drift(rows, window: int, backtest: dict | None) -> dict:
    g = sorted((r for r in _graded(rows) if _get(r, "kind") != "arb" and _get(r, "p_win") is not None),
               key=lambda r: _get(r, "settled_at", ""))[-window:]
    errs = [(_get(r, "p_win") - _won_event(r)) ** 2 for r in g]
    bt = [v["brier"] for v in (backtest or {}).values() if isinstance(v, dict) and v.get("brier")]
    out = {"n": len(errs), "rolling_brier": None, "backtest_brier": sum(bt) / len(bt) if bt else None,
           "worse": False}
    if errs:
        mean = sum(errs) / len(errs)
        sd = math.sqrt(sum((e - mean) ** 2 for e in errs) / (len(errs) - 1)) if len(errs) > 1 else 0.0
        out["rolling_brier"] = mean
        if out["backtest_brier"] is not None and len(errs) >= MIN_CAL_N:
            out["worse"] = mean - 2 * sd / math.sqrt(len(errs)) > out["backtest_brier"]
    return out


def calibration_drift(conn, window: int = 50, backtest: dict | None = None) -> dict:
    return _calibration_drift(_settled_rows(conn), window, backtest)


def _net_deposits(conn) -> int:
    return conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM ledger "
                        "WHERE kind IN ('deposit','withdraw')").fetchone()[0]


def build_report(conn, now, backtest: dict | None = None, window: int = 50,
                 drift_since: dict[str, str] | None = None) -> dict:
    """Everything the Performance tab shows, as plain data plus plain-English summary lines.

    backtest: the stored gate results per league (walkforward metrics), for calibration drift.
    drift_since: segment -> last drift reset time (see drift()).
    """
    rows = _settled_rows(conn)
    bets = [r for r in rows if _get(r, "kind") != "arb"]
    arbs = [r for r in rows if _get(r, "kind") == "arb" and _get(r, "result") in ("won", "lost", "refund")]
    by_seg: dict[str, list] = defaultdict(list)
    for r in bets:
        by_seg[segment(r)].append(r)
    won = sum(_get(r, "result") == "won" for r in rows)
    lost = sum(_get(r, "result") == "lost" for r in rows)
    refunds = sum(_get(r, "result") == "refund" for r in rows)
    placed_clv = clv_stats(_get(r, "clv") for r in rows if _get(r, "kind") != "arb")
    signals = _signal_rows(conn)
    legs = _leg_rows(conn)
    clv_by_seg: dict[str, dict] = {}
    seg_vals: dict[str, dict[str, list]] = defaultdict(lambda: {"placed": [], "all": []})
    for r in bets:
        if _get(r, "clv") is not None:
            seg_vals[segment(r)]["placed"].append(r["clv"])
    for r in signals:
        seg_vals[segment(r)]["all"].append(r["clv_close"])
    for r in legs:
        seg_vals[segment(r)]["all"].append(r["clv"])
    for seg, v in seg_vals.items():
        clv_by_seg[seg] = {"label": segment_label(seg), "placed": clv_stats(v["placed"]),
                           "all_signals": clv_stats(v["all"])}
    stale = [r for r in signals if r["clv_stale"]] + [r for r in legs if r["stale"]]
    report = {
        "as_of": now.isoformat(),
        "placed": {
            "roi": roi_stats(bets), "record": f"{won}-{lost}-{refunds}",
            "by_segment": {seg: {"label": segment_label(seg), **roi_stats(v)} for seg, v in by_seg.items()},
            "arb_profit_cents": sum(_profit(r) for r in arbs), "arb_count": len(arbs),
        },
        "clv": {"placed": placed_clv,
                "all_signals": clv_stats([r["clv_close"] for r in signals]),
                "prizepicks_legs": clv_stats([r["clv"] for r in legs]),
                "stale_share": len(stale) / (len(signals) + len(legs)) if signals or legs else None,
                "by_segment": clv_by_seg},
        "calibration": _calibration(rows),
        "drawdown": drawdown(rows, starting_cents=_net_deposits(conn)),
        "drift": {"window": window, "segments": drift(conn, window, drift_since),
                  "calibration": _calibration_drift(rows, window, backtest)},
    }
    report["summary"] = summary_lines(report)
    return report


def calibration_lines(reliability: list[dict], min_n: int = 1) -> list[str]:
    """How often our chances came true, per band: 'When we said 40-60%, it happened 55% of the time (12 bets)'."""
    out = []
    for b in reliability:
        if b["n"] >= min_n and b["hit_rate"] is not None:
            out.append(f"When we said {b['lo'] * 100:.0f}-{b['hi'] * 100:.0f}%, it happened "
                       f"{b['hit_rate'] * 100:.0f}% of the time ({b['n']} bet{'s' if b['n'] != 1 else ''})")
    return out


def summary_lines(r: dict) -> list[str]:
    """Plain-English, ASCII lines for the Performance tab and the log."""
    out = []
    roi = r["placed"]["roi"]
    w, lo, rf = r["placed"]["record"].split("-")
    if roi["n"]:
        out.append(f"Profit after fees: {_money(roi['profit_cents'])} on {_money(roi['cost_cents'])} spent "
                   f"({_pct(roi['roi'])} return). {w} won, {lo} lost, {rf} refunded.")
        if roi["ci_lo"] is not None:
            verdict = ("the edge looks real" if roi["ci_lo"] > 0 else "we are losing money for real"
                       if roi["ci_hi"] < 0 else "too few bets to tell luck from skill yet")
            out.append(f"Likely long-run return: {_pct(roi['ci_lo'])} to {_pct(roi['ci_hi'])} ({verdict}).")
    else:
        out.append("No settled bets yet.")
    if r["placed"]["arb_count"]:
        out.append(f"Locked-profit bets made {_money(r['placed']['arb_profit_cents'])} "
                   f"over {r['placed']['arb_count']} bets.")
    c = r["clv"]
    if c["placed"]["n"] or c["all_signals"]["n"]:
        parts = []
        if c["placed"]["n"]:
            parts.append(f"bets you placed got prices {_pct(c['placed']['mean'])} vs the closing price "
                         f"({c['placed']['n']} bets)")
        if c["all_signals"]["n"]:
            parts.append(f"all bet ideas, placed or not: {_pct(c['all_signals']['mean'])} "
                         f"({c['all_signals']['n']} ideas)")
        out.append("Beat the closing price (higher is better): " + "; ".join(parts) + ".")
    cal = r["calibration"]
    if cal["brier_market"] is not None:
        out.append(f"Accuracy score (lower is better): ours {cal['brier_model_same_bets']:.3f} vs "
                   f"{cal['brier_market']:.3f} for the market's own prices.")
    elif cal["brier"] is not None:
        out.append(f"Accuracy score (lower is better; 0.250 is a coin flip): {cal['brier']:.3f}.")
    dd = r["drawdown"]
    if dd["max_dd_cents"]:
        out.append(f"Biggest drop from a high point: {_money(dd['max_dd_cents'])} ({_pct(dd['max_dd_pct'], False)}). "
                   f"Now {_money(dd['current_dd_cents'])} below the high.")
    for seg in r["drift"]["segments"].values():
        if seg["alarm"]:  # whether it is paused is a separate "Paused ..." line (services.report)
            out.append(f"Warning: {seg['label']} have been getting worse prices than the closing price lately "
                       f"(the last {seg['n']} averaged {abs(seg['mean']) * 100:.1f}% worse).")
    if r["drift"]["calibration"]["worse"]:
        out.append(CALIBRATION_WARNING)
    return out
