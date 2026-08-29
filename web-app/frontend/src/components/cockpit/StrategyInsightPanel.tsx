import { Target } from "lucide-react";
import type { CommoditySnapshot, MarketSummary, Watchlist } from "../../types";
import { formatIndexPrice } from "../../utils/format";

interface StrategyInsightPanelProps {
  summary: MarketSummary | null;
  watchlist?: Watchlist | null;
  activeUnderlying: string;
  commodities?: CommoditySnapshot[];
}

function clampConf(value: number | null | undefined, fallback = 50) {
  if (value == null || Number.isNaN(Number(value))) return fallback;
  return Math.min(100, Math.max(0, Math.round(Number(value))));
}

function SideConfidenceBars({
  bullish,
  bearish,
}: {
  bullish: number;
  bearish: number;
}) {
  const bullLead = bullish >= bearish;
  return (
    <div className="side-confidence" aria-label="Side confidence">
      <div className={`side-conf-row bullish${bullLead ? " is-leading" : ""}`}>
        <span className="side-conf-label">CE</span>
        <div className="side-conf-track">
          <div className="side-conf-fill" style={{ width: `${bullish}%` }} />
        </div>
        <span className="side-conf-pct mono">{bullish}%</span>
      </div>
      <div className={`side-conf-row bearish${!bullLead ? " is-leading" : ""}`}>
        <span className="side-conf-label">PE</span>
        <div className="side-conf-track">
          <div className="side-conf-fill" style={{ width: `${bearish}%` }} />
        </div>
        <span className="side-conf-pct mono">{bearish}%</span>
      </div>
    </div>
  );
}

export function StrategyInsightPanel({
  summary,
  watchlist = null,
  activeUnderlying,
  commodities = [],
}: StrategyInsightPanelProps) {
  const commodity = commodities.find((c) => c.underlying === activeUnderlying);
  const spot =
    commodity?.spot_ltp ??
    watchlist?.spot_ltp ??
    summary?.spot_ltp ??
    null;
  const bias = (watchlist?.bias_5m ?? summary?.bias_5m ?? "NEUTRAL").toUpperCase();
  const bullish = clampConf(summary?.bullish_confidence, bias === "BULLISH" ? 72 : 45);
  const bearish = clampConf(summary?.bearish_confidence, bias === "BEARISH" ? 72 : 45);
  const strategy = (summary?.strategy ?? "vwap_reclaim").replace(/_/g, " ").toUpperCase();
  const routerConf = summary?.router_confidence ?? summary?.confidence;

  const reasons: string[] = [];
  reasons.push(`Bullish CE MTF ${bullish}% · Bearish PE MTF ${bearish}%`);
  if (summary?.spot_vs_vwap) {
    reasons.push(`Spot ${summary.spot_vs_vwap.toLowerCase()} session VWAP`);
  }
  if (routerConf != null) {
    reasons.push(`Router confidence ${Math.round(Number(routerConf))}%`);
  }
  if (summary?.consecutive_losses) {
    reasons.push(`${summary.consecutive_losses} consecutive loss(es) today`);
  }

  const spotNum = spot != null ? Number(spot) : null;
  const entryLow = spotNum != null ? spotNum * 0.998 : null;
  const entryHigh = spotNum != null ? spotNum * 1.002 : null;
  const t1 = spotNum != null ? spotNum * 1.01 : null;
  const sl = spotNum != null ? spotNum * 0.99 : null;

  return (
    <section className="cockpit-panel strategy-panel ref-style">
      <header className="cockpit-panel-head strategy-panel-head">
        <Target size={14} />
        <h3>Trade Panel</h3>
        <span className={`strategy-index-pill bias-${bias.toLowerCase()}`}>{bias}</span>
      </header>

      <div className="strategy-slide-inner">
        <div className="strategy-meta-grid">
          <div className="strategy-meta-cell">
            <span className="strategy-k">Strategy</span>
            <strong className="strategy-v">{strategy}</strong>
          </div>
          <div className="strategy-meta-cell align-end">
            <span className="strategy-k">Underlying</span>
            <strong className="strategy-v">{activeUnderlying}</strong>
          </div>
        </div>

        <section className="strategy-hero strategy-hero--dual">
          <p className="strategy-name">
            <Target size={15} strokeWidth={2.25} />
            <span>Side Confidence</span>
          </p>
          <SideConfidenceBars bullish={bullish} bearish={bearish} />
        </section>

        <ul className="strategy-reasons">
          {reasons.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>

        <div className="strategy-levels" aria-label="Reference levels">
          <div className="strategy-level-col entry">
            <span className="strategy-level-k">Spot</span>
            <span className="strategy-level-v mono">{formatIndexPrice(spot)}</span>
          </div>
          <div className="strategy-level-col">
            <span className="strategy-level-k">Entry band</span>
            <span className="strategy-level-v mono">
              {formatIndexPrice(entryLow)} – {formatIndexPrice(entryHigh)}
            </span>
          </div>
          <div className="strategy-level-col">
            <span className="strategy-level-k">T1</span>
            <span className="strategy-level-v mono">{formatIndexPrice(t1)}</span>
          </div>
          <div className="strategy-level-col">
            <span className="strategy-level-k">SL ref</span>
            <span className="strategy-level-v mono sl">{formatIndexPrice(sl)}</span>
          </div>
        </div>
      </div>
    </section>
  );
}
