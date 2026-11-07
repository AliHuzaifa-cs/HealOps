import { useApi } from "./api";
import { ago } from "./format";
import { link } from "./router";
import type { AppsPage } from "./types";
import { Header, Loading, Page, Planned, TONE } from "./ui";

const STATUS_TONE: Record<string, keyof typeof TONE> = {
  Healthy: "ok",
  Healing: "heal",
  Drifting: "warn",
  Degraded: "bad",
};
const COLUMNS = "minmax(120px, 1.2fr) 110px 110px 90px 70px minmax(150px, 1.4fr) 80px";

export function Models() {
  const { data, error } = useApi<AppsPage>("/api/apps");
  const shell = data?.shell ?? null;
  return (
    <Page active="models" shell={shell} error={error}>
      <Header
        crumb={`${shell?.environment || "local"} / models`}
        title="Models and applications"
        shell={shell}
        lede="Everything HealOps is watching, how it is doing right now, and how its incidents ended."
      />
      {!data ? (
        <Loading />
      ) : (
        <>
          <section className="card" style={{ marginTop: 24 }}>
            <div className="tbl-wrap" style={{ marginTop: 0 }}>
              <div className="tbl" role="table" aria-label="Applications">
                <div className="tr th" role="row" style={{ gridTemplateColumns: COLUMNS }}>
                  <span>MODEL</span><span>STATUS</span><span>LAST READING</span><span>READINGS 24H</span><span>DRIFT</span><span>INCIDENTS</span><span />
                </div>
                {data.apps.map((a) => (
                  <div className="tr" role="row" key={a.app} style={{ gridTemplateColumns: COLUMNS }}>
                    <span className="mono" style={{ overflowWrap: "anywhere" }}>{a.app}</span>
                    <span style={{ display: "flex", alignItems: "center", gap: 8, color: TONE[STATUS_TONE[a.status] ?? "idle"] }}>
                      <span className="dot" style={{ width: 7, height: 7, background: TONE[STATUS_TONE[a.status] ?? "idle"] }} />
                      {a.status}
                    </span>
                    <span style={{ color: "var(--soft)" }}>{a.last_seen ? ago(data.shell.now, a.last_seen) : "never"}</span>
                    <span>{a.readings_24h}</span>
                    <span>{a.drift === null ? "-" : a.drift}</span>
                    <span style={{ color: "var(--soft)", fontSize: 12 }}>
                      {a.incidents.total === 0
                        ? "none yet"
                        : `${a.incidents.open} open · ${a.incidents.by_agent} healed by the agent · ${a.incidents.by_human} by a person · ${a.incidents.by_self} on their own`}
                    </span>
                    <a href={link("/incidents", { q: a.app })}>View</a>
                  </div>
                ))}
              </div>
              {data.apps.length === 0 && <div className="empty">HealOps has not seen any application yet.</div>}
            </div>
            {data.apps.some((a) => a.signals.length > 0) && (
              <div className="hint" style={{ marginTop: 14, lineHeight: 1.6 }}>
                {data.apps.map((a) => `${a.app} reports: ${a.signals.join(", ")}`).join(" · ")}
              </div>
            )}
          </section>

          <section className="card" style={{ marginTop: 16 }}>
            <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
              <h2 style={{ margin: 0 }}>Versions, stages and accuracy</h2>
              <Planned>NOT TRACKED YET</Planned>
            </div>
            <div className="hint" style={{ marginTop: 12, lineHeight: 1.6 }}>
              HealOps does not know a model's version, stage (production, canary, shadow), last retrain or accuracy
              against a baseline, because the application has not told it. A model registry adapter will add that.
              Nothing is faked here.
            </div>
          </section>
        </>
      )}
    </Page>
  );
}
