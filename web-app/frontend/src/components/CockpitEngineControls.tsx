import { Database, KeyRound, Power, RefreshCw, RotateCcw } from "lucide-react";
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
  brokerName?: string;
}

export function CockpitEngineControls({
  onDataRefresh,
  brokerName = "Delta",
}: CockpitEngineControlsProps) {
  const [health, setHealth] = useState<EngineHealth | null>(null);
  const [summary, setSummary] = useState<MarketSummary | null>(null);
  const [killBusy, setKillBusy] = useState(false);
  const [autoBusy, setAutoBusy] = useState(false);
  const [syncBusy, setSyncBusy] = useState(false);
  const [reauthBusy, setReauthBusy] = useState(false);
  const [resetBusy, setResetBusy] = useState(false);

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
    load();
    await onDataRefresh?.();
    window.dispatchEvent(new Event(COCKPIT_REFRESH_EVENT));
  };

  const toggleKill = async () => {
    if (!health) return;
    const enabling = !health.kill_switch;
    if (enabling && !confirm("Enable kill switch? Blocks all new trades.")) return;
    setKillBusy(true);
    try {
      await setKillSwitch(enabling);
      await refreshAll();
    } finally {
      setKillBusy(false);
    }
  };

  const toggleAutoTrade = async () => {
    const currentlyOn = summary?.auto_trade_enabled !== false && !summary?.entries_blocked;
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
    if (!confirm("Reset paper account? Clears mock trades and restores $250.")) return;
    setResetBusy(true);
    try {
      await resetPaperAccount();
      await refreshAll();
    } finally {
      setResetBusy(false);
    }
  };

  const killOn = health?.kill_switch;
  const autoOn = summary?.auto_trade_enabled !== false && !summary?.entries_blocked;

  return (
    <div className="header-engine-controls">
      <button
        type="button"
        className={`header-ctl${autoOn ? " header-ctl-on" : ""}`}
        onClick={toggleAutoTrade}
        disabled={autoBusy || killOn}
        title={autoOn ? "Auto trade ON" : "Auto trade OFF"}
      >
        <Power size={14} />
      </button>
      <button
        type="button"
        className={`header-ctl${killOn ? " header-ctl-danger-on" : ""}`}
        onClick={toggleKill}
        disabled={killBusy}
        title={killOn ? "Kill switch ON" : "Kill switch OFF"}
      >
        <Power size={14} className={killOn ? "spin" : undefined} />
      </button>
      <button type="button" className="header-ctl" onClick={handleSync} disabled={syncBusy} title="Sync data">
        <Database size={14} className={syncBusy ? "spin" : undefined} />
      </button>
      <button type="button" className="header-ctl" onClick={handleReauth} disabled={reauthBusy} title="Re-auth broker">
        <KeyRound size={14} className={reauthBusy ? "spin" : undefined} />
      </button>
      <button type="button" className="header-ctl" onClick={handleReset} disabled={resetBusy} title="Reset paper">
        <RotateCcw size={14} className={resetBusy ? "spin" : undefined} />
      </button>
      <button type="button" className="header-ctl" onClick={() => refreshAll()} title="Refresh">
        <RefreshCw size={14} />
      </button>
    </div>
  );
}
