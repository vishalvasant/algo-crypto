-- Gap-Fix Phase 1: circuit-breaker / flip counters on daily_risk_state
ALTER TABLE daily_risk_state
  ADD COLUMN IF NOT EXISTS flip_count INT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS flips_disabled BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS risk_state TEXT DEFAULT 'NORMAL',
  ADD COLUMN IF NOT EXISTS losing_trade_timestamps TIMESTAMPTZ[] DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS flip_timestamps TIMESTAMPTZ[] DEFAULT '{}';

COMMENT ON COLUMN daily_risk_state.risk_state IS
  'NORMAL | WARNING | HALTED | EMERGENCY_FLATTEN';
COMMENT ON COLUMN daily_risk_state.flips_disabled IS
  'When true, trend_reversal flips are blocked; normal entries may continue';
