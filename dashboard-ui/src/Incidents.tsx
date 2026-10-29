import { useMemo, useState } from "react";
import { useApi } from "./api";
import { RANGE_LABEL, agoShort } from "./format";
import { RangeSelect } from "./RangeSelect";
import { go, link, type Route } from "./router";
import type { IncidentRow, IncidentsPage } from "./types";
import { Header, Loading, Page, SEVERITY_COLOR, Tag, TONE } from "./ui";

const FILTERS = [
  { key: "all", label: "All", test: () => true },
  { key: "open", label: "Open", test: (r: IncidentRow) => ["in_progress", "awaiting_approval", "escalated"].includes(r.status) },
  { key: "awaiting", label: "Awaiting approval", test: (r: IncidentRow) => r.status === "awaiting_approval" },
  { key: "resolved", label: "Resolved", test: (r: IncidentRow) => ["resolved", "acknowledged"].includes(r.status) },
  { key: "severe", label: "Critical and high", test: (r: IncidentRow) => r.severity === "critical" || r.severity === "high" },
];
const BY = { agent: "Agent", human: "Human", pending: "Pending" } as const;

export function Incidents({ route }: { route: Route }) {
  const range = route.query.get("range") || "7d";
  const { data, error } = useApi<IncidentsPage>(`/api/incidents?range=${encodeURIComponent(range)}`);
  const [filter, setFilter] = useState("all");
  const [model, setModel] = useState(route.query.get("q") ?? "");
  const shell = data?.shell ?? null;

  const rows = useMemo(() => {
    if (!data) return [];
    const test = FILTERS.find((f) => f.key === filter)?.test ?? (() => true);
    const needle = model.trim().toLowerCase();
    return data.rows.filter(
      (r) => test(r) && (!needle || r.app.toLowerCase().includes(needle) || r.title.toLowerCase().includes(needle) || r.short.toLowerCase().includes(needle)),
    );
  }, [data, filter, model]);

  const counts = data
    ? [
        { label: "Open", n: data.counts.open, tone: "var(--bad)" },
        { label: "Awaiting approval", n: data.counts.awaiting, tone: "var(--warn)" },
        { label: "Resolved by agent", n: data.counts.by_agent, tone: "var(--ok)" },
        { label: "Resolved by a human", n: data.counts.by_human, tone: "var(--info)" },
      ]
    : [];

  return (
    <Page active="incidents" shell={shell} error={error}>
      <Header crumb={`${shell?.environment || "local"} / incidents`} title="Incidents" shell={shell}>
        <RangeSelect value={range} onChange={(r) => go("/incidents", { range: r })} />
        <a className="btn-light" href={`/api/incidents.csv?range=${encodeURIComponent(range)}`} download>
          Export report
        </a>
      </Header>

      {!data ? (
        <Loading />
      ) : (
        <>
          <section className="counts">
            {counts.map((c) => (
              <div className="count" key={c.label}>
                <div className="l">{c.label}</div>
                <div className="n" style={{ color: c.tone }}>{c.n}</div>
              </div>
            ))}
          </section>

          <section className="row" style={{ alignItems: "flex-start" }}>
            <div className="card" style={{ flex: "4 1 620px", padding: "20px 22px" }}>
              <div className="pills">
                {FILTERS.map((f) => (
                  <button key={f.key} className={"pill" + (filter === f.key ? " on" : "")} onClick={() => setFilter(f.key)} aria-pressed={filter === f.key}>
                    {f.label}
                  </button>
                ))}
                <input
                  className="field"
                  style={{ marginLeft: "auto", padding: "8px 14px", minWidth: 150 }}
                  placeholder="Filter by model"
                  aria-label="Filter by model or text"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                />
              </div>

              <div className="tbl-wrap">
                <div className="tbl" role="table" aria-label="Incidents">
                  <div className="tr th" role="row">
                    <span>ID</span><span>INCIDENT</span><span>MODEL</span><span>STATE</span><span>HEALED BY</span><span>TIME</span>
                  </div>
                  {rows.map((r) => (
                    <a className="tr" role="row" key={r.id} href={link(`/incidents/${r.id}`)}>
                      <span className="mono" style={{ color: "var(--muted)" }}>{r.short}</span>
                      <span>
                        <span style={{ marginRight: 8 }}><Tag color={SEVERITY_COLOR[r.severity]}>{r.severity.toUpperCase()}</Tag></span>
                        {r.title}
                      </span>
                      <span className="mono" style={{ overflowWrap: "anywhere" }}>{r.app}</span>
                      <span style={{ display: "flex", alignItems: "center", gap: 8, color: TONE[r.state_tone] }}>
                        <span className="dot" style={{ width: 7, height: 7, background: TONE[r.state_tone] }} />
                        {r.state}
                      </span>
                      <span style={{ color: "var(--soft)" }}>{BY[r.by]}</span>
                      <span style={{ color: "var(--muted)" }}>{agoShort(data.shell.now, r.detected_at)}</span>
                    </a>
                  ))}
                </div>
                {rows.length === 0 && (
                  <div className="empty">
                    {data.rows.length === 0
                      ? `No incidents in ${RANGE_LABEL[range]?.toLowerCase() ?? "this period"}.`
                      : "No incident matches these filters."}
                  </div>
                )}
              </div>
            </div>

            <div className="stack" style={{ flex: "1 1 280px" }}>
              <div className="card" style={{ padding: "20px 22px" }}>
                <h2>Why incidents happen</h2>
                <div style={{ display: "flex", flexDirection: "column", gap: 14, marginTop: 18, fontSize: 13 }}>
                  {data.causes.map((c, i) => (
                    <div key={c.label}>
                      <div style={{ display: "flex", justifyContent: "space-between" }}>
                        <span>{c.label}</span>
                        <span style={{ color: "var(--muted)" }}>{c.n}</span>
                      </div>
                      <div className="bar">
                        <i style={{ width: `${c.w}%`, background: ["var(--accent)", "var(--info)", "var(--bad)", "var(--muted)"][i] }} />
                      </div>
                    </div>
                  ))}
                </div>
              </div>
              {data.note && (
                <div className="note-card">
                  <div className="k">AGENT NOTE</div>
                  <div className="t">{data.note}</div>
                </div>
              )}
            </div>
          </section>
        </>
      )}
    </Page>
  );
}
