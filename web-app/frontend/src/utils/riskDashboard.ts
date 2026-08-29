import type { MarketSummary } from "../types";

export type RiskDriver = "none" | "margin" | "loss_budget";

export interface RiskDashboardView {
  realizedPnl: number;
  unrealizedPnl: number;
  totalPnl: number;
  lossBudgetPct: number;
  marginDeployPct: number;
  riskPct: number;
  riskDriver: RiskDriver;
  riskLabel: string;
  riskSeverity: "success" | "warning" | "critical";
  hasOpenPosition: boolean;
  entryStatus: string;
}

function riskLabelFor(pct: number) {
  if (pct > 60) return "HIGH RISK";
  if (pct > 30) return "MEDIUM RISK";
  return "LOW RISK";
}

function riskSeverityFor(pct: number): "success" | "warning" | "critical" {
  if (pct > 60) return "critical";
  if (pct > 30) return "warning";
  return "success";
}

function riskDriverLabel(driver: RiskDriver) {
  if (driver === "margin") return "Margin deployed";
  if (driver === "loss_budget") return "Daily loss budget";
  return "Within limits";
}

export function buildRiskDashboard(summary: MarketSummary | null): RiskDashboardView | null {
  if (!summary) return null;

  const dash = summary.risk_dashboard;
  const isLive = summary.trading_mode === "live";
  if (dash) {
    const lossBudgetPct = isLive ? (dash.loss_budget_pct ?? 0) : 0;
    const marginDeployPct = dash.margin_deploy_pct ?? 0;
    const riskPct = Math.max(
      isLive ? (dash.risk_pct ?? 0) : 0,
      marginDeployPct,
    );
    let riskDriver: RiskDriver = dash.risk_driver ?? "none";
    if (!isLive) {
      riskDriver = marginDeployPct > 0 ? "margin" : "none";
    }
    return {
      realizedPnl: dash.realized_pnl ?? 0,
      unrealizedPnl: dash.unrealized_pnl ?? 0,
      totalPnl: dash.total_pnl ?? 0,
      lossBudgetPct,
      marginDeployPct,
      riskPct,
      riskDriver,
      riskLabel: riskLabelFor(riskPct),
      riskSeverity: riskSeverityFor(riskPct),
      hasOpenPosition: dash.has_open_position ?? Boolean(summary.has_open_position),
      entryStatus: dash.entry_status ?? "flat_scanning",
    };
  }

  const brokerRealized = summary.broker_realized_pnl ?? summary.today_realized_pnl ?? 0;
  const brokerUnrealized = summary.broker_unrealized_pnl ?? summary.unrealized_pnl ?? 0;
  const unrealized = isLive ? brokerUnrealized : (summary.unrealized_pnl ?? 0);
  const realized = isLive
    ? brokerRealized
    : (summary.today_pnl ?? 0);
  const totalPnl = isLive
    ? (summary.broker_day_pnl ?? summary.today_pnl ?? brokerRealized + brokerUnrealized)
    : realized + unrealized;

  const usedMargin = summary.used_margin ?? summary.deployed_capital ?? 0;
  const equity =
    summary.equity ??
    (isLive
      ? (summary.available_capital ?? 0) + usedMargin
      : (summary.starting_capital ?? 0) + realized + unrealized);
  const maxLoss = isLive ? (summary.max_daily_loss ?? 0) : 0;
  const deployCapPct = summary.max_deployed_pct_of_equity ?? 90;
  const deployCap = equity * (deployCapPct / 100);
  const marginDeployPct =
    deployCap > 0
      ? Math.min(100, Math.round((usedMargin / deployCap) * 100))
      : equity > 0
        ? Math.min(100, Math.round((usedMargin / equity) * 100))
        : 0;
  const lossBudgetPct =
    isLive && maxLoss > 0 && totalPnl < 0
      ? Math.min(100, Math.round((Math.abs(totalPnl) / maxLoss) * 100))
      : 0;
  const riskPct = Math.max(marginDeployPct, lossBudgetPct);
  let riskDriver: RiskDriver = "none";
  if (riskPct > 0) {
    riskDriver = marginDeployPct >= lossBudgetPct ? "margin" : "loss_budget";
  }

  const hasOpenPosition = Boolean(
    summary.has_open_position ?? (summary.open_positions?.length ?? 0) > 0,
  );
  let entryStatus = "flat_scanning";
  if (hasOpenPosition) entryStatus = "in_position";
  else if (summary.kill_switch) entryStatus = "kill_switch";
  else if (summary.entries_blocked) entryStatus = "entries_blocked";

  return {
    realizedPnl: realized,
    unrealizedPnl: unrealized,
    totalPnl,
    lossBudgetPct,
    marginDeployPct,
    riskPct,
    riskDriver,
    riskLabel: riskLabelFor(riskPct),
    riskSeverity: riskSeverityFor(riskPct),
    hasOpenPosition,
    entryStatus,
  };
}

export function riskDriverCaption(driver: RiskDriver, riskPct: number) {
  if (riskPct <= 0) return "No active risk pressure";
  return `${riskDriverLabel(driver)} · ${riskPct}%`;
}

export function entryStatusLabel(status: string) {
  switch (status) {
    case "in_position":
      return "Managing open position";
    case "kill_switch":
      return "Kill switch — entries blocked";
    case "entries_blocked":
      return "Daily risk brake — entries blocked";
    case "flat_scanning":
    default:
      return "Flat — scanning for entries";
  }
}
