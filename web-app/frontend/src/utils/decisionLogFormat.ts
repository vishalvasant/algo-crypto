import type { DecisionLogEvent } from "../types";

function humanizeToken(raw: string): string {
  return raw.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function fmtPct(n: unknown): string | null {
  if (n == null || Number.isNaN(Number(n))) return null;
  return `${Math.round(Number(n))}%`;
}

function regimeLabel(m: Record<string, unknown>): string | null {
  const regime = m.regime;
  if (!regime || typeof regime !== "object") return null;
  const primary = (regime as { primary?: string }).primary;
  return primary ? humanizeToken(primary) : null;
}

function scoreLine(m: Record<string, unknown>): string | null {
  const ce = m.ce_score;
  const pe = m.pe_score;
  const nt = m.no_trade_score;
  if (ce == null && pe == null && nt == null) return null;
  const parts = [
    ce != null ? `CE ${ce}` : null,
    pe != null ? `PE ${pe}` : null,
    nt != null ? `No-trade ${nt}` : null,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

function suppressedNote(m: Record<string, unknown>): string | null {
  const n = Number(m.suppressed_duplicate_scans);
  if (!n || Number.isNaN(n)) return null;
  return `${n} identical scan${n === 1 ? "" : "s"} since last log`;
}

export interface HumanDecision {
  title: string;
  summary: string;
  detail?: string;
}

export function formatHumanDecision(ev: DecisionLogEvent): HumanDecision {
  const m = ev.metadata ?? {};
  const ul = m.scan_underlying ? String(m.scan_underlying) : null;

  if (ev.event_type === "strategy_decision") {
    const strategy = String(m.selected_strategy ?? "NO_TRADE");
    const conf = fmtPct(m.confidence);
    const side = m.position_side && m.position_side !== "NONE" ? String(m.position_side) : null;
    const regime = regimeLabel(m);
    const allowed = m.trade_allowed === true;
    const reason = String(m.selected_reason || ev.message || "").trim();
    const prefix = ul ? `${ul} scan` : "Market scan";
    const scores = scoreLine(m);
    const suppressed = suppressedNote(m);
    const execStatus = String(m.execution_status ?? "");

    if (execStatus === "confirm_wait") {
      const wait = String(m.entry_confirm ?? ev.message ?? "confirming");
      return {
        title: `${prefix} · Confirming`,
        summary: humanizeToken(wait),
        detail: [regime ? `Regime: ${regime}` : null, scores, conf ? `Router ${conf}` : null]
          .filter(Boolean)
          .join(" · ") || undefined,
      };
    }

    if (execStatus === "blocked") {
      const blocks = Array.isArray(m.block_reasons)
        ? (m.block_reasons as string[]).map(humanizeToken).join(", ")
        : humanizeToken(String(ev.message || "entry blocked"));
      return {
        title: `${prefix} · Entry blocked`,
        summary: blocks,
        detail: [regime ? `Regime: ${regime}` : null, scores, conf ? `Signal was ${conf}` : null]
          .filter(Boolean)
          .join(" · ") || undefined,
      };
    }

    if (strategy === "NO_TRADE" || !allowed) {
      const why =
        reason && reason !== "NO_TRADE"
          ? humanizeToken(reason)
          : conf
            ? `Confidence ${conf} below entry threshold`
            : "No qualifying setup";
      const detailParts = [
        regime ? `Regime: ${regime}` : null,
        scores,
        suppressed,
      ].filter(Boolean);
      return {
        title: `${prefix} · No trade`,
        summary: why,
        detail: detailParts.length ? detailParts.join(" · ") : undefined,
      };
    }

    const parts = [
      humanizeToken(strategy),
      conf ? `confidence ${conf}` : null,
      side ? `${side} side` : null,
      regime ? `regime ${regime}` : null,
    ].filter(Boolean);

    return {
      title: `${prefix} · Trade signal`,
      summary: parts.join(" · "),
      detail: [reason ? humanizeToken(reason) : null, scores, suppressed]
        .filter(Boolean)
        .join(" · ") || undefined,
    };
  }

  if (ev.event_type === "entry_confirm_wait") {
    const setup = m.setup ? humanizeToken(String(m.setup)) : "Setup";
    const side = m.side ? String(m.side) : "";
    const conf = fmtPct(m.confidence);
    return {
      title: `Confirming · ${setup} ${side}`.trim(),
      summary: humanizeToken(String(ev.message || m.entry_confirm || "waiting for scans")),
      detail: conf ? `Router confidence ${conf}` : undefined,
    };
  }

  if (ev.event_type === "entry_skipped") {
    const tsym = m.tsym ? String(m.tsym) : "contract";
    const setup = m.setup ? humanizeToken(String(m.setup)) : "Entry";
    const side = m.side ? String(m.side) : "";
    const reasons = Array.isArray(m.rejection_reasons)
      ? (m.rejection_reasons as string[]).slice(0, 4).map(humanizeToken).join(", ")
      : "";
    const block = reasons
      ? reasons
      : m.block_reason
        ? humanizeToken(String(m.block_reason))
        : ev.message.replace(/^Signal | skipped.*$/gi, "").trim() || "Blocked by risk rules";
    const conf = fmtPct(m.confidence);

    return {
      title: `Entry blocked · ${setup} ${side}`.trim(),
      summary: `${tsym} — ${block}`,
      detail: conf ? `Router confidence ${conf} — signal did not pass validation` : undefined,
    };
  }

  if (ev.event_type === "manual_sync") {
    const u = m.universe as { action?: string; after?: number; expected?: number } | undefined;
    const c = m.candles as { m1_added?: number } | undefined;
    const q = m.quotes as { polled?: number } | undefined;
    return {
      title: "Manual data sync",
      summary: [
        u?.action ? `Universe ${u.action}` : null,
        u?.after != null ? `${u.after} contracts` : null,
        c?.m1_added ? `+${c.m1_added} candles` : null,
        q?.polled != null ? `${q.polled} quotes polled` : null,
      ]
        .filter(Boolean)
        .join(" · "),
      detail: ev.message || undefined,
    };
  }

  return {
    title: humanizeToken(ev.event_type),
    summary: ev.message || "—",
  };
}
