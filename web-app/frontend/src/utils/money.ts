/** USD money helpers for Algo-Crypto (Delta Exchange). */

export function formatUsd(
  value: number | null | undefined,
  opts?: { digits?: number; signed?: boolean },
) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  const digits = opts?.digits ?? 2;
  const body = Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  if (opts?.signed) {
    const sign = n > 0 ? "+" : n < 0 ? "−" : "";
    return `${sign}$${body}`;
  }
  return n < 0 ? `−$${body}` : `$${body}`;
}

export function formatPrice(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function formatLotsLabel(lots: number | null | undefined, contractSize?: number | null, underlying?: string) {
  if (lots == null) return "—";
  const size = contractSize ?? 0.001;
  const u = underlying ?? "BTC";
  const qty = lots * size;
  return `${lots} lot${lots === 1 ? "" : "s"} · ${qty.toFixed(3)} ${u}`;
}
