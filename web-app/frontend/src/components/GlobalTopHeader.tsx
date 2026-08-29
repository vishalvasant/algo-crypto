import { Menu } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useAuth } from "../auth/AuthContext";

export interface CryptoQuote {
  symbol: string;
  label: string;
  spot: number | null;
  sub?: string | null;
}

interface GlobalTopHeaderProps {
  quotes: CryptoQuote[];
  active: string;
  onChange: (symbol: string) => void;
  brokerConnected?: boolean;
  brokerName?: string;
  clock?: string;
  marketOpen?: boolean;
  marketSession?: string | null;
  feedMode?: string | null;
  onMenuToggle?: () => void;
  engineControls?: ReactNode;
}

function formatUsd(spot: number | null | undefined) {
  if (spot == null) return "—";
  return Number(spot).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function GlobalTopHeader({
  quotes,
  active,
  onChange,
  brokerConnected,
  brokerName = "Delta",
  clock,
  marketOpen = false,
  marketSession,
  feedMode,
  onMenuToggle,
  engineControls,
}: GlobalTopHeaderProps) {
  const { username } = useAuth();
  const initials = (username ?? "AC").slice(0, 2).toUpperCase();
  const prevPrices = useRef<Record<string, number>>({});
  const [priceFlash, setPriceFlash] = useState<Record<string, "up" | "down">>({});

  useEffect(() => {
    const flashes: Record<string, "up" | "down"> = {};
    for (const q of quotes) {
      if (q.spot == null) continue;
      const prev = prevPrices.current[q.symbol];
      if (prev != null && prev !== q.spot) {
        flashes[q.symbol] = q.spot > prev ? "up" : "down";
      }
      prevPrices.current[q.symbol] = q.spot;
    }
    if (Object.keys(flashes).length) {
      setPriceFlash(flashes);
      const t = setTimeout(() => setPriceFlash({}), 400);
      return () => clearTimeout(t);
    }
  }, [quotes]);

  const sessionLabel = marketSession ?? (marketOpen ? "OPEN" : "CLOSED");

  return (
    <header className="global-top-header">
      <div className="global-top-brand">
        {onMenuToggle ? (
          <button type="button" className="header-ctl mobile-menu-btn" onClick={onMenuToggle} aria-label="Menu">
            <Menu size={18} />
          </button>
        ) : null}
        <div className="brand-icon sm">AC</div>
        <div className="global-top-brand-text">
          <span className="global-top-logo">Algo-Crypto</span>
          <span className={`global-session-chip ${marketOpen ? "open" : "closed"}`}>
            <span className="global-session-dot" />
            {sessionLabel}
          </span>
        </div>
      </div>

      <div className="global-top-center">
        <div className="global-top-indices">
          {quotes.map((q) => {
            const isActive = q.symbol === active;
            const flash = priceFlash[q.symbol];
            return (
              <button
                key={q.symbol}
                type="button"
                className={`global-index-card${isActive ? " active" : ""}${flash ? ` price-flash-${flash}` : ""}`}
                onClick={() => {
                  if (q.symbol === "ETH") return;
                  onChange(q.symbol);
                }}
                disabled={q.symbol === "ETH"}
                title={q.symbol === "ETH" ? "ETH options chain not configured yet" : undefined}
              >
                <div className="global-index-top">
                  <span className="global-index-name">{q.label}</span>
                </div>
                <span className="global-index-price mono">{formatUsd(q.spot)}</span>
                {q.sub ? <span className="global-index-chg mono">{q.sub}</span> : null}
              </button>
            );
          })}
        </div>
      </div>

      <div className="global-top-meta">
        <span className={`global-broker-pill${brokerConnected ? " on" : ""}`}>
          {brokerName} {brokerConnected ? "LIVE" : "OFF"}
        </span>
        {feedMode ? <span className="global-feed-pill mono">{feedMode.toUpperCase()}</span> : null}
        {clock ? <span className="global-top-clock mono">{clock} IST</span> : null}
        {engineControls}
        <div className="global-avatar" title={username ?? ""}>{initials}</div>
      </div>
    </header>
  );
}
