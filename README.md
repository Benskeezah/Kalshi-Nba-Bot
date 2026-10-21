# kalshi-edge

A **read-only** research tool for Kalshi. For each market it builds an
independent probability estimate (without looking at the price), then compares
it with the market, costs out fees/spread/slippage, runs contrarian checks, and
gives a verdict. Every prediction is logged so calibration can be measured once
markets settle. There is no code that places, cancels or modifies orders.

## Daily automatic report

`python -m kalshi_edge daily` runs the whole props analysis for today and writes
`site/index.html` + `site/picks.json`, logs every prediction, and can push a phone
alert. `.github/workflows/daily.yml` runs it twice a day on GitHub for free;
**SETUP.md** walks through turning that on.

## Quick start

Python 3.10+, standard library only (pytest optional for tests).

```bash
cp .env.example .env              # optional; set NWS_USER_AGENT to your email
python examples/demo.py           # offline demo: weather markets
python examples/nba_demo.py       # offline demo: NBA winner / margin / total / parlays
python examples/props_demo.py     # offline demo: NBA player props (points / rebounds / assists)

# live (needs internet on your machine)
python -m kalshi_edge scan --event KXHIGHNY-26OCT22 --html report.html
python -m kalshi_edge scan --series KXNBAGAME --html nba.html     # every open NBA game
python -m kalshi_edge scan --event KXNBATOTAL-26OCT23HOUSAS
python -m kalshi_edge scan --event KXNBAPTS-26OCT21DALHOU --html props.html   # one game's points props
python -m kalshi_edge scan --series KXNBAAST                                   # every open assists prop
python -m kalshi_edge analyze KXHIGHNY-26OCT22-B65.5 --evidence my_evidence.json
python -m kalshi_edge resolve        # fetch settled results for logged markets
python -m kalshi_edge calibration    # how well-calibrated has the model been?
```

## Architecture

```
ingestion/kalshi.py      1. Market data: GET-only client + fixture client -> MarketSnapshot
external/weather.py      2. External data: NWS gridpoint forecast, Open-Meteo ensembles
external/nba_odds.py        NBA lines: The Odds API (key), ESPN scoreboard (no key), evidence file
external/nba_props.py       Prop lines (The Odds API, evidence file) and ESPN game logs
external/evidence_file.py   Your own sourced research (base rates, lines, forecasts)
estimators/              3. Probability models. Receive a ContractSpec with NO prices.
   weather.py               daily-high buckets: Normal(forecast, lead-time error)
   nba.py                   NBA winner / margin / total from de-vigged sportsbook lines
   nba_props.py             NBA player props: negative-binomial model fitted to prop lines
   parlay.py                multi-leg combos: product of sourced leg probabilities
   evidence.py              anything else: base rate + independent estimates in log-odds
   registry.py              picks a model; unknown market types -> INSUFFICIENT DATA
consistency.py              event-level check: what distribution do the bucket prices imply?
edge.py                  4. Market comparison, contrarian checks, verdict rules
costs.py                 5. Kalshi taker fee, order-book slippage, EV per contract
store.py                 6. SQLite prediction log
calibration.py           7. Brier/log loss vs market, reliability table, bias, slope
report.py, cli.py        8. Text, sortable HTML table, command line
pipeline.py                 Wires the steps in the anti-anchoring order
```

**Anti-anchoring.** Estimators only receive `ContractSpec`, which has no price
fields (enforced by a test). Prices are read only after the estimate exists.
Later steps may lower confidence or add notes; they never move the probability.

**Confidence caps.** No estimator can rate HIGH until it has 30 resolved,
non-mock predictions in the log. Mock evidence always means LOW.

**Ranking** is by verdict tier, then after-cost EV at the *pessimistic* end of
the model's range, discounted for confidence, liquidity, depth and time to
close. The biggest raw edge is not automatically first.

## Data sources and credentials

| Source | Use | Key? |
|---|---|---|
| Kalshi public API (`KALSHI_API_BASE`) | prices, books, rules, results | No (read-only market data) |
| api.weather.gov | NWS forecast highs | No; set `NWS_USER_AGENT` to contact info |
| ensemble-api.open-meteo.com | GFS/ECMWF ensemble spread | No |
| ESPN scoreboard API | NBA lines, one book (DraftKings) | No |
| The Odds API (`ODDS_API_KEY`) | NBA lines from many books incl. Pinnacle | Free key, ~500 req/month |
| evidence JSON (yours) | base rates, sportsbook lines, polls, etc. | — |

`KALSHI_API_KEY_ID` is read but unused; nothing here needs an account.

## NBA

Markets: `KXNBAGAME` (winner), `KXNBASPREAD` ("X wins by over N"), `KXNBATOTAL`
("over N points"), and parlays (`KXMVE...`) whose legs are those markets.

* Evidence is sportsbook lines with the vig removed two ways (proportional and
  power). Sharp books (Pinnacle etc.) get 3x weight. Margins and totals use a
  normal distribution around the line (SD 12.5 / 18.5 pts, uncalibrated defaults).
* HIGH confidence needs 3+ books including a sharp one and lines under 2 hours old.
  ESPN alone is one book, so expect MEDIUM at best without `ODDS_API_KEY`.
* Once the game tips off the model returns INSUFFICIENT DATA (Kalshi trades live).
* Parlay legs are read from Kalshi's own leg list (`mve_selected_legs`). Legs in
  different games multiply. Same-game winner/margin legs collapse to the strictest
  one (Spurs win + Spurs by 7.5 = Spurs by 7.5). Player props are not modelled yet,
  so any parlay with a prop leg is INSUFFICIENT DATA.
* What the first real run showed: Kalshi's NBA prices sat within ~1-3 points of the
  de-vigged DraftKings line on every market with a quote. Expect most NBA results to be
  MARKET APPEARS FAIR. Real gaps are most likely right after injury news, in thin
  markets with wide spreads, and in far-from-the-line margin/total tails.

## Daily NBA props workflow

```bash
python -m kalshi_edge build-stats                   # first time ~15 min; afterwards only new games
python -m kalshi_edge scan --event KXNBAPTS-26OCT23NYKBOS --mode high-prob --html picks.html
```

* `--mode high-prob` lists only sides with at least a 70% model win probability
  (`--min-prob` to change) that still beat their break-even rate (price + fee) at the
  pessimistic end of the model's range. LOW-confidence picks are hidden unless
  `--allow-low`.
* A high win rate is not the same as making money: at 90 cents one loss cancels
  about nine wins, so the mode refuses favourites that are overpriced.

## NBA player props

Markets: `KXNBAPTS`, `KXNBAREB`, `KXNBAAST` ladders such as "Kevin Durant: 25+ points".

* **Stats projection** (needs `build-stats`): expected stat = projected minutes x
  per-minute rate x opponent defence (per 100 possessions, half weight) x pace x home/away,
  with minutes cut for back-to-backs and likely blowouts. ESPN's injury report is
  checked: the player's own status and teammates who are out are flagged. Every
  factor is printed with the estimate.
* **Sportsbook lines** (needs `ODDS_API_KEY`) are blended in at `PROP_LINE_WEIGHT`
  (default 0.5; set 0 for a pure stats model). When the two disagree by more than
  15%, confidence drops, because books often know about minutes or injuries first.
* Each stat is modelled as a negative-binomial count. The book-implied expected value is solved
  from sportsbook prop lines (main and alternate lines, vig removed; one-sided prices
  allowed with an assumed 3-9% margin). Fresher quotes and sharp books weigh more.
* The spread of outcomes comes from the player's ESPN game log (this season plus last)
  when there are 15+ games, otherwise a default per stat (uncalibrated).
* Game logs alone also give a weaker estimate; without any sportsbook line the
  result is LOW confidence, and with neither it is INSUFFICIENT DATA.
* A ladder check fits the expected value Kalshi's own ladder implies, so you can see
  whether a gap is a real disagreement about the player or only about spread.
* Kalshi settles a player who doesn't play at a fair price, so the model is
  conditional on him playing.
* Prop parlays: the same player's ladder legs collapse to the strictest one (20+ and
  25+ = 25+). Different players or a player plus the team result in the same game are
  correlated; they are multiplied, flagged and kept at LOW confidence.
* First real run (Mavericks at Rockets, Oct 21): Fred VanVleet's assists ladder on
  Kalshi was within 1-3 points of the price implied by FanDuel's line, except a 2+ rung
  with a 26-cent spread. Most other players had no line available offline.

## Evidence file

See the docstring in `kalshi_edge/external/evidence_file.py`. Every input needs a
`source` (unsourced adjustments are ignored and reported). Set `"_mock": true`
for invented data; it is flagged everywhere and excluded from calibration.

## Known weaknesses (read these)

1. **Forecast-error widths are assumptions** (`SIGMA_BY_LEAD` in
   `estimators/weather.py`). On the Oct 22 NYC test the model used 2.6°F while the
   market implied ~1.4°F; every bucket "edge" came from that one disagreement. The
   consistency check now flags this and drops confidence. Fit the table from
   logged results before trusting weather edges.
2. Live NWS/Open-Meteo fetches are written but were not run end to end in the
   build environment (no network there); they are covered by parsing tests.
   Run one live scan and compare against forecast.weather.gov by hand first.
3. Settlement-station coordinates are approximations; the estimator refuses to
   run if the market rules don't name the expected climate report.
4. Fees assume taker orders and the 2026-07-07 schedule. Series multipliers vary;
   set `KALSHI_FEE_MULTIPLIER` per series if needed.
5. Only weather has a built-in data model. Sports, politics and economics need
   evidence files today; adding an estimator means one class in `estimators/`.
6. Parlay legs are multiplied as if independent; same-game legs are usually
   correlated, so parlay estimates stay LOW confidence.
7. NBA: no injury/lineup feed. The model only knows what the lines know, so a line
   older than the news is stale. Props: no minutes projection beyond what lines and
   logs imply; correlations between different players in one game aren't modelled.
8. Calibration needs volume: under ~50 resolved markets every metric is noise.

## Tests

```bash
python -m pytest -q
```
