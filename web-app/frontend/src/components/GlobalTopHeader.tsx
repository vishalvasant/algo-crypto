import { Menu } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useAuth } from "../auth/AuthContext";
import { describeFeedMode } from "../utils/feedMode";

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
  wsOpen?: boolean | null;
  wsQuoteAgeSec?: number | null;
  quoteAgeSec?: number | null;
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
  wsOpen,
  wsQuoteAgeSec,
  quoteAgeSec,
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

  const sessionLabel = (marketSession ?? (marketOpen ? "OPEN" : "CLOSED")).toUpperCase();
  const feedView = describeFeedMode(feedMode, wsOpen, wsQuoteAgeSec, quoteAgeSec);

  return (
    <header className="global-top-header">
      <div className="global-top-brand">
        {onMenuToggle ? (
          <button
            type="button"
            className="header-ctl global-menu-btn"
            onClick={onMenuToggle}
            aria-label="Toggle navigation"
          >
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
        <div className="global-top-indices" role="list">
          {quotes.map((q) => {
            const isActive = q.symbol === active;
            const flash = priceFlash[q.symbol];
            const disabled = q.symbol === "ETH";
            return (
              <button
                key={q.symbol}
                type="button"
                role="listitem"
                className={`global-index-card${isActive ? " active" : ""}${flash ? ` price-flash-${flash}` : ""}${disabled ? " display-only" : ""}`}
                onClick={() => {
                  if (!disabled) onChange(q.symbol);
                }}
                disabled={disabled}
                title={disabled ? "ETH options chain not configured yet" : `Trade ${q.label}`}
                aria-pressed={!disabled ? isActive : undefined}
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
        <div
          className="global-status-cluster"
          title={`${brokerName} · ${feedView.detail}`}
        >
          <span className="global-status-row">
            <span className={`broker-dot ${brokerConnected ? "on" : "off"}`} />
            <span className="meta-v">{brokerName}</span>
            <span className="global-meta-sep">·</span>
            <span className={`meta-feed ${feedView.tone === "live" ? "live" : ""}`}>
              {feedView.label}
            </span>
          </span>
          {clock ? (
            <span className="global-meta-clock mono" title="Asia/Kolkata">
              {clock}
              <span className="clock-tz"> IST</span>
            </span>
          ) : null}
        </div>

        {engineControls}

        <span className="global-avatar" title={username ?? "User"}>
          {initials}
        </span>
      </div>
    </header>
  );
}
