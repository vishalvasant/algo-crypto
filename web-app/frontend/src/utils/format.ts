/** Shared number formatting — en-US locale for USD crypto cockpit. */

export const IST = "Asia/Kolkata";

export function formatIndexPrice(value: number | null | undefined): string {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function formatBasisPoints(
  basis: number | null | undefined,
  limit?: number | null,
): string {
  if (basis == null || Number.isNaN(Number(basis))) return "—";
  const sign = basis >= 0 ? "+" : "";
  const pts = `${sign}${basis.toFixed(1)}`;
  if (limit == null || Number.isNaN(Number(limit))) return pts;
  return `${pts} / ±${Math.round(limit)}`;
}

export function formatCompactCount(value: number | null | undefined): string {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toLocaleString("en-US");
}

export function formatIstClock(now: Date = new Date()): string {
  const date = now.toLocaleDateString("en-IN", {
    timeZone: IST,
    weekday: "short",
    day: "numeric",
    month: "short",
  });
  const time = now.toLocaleTimeString("en-IN", {
    timeZone: IST,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
  });
  return `${date} · ${time}`;
}
