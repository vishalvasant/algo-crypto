import { useEffect, useMemo, useState } from "react";
import { LineChart } from "lucide-react";
import { fetchMarketSummary, fetchTradesToday } from "../api/client";
import type { MarketSummary, Trade } from "../types";
import { formatUsd, formatLotsLabel } from "../utils/money";

function formatTs(ts: string | null | undefined) {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleTimeString("en-US", {
      timeZone: "Asia/Kolkata",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return ts;
  }
}

function formatPrice(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatInr(value: number | null | undefined) {
  return formatUsd(value, { signed: true });
}

function pnlClass(pnl: number) {
  if (pnl > 0) return "positive";
  if (pnl < 0) return "negative";
  return "";
}

function holdLabel(seconds: number | null | undefined) {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s ? `${m}m ${s}s` : `${m}m`;
}

export function TradesPage() {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [summary, setSummary] = useState<MarketSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      const [t, sum]: [Trade[], MarketSummary] = await Promise.all([
        fetchTradesToday(200),
        fetchMarketSummary(),
      ]);
      setTrades(t);
      setSummary(sum);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, []);

  const stats = useMemo(() => {
    const pnls = trades.map((t) => Number(t.pnl));
    const wins = pnls.filter((p) => p > 0);
    const losses = pnls.filter((p) => p <= 0);
    const total = pnls.reduce((s, p) => s + p, 0);
    const unrealized = Number(summary?.unrealized_pnl ?? 0);
    const starting = Number(summary?.starting_capital ?? 250);
    const todayPnl = Number(summary?.today_pnl ?? total);
    return {
      total: todayPnl,
      wins: wins.length,
      losses: losses.length,
      winRate: pnls.length ? (wins.length / pnls.length) * 100 : 0,
      unrealized,
      starting,
      equity: Number(summary?.equity ?? starting + todayPnl + unrealized),
    };
  }, [trades, summary]);

  return (
    <>
      <div className="page-header">
        <h2>P&amp;L / Today</h2>
        <p>
          Today&apos;s closed trades · live equity (capital carries forward) ·
          older days are on Order Book · auto-refresh 10s
        </p>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="pnl-stats-grid">
        <div className="card pnl-stat">
          <span className="pnl-stat-label">Today P&amp;L</span>
          <span className={`pnl-stat-value ${pnlClass(stats.total)}`}>
            {formatInr(stats.total)}
          </span>
        </div>
        <div className="card pnl-stat">
          <span className="pnl-stat-label">Unrealized</span>
          <span className={`pnl-stat-value ${pnlClass(stats.unrealized)}`}>
            {formatInr(stats.unrealized)}
          </span>
        </div>
        <div className="card pnl-stat">
          <span className="pnl-stat-label">Trades</span>
          <span className="pnl-stat-value">
            {trades.length}{" "}
            <span className="muted" style={{ fontSize: "0.85rem", fontWeight: 500 }}>
              ({stats.wins}W / {stats.losses}L)
            </span>
          </span>
        </div>
        <div className="card pnl-stat">
          <span className="pnl-stat-label">Win rate</span>
          <span className="pnl-stat-value">
            {trades.length ? `${stats.winRate.toFixed(0)}%` : "—"}
          </span>
        </div>
        <div className="card pnl-stat">
          <span className="pnl-stat-label">Capital → Equity</span>
          <span className="pnl-stat-value" style={{ fontSize: "1.05rem" }}>
            {formatUsd(stats.starting, { digits: 0 })} →{" "}
            {formatUsd(stats.equity, { digits: 2 })}
          </span>
        </div>
      </div>

      <div className="card" style={{ overflowX: "auto" }}>
        <div className="panel-head" style={{ marginBottom: "0.75rem" }}>
          <h3>Closed trades today</h3>
          <span className="muted">{trades.length} closed</span>
        </div>
        {trades.length === 0 ? (
          <div className="empty-state">
            <LineChart size={32} strokeWidth={1.5} />
            <p>No closed trades today — they will appear here after exits</p>
          </div>
        ) : (
          <table className="trades-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Entry</th>
                <th>Exit</th>
                <th>Hold</th>
                <th>Contract</th>
                <th>Side</th>
                <th>Setup</th>
                <th className="num">Lots</th>
                <th className="num">Entry $</th>
                <th className="num">Exit $</th>
                <th className="num">Gross</th>
                <th className="num">Fees</th>
                <th className="num">Net P&amp;L</th>
                <th>Exit reason</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t, i) => {
                const pnl = Number(t.pnl);
                const gross = Number(t.gross_pnl ?? t.pnl);
                const fees = Number(t.fees_usd ?? 0);
                return (
                  <tr key={t.id}>
                    <td className="muted">{trades.length - i}</td>
                    <td>{formatTs(t.entry_ts)}</td>
                    <td>{formatTs(t.exit_ts)}</td>
                    <td>{holdLabel(t.hold_seconds)}</td>
                    <td className="mono">{t.tsym ?? "—"}</td>
                    <td>{t.side ?? "—"}</td>
                    <td>{t.setup_type}</td>
                    <td className="num">
                      {formatLotsLabel(t.lots ?? t.quantity, t.contract_size)}
                    </td>
                    <td className="num mono">{formatPrice(t.entry_price)}</td>
                    <td className="num mono">{formatPrice(t.exit_price)}</td>
                    <td className={`num mono ${pnlClass(gross)}`}>{formatInr(gross)}</td>
                    <td className="num mono muted">{formatInr(fees)}</td>
                    <td className={`num mono ${pnlClass(pnl)}`}>{formatInr(pnl)}</td>
                    <td>{t.exit_reason}</td>
                  </tr>
                );
              })}
            </tbody>
            <tfoot>
              <tr>
                <td colSpan={12} style={{ textAlign: "right", fontWeight: 600 }}>
                  Session realized net P&amp;L
                </td>
                <td className={`num mono ${pnlClass(stats.total)}`} style={{ fontWeight: 700 }}>
                  {formatInr(stats.total)}
                </td>
                <td />
              </tr>
            </tfoot>
          </table>
        )}
      </div>
    </>
  );
}
