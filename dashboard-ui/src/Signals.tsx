import { useApi } from "./api";
import { SignalChart } from "./charts";
import { ago, clock, RANGE_LABEL } from "./format";
import { RangeSelect } from "./RangeSelect";
import { go, link } from "./router";
import type { Route } from "./router";
import type { CheckTile, HeatRow, SignalItem, SignalsPage } from "./types";
import { Header, Loading, Page, Planned } from "./ui";

const TONE_COLOR: Record<CheckTile["tone"], string> = {
  ok: "var(--ok)",
  warn: "var(--warn)",
  bad: "var(--bad)",
  idle: "var(--muted)",
};
const HEAT = ["#1b2430", "#3b3566", "#7b6bd6", "#f5b73b", "#ff6b6b"];

export function formatSignal(item: Pick<SignalItem, "kind" | "name">, value: number): string {
  if (item.kind === "health") return value >= 1 ? "up" : "down";
  if (item.name.endsWith("_ms")) return `${Math.round(value)} ms`;
  return Math.abs(value) >= 100 ? String(Math.round(value)) : value.toFixed(3);
}

/** Colour steps follow the design: below 0.05, 0.05 to 0.10, 0.10 to the alert level, above it. */
function heatFill(value: number | null, limit: number): string {
  if (value === null) return "transparent";
  if (value < 0.05) return HEAT[0];
  if (value < 0.1) return HEAT[1];
  if (value < limit) return HEAT[3];
  return HEAT[4];
}
function heatText(value: number | null, limit: number): string {
  if (value === null) return "var(--muted)";
  return value >= limit ? "var(--bad)" : value >= 0.1 ? "var(--warn)" : "var(--soft)";
}

function Tile({ tile }: { tile: CheckTile }) {
  const color = TONE_COLOR[tile.tone];
  return (
    <div className="card" style={{ flex: "1 1 220px", padding: "16px 18px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontSize: 13, color: "var(--soft)" }}>{tile.label}</span>
        <span className="mono" style={{ fontSize: 10, color, border: `1px solid ${color}`, borderRadius: 4, padding: "2px 6px" }}>{tile.tag}</span>
      </div>
      <div style={{ marginTop: 10, fontSize: tile.tone === "idle" ? 18 : 24, fontWeight: 700, color: tile.tone === "idle" ? "var(--muted)" : undefined }}>{tile.value}</div>
      <div className="hint" style={{ marginTop: 4 }}>{tile.note}</div>
    </div>
  );
}

function Heatmap({ rows, days, limit, hidden, many }: { rows: HeatRow[]; days: string[]; limit: number; hidden: number; many: boolean }) {
  const legend = [
    { label: "below 0.05", fill: HEAT[0] },
    { label: "0.05 to 0.10", fill: HEAT[1] },
    { label: `0.10 to ${limit}`, fill: HEAT[3] },
    { label: `above ${limit}`, fill: HEAT[4] },
  ];
  return (
    <section className="card" style={{ marginTop: 16, padding: "22px 24px" }}>
      <div style={{ display: "flex", flexWrap: "wrap", justifyContent: "space-between", gap: 12, alignItems: "baseline" }}>
        <h2 style={{ margin: 0 }}>Feature drift, last 14 days</h2>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 14, fontSize: 12, color: "var(--soft)", alignItems: "center" }}>
          {legend.map((l) => (
            <span key={l.label} style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span style={{ width: 14, height: 14, borderRadius: 3, background: l.fill }} />
              {l.label}
            </span>
          ))}
        </div>
      </div>
      {rows.length === 0 ? (
        <div className="empty">No drift readings yet. They show up here once the application reports drift.</div>
      ) : (
        <div style={{ overflowX: "auto", marginTop: 18 }}>
          <div style={{ minWidth: 620, display: "flex", flexDirection: "column", gap: 4 }}>
            {rows.map((r) => (
              <div key={`${r.app}|${r.name}`} style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <div className="mono" style={{ flex: "0 0 190px", fontSize: 12, color: "var(--text)", overflowWrap: "anywhere" }}>
                  {r.name}
                  {many && <span className="hint" style={{ display: "block", fontSize: 10 }}>{r.app}</span>}
                </div>
                <div style={{ flex: 1, display: "grid", gridTemplateColumns: `repeat(${days.length}, minmax(0, 1fr))`, gap: 4 }}>
                  {r.cells.map((v, i) => (
                    <div
                      key={i}
                      title={v === null ? `${clock(days[i])}: no reading` : `${clock(days[i])}: highest ${v}`}
                      style={{ height: 28, borderRadius: 4, background: heatFill(v, limit), border: v === null ? "1px dashed #1f2733" : "none" }}
                    />
                  ))}
                </div>
                <div className="mono" style={{ flex: "0 0 56px", textAlign: "right", fontSize: 12, color: heatText(r.latest, limit) }}>
                  {r.latest === null ? "-" : r.latest.toFixed(2)}
                </div>
              </div>
            ))}
            <div style={{ display: "flex", gap: 12 }}>
              <div style={{ flex: "0 0 190px" }} />
              <div className="mono" style={{ flex: 1, display: "flex", justifyContent: "space-between", fontSize: 11, color: "var(--muted)" }}>
                <span>14 days ago</span>
                <span>today</span>
              </div>
              <div style={{ flex: "0 0 56px" }} />
            </div>
          </div>
        </div>
      )}
      <div className="hint" style={{ marginTop: 12, lineHeight: 1.6 }}>
        Each square is the highest drift reading of that day. A dashed square means no reading was stored that day.
        {hidden > 0 ? ` ${hidden} more signals are not shown.` : ""}
      </div>
    </section>
  );
}

function SignalCard({ item, now, many }: { item: SignalItem; now: string; many: boolean }) {
  const health = item.kind === "health";
  const bad = item.breaches > 0;
  return (
    <div className="card" style={{ flex: "1 1 420px", minWidth: 0 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "baseline" }}>
        <h2 style={{ margin: 0 }}>{item.label}</h2>
        {many && <span className="mono hint">{item.app}</span>}
      </div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginTop: 10, flexWrap: "wrap" }}>
        <span style={{ fontSize: 28, fontWeight: 700 }}>{formatSignal(item, item.latest)}</span>
        <span className="hint">latest, {ago(now, item.latest_at)}</span>
        <span className="tag plain" style={{ marginLeft: "auto", color: bad ? "var(--bad)" : item.threshold === null && !health ? "var(--muted)" : "var(--ok)" }}>
          {health
            ? bad ? `DOWN IN ${item.breaches} PERIODS` : "ALWAYS UP"
            : item.threshold === null ? "NO LIMIT SET" : bad ? `OVER THE LIMIT IN ${item.breaches} PERIODS` : "WITHIN THE LIMIT"}
        </span>
      </div>
      <SignalChart points={item.points} threshold={item.threshold} health={health} label={item.label} />
      <div className="hint" style={{ marginTop: 6 }}>
        {health
          ? `${item.readings} checks`
          : `lowest ${formatSignal(item, item.min)} · average ${formatSignal(item, item.avg)} · highest ${formatSignal(item, item.max)} · ${item.readings} readings`}
      </div>
    </div>
  );
}

export function Signals({ route }: { route: Route }) {
  const range = route.query.get("range") || "24h";
  const app = route.query.get("app") || "";
  const query = `range=${encodeURIComponent(range)}${app ? `&app=${encodeURIComponent(app)}` : ""}`;
  const { data, error } = useApi<SignalsPage>(`/api/signals?${query}`);
  const shell = data?.shell ?? null;
  const many = !app && (data?.apps.length ?? 0) > 1;
  return (
    <Page active="drift" shell={shell} error={error}>
      <Header
        crumb={`${shell?.environment || "local"} / drift and data`}
        title="Drift and data quality"
        shell={shell}
        lede="How far the inputs moved, whether the data is healthy, and how the application is doing."
      >
        <select className="field" aria-label="Model" value={app} onChange={(e) => go("/drift", { range, app: e.target.value })}>
          <option value="">Model: all</option>
          {(data?.apps ?? []).map((a) => (
            <option key={a} value={a}>{a}</option>
          ))}
        </select>
        <RangeSelect value={range} onChange={(r) => go("/drift", { range: r, app })} />
      </Header>
      {!data ? (
        <Loading />
      ) : (
        <>
          <section style={{ display: "flex", flexWrap: "wrap", gap: 12, marginTop: 24 }}>
            {data.checks.map((tile) => (
              <Tile key={tile.label} tile={tile} />
            ))}
          </section>

          <Heatmap rows={data.heatmap.rows} days={data.heatmap.days} limit={data.heatmap.limit} hidden={data.heatmap.hidden} many={many} />

          <h2 style={{ marginTop: 28 }}>Health, speed and errors <span className="hint" style={{ fontWeight: 400 }}>({RANGE_LABEL[range]?.toLowerCase() ?? range})</span></h2>
          <div className="row">
            {data.service.length === 0 && (
              <div className="card empty" style={{ flex: "1 1 100%" }}>
                No readings in {RANGE_LABEL[range]?.toLowerCase() ?? "this period"}. If this stays empty, the application does not report health or metrics to HealOps.
              </div>
            )}
            {data.service.map((item) => (
              <SignalCard key={`${item.app}|${item.name}`} item={item} now={data.shell.now} many={many} />
            ))}
          </div>
          {data.more_service > 0 && <div className="hint" style={{ marginTop: 10 }}>{data.more_service} more metrics are stored but not charted here.</div>}

          <section className="row" style={{ alignItems: "stretch" }}>
            <div className="card" style={{ flex: "3 1 460px", minWidth: 0 }}>
              <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
                <h2 style={{ margin: 0 }}>Training and live distributions</h2>
                <Planned>NOT COLLECTED YET</Planned>
              </div>
              <div className="hint" style={{ marginTop: 12, lineHeight: 1.6 }}>
                Putting the training and the live distribution of one feature side by side needs the application to send
                the shape of both, and HealOps stores single numbers only. Nothing is faked here.
              </div>
            </div>
            <div className="card" style={{ flex: "2 1 320px", minWidth: 0 }}>
              <h2>How to read PSI</h2>
              <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 16, fontSize: 13, lineHeight: 1.5, color: "var(--soft)" }}>
                <div><span className="mono" style={{ color: "var(--ok)" }}>below 0.10</span> · stable, nothing to do.</div>
                <div><span className="mono" style={{ color: "var(--warn)" }}>0.10 to {data.heatmap.limit}</span> · watch it, the agent keeps checking.</div>
                <div><span className="mono" style={{ color: "var(--bad)" }}>above {data.heatmap.limit}</span> · open an incident and diagnose.</div>
              </div>
              {data.related_incident ? (
                <a className="btn primary" style={{ display: "block", marginTop: 18, textAlign: "center" }} href={link(`/incidents/${data.related_incident}`)}>
                  Open the related incident
                </a>
              ) : (
                <div className="hint" style={{ marginTop: 18 }}>No drift incident is open right now.</div>
              )}
            </div>
          </section>
        </>
      )}
    </Page>
  );
}
