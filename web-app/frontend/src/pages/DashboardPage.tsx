import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { fetchHealth, fetchMarketSummary } from "../api/client";
import { IndexCandleChart, type ChartInterval } from "../components/IndexCandleChart";
import { useDashboardOutlet } from "../components/Layout";
import { COCKPIT_REFRESH_EVENT } from "../components/CockpitEngineControls";
import { AtmNearCards } from "../components/cockpit/AtmNearCards";
import { DecisionFlowPanel } from "../components/cockpit/DecisionFlowPanel";
import { MarketIntelligencePanel } from "../components/cockpit/MarketIntelligencePanel";
import { DecisionLogFeed } from "../components/cockpit/DecisionLogFeed";
import { PositionSummaryPanel } from "../components/cockpit/PositionSummaryPanel";
import { RiskManagerPanel } from "../components/cockpit/RiskManagerPanel";
import { StrategyInsightPanel } from "../components/cockpit/StrategyInsightPanel";
import { useStableOpenPositions } from "../hooks/useStableOpenPositions";
import { useResponsiveChartHeight } from "../hooks/useResponsiveChartHeight";
import type { CommoditySnapshot, Watchlist, WatchlistItem, WatchlistOpenPosition } from "../types";

type ChartPickMode = "auto" | "manual";

function positionKey(pos: WatchlistOpenPosition): string {
  return String(pos.position_id || pos.tsym);
}

function optionSideFromPosition(pos: WatchlistOpenPosition): "CE" | "PE" {
  if (pos.side === "CE" || pos.side === "PE") return pos.side;
  if (pos.tsym.includes("CE")) return "CE";
  if (pos.tsym.includes("PE")) return "PE";
  return "CE";
}

function parseStrikeFromTsym(tsym: string): number | null {
  const m = tsym.match(/(\d{4,6})(?:CE|PE)$/i);
  if (!m) return null;
  const n = Number(m[1]);
  return Number.isFinite(n) ? n : null;
}

function findChainItem(
  pos: WatchlistOpenPosition,
  watchlist: Watchlist | null,
): WatchlistItem | null {
  const token = String(pos.instrument_token || "").trim();
  const tsym = pos.tsym;
  const pools = watchlist?.items ?? [];

  if (token) {
    const byToken = pools.find((item) => item.token === token);
    if (byToken) return byToken;
  }
  if (tsym) {
    const byTsym = pools.find((item) => item.tsym === tsym);
    if (byTsym) return byTsym;
  }
  return null;
}

function itemFromPosition(pos: WatchlistOpenPosition): WatchlistItem | null {
  const token = String(pos.instrument_token || "").trim();
  if (!token && !pos.tsym) return null;
  const side = optionSideFromPosition(pos);
  return {
    token: token || pos.tsym,
    tsym: pos.tsym,
    strike: parseStrikeFromTsym(pos.tsym) ?? 0,
    option_type: side,
    is_atm: false,
    tradable: true,
    lot_size: pos.lot_size,
    ltp: pos.current_ltp ?? null,
    bid: null,
    ask: null,
    volume: null,
    oi: null,
    last_update_ts: pos.last_tick_ts ?? null,
  };
}

function resolveOptionForPosition(
  pos: WatchlistOpenPosition,
  watchlist: Watchlist | null,
): WatchlistItem | null {
  return findChainItem(pos, watchlist) ?? itemFromPosition(pos);
}

export function DashboardPage() {
  const { activeCommodity, watchlist, summary } = useDashboardOutlet();
  const [chartInterval, setChartInterval] = useState<ChartInterval>("5m");
  const [selectedOption, setSelectedOption] = useState<WatchlistItem | null>(null);
  const [pickMode, setPickMode] = useState<ChartPickMode>("auto");
  const [error, setError] = useState<string | null>(null);
  const seenPositionKeysRef = useRef<Set<string>>(new Set());
  const positionsReadyRef = useRef(false);

  const commodities: CommoditySnapshot[] = useMemo(
    () => [
      {
        underlying: activeCommodity,
        display_name: activeCommodity,
        spot_ltp: watchlist?.spot_ltp ?? summary?.spot_ltp ?? null,
        atm_strike: watchlist?.atm_strike ?? summary?.atm_strike ?? null,
        items: watchlist?.items ?? [],
      },
    ],
    [activeCommodity, watchlist, summary],
  );

  const selectedCommodity = commodities[0];
  const futSpot = selectedCommodity?.spot_ltp ?? null;

  const { positions: openPositions, live: positionStreamLive } = useStableOpenPositions(
    watchlist,
    summary,
  );
  const chartHeight = useResponsiveChartHeight();

  useEffect(() => {
    setSelectedOption(null);
    setPickMode("auto");
    seenPositionKeysRef.current = new Set();
    positionsReadyRef.current = false;
  }, [activeCommodity]);

  useEffect(() => {
    const relevant = openPositions;
    const currentKeys = new Set(relevant.map(positionKey));
    const prevKeys = seenPositionKeysRef.current;
    const newBuys = relevant.filter((p) => !prevKeys.has(positionKey(p)));
    seenPositionKeysRef.current = currentKeys;

    const applyItem = (item: WatchlistItem | null) => {
      setSelectedOption((prev) => {
        if (!item) return prev == null ? prev : null;
        if (prev?.token === item.token && prev?.tsym === item.tsym) return prev;
        return item;
      });
    };

    if (!relevant.length) {
      if (pickMode === "auto") applyItem(null);
      positionsReadyRef.current = true;
      return;
    }

    if (positionsReadyRef.current && newBuys[0]) {
      const item = resolveOptionForPosition(newBuys[0], watchlist);
      if (item) {
        setPickMode("auto");
        applyItem(item);
      }
    } else if (pickMode === "auto") {
      const item = resolveOptionForPosition(relevant[0], watchlist);
      if (item) applyItem(item);
    }

    positionsReadyRef.current = true;
  }, [openPositions, pickMode, watchlist]);

  const handleSelectOption = useCallback((item: WatchlistItem | null) => {
    setPickMode("manual");
    setSelectedOption(item);
  }, []);

  const liveSelectedOption = useMemo(() => {
    if (!selectedOption?.token) return null;
    const token = selectedOption.token;
    const fromWatchlist = watchlist?.items?.find((item) => item.token === token);
    if (fromWatchlist) return fromWatchlist;
    const fromPos = openPositions.find(
      (p) =>
        String(p.instrument_token || "") === token ||
        p.tsym === selectedOption.tsym,
    );
    if (fromPos) {
      return {
        ...selectedOption,
        ltp: fromPos.current_ltp ?? selectedOption.ltp,
      };
    }
    return selectedOption;
  }, [selectedOption, watchlist, openPositions]);

  const chartLiveSpot = liveSelectedOption?.ltp ?? futSpot;
  const chartDisplayName = liveSelectedOption
    ? liveSelectedOption.tsym
    : `${activeCommodity} PERP`;

  const load = useCallback(() => {
    fetchHealth()
      .then((h) => setError(h.error ?? null))
      .catch((e) => setError(String(e)));
    fetchMarketSummary().catch(() => undefined);
  }, []);

  useEffect(() => {
    load();
    const intervalMs = openPositions.length ? 5000 : 5000;
    const healthId = setInterval(load, intervalMs);
    const onRefresh = () => load();
    window.addEventListener(COCKPIT_REFRESH_EVENT, onRefresh);
    return () => {
      clearInterval(healthId);
      window.removeEventListener(COCKPIT_REFRESH_EVENT, onRefresh);
    };
  }, [load, openPositions.length]);

  const feedMode = summary?.feed_mode ?? watchlist?.feed_mode ?? "offline";

  return (
    <div className="terminal-cockpit terminal-cockpit--no-bottom">
      {error ? (
        <div className="error-banner">
          <AlertTriangle size={16} />
          {error}
        </div>
      ) : null}

      <section className="cockpit-main cockpit-main-ref">
        <main className="cockpit-col cockpit-col-center">
          <IndexCandleChart
            underlying={activeCommodity}
            displayName={chartDisplayName}
            liveSpot={chartLiveSpot}
            watchlist={watchlist}
            feedMode={watchlist?.feed_mode ?? feedMode}
            height={chartHeight}
            compact
            interval={chartInterval}
            onIntervalChange={setChartInterval}
            levels={liveSelectedOption ? null : (summary?.levels ?? null)}
            instrumentToken={liveSelectedOption?.token ?? null}
            instrumentTsym={liveSelectedOption?.tsym ?? null}
            instrumentExchange="DELTA"
          />
          <AtmNearCards
            watchlist={watchlist}
            summary={summary}
            activeCommodity={activeCommodity}
            selectedToken={liveSelectedOption?.token ?? null}
            onSelectOption={handleSelectOption}
          />
        </main>

        <aside className="cockpit-col cockpit-col-right">
          <DecisionFlowPanel summary={summary} />
          <MarketIntelligencePanel summary={summary} />
          <StrategyInsightPanel
            summary={summary}
            watchlist={watchlist}
            activeUnderlying={activeCommodity}
            commodities={commodities}
          />
          <PositionSummaryPanel
            openPositions={openPositions}
            summary={summary}
            onRefresh={load}
            live={positionStreamLive}
          />
          <RiskManagerPanel summary={summary} onRefresh={load} />
          <DecisionLogFeed />
        </aside>
      </section>
    </div>
  );
}
