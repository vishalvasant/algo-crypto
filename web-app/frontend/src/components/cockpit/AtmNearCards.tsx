import { useEffect, useMemo, useRef, useState } from "react";
import { BarChart3 } from "lucide-react";
import type { MarketSummary, Watchlist, WatchlistItem } from "../../types";
import { useCommodityChain } from "../../hooks/useCommodityChain";
import { describeFeedMode } from "../../utils/feedMode";
import { formatCompactCount, formatIndexPrice } from "../../utils/format";
import { Sparkline } from "../Sparkline";

const ATM_CARD_MAX_OFFSET = 2;
const ATM_CARD_OFFSETS = Array.from(
  { length: ATM_CARD_MAX_OFFSET * 2 + 1 },
  (_, i) => ATM_CARD_MAX_OFFSET - i,
);
const SPARK_POINTS = 28;

type TokenSpark = {
  prices: number[];
  vwaps: number[];
  cumPv: number;
  cumVol: number;
};

function formatPrice(value: number | null | undefined) {
  return formatIndexPrice(value);
}

function formatOi(value: number | null | undefined) {
  return formatCompactCount(value);
}

function formatIv(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "–";
  return `${Number(value).toFixed(1)}%`;
}

function formatSpread(bid: number | null | undefined, ask: number | null | undefined) {
  if (bid == null || ask == null || Number.isNaN(Number(bid)) || Number.isNaN(Number(ask))) {
    return null;
  }
  const mid = (Number(bid) + Number(ask)) / 2;
  if (mid <= 0) return null;
  return ((Number(ask) - Number(bid)) / mid) * 100;
}

function offsetLabel(offset: number): string {
  if (offset === 0) return "ATM";
  return offset > 0 ? `ATM+${offset}` : `ATM${offset}`;
}

interface AtmNearCardsProps {
  watchlist: Watchlist | null;
  summary?: MarketSummary | null;
  activeCommodity: string;
  selectedToken?: string | null;
  onSelectOption?: (item: WatchlistItem | null) => void;
}

function SideCard({
  side,
  item,
  flash,
  prices,
  vwaps,
  selected,
  onSelect,
}: {
  side: "CE" | "PE";
  item?: WatchlistItem;
  flash?: { dir: "up" | "down"; at: number };
  prices: number[];
  vwaps: number[];
  selected?: boolean;
  onSelect?: () => void;
}) {
  const spreadPct = formatSpread(item?.bid, item?.ask);
  const lastVwap = vwaps.length ? vwaps[vwaps.length - 1] : null;
  const ltp = item?.ltp != null ? Number(item.ltp) : null;
  const vsVwap =
    ltp != null && lastVwap != null && Number.isFinite(lastVwap)
      ? ltp - lastVwap
      : null;

  const ltpClass = [
    "atm-near-ltp",
    "mono",
    flash ? `ltp-tick ltp-tick-${flash.dir}` : "",
    item?.ltp == null ? "muted" : "",
  ]
    .filter(Boolean)
    .join(" ");

  const clickable = Boolean(item?.token && onSelect);

  return (
    <div
      className={[
        "atm-near-side-card",
        `side-${side.toLowerCase()}`,
        selected ? "is-selected" : "",
        clickable ? "is-clickable" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
      aria-pressed={clickable ? Boolean(selected) : undefined}
      onClick={clickable ? onSelect : undefined}
      onKeyDown={
        clickable
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onSelect?.();
              }
            }
          : undefined
      }
    >
      <div className="atm-near-card-top">
        <span className={`atm-near-side side-${side.toLowerCase()}`}>{side}</span>
        {spreadPct != null ? (
          <span className="atm-near-spread mono muted" title="Bid–ask spread">
            {spreadPct.toFixed(1)}% spr
          </span>
        ) : null}
      </div>

      <div className="atm-near-price-row">
        <div className={ltpClass}>{formatPrice(item?.ltp)}</div>
        <div className="atm-card-spark" title="Premium LTP vs tick VWAP">
          <Sparkline
            values={prices}
            secondary={vwaps}
            width={88}
            height={28}
            showArea
            className={`atm-card-vwap-spark side-${side.toLowerCase()}`}
          />
        </div>
      </div>

      <div className="atm-near-vwap-row mono">
        <span className="muted">VWAP</span>
        <strong className="accent">{formatPrice(lastVwap)}</strong>
        {vsVwap != null ? (
          <span className={vsVwap >= 0 ? "positive" : "negative"}>
            {vsVwap >= 0 ? "+" : ""}
            {vsVwap.toFixed(2)}
          </span>
        ) : (
          <span className="muted">—</span>
        )}
      </div>

      <div className="atm-near-ba mono muted">
        <span>B {formatPrice(item?.bid)}</span>
        <span>A {formatPrice(item?.ask)}</span>
      </div>
      <div className="atm-near-meta">
        <span>
          OI <strong className="mono">{formatOi(item?.oi)}</strong>
        </span>
        <span>
          IV <strong className="mono">{formatIv(item?.iv)}</strong>
        </span>
        <span>
          Δ{" "}
          <strong className="mono">
            {item?.delta != null ? Number(item.delta).toFixed(3) : "–"}
          </strong>
        </span>
        <span>
          Vol <strong className="mono">{formatOi(item?.volume)}</strong>
        </span>
      </div>
      {item?.tsym ? (
        <div className="atm-near-tsym mono muted" title={item.tsym}>
          {item.tsym}
        </div>
      ) : (
        <div className="atm-near-tsym mono muted">Waiting for quote…</div>
      )}
    </div>
  );
}

export function AtmNearCards({
  watchlist,
  summary,
  activeCommodity,
  selectedToken = null,
  onSelectOption,
}: AtmNearCardsProps) {
  const { selected, items, isRefreshing } = useCommodityChain(watchlist, summary, activeCommodity);
  const spot = selected?.spot_ltp ?? selected?.trading_spot_ltp ?? summary?.spot_ltp ?? null;
  const atm = selected?.atm_strike ?? null;
  const step = selected?.strike_step ?? watchlist?.strike_step ?? 50;
  const displayName = selected?.display_name ?? selected?.underlying ?? activeCommodity;
  const feedMode = watchlist?.feed_mode ?? summary?.feed_mode ?? "offline";
  const feed = describeFeedMode(
    feedMode,
    watchlist?.ws_open ?? summary?.ws_open,
    watchlist?.ws_quote_age_sec ?? summary?.ws_quote_age_sec,
    watchlist?.quote_age_sec ?? summary?.quote_age_sec,
  );
  const feedLabel = feed.label;
  const sessionVwap = summary?.session_vwap ?? summary?.levels?.vwap ?? null;
  const spotVsVwap = summary?.spot_vs_vwap ?? null;

  const byKey = useMemo(() => {
    const map = new Map<string, WatchlistItem>();
    for (const item of items) {
      map.set(`${item.strike}:${item.option_type}`, item);
    }
    return map;
  }, [items]);

  const rows = useMemo(() => {
    if (atm == null) return [];
    return ATM_CARD_OFFSETS.map((offset) => {
      const strike = atm + offset * step;
      return {
        offset,
        strike,
        ce: byKey.get(`${strike}:CE`),
        pe: byKey.get(`${strike}:PE`),
      };
    });
  }, [atm, step, byKey]);

  const flatItems = useMemo(
    () => rows.flatMap((r) => [r.ce, r.pe].filter(Boolean) as WatchlistItem[]),
    [rows],
  );

  const sparkRef = useRef<Map<string, TokenSpark>>(new Map());
  const [sparkSnap, setSparkSnap] = useState<Record<string, { prices: number[]; vwaps: number[] }>>(
    {},
  );

  useEffect(() => {
    sparkRef.current = new Map();
    setSparkSnap({});
  }, [activeCommodity]);

  useEffect(() => {
    let changed = false;
    const nextSnap: Record<string, { prices: number[]; vwaps: number[] }> = {};

    for (const [token, hist] of sparkRef.current.entries()) {
      nextSnap[token] = { prices: [...hist.prices], vwaps: [...hist.vwaps] };
    }

    for (const item of flatItems) {
      if (item.ltp == null || Number.isNaN(Number(item.ltp))) continue;
      const price = Number(item.ltp);
      const vol = Number(item.volume ?? 0);
      const weight = vol > 0 ? Math.max(1, Math.min(vol / 1000, 25)) : 1;

      let hist = sparkRef.current.get(item.token);
      if (!hist) {
        hist = { prices: [price], vwaps: [price], cumPv: price * weight, cumVol: weight };
        sparkRef.current.set(item.token, hist);
        nextSnap[item.token] = { prices: [...hist.prices], vwaps: [...hist.vwaps] };
        changed = true;
        continue;
      }

      const last = hist.prices[hist.prices.length - 1];
      if (last === price) continue;

      hist.cumPv += price * weight;
      hist.cumVol += weight;
      hist.prices.push(price);
      hist.vwaps.push(hist.cumPv / hist.cumVol);
      if (hist.prices.length > SPARK_POINTS) {
        hist.prices = hist.prices.slice(-SPARK_POINTS);
        hist.vwaps = hist.vwaps.slice(-SPARK_POINTS);
      }
      nextSnap[item.token] = { prices: [...hist.prices], vwaps: [...hist.vwaps] };
      changed = true;
    }

    if (changed) setSparkSnap(nextSnap);
  }, [flatItems]);

  const prevLtp = useRef<Record<string, number>>({});
  const [ltpFlash, setLtpFlash] = useState<Record<string, { dir: "up" | "down"; at: number }>>({});

  useEffect(() => {
    prevLtp.current = {};
    setLtpFlash({});
  }, [activeCommodity]);

  useEffect(() => {
    const next: Record<string, { dir: "up" | "down"; at: number }> = {};
    const now = Date.now();
    for (const item of flatItems) {
      if (item.ltp == null || Number.isNaN(Number(item.ltp))) continue;
      const price = Number(item.ltp);
      const prev = prevLtp.current[item.token];
      if (prev != null && price !== prev) {
        next[item.token] = { dir: price > prev ? "up" : "down", at: now };
      }
      prevLtp.current[item.token] = price;
    }
    if (Object.keys(next).length === 0) return;
    setLtpFlash((cur) => ({ ...cur, ...next }));
    const timer = window.setTimeout(() => {
      setLtpFlash((cur) => {
        const cut = Date.now() - 650;
        const kept: Record<string, { dir: "up" | "down"; at: number }> = {};
        for (const [k, v] of Object.entries(cur)) {
          if (v.at >= cut) kept[k] = v;
        }
        return kept;
      });
    }, 700);
    return () => window.clearTimeout(timer);
  }, [flatItems]);

  const vsClass =
    spotVsVwap?.toLowerCase() === "above"
      ? "positive"
      : spotVsVwap?.toLowerCase() === "below"
        ? "negative"
        : "";

  const indexVsVwap =
    spot != null && sessionVwap != null
      ? `${spot - sessionVwap >= 0 ? "+" : ""}${(spot - sessionVwap).toFixed(1)}`
      : spotVsVwap ?? "—";

  return (
    <section className={`cockpit-panel atm-near-panel${isRefreshing ? " atm-near-panel--stale" : ""}`}>
      <header className="cockpit-panel-head atm-near-head">
        <BarChart3 size={14} strokeWidth={2} />
        <h3>Near ATM · {displayName}</h3>
        <span className="watchlist-feed-tag">{feedLabel}</span>
        {feedMode === "websocket" || feedMode === "rest" ? (
          <span className="live-dot" aria-label="live" />
        ) : null}
        <span className="logs-range-pill mono muted">
          ATM ±{ATM_CARD_MAX_OFFSET}
          {atm != null ? ` · ${atm.toLocaleString("en-IN")}` : ""}
          {sessionVwap != null ? (
            <>
              {" · "}
              idx VWAP {formatPrice(sessionVwap)}{" "}
              <span className={vsClass}>({indexVsVwap})</span>
            </>
          ) : null}
        </span>
      </header>

      {rows.length === 0 ? (
        <p className="blotter-empty decision-log-empty">
          Loading ATM band… · WebSocket live · REST fallback
        </p>
      ) : (
        <div className="atm-near-stack">
          <div className="atm-near-col-labels">
            <span>CALL (CE)</span>
            <span>Strike</span>
            <span>PUT (PE)</span>
          </div>
          {rows.map((row) => {
            const ceSpark = row.ce?.token ? sparkSnap[row.ce.token] : undefined;
            const peSpark = row.pe?.token ? sparkSnap[row.pe.token] : undefined;
            return (
              <div
                key={row.strike}
                className={`atm-near-row${row.offset === 0 ? " is-atm" : ""}`}
              >
                <SideCard
                  side="CE"
                  item={row.ce}
                  flash={row.ce?.token ? ltpFlash[row.ce.token] : undefined}
                  prices={ceSpark?.prices ?? (row.ce?.ltp != null ? [Number(row.ce.ltp)] : [])}
                  vwaps={ceSpark?.vwaps ?? (row.ce?.ltp != null ? [Number(row.ce.ltp)] : [])}
                  selected={Boolean(row.ce?.token && selectedToken === row.ce.token)}
                  onSelect={
                    onSelectOption && row.ce?.token
                      ? () =>
                          onSelectOption(
                            selectedToken === row.ce!.token ? null : row.ce!,
                          )
                      : undefined
                  }
                />
                <div className="atm-near-strike-cell">
                  <span className="atm-near-offset mono">{offsetLabel(row.offset)}</span>
                  <strong className="atm-near-strike mono">
                    {row.strike.toLocaleString("en-IN")}
                  </strong>
                  {spot != null ? (
                    <span className="atm-near-dist mono muted">
                      {row.strike - spot >= 0 ? "+" : ""}
                      {(row.strike - spot).toFixed(0)}
                    </span>
                  ) : null}
                </div>
                <SideCard
                  side="PE"
                  item={row.pe}
                  flash={row.pe?.token ? ltpFlash[row.pe.token] : undefined}
                  prices={peSpark?.prices ?? (row.pe?.ltp != null ? [Number(row.pe.ltp)] : [])}
                  vwaps={peSpark?.vwaps ?? (row.pe?.ltp != null ? [Number(row.pe.ltp)] : [])}
                  selected={Boolean(row.pe?.token && selectedToken === row.pe.token)}
                  onSelect={
                    onSelectOption && row.pe?.token
                      ? () =>
                          onSelectOption(
                            selectedToken === row.pe!.token ? null : row.pe!,
                          )
                      : undefined
                  }
                />
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
