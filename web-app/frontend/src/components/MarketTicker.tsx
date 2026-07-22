import type { MarketSummary } from "../types";
import { formatUsd, formatPrice } from "../utils/money";



interface MarketTickerProps {
  summary: MarketSummary | null;
  spotLtp?: string | null;
}

export function MarketTicker({ summary, spotLtp }: MarketTickerProps) {
  const spot = summary?.spot_ltp ?? (spotLtp ? Number(spotLtp) : null);
  const items = [
    { label: summary?.underlying ?? "BTC", value: formatPrice(spot), accent: true },
    { label: "VWAP", value: formatPrice(summary?.session_vwap ?? null) },
    { label: "vs VWAP", value: summary?.spot_vs_vwap ?? "—" },
    { label: "ATM", value: summary?.atm_strike?.toLocaleString("en-US") ?? "—" },
    { label: "5m Bias", value: summary?.bias_5m ?? "NEUTRAL" },
    { label: "Strategy", value: (summary?.strategy ?? "vwap_reclaim").replace(/_/g, " ").toUpperCase() },
    { label: "Session", value: summary?.market_session ?? "—" },
    {
      label: "Today P&L",
      value: summary?.today_pnl != null ? formatUsd(summary.today_pnl, { signed: true }) : "—",
      pnl: summary?.today_pnl,
    },
    { label: "Trades", value: String(summary?.trade_count ?? 0) },
    { label: "Mode", value: (summary?.trading_mode ?? "paper").toUpperCase() },
  ];

  const tape = [...items, ...items];

  return (
    <div className="market-ticker" aria-label="Live market tape">
      <div className="ticker-viewport">
        <div className="ticker-track">
          {tape.map((item, i) => (
            <div key={`${item.label}-${i}`} className="ticker-item">
              <span className="ticker-label">{item.label}</span>
              <span
                className={`ticker-value ${item.accent ? "accent" : ""} ${
                  item.pnl != null ? (item.pnl >= 0 ? "up" : "down") : ""
                }`}
              >
                {item.value}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
