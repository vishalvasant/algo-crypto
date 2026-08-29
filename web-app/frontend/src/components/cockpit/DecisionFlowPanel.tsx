import { GitBranch } from "lucide-react";
import type { DecisionFlowStageStatus, MarketSummary } from "../../types";
import { describeFeedMode } from "../../utils/feedMode";
import { formatIndexPrice } from "../../utils/format";

interface DecisionFlowPanelProps {
  summary: MarketSummary | null;
}

function statusLabel(status: DecisionFlowStageStatus): string {
  switch (status) {
    case "ok":
      return "Ready";
    case "warn":
      return "Caution";
    case "block":
      return "Blocked";
    case "pending":
      return "Waiting";
    default:
      return "Idle";
  }
}

function formatConfidence(
  routerConfidence?: number | null,
  minConfidence?: number,
): string {
  if (routerConfidence == null) {
    return minConfidence != null ? `min ${minConfidence}%` : "—";
  }
  if (minConfidence != null) {
    return `${routerConfidence}% / ${minConfidence}%`;
  }
  return `${routerConfidence}%`;
}

export function DecisionFlowPanel({ summary }: DecisionFlowPanelProps) {
  const flow = summary?.decision_flow;
  const stages = flow?.stages ?? [];
  const feed = describeFeedMode(
    summary?.feed_mode,
    summary?.ws_open,
    summary?.ws_quote_age_sec,
    summary?.quote_age_sec,
  );
  const ob = flow?.orderbook_gates;

  return (
    <section className="cockpit-panel decision-flow-panel">
      <header className="cockpit-panel-head decision-flow-head">
        <div className="decision-flow-title">
          <GitBranch size={14} aria-hidden />
          <h3>Decision Flow</h3>
        </div>
        {flow?.momentum_aligned === false ? (
          <span className="decision-flow-badge tone-warn">Momentum mixed</span>
        ) : flow?.last_entry_block ? (
          <span className="decision-flow-badge tone-block">Entry blocked</span>
        ) : (
          <span
            className={`decision-flow-badge tone-${feed.tone === "live" ? "ok" : feed.tone === "backup" ? "warn" : "block"}`}
          >
            {feed.label}
          </span>
        )}
      </header>

      <div className="decision-flow-metrics">
        <div className="decision-flow-metric">
          <span className="decision-flow-k">Spot</span>
          <strong className="mono">{formatIndexPrice(summary?.spot_ltp)}</strong>
        </div>
        <div className="decision-flow-metric">
          <span className="decision-flow-k">VWAP</span>
          <strong className="mono">{formatIndexPrice(summary?.session_vwap)}</strong>
        </div>
        <div className="decision-flow-metric">
          <span className="decision-flow-k">Conf</span>
          <strong className="mono">
            {formatConfidence(flow?.router_confidence, flow?.min_confidence)}
          </strong>
        </div>
        <div className="decision-flow-metric">
          <span className="decision-flow-k">Bias</span>
          <strong className="mono">{summary?.bias_5m ?? flow?.bias_5m ?? "—"}</strong>
        </div>
      </div>

      <p className="decision-flow-feed-hint muted">{feed.detail}</p>
      {ob?.enabled ? (
        <p className="decision-flow-feed-hint muted">
          Orderbook gate: min ask {ob.min_ask_size_lots} lots · depth ≥
          {ob.min_ask_coverage_mult.toFixed(1)}× size · ask/bid ≤
          {ob.max_ask_bid_size_ratio.toFixed(0)}×
        </p>
      ) : null}

      <ol className="decision-flow-stages" aria-label="Trade decision pipeline">
        {stages.length === 0 ? (
          <li className="decision-flow-stage tone-pending">
            <span className="decision-flow-stage-label">Pipeline</span>
            <span className="decision-flow-stage-detail muted">Waiting for first scan…</span>
          </li>
        ) : (
          stages.map((stage, index) => (
            <li
              key={stage.id}
              className={`decision-flow-stage tone-${stage.status}${index < stages.length - 1 ? " has-connector" : ""}`}
            >
              <div className="decision-flow-stage-marker" aria-hidden />
              <div className="decision-flow-stage-body">
                <div className="decision-flow-stage-top">
                  <span className="decision-flow-stage-label">{stage.label}</span>
                  <span className={`decision-flow-stage-status tone-${stage.status}`}>
                    {statusLabel(stage.status)}
                  </span>
                </div>
                <p className="decision-flow-stage-detail">{stage.detail}</p>
              </div>
            </li>
          ))
        )}
      </ol>
    </section>
  );
}
