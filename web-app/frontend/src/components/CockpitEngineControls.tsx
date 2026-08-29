import {
  Bot,
  Database,
  KeyRound,
  LogOut,
  Power,
  RefreshCw,
  RotateCcw,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  fetchHealth,
  fetchMarketSummary,
  reauthenticate,
  resetPaperAccount,
  setAutoTrade,
  setKillSwitch,
  syncMissingData,
} from "../api/client";
import type { EngineHealth, MarketSummary } from "../types";

export const COCKPIT_REFRESH_EVENT = "algocrypto:cockpit-refresh";

interface CockpitEngineControlsProps {
  onDataRefresh?: () => void | Promise<void>;
  onLogout?: () => void | Promise<void>;
  brokerName?: string;
}

export function CockpitEngineControls({
  onDataRefresh,
  onLogout,
  brokerName = "Delta",
}: CockpitEngineControlsProps) {
  const [health, setHealth] = useState<EngineHealth | null>(null);
  const [summary, setSummary] = useState<MarketSummary | null>(null);
  const [killBusy, setKillBusy] = useState(false);
  const [autoBusy, setAutoBusy] = useState(false);
  const [syncBusy, setSyncBusy] = useState(false);
  const [refreshBusy, setRefreshBusy] = useState(false);
  const [reauthBusy, setReauthBusy] = useState(false);
  const [logoutBusy, setLogoutBusy] = useState(false);
  const [resetBusy, setResetBusy] = useState(false);

  const executionMode = summary?.trading_mode ?? health?.trading_mode ?? "paper";
  const isLiveMode = executionMode === "live";

  const load = useCallback(() => {
    fetchHealth().then(setHealth).catch(() => setHealth(null));
    fetchMarketSummary().then(setSummary).catch(() => setSummary(null));
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [load]);

  const refreshAll = async () => {
    setRefreshBusy(true);
    try {
      await onDataRefresh?.();
      window.dispatchEvent(new Event(COCKPIT_REFRESH_EVENT));
      load();
    } finally {
      setRefreshBusy(false);
    }
  };

  const toggleKill = async () => {
    if (!health) return;
    const enabling = !health.kill_switch;
    if (enabling && !confirm("Enable kill switch? This blocks all new trades.")) return;
    setKillBusy(true);
    try {
      await setKillSwitch(enabling);
      await refreshAll();
    } finally {
      setKillBusy(false);
    }
  };

  const toggleAutoTrade = async () => {
    const currentlyOn =
      summary?.auto_trade_enabled !== false && !summary?.entries_blocked;
    const enabling = !currentlyOn;
    if (!enabling && !confirm("Turn OFF auto trading?")) return;
    setAutoBusy(true);
    try {
      await setAutoTrade(enabling);
      await refreshAll();
    } finally {
      setAutoBusy(false);
    }
  };

  const handleSync = async () => {
    setSyncBusy(true);
    try {
      await syncMissingData();
      await refreshAll();
    } finally {
      setSyncBusy(false);
    }
  };

  const handleReauth = async () => {
    if (!confirm(`Re-authenticate with ${brokerName}?`)) return;
    setReauthBusy(true);
    try {
      await reauthenticate(true);
      await refreshAll();
    } finally {
      setReauthBusy(false);
    }
  };

  const handleReset = async () => {
    if (isLiveMode) {
      alert("Switch to paper mode before resetting the paper ledger.");
      return;
    }
    if (
      !confirm(
        "Reset paper account?\n\nClears mock trades and restores starting capital.",
      )
    ) {
      return;
    }
    setResetBusy(true);
    try {
      await resetPaperAccount();
      await refreshAll();
    } finally {
      setResetBusy(false);
    }
  };

  const handleLogout = async () => {
    if (!onLogout) return;
    setLogoutBusy(true);
    try {
      await onLogout();
    } finally {
      setLogoutBusy(false);
    }
  };

  const killOn = Boolean(health?.kill_switch);
  const autoEnabled = summary?.auto_trade_enabled !== false;
  const entriesBlocked = summary?.entries_blocked === true;
  const autoOn = autoEnabled && !entriesBlocked && !killOn;

  const autoTitle = killOn
    ? "Kill switch active — auto trade blocked"
    : entriesBlocked
      ? `Auto trade paused: ${summary?.block_reason?.replace(/_/g, " ") ?? "entries blocked"}`
      : autoEnabled
        ? "Auto trade ON — click to disable"
        : "Auto trade OFF — click to enable";

  return (
    <div className="global-top-engine" role="toolbar" aria-label="Engine controls">
      <span
        className={`header-ctl header-ctl-mode ${isLiveMode ? "live" : "paper"}`}
        title={isLiveMode ? "Live trading mode" : "Paper trading mode"}
        aria-label={isLiveMode ? "Live mode" : "Paper mode"}
      >
        {isLiveMode ? "LIVE" : "PAPER"}
      </span>

      <button
        type="button"
        className={`header-ctl header-ctl-auto${autoOn ? " on" : ""}${entriesBlocked && autoEnabled ? " warn" : ""}`}
        onClick={toggleAutoTrade}
        disabled={autoBusy || killOn || !summary}
        title={autoTitle}
        aria-label={autoTitle}
        aria-pressed={autoOn}
      >
        <Bot size={14} aria-hidden />
        <span>{autoOn ? "AUTO" : "OFF"}</span>
      </button>

      <button
        type="button"
        className={`header-ctl danger${killOn ? " on" : ""}`}
        onClick={toggleKill}
        disabled={killBusy || !health}
        title={killOn ? "Kill switch ON — click to disable" : "Kill switch OFF"}
        aria-label={killOn ? "Kill switch on" : "Kill switch off"}
        aria-pressed={killOn}
      >
        <Power size={14} aria-hidden />
      </button>

      {!isLiveMode ? (
        <button
          type="button"
          className="header-ctl"
          onClick={handleReset}
          disabled={resetBusy}
          title="Reset paper account"
          aria-label="Reset paper account"
        >
          <RotateCcw size={14} className={resetBusy ? "spin" : undefined} aria-hidden />
        </button>
      ) : null}

      <button
        type="button"
        className="header-ctl"
        onClick={handleSync}
        disabled={syncBusy}
        title="Sync missing data"
        aria-label="Sync missing data"
      >
        <Database size={14} className={syncBusy ? "spin" : undefined} aria-hidden />
      </button>

      <button
        type="button"
        className="header-ctl"
        onClick={() => refreshAll()}
        disabled={refreshBusy}
        title="Refresh dashboard"
        aria-label="Refresh dashboard"
      >
        <RefreshCw size={14} className={refreshBusy ? "spin" : undefined} aria-hidden />
      </button>

      <button
        type="button"
        className="header-ctl"
        onClick={handleReauth}
        disabled={reauthBusy}
        title={`Re-authenticate ${brokerName}`}
        aria-label={`Re-authenticate ${brokerName}`}
      >
        <KeyRound size={14} className={reauthBusy ? "spin" : undefined} aria-hidden />
      </button>

      {onLogout ? (
        <button
          type="button"
          className="header-ctl"
          onClick={handleLogout}
          disabled={logoutBusy}
          title="Logout"
          aria-label="Logout"
        >
          <LogOut size={14} aria-hidden />
        </button>
      ) : null}
    </div>
  );
}
