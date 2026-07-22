import { useEffect, useMemo, useRef, useState } from "react";
import type { MarketSummary, Watchlist, WatchlistItem } from "../types";
import { formatPrice } from "../utils/money";

function hasPrice(value: number | null | undefined): boolean {
  return value !== null && value !== undefined && !Number.isNaN(Number(value));
}

function formatOi(value: number | null | undefined) {
  if (value === null || value === undefined) return "–";
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `$${(value / 1_000).toFixed(1)}K`;
  return `$${value.toLocaleString("en-US")}`;
}

function formatIv(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "–";
  return `${Number(value).toFixed(1)}%`;
}

function formatDelta(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "–";
  return Number(value).toFixed(3);
}

function formatTheta(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "–";
  return Number(value).toFixed(2);
}

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

interface StrikeRow {
  strike: number;
  isAtm: boolean;
  lotSize: number;
  contractSize: number;
  underlying: string;
  ce?: WatchlistItem;
  pe?: WatchlistItem;
}

interface WatchlistPanelProps {
  watchlist: Watchlist | null;
  summary?: MarketSummary | null;
}

export function WatchlistPanel({ watchlist, summary }: WatchlistPanelProps) {
  const items = watchlist?.items ?? [];
  const spot = watchlist?.spot_ltp ?? summary?.spot_ltp ?? null;
  const atm = watchlist?.atm_strike ?? summary?.atm_strike ?? null;
  const expiry = watchlist?.expiry_symbol ?? summary?.expiry_symbol ?? null;
  const feedMode = watchlist?.feed_mode ?? summary?.feed_mode ?? "offline";
  const band = watchlist?.strike_band_points ?? 1600;
  const step = watchlist?.strike_step ?? 200;
  const under = watchlist?.underlying ?? summary?.underlying ?? "BTC";
  const contractSize = items[0]?.contract_size ?? (under === "ETH" ? 0.01 : 0.001);
  const feedLabel =
    feedMode === "websocket"
      ? "WebSocket LIVE"
      : feedMode === "rest"
        ? "REST live"
        : "Offline";

  const strikeRows = useMemo(() => {
    const byStrike = new Map<number, StrikeRow>();
    for (const item of items) {
      let row = byStrike.get(item.strike);
      if (!row) {
        row = {
          strike: item.strike,
          isAtm: item.is_atm,
          lotSize: item.lot_size ?? 1,
          contractSize: item.contract_size ?? contractSize,
          underlying: item.underlying ?? under,
          ce: undefined,
          pe: undefined,
        };
        byStrike.set(item.strike, row);
      }
      row.lotSize = item.lot_size ?? row.lotSize;
      row.contractSize = item.contract_size ?? row.contractSize;
      row.isAtm = row.isAtm || item.is_atm;
      if (item.option_type === "CE") row.ce = item;
      if (item.option_type === "PE") row.pe = item;
    }
    return [...byStrike.values()].sort((a, b) => a.strike - b.strike);
  }, [items, contractSize, under]);

  const prevLtp = useRef<Record<string, number | null>>({});
  const [flashTokens, setFlashTokens] = useState<Record<string, number>>({});

  useEffect(() => {
    const nextFlash: Record<string, number> = {};
    for (const item of items) {
      const prev = prevLtp.current[item.token];
      if (prev != null && item.ltp != null && prev !== item.ltp) {
        nextFlash[item.token] = Date.now();
      }
      prevLtp.current[item.token] = item.ltp;
    }
    if (Object.keys(nextFlash).length > 0) {
      setFlashTokens((cur) => ({ ...cur, ...nextFlash }));
      const timer = window.setTimeout(() => {
        setFlashTokens((cur) => {
          const cut = Date.now() - 600;
          const kept: Record<string, number> = {};
          for (const [k, v] of Object.entries(cur)) {
            if (v >= cut) kept[k] = v;
          }
          return kept;
        });
      }, 700);
      return () => window.clearTimeout(timer);
    }
  }, [items]);

  return (
    <div className="card watchlist-card pro">
      <div className="watchlist-header">
        <div>
          <h3>
            {under} Option Chain · {feedLabel}
            {feedMode === "websocket" || feedMode === "rest" ? (
              <span className="live-dot" aria-label="live" />
            ) : null}
          </h3>
          <p className="watchlist-sub">
            Daily {expiry ?? "—"} · ATM ±{band} · step {step} · {strikeRows.length} strikes ·{" "}
            {items.length} contracts · 1 Lot = {contractSize} {under} · prices in USD
          </p>
        </div>
        <div className="watchlist-meta">
          <div>
            <span className="meta-label">Spot</span>
            <span className="meta-value accent">
              {spot != null ? `$${formatPrice(spot)}` : "—"}
            </span>
          </div>
          <div>
            <span className="meta-label">ATM</span>
            <span className="meta-value">
              {atm != null ? atm.toLocaleString("en-US") : "—"}
            </span>
          </div>
          <div>
            <span className="meta-label">Strikes</span>
            <span className="meta-value">{strikeRows.length}</span>
          </div>
          <div>
            <span className="meta-label">Last tick</span>
            <span className="meta-value sm">
              {formatTs(watchlist?.last_quote_ts ?? null)}
            </span>
          </div>
        </div>
      </div>

      {strikeRows.length === 0 ? (
        <div className="empty-state pro">
          <p>Loading option chain…</p>
          <span>Delta symbols · e.g. C-BTC-64400-200726</span>
        </div>
      ) : (
        <div className="watchlist-scroll">
          <table className="watchlist-table pro chain-compact chain-greeks">
            <thead>
              <tr className="chain-group-row">
                <th colSpan={6} className="ce-group">
                  CALLS
                </th>
                <th className="strike-group">Strike</th>
                <th colSpan={6} className="pe-group">
                  PUTS
                </th>
              </tr>
              <tr>
                <th>Symbol</th>
                <th className="num">OI</th>
                <th className="num">IV</th>
                <th className="num">Δ</th>
                <th className="num">θ</th>
                <th className="num">Mark $</th>
                <th className="num strike-head">Strike</th>
                <th className="num">Mark $</th>
                <th className="num">Δ</th>
                <th className="num">θ</th>
                <th className="num">IV</th>
                <th className="num">OI</th>
                <th>Symbol</th>
              </tr>
            </thead>
            <tbody>
              {strikeRows.map((row) => {
                const ceFlash = row.ce && flashTokens[row.ce.token] != null;
                const peFlash = row.pe && flashTokens[row.pe.token] != null;
                return (
                  <tr
                    key={row.strike}
                    className={row.isAtm ? "watchlist-row tradable atm-row" : "watchlist-row"}
                  >
                    <td className="mono muted" title={row.ce?.tsym}>
                      {row.ce?.tsym ?? "—"}
                    </td>
                    <td className="mono num muted">{formatOi(row.ce?.oi)}</td>
                    <td className="mono num muted">{formatIv(row.ce?.iv)}</td>
                    <td className="mono num muted">{formatDelta(row.ce?.delta)}</td>
                    <td className="mono num muted">{formatTheta(row.ce?.theta)}</td>
                    <td
                      className={`mono num ${hasPrice(row.ce?.ltp) ? "ltp" : "muted empty-ltp"}${
                        ceFlash ? " ltp-tick" : ""
                      }`}
                    >
                      {hasPrice(row.ce?.ltp) ? formatPrice(row.ce?.ltp) : "–"}
                    </td>
                    <td className="mono num strike-cell">
                      <strong>{row.strike.toLocaleString("en-US")}</strong>
                      {row.isAtm ? <span className="atm-pill">ATM</span> : null}
                    </td>
                    <td
                      className={`mono num ${hasPrice(row.pe?.ltp) ? "ltp" : "muted empty-ltp"}${
                        peFlash ? " ltp-tick" : ""
                      }`}
                    >
                      {hasPrice(row.pe?.ltp) ? formatPrice(row.pe?.ltp) : "–"}
                    </td>
                    <td className="mono num muted">{formatDelta(row.pe?.delta)}</td>
                    <td className="mono num muted">{formatTheta(row.pe?.theta)}</td>
                    <td className="mono num muted">{formatIv(row.pe?.iv)}</td>
                    <td className="mono num muted">{formatOi(row.pe?.oi)}</td>
                    <td className="mono muted" title={row.pe?.tsym}>
                      {row.pe?.tsym ?? "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
