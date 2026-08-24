# Gap-Fix progress

Source: `/Users/vishal/Desktop/Algo/Gap-Fix.md`  
Updated as phases land. Only claim what exists in code.

## Done

### Phase 1.1 — Hard circuit breakers
- `risk/circuit_breakers.py`, `risk/states.py`, enhanced `risk/engine.py`
- Config: `config/risk_config.yaml` (non-zero limits; deploy 40%/50%; max lots 60)
- Migration: `db/migrations/004_risk_circuit_breakers.sql`
- Tests: `tests/unit/test_circuit_breakers.py` + updated `test_risk_sizing.py`
- E2E: engine healthy; columns `flip_count`, `risk_state`, etc. present

### Phase 2 — VWAP flip whipsaw
- ATR dead zone bias: `strategy_config.vwap_bias` + `position/reversal_confirm.py`
- Confirmed flips: ATR distance, candle close, momentum; QualityGate; cooldown 300s
- Removed forced `confidence=80`
- Config: `position_exit_config.yaml`
- Tests: `tests/unit/test_reversal_confirm.py` (29 Phase1+2 related tests green)
- E2E: engine restarted healthy after rebuild

### Phase 1.2 — SafetyEngine
- `safety/engine.py` — NORMAL / DEGRADED / HALTED
- Wired into `_scan_for_entry` (blocks entries on stale feeds)
- Config: `runtime_config.yaml` → `safety.*`
- Tests: `tests/unit/test_safety_engine.py`

### Phase 3 — Executable price / slippage / paper realism
- `execution/price_model.py` — book walk VWAP, expected slippage, liquidity lot cap
- `broker/paper.py` — orderbook_walk fills, partials, latency, rejection reasons
- Gates: `excessive_expected_slippage`, `excessive_spread`, liquidity size cap
- Config: `execution_config.yaml`, `paper_trading_config.yaml`
- Tests: `tests/unit/test_price_model.py`

### Phase 4 — Risk-based sizing + portfolio exposure
- `risk/sizing.py` — min(confidence, risk-loss budget, capital, liquidity, max)
- `risk/portfolio.py` — BTC/ETH underlying, directional, correlated same-side
- `EntrySizing.size_breakdown` + `binding_reason` journaled on signals
- Config: `risk_config.yaml` → `max_loss_per_trade_*`, `portfolio.*`
- Tests: `tests/unit/test_portfolio_sizing.py`

### Phase 5 — IV / RV / TTE / vol-aware strikes
- `option_data/vol_model.py` — IV history, regime, RV, expected move, expiry buckets
- Wired in orchestrator option context → features.extra
- `strike_picker.py` vol/TTE score adjustments + explainable `pick_meta`
- TTE size / min-confidence adjustments at entry
- Soft IV×setup quality in QualityGate
- Config: `strategy_config.yaml` → `vol_model`, `strike_selection.vol_aware`

### Phase 6 — EV + thesis + dynamic exits
- `trading/ev_engine.py` — EV from learner + prior (not rule_score→P(win))
- Journal `ev_estimate`; block `negative_expected_value`
- `position/thesis.py` — entry thesis; `thesis_degradation` exit
- ATR soft-scale on adverse % (`dynamic_exits_enabled`)
- Config: `strategy_config.ev_engine`, `position_exit_config` thesis/dynamic keys

### Phase 7 — Strategy families / health / regime perf
- `strategy/families.py` — family map + health score
- Router collapses correlated same-family signals; journals `strategy_family`
- `journal/analytics.py` — avg win/loss, PF, MFE/MAE, by regime/underlying/expiry
- Weak health soft-demotes confidence (min sample)

### Phase 8 — Walk-forward
- `algocrypto/backtest/walk_forward.py` — rolling IS/OOS windows
- `scripts/walk_forward.py` — multi-day harness over `reports/snaps`
- `day_backtest.run_backtest(..., return_stats=True)` for aggregation

## Tests
- `tests/unit/test_gapfix_phase5_8.py` (+ prior phase suites)

## Config default changes (documented)
- `strike_selection.vol_aware: true`
- `vol_model.*`, `ev_engine.*`, `strategy_health.*` added
- `thesis_exit_enabled`, `thesis_degrade_below: 40`, `dynamic_exits_enabled`
