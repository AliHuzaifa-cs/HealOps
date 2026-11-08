import { useState } from "react";
import { useApi } from "./api";
import { ago, clock } from "./format";
import { link } from "./router";
import type { AppRow, AppsPage } from "./types";
import { Header, Loading, Page, Planned, TONE } from "./ui";

const STATUS_TONE: Record<string, keyof typeof TONE> = {
  Healthy: "ok",
  Healing: "heal",
  Drifting: "warn",
  Degraded: "bad",
};
const COLUMNS = "minmax(150px, 1.3fr) 100px 110px 100px 110px minmax(120px, 1fr)";

function driftColor(value: number | null, limit: number | null): string {
  if (value === null) return "var(--muted)";
  if (limit !== null && value >= limit) return "var(--bad)";
  return value >= 0.1 ? "var(--warn)" : "var(--soft)";
}

function Lineage({ app, now }: { app: AppRow | undefined; now: string }) {
  return (
    <section className="card">
      <div className="card-head">
        <h2 style={{ margin: 0 }}>{app ? `${app.app} changes` : "Changes"}</h2>
        <span className="mono hint">selected</span>
      </div>
      {!app || app.changes.length === 0 ? (
        <div className="empty">The agent has not changed this model yet.</div>
      ) : (
        <ol style={{ listStyle: "none", margin: "18px 0 0", padding: 0, position: "relative" }}>
          {app.changes.map((c, i) => (
            <li key={`${c.at}|${i}`} style={{ display: "flex", gap: 12, paddingBottom: 14, position: "relative" }}>
              <span style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
                <span className="dot" style={{ background: i === 0 ? "var(--accent)" : "var(--muted)", marginTop: 5 }} />
                {i < app.changes.length - 1 && <span style={{ flex: 1, width: 1, background: "var(--line)", marginTop: 4 }} />}
              </span>
              <span style={{ fontSize: 13, lineHeight: 1.5, color: "var(--soft)" }}>
                <span className="mono" style={{ color: "var(--muted)", marginRight: 8 }}>{ago(now, c.at)}</span>
                {c.text}
              </span>
            </li>
          ))}
        </ol>
      )}
      <div className="hint" style={{ marginTop: 6, lineHeight: 1.6 }}>
        Versions and stages (production, canary, shadow) need a model registry adapter, so they are not listed.
      </div>
      {app && (
        <div className="btns">
          <a className="btn ghost" href={link("/incidents", { q: app.app })}>Open its incidents</a>
        </div>
      )}
    </section>
  );
}

export function Models() {
  const { data, error } = useApi<AppsPage>("/api/apps");
  const [picked, setPicked] = useState("");
  const shell = data?.shell ?? null;
  const selected = data ? data.apps.find((a) => a.app === picked) ?? data.apps.find((a) => a.incidents.open > 0) ?? data.apps[0] : undefined;
  const limit = data?.limits.drift ?? null;
  return (
    <Page active="models" shell={shell} error={error}>
      <Header crumb={`${shell?.environment || "local"} / models`} title="Model registry" shell={shell}>
        <Planned>VERSIONS NOT TRACKED YET</Planned>
      </Header>
      {!data ? (
        <Loading />
      ) : (
        <div className="row" style={{ alignItems: "flex-start", marginTop: 24 }}>
          <section className="card" style={{ flex: "3 1 640px" }}>
            <div className="tbl-wrap" style={{ marginTop: 0 }}>
              <div className="tbl" role="table" aria-label="Models" style={{ minWidth: 700 }}>
                <div className="tr th" role="row" style={{ gridTemplateColumns: COLUMNS }}>
                  <span>MODEL</span><span>STATUS</span><span>LAST READING</span><span>DRIFT</span><span>LAST RETRAIN</span><span>INCIDENTS</span>
                </div>
                {data.apps.map((a) => {
                  const tone = TONE[STATUS_TONE[a.status] ?? "idle"];
                  const on = selected?.app === a.app;
                  return (
                    <div
                      className="tr"
                      role="row"
                      key={a.app}
                      tabIndex={0}
                      onClick={() => setPicked(a.app)}
                      onKeyDown={(e) => e.key === "Enter" && setPicked(a.app)}
                      style={{ gridTemplateColumns: COLUMNS, cursor: "pointer", background: on ? "rgba(139,124,246,0.07)" : undefined }}
                    >
                      <span className="mono" style={{ display: "flex", alignItems: "center", gap: 10, overflowWrap: "anywhere" }}>
                        <span className="dot" style={{ background: tone }} />
                        {a.app}
                      </span>
                      <span style={{ color: tone }}>{a.status}</span>
                      <span style={{ color: "var(--soft)" }}>{a.last_seen ? ago(data.shell.now, a.last_seen) : "never"}</span>
                      <span className="mono" style={{ color: driftColor(a.drift, limit) }}>{a.drift === null ? "-" : `PSI ${a.drift.toFixed(2)}`}</span>
                      <span style={{ color: "var(--soft)" }} title={a.last_retrain ? clock(a.last_retrain) : undefined}>
                        {a.last_retrain ? ago(data.shell.now, a.last_retrain) : "none yet"}
                      </span>
                      <span style={{ color: "var(--soft)", fontSize: 12 }}>
                        {a.incidents.total === 0
                          ? "none yet"
                          : `${a.incidents.open} open · ${a.incidents.by_agent + a.incidents.by_human + a.incidents.by_self} closed`}
                      </span>
                    </div>
                  );
                })}
              </div>
              {data.apps.length === 0 && <div className="empty">HealOps has not seen any application yet.</div>}
            </div>
            <div className="hint" style={{ marginTop: 14, lineHeight: 1.6 }}>
              “Drift” is the latest PSI the application reported. “Last retrain” is when the agent last started a retrain.
              Click a row to see what the agent changed on that model.
            </div>
            {data.apps.some((a) => a.signals.length > 0) && (
              <div className="hint" style={{ marginTop: 10, lineHeight: 1.6 }}>
                {data.apps.map((a) => `${a.app} reports: ${a.signals.join(", ")}`).join(" · ")}
              </div>
            )}
          </section>

          <div style={{ flex: "1 1 320px", display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
            <Lineage app={selected} now={data.shell.now} />
            <section className="card">
              <h2>Release gates</h2>
              {data.gates.length === 0 ? (
                <div className="empty">No retrain policy to show without a config file.</div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: 14, marginTop: 16, fontSize: 13 }}>
                  {data.gates.map((g) => (
                    <div key={g.label} style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center" }}>
                      <span>{g.label}</span>
                      <span className="mono" style={{ fontSize: 11, color: TONE[g.tone] }}>{g.tag}</span>
                    </div>
                  ))}
                </div>
              )}
            </section>
          </div>
        </div>
      )}
    </Page>
  );
}
