import { useApi } from "./api";
import { SignalChart } from "./charts";
import { ago, RANGE_LABEL } from "./format";
import { RangeSelect } from "./RangeSelect";
import { go } from "./router";
import type { Route } from "./router";
import type { SignalItem, SignalsPage } from "./types";
import { Header, Loading, Page, Planned } from "./ui";

export function formatSignal(item: Pick<SignalItem, "kind" | "name">, value: number): string {
  if (item.kind === "health") return value >= 1 ? "up" : "down";
  if (item.name.endsWith("_ms")) return `${Math.round(value)} ms`;
  return Math.abs(value) >= 100 ? String(Math.round(value)) : value.toFixed(3);
}

function SignalCard({ item, now }: { item: SignalItem; now: string }) {
  const health = item.kind === "health";
  const bad = item.breaches > 0;
  return (
    <div className="card" style={{ flex: "1 1 420px", minWidth: 0 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "baseline" }}>
        <h2 style={{ margin: 0 }}>{item.label}</h2>
        <span className="mono hint">{item.app}</span>
      </div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginTop: 10 }}>
        <span style={{ fontSize: 28, fontWeight: 700 }}>{formatSignal(item, item.latest)}</span>
        <span className="hint">latest, {ago(now, item.latest_at)}</span>
        <span className="tag plain" style={{ marginLeft: "auto", color: bad ? "var(--bad)" : "var(--ok)" }}>
          {health
            ? bad ? `DOWN IN ${item.breaches} READINGS` : "ALWAYS UP"
            : item.threshold === null ? "NO LIMIT SET" : bad ? `${item.breaches} OVER THE LIMIT` : "WITHIN THE LIMIT"}
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
  const { data, error } = useApi<SignalsPage>(`/api/signals?range=${encodeURIComponent(range)}`);
  const shell = data?.shell ?? null;
  return (
    <Page active="drift" shell={shell} error={error}>
      <Header
        crumb={`${shell?.environment || "local"} / drift and data`}
        title="Drift and data quality"
        shell={shell}
        lede="What HealOps measured about the application: how far its inputs moved, and whether it was up, fast and error free."
      >
        <RangeSelect value={range} onChange={(r) => go("/drift", { range: r })} />
      </Header>
      {!data ? (
        <Loading />
      ) : (
        <>
          <h2 style={{ marginTop: 28 }}>Drift</h2>
          <div className="row">
            {data.drift.length === 0 && (
              <div className="card empty" style={{ flex: "1 1 100%" }}>
                No drift readings in {RANGE_LABEL[range]?.toLowerCase() ?? "this period"}.
              </div>
            )}
            {data.drift.map((item) => (
              <SignalCard key={`${item.app}|${item.name}`} item={item} now={data.shell.now} />
            ))}
          </div>

          <h2 style={{ marginTop: 28 }}>Health, speed and errors</h2>
          <div className="row">
            {data.service.length === 0 && (
              <div className="card empty" style={{ flex: "1 1 100%" }}>
                No readings in {RANGE_LABEL[range]?.toLowerCase() ?? "this period"}. If this stays empty, the application
                does not report health or metrics to HealOps.
              </div>
            )}
            {data.service.map((item) => (
              <SignalCard key={`${item.app}|${item.name}`} item={item} now={data.shell.now} />
            ))}
          </div>

          <section className="card" style={{ marginTop: 24 }}>
            <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
              <h2 style={{ margin: 0 }}>Data checks</h2>
              <Planned>NOT COLLECTED YET</Planned>
            </div>
            <div className="hint" style={{ marginTop: 12, lineHeight: 1.6 }}>
              Schema, freshness, missing values and values out of range are not measured by HealOps yet, and training
              versus live distributions need the drift toolbox. Nothing is faked here: this page shows only what the
              application actually reported.
            </div>
          </section>
        </>
      )}
    </Page>
  );
}
