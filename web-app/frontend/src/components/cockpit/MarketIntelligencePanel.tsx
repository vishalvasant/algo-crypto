import { Brain } from "lucide-react";
import type { MarketSummary } from "../../types";
import { formatIndexPrice } from "../../utils/format";

interface MarketIntelligencePanelProps {
  summary: MarketSummary | null;
}

export function MarketIntelligencePanel({ summary }: MarketIntelligencePanelProps) {
  const strategy = (summary?.strategy ?? "vwap_reclaim").replace(/_/g, " ").toUpperCase();
  const scanSec = summary?.scan_interval_seconds ?? 10;

  return (
    <section className="cockpit-panel mi-panel">
      <header className="cockpit-panel-head">
        <Brain size={14} />
        <h3>Market Intelligence</h3>
        <span className="mi-version muted">Crypto v1</span>
      </header>

      <div className="mi-grid">
        <div className="mi-cell">
          <span className="mi-k">Strategy</span>
          <strong>{strategy}</strong>
        </div>
        <div className="mi-cell">
          <span className="mi-k">Mode</span>
          <strong>{(summary?.trading_mode ?? "paper").toUpperCase()}</strong>
        </div>
        <div className="mi-cell">
          <span className="mi-k">Spot vs VWAP</span>
          <strong className="mono">{summary?.spot_vs_vwap ?? "—"}</strong>
        </div>
        <div className="mi-cell">
          <span className="mi-k">ATM strike</span>
          <strong className="mono">{formatIndexPrice(summary?.atm_strike)}</strong>
        </div>
      </div>

      <div className="mi-flags">
        {summary?.auto_trading_active ? (
          <span className="mi-flag tone-ok">Auto trading active</span>
        ) : null}
        {summary?.entries_blocked ? (
          <span className="mi-flag tone-block">Entries blocked</span>
        ) : null}
        {summary?.is_expiry_day ? (
          <span className="mi-flag tone-warn">Expiry session</span>
        ) : null}
      </div>

      <p className="mi-ml muted">
        Scan every {scanSec}s · Candidates {summary?.candidate_count ?? 0} · Rejections{" "}
        {summary?.rejection_count ?? 0}
      </p>
    </section>
  );
}
