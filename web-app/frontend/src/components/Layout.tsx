import { Outlet, useLocation, useNavigate, useOutletContext } from "react-router-dom";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useAuth } from "../auth/AuthContext";
import {
  fetchHealth,
  fetchMarketSummary,
  fetchWatchlist,
  openWatchlistStream,
} from "../api/client";
import type { EngineHealth, MarketSummary, Watchlist } from "../types";
import { MarketStatsPanel } from "./cockpit/MarketStatsPanel";
import { CockpitEngineControls, COCKPIT_REFRESH_EVENT } from "./CockpitEngineControls";
import { GlobalTopHeader, type CryptoQuote } from "./GlobalTopHeader";
import { RefSidebarFooter, RefSidebarNav } from "./RefSidebarNav";
import { formatIstClock } from "../utils/format";

export interface DashboardOutletContext {
  activeCommodity: string;
  setActiveCommodity: (v: string) => void;
  watchlist: Watchlist | null;
  summary: MarketSummary | null;
}

export function useDashboardOutlet() {
  return useOutletContext<DashboardOutletContext>();
}

function useIstClock() {
  const [now, setNow] = useState("");
  useEffect(() => {
    const tick = () => setNow(formatIstClock());
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}

export function Layout() {
  const location = useLocation();
  const navigate = useNavigate();
  const { username, logout } = useAuth();
  const ist = useIstClock();
  const [health, setHealth] = useState<EngineHealth | null>(null);
  const [summary, setSummary] = useState<MarketSummary | null>(null);
  const [watchlist, setWatchlist] = useState<Watchlist | null>(null);
  const [activeSymbol, setActiveSymbol] = useState("BTC");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const isDashboard = location.pathname === "/";

  const load = useCallback(() => {
    fetchHealth().then(setHealth).catch(() => setHealth(null));
    fetchMarketSummary().then(setSummary).catch(() => setSummary(null));
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 4000);
    const onRefresh = () => load();
    window.addEventListener(COCKPIT_REFRESH_EVENT, onRefresh);
    return () => {
      clearInterval(id);
      window.removeEventListener(COCKPIT_REFRESH_EVENT, onRefresh);
    };
  }, [load]);

  useEffect(() => {
    fetchWatchlist().then(setWatchlist).catch(() => setWatchlist(null));
    const stop = openWatchlistStream(
      (wl) => setWatchlist(wl),
      () => undefined,
    );
    const id = setInterval(() => {
      fetchWatchlist().then(setWatchlist).catch(() => setWatchlist(null));
    }, 8000);
    return () => {
      stop();
      clearInterval(id);
    };
  }, []);

  useEffect(() => {
    if (!watchlist?.underlying) return;
    if (activeSymbol !== watchlist.underlying) {
      setActiveSymbol(watchlist.underlying);
    }
  }, [watchlist?.underlying, activeSymbol]);

  useEffect(() => {
    setSidebarOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    document.body.style.overflow = sidebarOpen ? "hidden" : "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [sidebarOpen]);

  const handleLogout = async () => {
    await logout();
    navigate("/login");
  };

  const refreshDashboardData = useCallback(async () => {
    load();
    try {
      const wl = await fetchWatchlist();
      setWatchlist(wl);
    } catch {
      setWatchlist(null);
    }
  }, [load]);

  const quotes: CryptoQuote[] = useMemo(() => {
    const btcSpot =
      watchlist?.underlying === "BTC"
        ? watchlist.spot_ltp
        : summary?.underlying === "BTC"
          ? summary.spot_ltp
          : health?.spot_ltp
            ? Number(health.spot_ltp)
            : null;
    return [
      {
        symbol: "BTC",
        label: "BTC PERP",
        spot: btcSpot,
        sub: summary?.spot_vs_vwap ?? null,
      },
      {
        symbol: "ETH",
        label: "ETH OPTIONS",
        spot: summary?.underlying === "ETH" ? summary.spot_ltp : null,
        sub: "Coming soon",
      },
    ];
  }, [watchlist, summary, health?.spot_ltp]);

  const outletContext: DashboardOutletContext = {
    activeCommodity: activeSymbol,
    setActiveCommodity: setActiveSymbol,
    watchlist,
    summary,
  };

  return (
    <div className={`app-shell app-shell-dashboard ref-layout${sidebarOpen ? " ref-sidebar-open" : ""}`}>
      <GlobalTopHeader
        quotes={quotes}
        active={activeSymbol}
        onChange={setActiveSymbol}
        brokerConnected={health?.broker_connected}
        brokerName="Delta"
        clock={ist}
        marketOpen={watchlist?.market_open ?? summary?.market_open}
        marketSession={summary?.market_session}
        feedMode={watchlist?.feed_mode ?? summary?.feed_mode}
        wsOpen={summary?.ws_open ?? watchlist?.ws_open}
        wsQuoteAgeSec={summary?.ws_quote_age_sec ?? watchlist?.ws_quote_age_sec}
        quoteAgeSec={summary?.quote_age_sec ?? watchlist?.quote_age_sec}
        onMenuToggle={() => setSidebarOpen((open) => !open)}
        engineControls={
          <CockpitEngineControls
            onDataRefresh={refreshDashboardData}
            onLogout={handleLogout}
            brokerName="Delta"
          />
        }
      />

      <div className="ref-body">
        <button
          type="button"
          className="ref-sidebar-backdrop"
          aria-label="Close navigation"
          onClick={() => setSidebarOpen(false)}
        />
        <aside className={`sidebar ref-sidebar${sidebarOpen ? " is-open" : ""}`}>
          <RefSidebarNav alertCount={summary?.unread_notifications ?? 0} />
          <div className="sidebar-widgets">
            <MarketStatsPanel
              watchlist={watchlist}
              summary={summary}
              activeCommodity={activeSymbol}
            />
          </div>
          <RefSidebarFooter
            status={health?.status}
            username={username ?? undefined}
            brokerOn={health?.broker_connected}
            onLogout={handleLogout}
          />
        </aside>

        <div className="main ref-main">
          <main className={isDashboard ? "content content-cockpit" : "content content-page"}>
            <Outlet context={outletContext} />
          </main>
        </div>
      </div>
    </div>
  );
}
