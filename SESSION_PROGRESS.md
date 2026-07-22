# Algo-Crypto — Session Progress Summary

**Last updated:** 2026-07-22  
**Scope:** Delta Exchange BTC/ETH options — capital utilization (85%), orderbook-aware entries, fee-aware P&L accounting, margin reconciliation, and UI updates.

---

## Executive Summary

Algo-Crypto is the Delta Exchange India options trading engine (BTC/ETH weekly options, paper + live). This session added realistic fee modeling, L2 orderbook quality gates for entries, raised capital deployment to 85%, and fixed "Used Margin" not resetting after closes or restarts.

**Account:** Paper account reset to **$250** starting balance.

---

## 1. Capital Utilization — 85%

### Config (`config/risk_config.yaml`)

```yaml
max_premium_pct_of_available: 85   # was lower
max_deployed_pct_of_equity: 85   # was lower
```

Per-trade and total deploy caps now allow using up to **85%** of available capital/equity, matching the user's target utilization.

### Leverage Model
Long options at 100× leverage: capital locked ≈ **full premium** (not underlying notional). Documented in `config/fees_config.yaml` as `long_option_leverage: 100`.

---

## 2. Fee Model (Delta India Options)

### New Config — `config/fees_config.yaml`

| Parameter | Value | Notes |
|-----------|-------|-------|
| `options_taker_rate` | 0.0001 (0.01%) | Fallback; prefer live product rates |
| `options_maker_rate` | 0.0001 | |
| `premium_fee_cap_pct` | 3.5% | Fee capped as % of premium |
| `gst_pct` | 18% | Applied on base fee |
| `assume_role` | taker | Paper market orders = taker |

**Formula:** `fee = min(notional × rate, premium × cap%) × (1 + GST)`

### New Module — `trading-engine/src/algocrypto/fees.py`

- `FeeQuote` dataclass — breakdown of notional, raw fee, capped fee, GST, total
- `option_fee()` — computes entry/exit fees per lot
- `OrderBookSnapshot` — parsed L2 book state
- `parse_l2_orderbook()` — normalizes Delta `/v2/l2orderbook/{symbol}` response
- `orderbook_entry_ok()` — entry quality gate (see §3)

### Database — `db/migrations/003_trade_fees.sql`

New columns on `closed_trades`:
- `gross_pnl` — P&L before fees
- `fees_usd`, `entry_fee_usd`, `exit_fee_usd`
- `fee_detail` (JSONB) — full fee breakdown

---

## 3. Orderbook-Aware Entry Decisions

### Delta L2 API
`trading-engine/src/algocrypto/broker/delta.py` — `get_l2_orderbook(symbol)` fetches live depth.

### Quality Gates (`config/fees_config.yaml` → `orderbook`)

| Gate | Threshold |
|------|-----------|
| `enabled` | true |
| `depth` | 10 levels |
| `min_ask_size_lots` | 50 |
| `min_ask_coverage_mult` | 1.0× order size |
| `max_ask_bid_size_ratio` | 8.0 (reject thin bid side) |

### Integration
- `trading/orchestrator.py` — calls `orderbook_entry_ok()` before placing entries
- `broker/paper.py` — uses L2 for realistic fills (buy at ask, sell at bid); passthrough `get_l2_orderbook`

---

## 4. Position & P&L Accounting

### `trading-engine/src/algocrypto/position/manager.py`

- `OpenPosition` stores `entry_spot`, `entry_fee_usd`
- `register_open()` — calculates and records entry fee
- `_close_position()` — computes `gross_pnl`, `exit_fee_usd`, `total_fees`, `net_pnl`
- `reconcile_margin()` — syncs `deployed_capital` with live open positions (0 when flat)

### `journal/writer.py`
`write_closed_trade()` persists gross/net P&L and fee columns.

### `execution/engine.py`
Trade entry notification includes estimated entry fee.

---

## 5. Margin / Used Capital Fix

### Problem
After closing all positions (especially after engine restart), **Used Margin** in the UI stayed non-zero.

### Fix
1. **`reconcile_margin()`** in risk engine — recalculates deployed capital from open positions
2. Called after every entry and exit in orchestrator
3. **`api/health.py`** — `market_summary` uses reconciled margin
4. **`web-app`** — `used_margin` shows 0 when no open positions

---

## 6. UI Updates

### Types (`web-app/frontend/src/types.ts`)
`ClosedTrade` extended with `gross_pnl`, `fees_usd`, `entry_fee_usd`, `exit_fee_usd`.

### Pages
| Page | Change |
|------|--------|
| `TradesPage.tsx` | Shows Gross P&L and Fees columns |
| `TradeBlotterPanel.tsx` | Shows Fees in blotter |
| `DashboardPage.tsx` | Market summary reflects reconciled margin |

### Web API (`web-app/src/algocrypto_web/main.py`)
- `_fetch_closed_trades` returns new fee columns
- `market_summary` — `used_margin: 0` when flat

---

## 7. Supporting Changes

| File | Change |
|------|--------|
| `config.py` | Loads `fees_config.yaml` into `self.fees` |
| `symbols_util.py` | `underlying_from_tsym()` helper for symbol parsing |
| `broker/paper.py` | L2-based paper fills |

---

## 8. Paper Account Reset

User updated Delta API key in `.env` and requested a fresh $250 paper start.

**Note:** If Delta API returns `invalid_api_key`, verify:
- Key is for **India production** (not testnet)
- VPN IP matches whitelisted IP on Delta
- Key has not been revoked/regenerated elsewhere

---

## 9. Deployment

```bash
# Rebuild and restart (from project root)
docker compose build trading-engine web-app
docker compose up -d trading-engine web-app

# Apply DB migration
docker compose exec trading-engine psql ... -f db/migrations/003_trade_fees.sql
```

---

## 10. What to Verify Live

1. **Capital deploy** — new entries use up to 85% of available equity
2. **Orderbook gate** — weak books logged/rejected in decision logs
3. **Fees** — closed trades show gross P&L, fees, and net P&L
4. **Used margin** — returns to **$0** when all positions closed
5. **Paper fills** — buy at ask / sell at bid from L2 (not mid)

---

## File Index (this session)

```
config/
  fees_config.yaml             # NEW — fee model + orderbook gates
  risk_config.yaml             # 85% deploy caps

db/migrations/
  003_trade_fees.sql           # NEW — fee columns on closed_trades

trading-engine/src/algocrypto/
  fees.py                      # NEW — fee calc + orderbook validation
  config.py                    # loads fees config
  broker/delta.py              # get_l2_orderbook()
  broker/paper.py              # L2 fills + orderbook passthrough
  position/manager.py          # fee tracking, reconcile_margin
  journal/writer.py            # persist fee fields
  trading/orchestrator.py        # orderbook gate + reconcile
  execution/engine.py          # entry fee in notifications
  api/health.py                # reconciled margin in summary
  symbols_util.py              # underlying_from_tsym

web-app/
  src/algocrypto_web/main.py   # fee columns, margin fix
  frontend/src/types.ts        # ClosedTrade fee fields
  frontend/src/pages/TradesPage.tsx
  frontend/src/components/TradeBlotterPanel.tsx
```

---

## Relationship to Algo-Flat

Both projects share similar architecture (scanner → validator → quality gate → execution → position manager) but target different markets:

| | Algo-Crypto | Algo-Flat |
|---|-------------|-----------|
| Broker | Delta Exchange | Flattrade |
| Underlying | BTC / ETH | NIFTY |
| Session | 24×7 | NSE hours |
| This session focus | Fees, orderbook, 85% deploy | WS monitoring, trap avoidance, MTF |

See Algo-Flat progress: `../Algo-Flat/SESSION_PROGRESS.md`
