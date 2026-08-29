interface SparklineProps {
  values: number[];
  secondary?: number[];
  width?: number;
  height?: number;
  className?: string;
  showArea?: boolean;
}

function seriesPoints(values: number[], width: number, height: number, min: number, range: number) {
  return values.map((v, i) => {
    const x = values.length === 1 ? width / 2 : (i / (values.length - 1)) * width;
    const y = height - ((v - min) / range) * (height - 4) - 2;
    return { x, y };
  });
}

export function Sparkline({
  values,
  secondary,
  width = 120,
  height = 36,
  className = "",
  showArea = false,
}: SparklineProps) {
  const primary = values.filter((v) => Number.isFinite(v));
  const secondaryVals = (secondary ?? []).filter((v) => Number.isFinite(v));

  if (primary.length < 2 && secondaryVals.length < 2) {
    return (
      <svg width={width} height={height} className={`sparkline ${className}`} aria-hidden>
        <line x1={0} y1={height / 2} x2={width} y2={height / 2} className="sparkline-flat" />
      </svg>
    );
  }

  const all = [...primary, ...secondaryVals];
  const min = Math.min(...all);
  const max = Math.max(...all);
  const range = max - min || 1;
  const pts = seriesPoints(primary.length ? primary : secondaryVals, width, height, min, range);
  const up = pts.length >= 2 ? pts[pts.length - 1].y <= pts[0].y : true;
  const poly = pts.map((p) => `${p.x},${p.y}`).join(" ");
  const area =
    showArea && pts.length >= 2
      ? `M ${pts[0].x} ${height} L ${pts.map((p) => `${p.x} ${p.y}`).join(" L ")} L ${pts[pts.length - 1].x} ${height} Z`
      : null;

  const secPts =
    secondaryVals.length >= 2
      ? seriesPoints(secondaryVals, width, height, min, range)
          .map((p) => `${p.x},${p.y}`)
          .join(" ")
      : null;

  return (
    <svg
      width={width}
      height={height}
      className={`sparkline ${up ? "up" : "down"} ${className}`}
      aria-hidden
    >
      {area ? <path d={area} className="sparkline-area" /> : null}
      {secPts ? (
        <polyline points={secPts} fill="none" strokeWidth="1.25" className="sparkline-secondary" />
      ) : null}
      {primary.length >= 2 ? (
        <polyline points={poly} fill="none" strokeWidth="1.6" className="sparkline-primary" />
      ) : null}
    </svg>
  );
}
