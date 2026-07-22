import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Bot,
  Database,
  KeyRound,
  Power,
  RefreshCw,
  Shield,
  Wallet,
} from "lucide-react";
import {
  fetchHealth,
  fetchMarketSummary,
  fetchTradeBlotter,
  fetchWatchlist,
  openWatchlistStream,
  reauthenticate,
  resetPaperAccount,
  setAutoTrade,
  setKillSwitch,
  syncMissingData,
} from "../api/client";
import type { EngineHealth, MarketSummary, TradeBlotter, Watchlist } from "../types";
import { IndicatorCard } from "../components/IndicatorCard";
import { StatusBadge } from "../components/StatusBadge";
import { TradeBlotterPanel } from "../components/TradeBlotterPanel";
import { WatchlistPanel } from "../components/WatchlistPanel";
import { formatUsd } from "../utils/money";

function formatMoney(value: number | undefined, digits = 0) {
  return formatUsd(value ?? 0, { digits });
}

export function DashboardPage() {
  const [health, setHealth] = useState<EngineHealth | null>(null);
  const [summary, setSummary] = useState<MarketSummary | null>(null);
  const [watchlist, setWatchlist] = useState<Watchlist | null>(null);
  const [blotter, setBlotter] = useState<TradeBlotter | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [killBusy, setKillBusy] = useState(false);
  const [autoBusy, setAutoBusy] = useState(false);
  const [syncBusy, setSyncBusy] = useState(false);
  const [reauthBusy, setReauthBusy] = useState(false);
  const [resetBusy, setResetBusy] = useState(false);
  const [reauthMsg, setReauthMsg] = useState<string | null>(null);
  const [syncMsg, setSyncMsg] = useState<string | null>(null);
  const spotHistory = useRef<number[]>([]);

  const load = useCallback(() => {
    fetchHealth()
      .then((h) => {
        setHealth(h);
        setError(h.error ?? null);
        if (h.spot_ltp) {
          const n = Number(h.spot_ltp);
          const hist = spotHistory.current;
          if (hist[hist.length - 1] !== n) {
            spotHistory.current = [...hist.slice(-29), n];
          }
        }
      })
      .catch((e) => setError(String(e)));
    fetchMarketSummary().then(setSummary).catch(() => setSummary(null));
    fetchTradeBlotter()
      .then(setBlotter)
      .catch(() => setBlotter(null));
  }, []);

  const loadWatchlist = useCallback(() => {
    fetchWatchlist()
      .then(setWatchlist)
      .catch(() => setWatchlist(null));
  }, []);

  useEffect(() => {
    load();
    loadWatchlist();
    const healthId = setInterval(load, 5000);
    // Live ticker via SSE; REST poll is fallback only.
    const stopStream = openWatchlistStream(
      (wl) => setWatchlist(wl),
      () => {
        /* EventSource reconnects; keep last snapshot */
      },
    );
    const watchId = setInterval(loadWatchlist, 15000);
    return () => {
      clearInterval(healthId);
      clearInterval(watchId);
      stopStream();
    };
  }, [load, loadWatchlist]);

  const toggleKill = async () => {
    if (!health) return;
    const enabling = !health.kill_switch;
    if (enabling && !confirm("Enable kill switch? This blocks all new trades.")) return;
    setKillBusy(true);
    try {
      await setKillSwitch(enabling);
      load();
    } catch (e) {
      setError(String(e));
    } finally {
      setKillBusy(false);
    }
  };

  const toggleAutoTrade = async () => {
    const currentlyOn = summary?.auto_trade_enabled !== false && !summary?.entries_blocked;
    const enabling = !currentlyOn;
    if (!enabling && !confirm("Turn OFF auto trading? Scans continue; no new entries.")) return;
    setAutoBusy(true);
    try {
      const res = await setAutoTrade(enabling);
      if (enabling && res.kill_switch) {
        setError("Kill switch is ON — disable it before enabling auto trade.");
      }
      load();
    } catch (e) {
      setError(String(e));
    } finally {
      setAutoBusy(false);
    }
  };

  const handleSync = async () => {
    setSyncBusy(true);
    setSyncMsg(null);
    try {
      const res = await syncMissingData();
      const u = res.universe as { action?: string; after?: number; expected?: number };
      const c = res.candles as { m1_added?: number };
      const q = res.quotes as { polled?: number; missing_ltp_after?: number };
      setSyncMsg(
        `Sync OK · universe ${u?.action} (${u?.after}/${u?.expected}) · ` +
          `+${c?.m1_added ?? 0} 1m candles · quotes polled ${q?.polled ?? 0}` +
          (q?.missing_ltp_after ? ` · ${q.missing_ltp_after} still without LTP` : ""),
      );
      load();
      loadWatchlist();
    } catch (e) {
      setError(String(e));
    } finally {
      setSyncBusy(false);
    }
  };

  const handleReauth = async () => {
    if (!confirm("Reconnect Delta API session?")) return;
    setReauthBusy(true);
    setReauthMsg(null);
    setError(null);
    try {
      const result = await reauthenticate(true);
      setReauthMsg(`Session refreshed · ${result.user_id}`);
      load();
      loadWatchlist();
    } catch (e) {
      setError(String(e));
    } finally {
      setReauthBusy(false);
    }
  };

  const handleResetAccount = async () => {
    if (
      !confirm(
        "Reset paper account? This clears all mock trades/orders and restores $250 capital (P&L carry resets).",
      )
    ) {
      return;
    }
    setResetBusy(true);
    setError(null);
    try {
      const result = await resetPaperAccount();
      setReauthMsg(
        `Paper account reset · ${formatUsd(result.starting_capital, { digits: 0 })} · ${result.instrument_count ?? 0} contracts`,
      );
      load();
      loadWatchlist();
    } catch (e) {
      setError(String(e));
    } finally {
      setResetBusy(false);
    }
  };

  const session = health?.flattrade_session;
  const pnl = summary?.today_pnl ?? 0;
  const balance = summary?.starting_capital ?? 250;
  const available = summary?.available_capital ?? balance;
  const usedMargin = summary?.used_margin ?? summary?.deployed_capital ?? 0;
  const openPos = summary?.open_position;
  const openPositions = summary?.open_positions ?? (openPos ? [openPos] : []);
  const unrealized = summary?.unrealized_pnl ?? 0;
  const equity = summary?.equity ?? balance + unrealized;
  const autoOn = summary?.auto_trading_active;
  const feedMode = summary?.feed_mode ?? watchlist?.feed_mode ?? "offline";

  return (
    <div className="terminal-page">
      {error && (
        <div className="error-banner">
          <AlertTriangle size={16} />
          {error}
        </div>
      )}
      {reauthMsg && <div className="success-banner">{reauthMsg}</div>}
      {syncMsg && <div className="success-banner">{syncMsg}</div>}

      <section className="indicator-strip">
        <IndicatorCard
          label="Account Balance"
          value={formatMoney(balance)}
          sub={`Available ${formatMoney(available)}`}
          tone="accent"
        />
        <IndicatorCard
          label="Used Margin"
          value={formatMoney(usedMargin)}
          sub={openPositions.length ? `${openPositions.length} leg(s)` : "No deployment"}
          tone={usedMargin > 0 ? "warning" : "neutral"}
        />
        <IndicatorCard
          label="Today P&L"
          value={formatMoney(pnl, 2)}
          sub={`${summary?.trade_count ?? 0} closed · ${summary?.rejection_count ?? 0} rejected`}
          tone={pnl > 0 ? "positive" : pnl < 0 ? "negative" : "neutral"}
        />
        <IndicatorCard
          label={`${summary?.underlying ?? "BTC"} Spot`}
          value={
            health?.spot_ltp
              ? Number(health.spot_ltp).toLocaleString("en-US", { maximumFractionDigits: 2 })
              : "—"
          }
          sub={summary?.spot_vs_vwap ? `${summary.spot_vs_vwap} VWAP` : undefined}
          tone="accent"
          spark={spotHistory.current}
        />
        <IndicatorCard
          label="Account Equity"
          value={formatMoney(equity)}
          sub={`Realized ${formatMoney(pnl, 2)} + Unrealized ${formatMoney(unrealized, 2)}`}
          tone={equity >= balance ? "positive" : "negative"}
        />
        <IndicatorCard
          label="Auto Trading"
          value={autoOn ? "ACTIVE" : "PAUSED"}
          sub={
            summary?.kill_switch
              ? "Kill switch ON"
              : summary?.block_reason === "auto_trade_off"
                ? "Manually OFF"
                : summary?.market_open
                  ? `Scanning every ${summary?.scan_interval_seconds ?? 10}s`
                  : "Market closed"
          }
          tone={autoOn ? "positive" : "warning"}
        />
        <IndicatorCard
          label="Data Feed"
          value={feedMode.toUpperCase()}
          sub={summary?.expiry_symbol ? `Expiry ${summary.expiry_symbol}` : "—"}
          tone={feedMode === "websocket" ? "positive" : feedMode === "rest" ? "neutral" : "warning"}
        />
      </section>

      <div className="terminal-grid">
        <div className="panel-stack">
          <div className="card panel">
            <div className="panel-head">
              <h3>
                <Wallet size={14} />
                Paper Account
              </h3>
              <StatusBadge severity="info" label="$250 START" />
            </div>
            <div className="metric-grid compact">
              <div className="metric">
                <span>Available</span>
                <strong className="positive">{formatMoney(available)}</strong>
              </div>
              <div className="metric">
                <span>Used margin</span>
                <strong>{formatMoney(usedMargin)}</strong>
              </div>
              <div className="metric">
                <span>Realized P&L</span>
                <strong className={pnl >= 0 ? "positive" : "negative"}>{formatMoney(pnl, 2)}</strong>
              </div>
              <div className="metric">
                <span>Unrealized</span>
                <strong className={unrealized >= 0 ? "positive" : "negative"}>
                  {formatMoney(unrealized, 2)}
                </strong>
              </div>
            </div>
          </div>

          <div className="card panel">
            <div className="panel-head">
              <h3>
                <Bot size={14} />
                Session
              </h3>
              <StatusBadge
                severity={
                  summary?.bias_5m === "BULLISH"
                    ? "success"
                    : summary?.bias_5m === "BEARISH"
                      ? "critical"
                      : "neutral"
                }
                label={summary?.bias_5m ?? "NEUTRAL"}
              />
            </div>
            <div className="metric-grid compact">
              <div className="metric">
                <span>Candidates</span>
                <strong>{summary?.candidate_count ?? 0}</strong>
              </div>
              <div className="metric">
                <span>Rejections</span>
                <strong>{summary?.rejection_count ?? 0}</strong>
              </div>
              <div className="metric">
                <span>Closed</span>
                <strong>{summary?.trade_count ?? 0}</strong>
              </div>
              <div className="metric">
                <span>Consec. losses</span>
                <strong>{summary?.consecutive_losses ?? 0}</strong>
              </div>
            </div>
            {(summary?.recent_rejections?.length ?? 0) > 0 && (
              <ul className="rejection-list">
                {summary?.recent_rejections?.map((r, i) => (
                  <li key={`${r.tsym}-${i}`}>
                    <span className="mono">{r.tsym}</span>
                    <span className="muted">{r.reasons.join(", ")}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="card panel controls-panel">
            <div className="panel-head">
              <h3>
                <Shield size={14} />
                Controls
              </h3>
              {summary?.auto_trade_enabled ? (
                <StatusBadge severity="success" label="ENTRIES ON" />
              ) : (
                <StatusBadge severity="warning" label="ENTRIES OFF" />
              )}
            </div>

            <div className="controls-block">
              <p className="panel-copy">
                Auto trade · scans every {summary?.scan_interval_seconds ?? 10}s
              </p>
              <div className="panel-actions stacked">
                <button
                  className={`btn btn-sm ${summary?.auto_trade_enabled ? "btn-ghost" : "btn-primary"}`}
                  onClick={toggleAutoTrade}
                  disabled={autoBusy || !summary}
                >
                  <Bot size={14} />
                  {autoBusy
                    ? "Updating…"
                    : summary?.auto_trade_enabled
                      ? "Auto trade OFF"
                      : "Auto trade ON"}
                </button>
                <button
                  className="btn btn-sm btn-ghost"
                  onClick={handleSync}
                  disabled={syncBusy}
                >
                  <Database size={14} />
                  {syncBusy ? "Syncing…" : "Sync missing data"}
                </button>
              </div>
            </div>

            <div className="controls-divider" />

            <div className="controls-block">
              <div className="broker-status-row">
                <span>
                  Broker{" "}
                  {health?.broker_connected ? (
                    <StatusBadge severity="success" label="LIVE" />
                  ) : (
                    <StatusBadge severity="warning" label="OFF" />
                  )}
                </span>
                <span>
                  Token{" "}
                  {session?.valid ? (
                    <StatusBadge severity="success" label="OK" />
                  ) : (
                    <StatusBadge severity="critical" label="BAD" />
                  )}
                </span>
              </div>
              <p className="panel-copy muted mono sm">{session?.user_id ?? "—"}</p>
              <div className="panel-actions stacked">
                <button
                  className="btn btn-primary btn-sm"
                  onClick={handleReauth}
                  disabled={reauthBusy}
                >
                  <KeyRound size={14} />
                  {reauthBusy ? "Refreshing…" : "Re-authenticate"}
                </button>
                <button className="btn btn-ghost btn-sm" onClick={load}>
                  <RefreshCw size={14} />
                  Refresh status
                </button>
              </div>
            </div>

            <div className="controls-divider" />

            <div className="controls-block">
              <p className="panel-copy">Risk · emergency stop</p>
              <div className="panel-actions stacked">
                <button
                  className={`btn btn-sm ${health?.kill_switch ? "btn-ghost" : "btn-danger"}`}
                  onClick={toggleKill}
                  disabled={killBusy || !health}
                >
                  <Power size={14} />
                  {health?.kill_switch ? "Disable kill switch" : "Enable kill switch"}
                </button>
                <button
                  className="btn btn-sm btn-ghost"
                  onClick={handleResetAccount}
                  disabled={resetBusy}
                >
                  <Wallet size={14} />
                  {resetBusy ? "Resetting…" : "Reset paper account"}
                </button>
              </div>
            </div>
          </div>
        </div>

        <div className="watchlist-column">
          <WatchlistPanel watchlist={watchlist} summary={summary} />
        </div>
      </div>

      <TradeBlotterPanel blotter={blotter} />
    </div>
  );
}
