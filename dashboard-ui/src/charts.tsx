import { clock } from "./format";

interface Point {
  at: string;
  value: number | null;
}

/** Split a series into runs without gaps, so a missing reading breaks the line. */
function runs(points: Point[]): { x: number; y: number }[][] {
  const out: { x: number; y: number }[][] = [];
  let current: { x: number; y: number }[] = [];
  points.forEach((p, i) => {
    if (p.value === null) {
      if (current.length) out.push(current);
      current = [];
    } else {
      current.push({ x: i, y: p.value });
    }
  });
  if (current.length) out.push(current);
  return out;
}

export function DriftPulse({ points, threshold }: { points: Point[]; threshold: number | null }) {
  const values = points.map((p) => p.value).filter((v): v is number => v !== null);
  if (values.length === 0) {
    return <div className="empty">No drift readings in this period yet.</div>;
  }
  const top = Math.max(...values, threshold ?? 0) * 1.25 || 1;
  const W = 640;
  const H = 220;
  const floor = 200;
  const ceiling = 20;
  const x = (i: number) => (i / Math.max(1, points.length - 1)) * W;
  const y = (v: number) => floor - (v / top) * (floor - ceiling);
  const peak = values.indexOf(Math.max(...values));
  const peakIndex = points.findIndex((p) => p.value === values[peak]);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height="220" role="img" aria-label="Highest drift score per time slot" style={{ marginTop: 14 }}>
      {[40, 100, 160].map((gy) => (
        <line key={gy} x1="0" y1={gy} x2={W} y2={gy} stroke="#1f2733" />
      ))}
      {runs(points).map((run, i) => {
        const line = run.map((p, j) => `${j === 0 ? "M" : "L"}${x(p.x).toFixed(1)} ${y(p.y).toFixed(1)}`).join(" ");
        const area = `${line} L${x(run[run.length - 1].x).toFixed(1)} ${floor} L${x(run[0].x).toFixed(1)} ${floor} Z`;
        return (
          <g key={i}>
            <path d={area} fill="#9b8cff" fillOpacity="0.16" />
            <path d={line} fill="none" stroke="#9b8cff" strokeWidth="2.5" strokeLinejoin="round" />
          </g>
        );
      })}
      {threshold !== null && (
        <g>
          <line x1="0" y1={y(threshold)} x2={W} y2={y(threshold)} stroke="#ff6b6b" strokeDasharray="6 6" />
          <text x={W - 8} y={y(threshold) - 8} textAnchor="end" fill="#ff8f8f" fontSize="11" fontFamily="JetBrains Mono">
            alert at drift {threshold}
          </text>
        </g>
      )}
      {threshold !== null && values[peak] > threshold && (
        <circle cx={x(peakIndex)} cy={y(values[peak])} r="5" fill="#ff6b6b" />
      )}
      <text x="0" y="216" fill="#8b97a9" fontSize="11" fontFamily="JetBrains Mono">{clock(points[0].at)}</text>
      <text x={W} y="216" textAnchor="end" fill="#8b97a9" fontSize="11" fontFamily="JetBrains Mono">{clock(points[points.length - 1].at)}</text>
    </svg>
  );
}

export function EvidenceChart({
  name,
  points,
  threshold,
  baseline,
  detectedAt,
  hours,
}: {
  name: string;
  points: { at: string; value: number }[];
  threshold: number | null;
  baseline: number | null;
  detectedAt: string;
  hours: number;
}) {
  if (points.length < 2) {
    return <div className="empty">Not enough stored readings of {name} to draw a chart.</div>;
  }
  const W = 640;
  const H = 210;
  const t0 = Date.parse(points[0].at);
  const t1 = Date.parse(points[points.length - 1].at);
  const span = Math.max(1, t1 - t0);
  const vals = points.map((p) => p.value);
  const marks = [...vals, ...(threshold !== null ? [threshold] : []), ...(baseline !== null ? [baseline] : [])];
  let lo = Math.min(...marks);
  let hi = Math.max(...marks);
  if (hi === lo) {
    hi += 1;
    lo -= 1;
  }
  const pad = (hi - lo) * 0.2;
  lo -= pad;
  hi += pad;
  const x = (t: number) => ((t - t0) / span) * W;
  const y = (v: number) => 190 - ((v - lo) / (hi - lo)) * 150;
  const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(Date.parse(p.at)).toFixed(1)} ${y(p.value).toFixed(1)}`).join(" ");
  const det = Math.min(W, Math.max(0, x(Date.parse(detectedAt))));
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height="210" role="img" aria-label={`${name} over the last ${hours} hours, with the moment the incident was opened`} style={{ marginTop: 14 }}>
      {[40, 100, 160].map((gy) => (
        <line key={gy} x1="0" y1={gy} x2={W} y2={gy} stroke="#1f2733" />
      ))}
      <rect x={det} y="20" width={Math.max(0, W - det)} height="170" fill="#ff6b6b" fillOpacity="0.08" />
      {baseline !== null && (
        <g>
          <line x1="0" y1={y(baseline)} x2={W} y2={y(baseline)} stroke="#8b97a9" strokeWidth="1.5" strokeDasharray="6 6" />
          <text x="8" y={y(baseline) + 14} fill="#a9b4c4" fontSize="11" fontFamily="JetBrains Mono">baseline</text>
        </g>
      )}
      {threshold !== null && (
        <g>
          <line x1="0" y1={y(threshold)} x2={W} y2={y(threshold)} stroke="#ff6b6b" strokeDasharray="6 6" />
          <text x="8" y={y(threshold) - 6} fill="#ff8f8f" fontSize="11" fontFamily="JetBrains Mono">alert limit</text>
        </g>
      )}
      <path d={line} fill="none" stroke="#9b8cff" strokeWidth="2.5" strokeLinejoin="round" />
      <text x="8" y="16" fill="#c9c1ff" fontSize="11" fontFamily="JetBrains Mono">{name}</text>
      {det < W - 60 && (
        <text x={det + 8} y="38" fill="#ff8f8f" fontSize="11" fontFamily="JetBrains Mono">since detection</text>
      )}
      <text x="0" y="206" fill="#8b97a9" fontSize="11" fontFamily="JetBrains Mono">{clock(points[0].at)}</text>
      <text x={W} y="206" textAnchor="end" fill="#8b97a9" fontSize="11" fontFamily="JetBrains Mono">{clock(points[points.length - 1].at)}</text>
    </svg>
  );
}
