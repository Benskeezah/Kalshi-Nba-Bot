"""Output: plain-text market reports, a ranking table, and an HTML report."""
from __future__ import annotations

import html
from datetime import datetime
from typing import Optional

from .models import Analysis, SideCost


def pct(x: Optional[float], d: int = 0) -> str:
    return "n/a" if x is None else f"{x*100:.{d}f}%"


def cents(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x*100:.0f}¢"


def money(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{'+' if x >= 0 else '-'}${abs(x):.3f}"


def _when(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "unknown"


def _side_line(s: Optional[SideCost]) -> str:
    if not s or s.avg_fill is None:
        return "no ask available"
    depth = {True: "fills", False: "NOT enough depth", None: "depth unknown"}[s.fillable]
    return (f"buy at {cents(s.top_of_book)} (avg fill {s.avg_fill*100:.1f}¢ for {s.contracts}, {depth}), "
            f"fee {s.fee_per_contract*100:.2f}¢/contract -> gross {money(s.ev_gross)}, "
            f"net {money(s.ev_net)}, pessimistic {money(s.ev_net_conservative)}")


def text_report(a: Analysis) -> str:
    m, e = a.market, a.estimate
    mock = any(x.is_mock for x in e.evidence)
    L = []
    if mock:
        L.append("!!! CONTAINS MOCK DEMO DATA - NOT A REAL ANALYSIS !!!")
    L += ["MARKET",
          f"  Contract:        {m.title} [{m.yes_sub_title}]  ({m.ticker})",
          f"  Resolution:      closes {_when(m.close_time)}",
          f"  Rules:           {m.rules_primary[:240] or 'not provided'}",
          f"  Current YES:     bid {cents(m.yes_bid)} / ask {cents(m.yes_ask)}",
          f"  Current NO:      bid {cents(m.no_bid)} / ask {cents(m.no_ask)}",
          f"  Spread:          {cents(m.spread)}",
          f"  Volume/liq:      volume {m.volume:,.0f}, 24h {m.volume_24h:,.0f}, open interest "
          f"{m.open_interest:,.0f}  -> {a.liquidity_label}",
          f"  Prices as of:    {_when(m.fetched_at)} ({m.source})",
          "", "PROBABILITY"]
    if e.insufficient:
        L.append(f"  Model YES probability: INSUFFICIENT DATA - {e.insufficient_reason}")
        L.append(f"  Market-implied:        {pct(a.market_prob)}")
    else:
        L += [f"  Model YES probability: {pct(e.p_yes)}   (NO: {pct(1 - e.p_yes)})",
              f"  Probability range:     {pct(e.p_low)}-{pct(e.p_high)}",
              f"  Market-implied (mid):  {pct(a.market_prob, 1)}",
              f"  Estimated edge:        {'n/a' if a.edge_pp is None else f'{a.edge_pp:+.1f} pp'} (on YES)",
              f"  Confidence:            {e.confidence}  (quality of evidence, not a probability)"]
        if e.time_sensitive:
            L.append("  Probability is highly time-sensitive.")
        L += ["", "EXPECTED VALUE (per $1 contract, taker orders)",
              f"  YES: {_side_line(a.yes_cost)}",
              f"  NO:  {_side_line(a.no_cost)}"]
    L += ["", "EVIDENCE USED"]
    for x in e.evidence or []:
        L.append(f"  - {x.source}: {x.description} (issued {_when(x.observed_at)})"
                 + (" [MOCK]" if x.is_mock else ""))
    if not e.evidence:
        L.append("  - none")
    if not e.insufficient:
        L += ["", "ANALYSIS", "  Supporting factors:"] + [f"   - {s}" for s in e.supporting]
        L += ["  Opposing factors:"] + [f"   - {s}" for s in e.opposing or ["none identified"]]
        L += ["  Why the market could be right:"] + [f"   - {s}" for s in a.contrarian or ["no specific flags"]]
        L += ["  Key assumptions:"] + [f"   - {s}" for s in e.assumptions]
        L += ["  What would change the estimate:"] + [f"   - {s}" for s in e.would_change]
    L += ["", "VERDICT", f"  {a.verdict}", f"  {a.verdict_reason}",
          "  (Analysis only. Position size is a separate decision; risk a small fraction of bankroll.)"]
    return "\n".join(L)


def ranking_rows(results: list[Analysis]) -> list[dict]:
    rows = []
    for i, a in enumerate(results, 1):
        e = a.estimate
        rows.append({"rank": i, "market": f"{a.market.yes_sub_title or a.market.title}",
                     "ticker": a.market.ticker, "kalshi": a.market_prob,
                     "model": e.p_yes, "range": (e.p_low, e.p_high),
                     "edge": a.edge_pp, "confidence": e.confidence,
                     "liquidity": a.liquidity_label, "verdict": a.verdict, "score": a.rank_score})
    return rows


def text_table(results: list[Analysis]) -> str:
    hdr = f"{'Rank':<4} {'Market':<34} {'Kalshi%':>7} {'Model%':>7} {'Edge':>7} {'Conf':<6} {'Liq':<6} Verdict"
    L = [hdr, "-" * len(hdr)]
    for r in ranking_rows(results):
        edge = "n/a" if r["edge"] is None else f"{r['edge']:+.1f}"
        L.append(f"{r['rank']:<4} {(r['market'] + ' ' + r['ticker'])[:34]:<34} {pct(r['kalshi'],1):>7} "
                 f"{pct(r['model']):>7} {edge:>7} {r['confidence']:<6} {r['liquidity']:<6} {r['verdict']}")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
CSS = """
/* Layout: one column; summary table first, then a detail sheet per market. */
:root{
  --paper:#f3f5f4; --sheet:#ffffff; --ink:#1b2422; --muted:#5d6b67; --rule:#d5dcd9;
  --accent:#0f6b5c; --good:#1d7a3e; --warn:#a15c00; --bad:#a3302a; --flag:#fff3d6;
  --sans:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,"SFMono-Regular",Menlo,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --paper:#121816; --sheet:#1a2220; --ink:#e3ebe8; --muted:#97a6a1; --rule:#2c3835;
  --accent:#4cc2ac; --good:#5fc37f; --warn:#e3a14a; --bad:#ef7d74; --flag:#3a2f14; color-scheme:dark}}
:root[data-theme="dark"]{
  --paper:#121816; --sheet:#1a2220; --ink:#e3ebe8; --muted:#97a6a1; --rule:#2c3835;
  --accent:#4cc2ac; --good:#5fc37f; --warn:#e3a14a; --bad:#ef7d74; --flag:#3a2f14; color-scheme:dark}
body{background:var(--paper);color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.5}
.wrap{max-width:980px;margin:0 auto;padding-block:28px 56px;
  padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));
  display:grid;gap:28px}
h1{font-size:1.6rem;margin:0;text-wrap:balance;letter-spacing:-.01em}
h2{font-size:1.1rem;margin:0;text-wrap:balance}
h3{font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 6px}
.meta{color:var(--muted);font-size:.85rem;margin:6px 0 0}
.mock{background:var(--flag);border:1px solid var(--warn);padding:10px 12px;border-radius:6px;font-weight:600}
.note{color:var(--muted);font-size:.85rem;max-width:68ch}
.tbl{overflow-x:auto;background:var(--sheet);border:1px solid var(--rule);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:.88rem}
th,td{padding:9px 10px;border-bottom:1px solid var(--rule);text-align:left;white-space:nowrap}
th{font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);cursor:pointer;user-select:none}
th button{all:unset;cursor:pointer}
th button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
td.num{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right}
tr:last-child td{border-bottom:0}
.tick{font-family:var(--mono);font-size:.75rem;color:var(--muted)}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.72rem;font-weight:600;
  border:1px solid currentColor;letter-spacing:.02em}
.v-strong,.v-potential{color:var(--good)} .v-small{color:var(--warn)}
.v-fair,.v-noedge{color:var(--muted)} .v-insufficient{color:var(--bad)}
.sheet{background:var(--sheet);border:1px solid var(--rule);border-radius:8px;padding:18px;display:grid;gap:16px;min-width:0}
.sheet header{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:baseline;justify-content:space-between}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px 18px}
.kv div{min-width:0} .kv b{display:block;font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:1rem}
.kv span{font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.bar{position:relative;height:30px;border-radius:4px;background:var(--paper);border:1px solid var(--rule)}
.bar .rng{position:absolute;top:6px;bottom:6px;background:color-mix(in srgb,var(--accent) 25%,transparent);border-radius:3px}
.bar .mod{position:absolute;top:2px;bottom:2px;width:3px;background:var(--accent)}
.bar .mkt{position:absolute;top:0;bottom:0;width:0;border-left:2px dashed var(--ink)}
.legend{font-size:.75rem;color:var(--muted);display:flex;gap:14px;flex-wrap:wrap}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
ul{margin:0;padding-left:18px} li{margin:2px 0}
.verdict{border-top:1px solid var(--rule);padding-top:12px}
.mono{font-family:var(--mono);font-size:.82rem;overflow-wrap:anywhere}
details summary{cursor:pointer;color:var(--accent);font-size:.85rem}
"""

JS = """
document.querySelectorAll('th button').forEach((b,i)=>b.addEventListener('click',()=>{
  const tb=b.closest('table').tBodies[0], rows=[...tb.rows];
  const dir=b.dataset.dir==='asc'?'desc':'asc'; b.dataset.dir=dir;
  rows.sort((x,y)=>{const a=x.cells[i].dataset.v, c=y.cells[i].dataset.v;
    const na=parseFloat(a), nc=parseFloat(c);
    const r=(!isNaN(na)&&!isNaN(nc))?na-nc:String(a).localeCompare(String(c));
    return dir==='asc'?r:-r;});
  rows.forEach(r=>tb.appendChild(r));}));
"""

VCLASS = {"STRONG POTENTIAL EDGE": "v-strong", "POTENTIAL EDGE": "v-potential", "SMALL EDGE": "v-small",
          "MARKET APPEARS FAIR": "v-fair", "NO CLEAR EDGE": "v-noedge", "INSUFFICIENT DATA": "v-insufficient"}
CONF_N = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "NONE": 0}


def _e(s) -> str:
    return html.escape(str(s))


def _ul(items):
    return "<ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>" if items else "<p class='note'>None identified.</p>"


def _bar(a: Analysis) -> str:
    e = a.estimate
    if e.insufficient:
        return ""
    parts = [f"<div class='rng' style='left:{e.p_low*100:.1f}%;width:{(e.p_high-e.p_low)*100:.1f}%'></div>",
             f"<div class='mod' style='left:calc({e.p_yes*100:.1f}% - 1px)'></div>"]
    if a.market_prob is not None:
        parts.append(f"<div class='mkt' style='left:{a.market_prob*100:.1f}%'></div>")
    return (f"<div><div class='bar' role='img' aria-label='Model {pct(e.p_yes)} (range {pct(e.p_low)} to "
            f"{pct(e.p_high)}) versus market {pct(a.market_prob)} on a 0 to 100% scale'>{''.join(parts)}</div>"
            "<div class='legend'><span>0%</span><span>shaded: model range</span><span>solid: model</span>"
            "<span>dashed: market</span><span>100%</span></div></div>")


def _sheet(a: Analysis) -> str:
    m, e = a.market, a.estimate
    vc = VCLASS[a.verdict]
    ev = "".join(f"<li>{_e(x.source)}: {_e(x.description)} <span class='tick'>issued {_e(_when(x.observed_at))}"
                 f"{' · MOCK' if x.is_mock else ''}</span></li>" for x in e.evidence) or "<li>None</li>"
    if e.insufficient:
        prob = f"<p><b>INSUFFICIENT DATA.</b> {_e(e.insufficient_reason)}</p>"
        analysis = ""
    else:
        prob = f"""<div class="kv">
          <div><span>Model YES</span><b>{pct(e.p_yes)}</b></div>
          <div><span>Range</span><b>{pct(e.p_low)}–{pct(e.p_high)}</b></div>
          <div><span>Market (mid)</span><b>{pct(a.market_prob,1)}</b></div>
          <div><span>Edge on YES</span><b>{'n/a' if a.edge_pp is None else f'{a.edge_pp:+.1f} pp'}</b></div>
          <div><span>Confidence</span><b>{e.confidence}</b></div></div>{_bar(a)}
          {"<p class='note'><b>Probability is highly time-sensitive.</b></p>" if e.time_sensitive else ""}
          <div><h3>Expected value per $1 contract (taker)</h3>
          <p class="mono">YES: {_e(_side_line(a.yes_cost))}<br>NO: {_e(_side_line(a.no_cost))}</p></div>"""
        analysis = f"""<div class="cols">
          <div><h3>Supporting factors</h3>{_ul(e.supporting)}</div>
          <div><h3>Opposing factors</h3>{_ul(e.opposing)}</div>
          <div><h3>Why the market could be right</h3>{_ul(a.contrarian)}</div>
          <div><h3>Key assumptions</h3>{_ul(e.assumptions)}</div>
          <div><h3>What would change the estimate</h3>{_ul(e.would_change)}</div></div>"""
    return f"""<section class="sheet" id="{_e(m.ticker)}">
      <header><div><h2>{_e(m.title)} — {_e(m.yes_sub_title)}</h2>
        <div class="tick">{_e(m.ticker)} · closes {_e(_when(m.close_time))}</div></div>
        <span class="pill {vc}">{_e(a.verdict)}</span></header>
      <div class="kv">
        <div><span>YES bid / ask</span><b>{cents(m.yes_bid)} / {cents(m.yes_ask)}</b></div>
        <div><span>NO bid / ask</span><b>{cents(m.no_bid)} / {cents(m.no_ask)}</b></div>
        <div><span>Spread</span><b>{cents(m.spread)}</b></div>
        <div><span>Vol 24h · OI</span><b>{m.volume_24h:,.0f} · {m.open_interest:,.0f}</b></div>
        <div><span>Liquidity</span><b>{a.liquidity_label}</b></div></div>
      {prob}
      <div><h3>Evidence used</h3><ul>{ev}</ul></div>
      {analysis}
      <details><summary>Settlement rules</summary><p class="note">{_e(m.rules_primary or 'Not provided.')}</p></details>
      <div class="verdict"><h3>Verdict</h3><p><b>{_e(a.verdict)}.</b> {_e(a.verdict_reason)}</p></div>
    </section>"""


def _picks_html(picks, min_prob) -> str:
    if picks is None:
        return ""
    if not picks:
        return (f"<div><h2>High-probability picks</h2><p class='note'>None today. No side had a "
                f"{min_prob:.0%}+ win probability that also beat its break-even price at the pessimistic end "
                "of the model's range.</p></div>")
    rows = "".join(
        f"<tr><td class='num' data-v='{i}'>{i}</td><td data-v='{_e(p.label)}'>"
        f"<a href='#{_e(p.analysis.market.ticker)}'>{_e(p.label)}</a></td>"
        f"<td class='num' data-v='{p.win_p}'>{p.win_p:.0%}</td>"
        f"<td class='num' data-v='{p.win_low}'>{p.win_low:.0%}–{p.win_high:.0%}</td>"
        f"<td class='num' data-v='{p.price}'>{p.price*100:.0f}¢</td>"
        f"<td class='num' data-v='{p.breakeven}'>{p.breakeven:.1%}</td>"
        f"<td class='num' data-v='{p.margin}'>{p.margin*100:+.1f}</td>"
        f"<td data-v='{_e(p.risk_to_win)}'>{_e(p.risk_to_win)}</td>"
        f"<td data-v='{CONF_N[p.analysis.estimate.confidence]}'>{p.analysis.estimate.confidence}</td></tr>"
        for i, p in enumerate(picks, 1))
    heads = ["#", "Pick", "Win %", "Range", "Price", "Break-even", "Margin (pp)", "Payout", "Confidence"]
    worst = max(picks, key=lambda p: p.losses_wiped)
    head_html = "".join('<th><button type="button">' + h + "</button></th>" for h in heads)
    return (f"<div style='display:grid;gap:10px'><h2>High-probability picks</h2>"
            "<p class='note'>Most likely to win <b>and</b> not overpriced: the win probability must beat the "
            "break-even rate (price + fee) even at the pessimistic end of the model's range.</p>"
            f"<div class='tbl'><table><thead><tr>{head_html}"
            f"</tr></thead><tbody>{rows}</tbody></table></div>"
            f"<p class='note'>At {worst.price*100:.0f}¢ one loss cancels about {worst.losses_wiped:.0f} wins. "
            "Keep each position small, and treat picks from the same game as one bet.</p></div>")


def html_report(results: list[Analysis], title: str = "Kalshi Edge Scan", note: str = "",
                picks=None, min_prob: float = 0.70, detail_tickers: Optional[set] = None,
                footer: str = "", value_section: str = "") -> str:
    any_mock = any(x.is_mock for a in results for x in a.estimate.evidence)
    rows = []
    for r, a in zip(ranking_rows(results), results):
        vc = VCLASS[r["verdict"]]
        edge_txt = "n/a" if r["edge"] is None else f"{r['edge']:+.1f}"
        rows.append(
            f"<tr><td class='num' data-v='{r['rank']}'>{r['rank']}</td>"
            f"<td data-v='{_e(r['market'])}'><a href='#{_e(r['ticker'])}'>{_e(r['market'])}</a>"
            f"<div class='tick'>{_e(r['ticker'])}</div></td>"
            f"<td class='num' data-v='{r['kalshi'] if r['kalshi'] is not None else -1}'>{pct(r['kalshi'],1)}</td>"
            f"<td class='num' data-v='{r['model'] if r['model'] is not None else -1}'>{pct(r['model'])}</td>"
            f"<td class='num' data-v='{r['edge'] if r['edge'] is not None else -999}'>"
            f"{edge_txt}</td>"
            f"<td data-v='{CONF_N[r['confidence']]}'>{r['confidence']}</td>"
            f"<td data-v='{CONF_N.get(r['liquidity'],0)}'>{r['liquidity']}</td>"
            f"<td data-v='{_e(r['verdict'])}'><span class='pill {vc}'>{_e(r['verdict'])}</span></td></tr>")
    heads = ["Rank", "Market", "Kalshi %", "Model %", "Edge (pp)", "Confidence", "Liquidity", "Verdict"]
    generated = max((a.market.fetched_at for a in results), default=None)
    head_html = "".join('<th><button type="button">' + h + "</button></th>" for h in heads)
    return f"""<title>{_e(title)}</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;600&display=swap">
<style>{CSS}</style>
<div class="wrap">
  <div><h1>{_e(title)}</h1>
    <p class="meta">Market prices as of {_e(_when(generated))} · read-only analysis · {len(results)} markets</p></div>
  {"<div class='mock'>Contains mock demo data. Probabilities built on it are not real analysis.</div>" if any_mock else ""}
  {f"<p class='note'>{_e(note)}</p>" if note else ""}
  <p class="note">Ranked by after-cost expected value at the pessimistic end of the model's range, discounted for
  confidence, liquidity and time to close, so the biggest raw edge is not automatically first. Tap a column to sort.</p>
  {_picks_html(picks, min_prob)}
  {value_section}
  <h2>All markets</h2>
  <div class="tbl"><table><thead><tr>{head_html}</tr></thead>
  <tbody>{''.join(rows)}</tbody></table></div>
  {''.join(_sheet(a) for a in results if detail_tickers is None or a.market.ticker in detail_tickers)}
  {footer}
  <p class="note">This tool estimates probabilities; it does not place trades. A positive estimate is not a reason to
  bet: size any position as a small fraction of bankroll and expect the model to be wrong regularly.</p>
</div>
<script>{JS}</script>"""
