import { useMemo } from "react";
import { BarChart3, TrendingDown, TrendingUp } from "lucide-react";
import type { MarketSummary, Watchlist } from "../../types";
import { useCommodityChain } from "../../hooks/useCommodityChain";
import { formatIndexPrice } from "../../utils/format";

function formatPct(n: number) {
  return `${n.toFixed(0)}%`;
}

type PcrTone = "bullish" | "bearish" | "neutral";

function pcrTone(value: number): PcrTone {
  if (value < 0.9) return "bullish";
  if (value > 1.1) return "bearish";
  return "neutral";
}

function biasSentiment(bias: string): {
  label: string;
  tone: "bullish" | "bearish" | "neutral";
} {
  const v = bias.toUpperCase();
  if (v === "BULLISH") return { label: "Bullish", tone: "bullish" };
  if (v === "BEARISH") return { label: "Bearish", tone: "bearish" };
  return { label: "Neutral", tone: "neutral" };
}

interface MarketStatsPanelProps {
  watchlist: Watchlist | null;
  summary: MarketSummary | null;
  activeCommodity: string;
}

export function MarketStatsPanel({
  watchlist,
  summary,
  activeCommodity,
}: MarketStatsPanelProps) {
  const { items } = useCommodityChain(watchlist, summary, activeCommodity);

  const { liveOiPcr, liveVolPcr, ceOi, peOi } = useMemo(() => {
    let ceOiSum = 0;
    let peOiSum = 0;
    let ceVol = 0;
    let peVol = 0;
    for (const item of items) {
      const oi = item.oi ?? 0;
      const vol = item.volume ?? 0;
      if (item.option_type === "CE") {
        ceOiSum += oi;
        ceVol += vol;
      }
      if (item.option_type === "PE") {
        peOiSum += oi;
        peVol += vol;
      }
    }
    return {
      liveOiPcr: ceOiSum > 0 ? peOiSum / ceOiSum : null,
      liveVolPcr: ceVol > 0 ? peVol / ceVol : null,
      ceOi: ceOiSum,
      peOi: peOiSum,
    };
  }, [items]);

  const bias = watchlist?.bias_5m ?? summary?.bias_5m ?? "NEUTRAL";
  const sentiment = biasSentiment(bias);
  const spotVsVwap = summary?.spot_vs_vwap?.toLowerCase();
  const vwap = summary?.session_vwap ?? null;
  const spot = watchlist?.spot_ltp ?? summary?.spot_ltp ?? null;

  const totalOi = ceOi + peOi;
  const cePct = totalOi > 0 ? (ceOi / totalOi) * 100 : 50;
  const pePct = totalOi > 0 ? (peOi / totalOi) * 100 : 50;
  const oiPcr = liveOiPcr ?? 1;
  const volPcr = liveVolPcr ?? 1;

  return (
    <section className="cockpit-panel market-stats-panel market-breadth-panel">
      <header className="cockpit-panel-head market-breadth-head">
        <div className="market-breadth-title">
          <BarChart3 size={14} aria-hidden />
          <h3>Chain Stats</h3>
        </div>
        <span className={`breadth-sentiment-badge tone-${sentiment.tone}`}>
          {sentiment.label}
        </span>
      </header>

      <div className="breadth-summary">
        <p className="breadth-net mono">
          {formatIndexPrice(spot)}
          <span className="breadth-net-label">spot</span>
        </p>
        <p className="breadth-participation muted">{activeCommodity} options</p>
      </div>

      <div
        className="breadth-bar"
        role="img"
        aria-label={`CE OI ${formatPct(cePct)}, PE OI ${formatPct(pePct)}`}
      >
        <div className="breadth-bar-seg adv" style={{ width: `${cePct}%` }} title="CE OI" />
        <div className="breadth-bar-seg dec" style={{ width: `${pePct}%` }} title="PE OI" />
      </div>

      <ul className="breadth-chips">
        <li className="breadth-chip adv">
          <span className="breadth-chip-value mono">CE</span>
          <span className="breadth-chip-meta">
            <i className="breadth-dot" aria-hidden />
            OI · {formatPct(cePct)}
          </span>
        </li>
        <li className="breadth-chip dec">
          <span className="breadth-chip-value mono">PE</span>
          <span className="breadth-chip-meta">
            <i className="breadth-dot" aria-hidden />
            OI · {formatPct(pePct)}
          </span>
        </li>
      </ul>

      <dl className="breadth-metrics">
        <div className="breadth-metric">
          <dt>
            Vol PCR
            <span className="breadth-metric-sub">{activeCommodity}</span>
          </dt>
          <dd className={`mono tone-${pcrTone(volPcr)}`}>
            {liveVolPcr != null ? volPcr.toFixed(2) : "—"}
          </dd>
        </div>
        <div className="breadth-metric">
          <dt>
            OI PCR
            <span className="breadth-metric-sub">{activeCommodity}</span>
          </dt>
          <dd className={`mono tone-${pcrTone(oiPcr)}`}>
            {liveOiPcr != null ? oiPcr.toFixed(2) : "—"}
          </dd>
        </div>
        <div className="breadth-metric breadth-metric--wide">
          <dt>
            VWAP
            <span className="breadth-metric-sub">{activeCommodity}</span>
          </dt>
          <dd className="mono accent">{formatIndexPrice(vwap)}</dd>
          {spotVsVwap ? (
            <dd
              className={`breadth-metric-hint tone-${spotVsVwap === "above" ? "bullish" : spotVsVwap === "below" ? "bearish" : "neutral"}`}
            >
              Spot {spotVsVwap} VWAP
            </dd>
          ) : null}
        </div>
        <div className="breadth-metric">
          <dt>5m Bias</dt>
          <dd
            className={`mono breadth-vix ${sentiment.tone === "bullish" ? "tone-bullish" : sentiment.tone === "bearish" ? "tone-bearish" : ""}`}
          >
            {sentiment.tone === "bullish" ? (
              <TrendingUp size={12} aria-hidden />
            ) : sentiment.tone === "bearish" ? (
              <TrendingDown size={12} aria-hidden />
            ) : null}
            <span>{bias}</span>
          </dd>
        </div>
      </dl>
    </section>
  );
}
