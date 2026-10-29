import { useState } from "react";
import { useApi } from "./api";
import { DriftPulse } from "./charts";
import { RANGE_SHORT, agoShort, clock, duration } from "./format";
import { RangeSelect } from "./RangeSelect";
import { go, link, type Route } from "./router";
import type { Overview as OverviewData } from "./types";
import { Header, Loading, Page, SEVERITY_COLOR, Spark } from "./ui";

const TILE_TONE: Record<string, string> = {
  Healthy: "var(--ok)",
  Drifting: "var(--warn)",
  Healing: "var(--accent)",
  Degraded: "var(--bad)",
};
const TILE_SQUARE = new Set(["Drifting", "Degraded"]);

export function Overview({ route }: { route: Route }) {
  const range = route.query.get("range") || "24h";
  const { data, error } = useApi<OverviewData>(`/api/overview?range=${encodeURIComponent(range)}`);
  const [search, setSearch] = useState("");
  const shell = data?.shell ?? null;

  const k = data?.kpis;
  const mttrDelta = (() => {
    if (!k || k.mttr.seconds === null) return "no healed incidents in this period";
    if (k.mttr.previous_seconds === null) return "no earlier period to compare";
    const diff = k.mttr.seconds - k.mttr.previous_seconds;
    if (Math.abs(diff) < 1) return "same as the week before";
    return `${diff < 0 ? "down" : "up"} from ${duration(k.mttr.previous_seconds)} the week before`;
  })();
  const mttrTone =
    k && k.mttr.seconds !== null && k.mttr.previous_seconds !== null && k.mttr.seconds > k.mttr.previous_seconds
      ? "var(--warn)"
      : "var(--ok)";

  const kpis = k
    ? [
        {
          label: "Models healthy",
          value: `${k.healthy.healthy} of ${k.healthy.total}`,
          delta: k.healthy.attention ? `${k.healthy.attention} need attention` : "all healthy",
          tone: k.healthy.attention ? "var(--warn)" : "var(--ok)",
          spark: k.healthy.spark,
        },
        {
          label: "Open incidents",
          value: String(k.open.n),
          delta: `${k.open.awaiting} awaiting a human`,
          tone: k.open.n ? "var(--bad)" : "var(--ok)",
          spark: k.open.spark,
        },
        {
          label: `Healed by the agent, ${RANGE_SHORT[range] ?? range}`,
          value: String(k.healed.n),
          delta: `of ${k.healed.resolved} resolved`,
          tone: "var(--ok)",
          spark: k.healed.spark,
        },
        {
          label: "Mean time to heal",
          value: duration(k.mttr.seconds),
          delta: mttrDelta,
          tone: mttrTone,
          spark: null,
        },
      ]
    : [];

  return (
    <Page active="overview" shell={shell} error={error}>
      <Header
        crumb={`${shell?.environment || "local"} / ${shell?.workspace ?? "…"}`}
        title="Fleet overview"
        shell={shell}
      >
        <RangeSelect value={range} onChange={(r) => go("/", { range: r })} />
        <input
          className="field"
          placeholder="Search models, incidents"
          aria-label="Search models and incidents"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") go("/incidents", { q: search.trim() });
          }}
        />
      </Header>

      {!data ? (
        <Loading />
      ) : (
        <>
          <section className="card" style={{ marginTop: 28 }}>
            <div className="card-head">
              <h2>The healing loop, right now</h2>
              <span className="hint">Incidents currently in each stage</span>
            </div>
            <div className="loop">
              {data.loop.map((s) => (
                <div key={s.no} className={`stage s${s.no}${s.n === 0 ? " zero" : ""}`}>
                  <div className="top">
                    <span className="no">STEP {s.no}</span>
                    <span className="n">{s.n}</span>
                  </div>
                  <div className="name">{s.name}</div>
                  <div className="note">{s.note}</div>
                </div>
              ))}
            </div>
          </section>

          <section className="kpis">
            {kpis.map((item) => (
              <div className="kpi" key={item.label}>
                <div className="l">{item.label}</div>
                <div className="body">
                  <div>
                    <div className="v">{item.value}</div>
                    <div className="d" style={{ color: item.tone }}>{item.delta}</div>
                  </div>
                  <Spark values={item.spark} color={item.tone} />
                </div>
              </div>
            ))}
          </section>

          <section className="row">
            <div className="card" style={{ flex: "3 1 480px" }}>
              <div className="card-head">
                <h2>Model health</h2>
                <a className="link" href={link("/models")}>Open registry</a>
              </div>
              {data.tiles.length === 0 ? (
                <div className="empty">
                  No application has reported anything yet. When HealOps starts checking one, it appears here.
                </div>
              ) : (
                <div className="tiles">
                  {data.tiles.map((t) => (
                    <div className="tile" key={t.name}>
                      <div className="st" style={{ color: TILE_TONE[t.status] }}>
                        <span className="dot" style={{ background: TILE_TONE[t.status], borderRadius: TILE_SQUARE.has(t.status) ? 2 : "50%" }} />
                        {t.status}
                      </div>
                      <div className="nm">{t.name}</div>
                      <div className="ps">{t.drift === null ? "no drift reading" : `Drift ${t.drift.toFixed(2)}`}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="card live" style={{ flex: "2 1 340px" }}>
              <div className="card-head">
                <h2>Live incidents</h2>
                <a className="link" href={link("/incidents")}>View all</a>
              </div>
              {data.live.length === 0 ? (
                <div className="empty">No open incidents. Everything HealOps watches is inside its limits.</div>
              ) : (
                <div className="list" style={{ marginTop: 12 }}>
                  {data.live.map((i) => (
                    <a key={i.id} href={link(`/incidents/${i.id}`)}>
                      <div className="meta">
                        <span style={{ color: SEVERITY_COLOR[i.severity], fontWeight: 500 }}>{i.severity.toUpperCase()}</span>
                        <span style={{ color: "var(--muted)" }}>{agoShort(data.shell.now, i.detected_at)}</span>
                      </div>
                      <div className="ttl">{i.title}</div>
                      <div className="sub">{i.app} · {i.state}</div>
                    </a>
                  ))}
                </div>
              )}
            </div>
          </section>

          <section className="row">
            <div className="card" style={{ flex: "3 1 480px" }}>
              <div className="card-head">
                <h2>Drift pulse, all features</h2>
                <span className="hint">Highest drift score per slot, {RANGE_SHORT[range] ?? range}</span>
              </div>
              <DriftPulse points={data.drift_pulse} threshold={data.drift_threshold} />
            </div>
            <div className="card" style={{ flex: "2 1 340px" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <span className="dot pulse" style={{ background: "var(--accent)", animation: "pulse 2s ease-in-out infinite" }} />
                <h2>Agent activity</h2>
              </div>
              {data.activity.length === 0 ? (
                <div className="empty">Nothing yet. The agent's steps show up here as they happen.</div>
              ) : (
                <div className="act">
                  {data.activity.map((a, i) => (
                    <div className="it" key={i}>
                      <div className="tm">{clock(a.at)}</div>
                      <div className="tx">{a.text}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </section>
        </>
      )}
    </Page>
  );
}

