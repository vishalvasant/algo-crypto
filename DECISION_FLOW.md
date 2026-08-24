# Algo-Crypto — End-to-End Trade Decision Flow

This document explains **how** the system decides to enter, hold, and exit BTC/ETH options on Delta Exchange India, and **on what basis** each step is taken.

**Mode today:** paper trading by default (`TRADING_MODE=paper`). Market data comes from Delta; fills are simulated. Live mode uses the same decision path but places real Delta orders.

**Core idea:** Rule-based (no LLM). Scan every ~30s → pick **one** strategy with confidence ≥ 75 → validate liquidity/risk → buy option premium → manage exit by momentum (VWAP bias, premium drawdown, trail, time) — not fixed $ stop/target.

**Gap-Fix upgrades (active):**
- **Hard circuit breakers** — daily loss $40 / 20%, 4 consecutive losses, 20 trades/day, 8 losing/hour, max 2 positions, flip caps (see `risk_config.yaml`). States: `NORMAL` / `WARNING` / `HALTED` / `EMERGENCY_FLATTEN`.
- **VWAP dead zone** — ATR-relative NEUTRAL bias near VWAP (`strategy_config.vwap_bias`).
- **Confirmed flips only** — no forced `confidence=80`; ATR distance + candle close + momentum; 300s flip cooldown; QualityGate must still pass.
- **Executable pricing** — BUY/SELL from L2 book walk (not LTP alone); slippage + spread gates; liquidity lot cap; paper fills walk the book with partials/latency.
- **Risk-based sizing** — lots = min(confidence, max-loss/adverse-move, capital, liquidity, max); binding reason logged.
- **Portfolio exposure** — per-underlying / directional / BTC↔ETH correlated same-side caps.
- **IV / TTE / strikes** — IV regime, RV, expected move, expiry buckets; vol-aware strike scoring; near-expiry size/confidence adjustments.
- **EV gate** — historical learner P(win) with prior (never `rule_score`→probability); fees + slippage in EV; `negative_expected_value` blocks.
- **Thesis monitor** — entry thesis tracked; `thesis_degradation` exit when score collapses.
- **Strategy families / health** — correlated VWAP/Trend/… collapse in router; expectancy / PF / by-regime stats; weak health soft-demotes.
- **Walk-forward** — `scripts/walk_forward.py` + `algocrypto.backtest.walk_forward` over day snaps.

---

## 1. What the system trades

| Item | Value |
|------|--------|
| Underlyings | **BTC** and **ETH** options only |
| Lead index | BTC (`BTCUSD` perpetual marks for VWAP / features) |
| Expiry preference | Nearest daily **D1**, fallback **D2** |
| Contract style | Long options only (BUY CE or BUY PE) |
| Lot meaning | BTC: 1 lot = 0.001 BTC · ETH: 1 lot = 0.01 ETH |
| Strike selection | ATM band (±1 step by default) using delta/gamma + spread |
| Session | 24×7 (`market_session_config`) |

Config: `config/symbols_config.yaml`, `config/broker_config.yaml`.

---

## 2. Runtime architecture (who does what)

```
┌─────────────────────────────────────────────────────────────────┐
│                     trading-engine (one process)                  │
│                                                                   │
│  Delta WS/REST ──► MarketDataEngine + OptionDataLayer             │
│         │                                                         │
│         ▼                                                         │
│  TradingOrchestrator                                              │
│    ├─ FeatureEngine      (spot, VWAP, setups, chain, Greeks)      │
│    ├─ RegimeClassifier   (trending / sideways / high-vol …)       │
│    ├─ StrategyRouter     (21 scanners → one winner or NO_TRADE)   │
│    ├─ QualityGate        (0–100 confidence score)                 │
│    ├─ RuleValidator      (spread, cooldown, fields)               │
│    ├─ RiskEngine         (lots, capital, kill switch)             │
│    ├─ ExecutionEngine    (BUY / SELL via Paper or Delta)          │
│    └─ PositionManager    (hold, exit rules, flip queue)           │
│                                                                   │
│  JournalWriter ──► Postgres (signals, validations, trades, logs)  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    web-app UI (dashboard, decision logs, P&L)
```

| Component | File |
|-----------|------|
| Process entry | `trading-engine/src/algocrypto/main.py` |
| Orchestrator | `trading-engine/src/algocrypto/trading/orchestrator.py` |
| Router | `trading-engine/src/algocrypto/strategy/router.py` |
| Confidence | `trading-engine/src/algocrypto/quality/gate.py` |
| Exits | `trading-engine/src/algocrypto/position/exit_rules.py` |

---

## 3. Clocks and feeds (when decisions run)

| Loop | Interval | Purpose |
|------|----------|---------|
| Periodic entry scan | **30s** (`runtime_config.scan_interval_seconds`) | Full feature → router → maybe enter |
| Quote → exit check | Every option tick (WS) or ~**2s** REST | Update MFE/MAE; run exit rules |
| Candle refresh | On each scan | Rebuild 1m / 3m / 5m bars + session VWAP |
| Universe / WS subscribe | On market open + roll | Keep ATM band + holdings on feed |

**Stale guard:** If candles are stale → **NO_TRADE** (`stale_candle_feed`). No entry on outdated VWAP/setups.

---

## 4. ENTRY — full pipeline

### 4.1 Overview

```
refresh candles
    → features.compute()
    → regime.classify()
    → retarget ATM from live spot
    → chain + option Greeks context
    → features.compute() again
    → StrategyRouter.route()     ← pick ONE strategy or NO_TRADE
    → (if signal) _execute_signal:
          kill/block check
          → journal candidate
          → RuleValidator
          → L2 orderbook gate
          → RiskEngine.size_entry
          → reserve capital
          → ExecutionEngine.enter (BUY)
          → PositionManager.register_open
```

### 4.2 Step A — Market features (what “setup” is based on)

`FeatureEngine.compute()` builds a `FeatureSnapshot` from:

| Input | Basis |
|-------|--------|
| Spot LTP | Index / perpetual mark (`BTCUSD` lead) |
| Session VWAP | Cumulative session volume-weighted spot |
| Bias (`bias_5m`) | Last 1m close **above** VWAP → BULLISH → prefer **CE**; below → BEARISH → prefer **PE**; near → NEUTRAL |
| 3m / 1m setups | Strategy-specific patterns (reclaim, pullback, bounce, rejection, …) |
| Structure | 5m HHHL / LLLH / mixed |
| Chain intel | OI, PCR, max pain, OI delta vs prior scan |
| Option context | ATM LTP, spread, option VWAP, Black-Scholes delta/gamma/IV/theta/vega |

**Basis for side:** Bullish bias → call strategies; bearish → put strategies. Many scanners refuse to fire on **NEUTRAL** bias.

### 4.3 Step B — Regime (may block all entries)

`RegimeClassifier` scores regimes using ATR, VWAP bias, 5m structure, range, session phase:

| Regime signals | Examples |
|----------------|----------|
| trending_up / trending_down | Spot vs VWAP + structure |
| sideways | Tight range + mixed structure |
| high_volatility / low_volatility | ATR vs its average |
| breakout / opening_range / expiry | Heuristics |

**Hard block (current config):**

- `regime.block_sideways: true` → if primary is sideways → `trade_allowed = false` → **NO_TRADE**
- `max_risk_score_to_trade: 80` → high risk score can block

Config: `config/strategy_config.yaml` → `regime.*`.

### 4.4 Step C — Strategy scanners (who can propose a trade)

Router runs **every enabled scanner**. Each either returns a `CandidateSignal` (side + contract + setup name) or `None`.

**Enabled strategies** (`strategy_config.yaml`):

| Family | Strategies |
|--------|------------|
| VWAP | `vwap_reclaim`, `vwap_bounce`, `vwap_pullback`, `vwap_rejection`, `vwap_trend` |
| Trend / momentum | `ema_pullback`, `momentum_continuation`, `trend_continuation`, `trend_day`, `delta_momentum` |
| Levels | `cpr_breakout`, `pdh_pdl_break`, `opening_range_breakout`, `gap_and_go` |
| Options microstructure | `oi_breakout`, `gamma_expansion`, `iv_expansion` |
| Other | `reversal`, `mean_reversion`, `liquidity_sweep`, `expiry_scalping` |

**Each scanner’s basis (examples):**

| Strategy | Typical trigger basis |
|----------|----------------------|
| `vwap_reclaim` | 3m reclaim setup + 1m cross trigger aligned with bias |
| `vwap_pullback` | Pullback toward VWAP then continuation trigger |
| `vwap_rejection` | Rejection away from VWAP |
| `ema_pullback` | Price pulls to EMA then resumes trend |
| `oi_breakout` | Open-interest / chain breakout cues |
| `cpr_breakout` | Prior-day CPR level break |
| `gap_and_go` | Gap + continuation |
| `momentum_continuation` | Momentum persistence on setup timeframe |
| `trend_reversal_flip` | **Not a scanner** — forced after `trend_reversal` exit (see §6) |

If a scanner produces nothing, Decision Logs show a diagnose reason (`neutral_bias`, `no_reclaim_setup`, `option_ltp_missing`, …).

### 4.5 Step D — Strike / contract selection

For a chosen side (CE/PE):

1. Retarget ATM from live spot.
2. Consider ATM ± `atm_band_steps` (strategy default **1**).
3. Prefer strikes near **target delta ~0.50** (band 0.40–0.65), higher gamma, spread ≤ `max_spread_pct` (strike picker).

Result: concrete Delta product (`C-BTC-77400-240826`, etc.) + LTP + metadata.

### 4.6 Step E — Quality / confidence score (0–100)

`QualityGate.score()` — institutional weighted sum. **Trade only if ≥ 75** (`router.min_confidence`).

| Component | Max pts | Basis |
|-----------|---------|--------|
| Spot vs VWAP | 20 | CE with spot > VWAP (or PE with spot < VWAP) |
| Option vs option VWAP | 15 | Premium above its own VWAP preferred |
| Market regime | 15 | Side aligned with trending_up/down / breakout / low_vol |
| Volume | 10 | Option volume context |
| OI | 10 | Open interest context |
| EMA | 10 | Trend alignment |
| Delta | 8 | Prefer ~ATM deltas |
| Gamma | 5 | Prefer higher gamma in band |
| Theta | 2 | Soft penalty/bonus |
| Vega / IV | 3 | IV context |
| Spread | 2 | Tighter spread better |

**Router rule:** Among scanners that fire, collapse **same strategy family** (e.g. VWAP reclaim/bounce/pullback) to the best member, then pick the highest confidence across families. If best &lt; 75 → **NO_TRADE**. Same-family rivals are logged as correlated, not independent evidence.

Every scan is written to `system_events` (`strategy_decision`) → UI **Decision Logs**.

### 4.7 Step F — Pre-trade hard gates (`_execute_signal`)

Before sizing/buying, **all** of these must pass:

| Gate | Basis | Config |
|------|--------|--------|
| Duplicate token | No second open on same instrument | runtime |
| Kill switch / entries blocked | Manual or auto pause | DB `daily_risk_state` |
| Validator — clock | Entry window 00:00–23:59 IST | `validator_config` |
| Validator — spread | `spread_pct` ≤ **12%** | `max_spread_pct` |
| Validator — fields | Need LTP (paper relaxes bid/ask/OI) | `paper_relaxed_liquidity` |
| Validator — cooldown | **5 minutes** after exit on that token (**skipped for flips**) | `cooldown_after_exit_minutes` |
| Orderbook L2 | Ask size ≥ 50 lots, depth covers order, ask/bid size ratio ≤ 8 | `fees_config.orderbook` |
| Risk caps | Daily loss / consec losses / max trades / concurrent — **currently 0 = OFF** | `risk_config` |

Failures are journaled (validation_results / notifications) and **no order** is sent.

### 4.7b Step F2 — Expected value + TTE (Gap-Fix Phase 5–6)

Before sizing capital is reserved:

1. **TTE** — near-expiry buckets raise `min_confidence` and shrink size (`vol_model.tte_adjustments`).
2. **EV** — `estimate_ev()` uses learner win-rate (with prior) × expected win/loss − fees − slippage. Does **not** treat rule_score as P(win). Fail → `negative_expected_value`.
3. **Thesis** recorded at fill for later monitoring.

### 4.8 Step G — Position sizing (how many lots)

`RiskEngine.size_entry()`:

1. Lots = **min**(confidence ladder, max-loss / adverse-move budget, capital room, liquidity depth, `max_lots`). Binding reason logged in `size_breakdown`.
2. Portfolio gates — per-underlying / directional / BTC↔ETH correlated same-side.
3. Cap deploy ≈ **40–50%** equity / available (`risk_config`); hard `max_lots` **60**.
4. Reserve capital → place order → register open (with thesis) → reconcile margin.

### 4.9 Step H — Fill (paper vs live)

| Mode | Broker | Fill basis |
|------|--------|------------|
| **paper** | `PaperBrokerAdapter` over Delta | Prefer L2 **ask** (buy) / **bid** (sell); else LTP cache |
| **live** | `DeltaAdapter` | Real Delta order (`DELTA_API_KEY` / `SECRET`) |

Order type from `execution_config.yaml`. Fees estimated via `fees_config.yaml` (taker rate + GST + premium cap) and stored on close.

---

## 5. HOLD — what happens while a position is open

On every matching option quote:

1. Update **MFE** (max favorable premium points vs entry) and **MAE** (max adverse).
2. Call `evaluate_momentum_exit(...)` with:
   - option side (CE/PE)
   - entry price / time
   - current LTP
   - MFE
   - live **spot vs session VWAP**
   - current regime (`high_volatility` tightens adverse/trail)
3. If no exit → keep holding.
4. Holding token stays on WebSocket for tick-level trails.

There is **no** classic fixed $ take-profit or stop-loss. Exits are structural / percentage-of-premium / time.

---

## 6. EXIT — rules and priority

Config: `config/position_exit_config.yaml`  
Code: `position/exit_rules.py` → `evaluate_momentum_exit`

**First matching rule wins** (after force-exit check):

| Priority | Reason code | When it fires | Current settings |
|----------|-------------|----------------|------------------|
| 0 | `force_exit` | Daily flatten time | **Disabled** (`force_exit_time: null`) |
| — | *(hold)* | Age &lt; min hold | **45 seconds** — no exit yet |
| 1 | `time_stop` | Held too long | **25 minutes** |
| 2 | `trend_reversal` | Bias flipped vs VWAP (+ ATR buffer) | CE: spot &lt; VWAP − buffer · PE: opposite |
| 3 | `adverse_momentum` | Premium crushed from entry | ~**12%** (ATR soft-scaled when dynamic exits on) |
| 4 | `momentum_trail` | Gave back run after profit | Need MFE ≥ **+18%** of entry, then give back **35%** of that run |
| 5 | `thesis_degradation` | Entry thesis score &lt; 40 | After **60s** hold; VWAP/regime/structure/IV collapse |
| — | HOLD | None of the above | Stay open |

### On exit

1. SELL (paper/live) at current LTP/book.
2. Compute gross PnL, entry/exit fees, net PnL.
3. Write `positions` closed + `closed_trades` (+ fee columns).
4. Release capital; `reconcile_margin()` so UI “Used Margin” matches reality.
5. Strategy learner records setup + PnL + exit reason (`reports/strategy_learner.json`).
6. Start **5-minute cooldown** on that token (unless a flip is about to fire).

### Flip-on-reversal (confirmed, not forced)

If exit reason is **`trend_reversal`** and `flip_on_trend_reversal: true`:

```
close CE  →  queue flip candidate to PE (not auto-fill)
close PE  →  queue flip candidate to CE
```

Next quote under the scan lock (`_try_reversal_flip`):

1. Flip cooldown (`flip_cooldown_seconds`, default **300**)
2. Circuit breakers (`max_flips_per_day` / hour, `flips_disabled`)
3. **Reversal confirmation** — ATR distance, completed 1m close beyond VWAP, momentum
4. Resolve ATM contract → **QualityGate** (no fake `confidence=80`)
5. If `rule_score` ≥ `flip_min_rule_score` (75) → same `_execute_signal` path
6. Failed confirmation journaled as `reversal_confirmation` / `NO_TRADE`

---

## 7. What is logged / visible in the UI

| Event | Where you see it |
|-------|------------------|
| Every scan (including NO_TRADE) | Decision Logs (`strategy_decision`) |
| Candidate signal | DB `candidate_signals` |
| Validator pass/fail | `validation_results` |
| Orders / fills | `orders` |
| Open / closed positions | Holdings + Trades |
| Closed trade PnL + fees | Trades blotter |
| Notifications | Notifications page |
| Kill switch / auto-trade off | Risk state + notifications |

---

## 8. End-to-end diagrams

### 8.1 Entry decision tree

```
Scan tick (≤ every 30s)
│
├─ Candles stale? ──────────────────────────────► NO_TRADE (stale_candle_feed)
│
├─ Features + Regime
│     └─ regime blocks (e.g. sideways)? ────────► NO_TRADE
│
├─ For each enabled strategy scanner:
│     └─ pattern match? → CandidateSignal + QualityGate score
│
├─ Best confidence < 75? ───────────────────────► NO_TRADE
│
├─ Kill switch / entries blocked? ──────────────► skip
├─ Validator fail (spread/cooldown/fields)? ────► reject
├─ Orderbook thin / one-sided? ─────────────────► reject
├─ Risk sizing reject? ─────────────────────────► reject
│
└─ BUY → open position → journal
```

### 8.2 Hold / exit / flip

```
Option quote
│
├─ Update MFE / MAE
├─ evaluate_momentum_exit
│     ├─ < 20s hold ────────────────────────────► HOLD
│     ├─ > 25m ─────────────────────────────────► EXIT time_stop
│     ├─ CE/PE bias vs VWAP±8 ──────────────────► EXIT trend_reversal
│     │                                              └─ flip_on? → queue opposite BUY
│     ├─ premium ≤ −12% (−10% hi-vol) ──────────► EXIT adverse_momentum
│     ├─ trail giveback after +18% MFE ─────────► EXIT momentum_trail
│     └─ else ──────────────────────────────────► HOLD
│
└─ If flip queued → next lock: trend_reversal_flip entry (confidence 80)
```

---

## 9. Config cheat sheet (knobs that change behavior)

| Concern | File | Key settings |
|---------|------|--------------|
| Scan pace | `runtime_config.yaml` | `scan_interval_seconds: 30` |
| Which strategies | `strategy_config.yaml` | `router.enabled_strategies`, `min_confidence: 75` |
| Regime blocks | `strategy_config.yaml` | `block_sideways`, ATR / range params |
| Strike band | `strategy_config.yaml` | `strike_selection.*` |
| Liquidity | `validator_config.yaml` | `max_spread_pct`, cooldown, paper relax |
| Book quality | `fees_config.yaml` | `orderbook.*` |
| Size / capital | `risk_config.yaml` | 85% deploy, lot tiers, **circuit breakers currently 0=off** |
| Exits / flip | `position_exit_config.yaml` | bias buffer, adverse %, trail, max hold, **flip_on_trend_reversal** |
| Mode | `.env` | `TRADING_MODE=paper\|live`, Delta keys |

---

## 10. Mental model (one paragraph)

The engine continuously measures **where spot sits vs session VWAP** and whether a coded **setup** (reclaim, pullback, OI breakout, etc.) is active. It scores that idea 0–100 on spot/option VWAP alignment, regime, volume/OI, Greeks, and spread. Only scores **≥ 75** become trades, after liquidity and capital checks. Once in, it does **not** hunt a fixed dollar target: it exits when **bias flips**, premium **drops hard**, a **trail** gives back enough after a run, or **25 minutes** elapse. On bias flip it can **immediately buy the other side**. That whole chain is what you see on the Trades page as setup name + exit reason.

---

*Generated from the live codebase paths in `trading-engine/` and `config/`. Update this file when router, exit, or risk defaults change.*
