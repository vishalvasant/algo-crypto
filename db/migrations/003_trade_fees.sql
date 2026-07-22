-- Fee-aware closed trade accounting (gross vs net after Delta fees + GST).
ALTER TABLE closed_trades
    ADD COLUMN IF NOT EXISTS gross_pnl NUMERIC(12,4),
    ADD COLUMN IF NOT EXISTS fees_usd NUMERIC(12,4) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS entry_fee_usd NUMERIC(12,4) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS exit_fee_usd NUMERIC(12,4) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS fee_detail JSONB;
