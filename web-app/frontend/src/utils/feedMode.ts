export interface FeedModeView {
  mode: string;
  label: string;
  tone: "live" | "backup" | "offline";
  detail: string;
}

function effectiveWsOpen(
  wsOpen?: boolean | null,
  wsQuoteAgeSec?: number | null,
  quoteAgeSec?: number | null,
): boolean {
  if (wsOpen) return true;
  if (wsQuoteAgeSec != null && Number.isFinite(wsQuoteAgeSec) && wsQuoteAgeSec <= 20) {
    return true;
  }
  if (quoteAgeSec != null && Number.isFinite(quoteAgeSec) && quoteAgeSec <= 15) {
    return true;
  }
  return false;
}

export function describeFeedMode(
  feedMode?: string | null,
  wsOpen?: boolean | null,
  wsQuoteAgeSec?: number | null,
  quoteAgeSec?: number | null,
): FeedModeView {
  const mode = (feedMode ?? "offline").toLowerCase();
  const live = effectiveWsOpen(wsOpen, wsQuoteAgeSec, quoteAgeSec);
  const restAge =
    quoteAgeSec != null && Number.isFinite(quoteAgeSec)
      ? `${Math.round(quoteAgeSec)}s ago`
      : null;
  if (mode === "websocket" || mode === "ws") {
    const stale = wsQuoteAgeSec != null && wsQuoteAgeSec > 12;
    return {
      mode: "websocket",
      label: live ? (stale ? "WS STALE" : "WS LIVE") : "WS CONNECTING",
      tone: live && !stale ? "live" : "backup",
      detail: live
        ? stale
          ? `Delta WebSocket — last tick ${Math.round(wsQuoteAgeSec!)}s ago; REST backup active`
          : restAge
            ? `Delta WebSocket live · last quote ${restAge}`
            : "Delta WebSocket stream active"
        : "WebSocket connecting — REST backup may apply",
    };
  }
  if (mode === "rest") {
    const ageBit = restAge ? ` · last tick ${restAge}` : "";
    if (live) {
      return {
        mode: "rest",
        label: "REST LIVE",
        tone: "backup",
        detail: `Quotes via Delta REST (~1s)${ageBit}. WebSocket linked — waiting for live ticks.`,
      };
    }
    return {
      mode: "rest",
      label: "REST LIVE",
      tone: "backup",
      detail: `Delta REST quote poll (~1s)${ageBit} — WebSocket not connected`,
    };
  }
  return {
    mode: "offline",
    label: "OFFLINE",
    tone: "offline",
    detail: "Market feed not connected",
  };
}
