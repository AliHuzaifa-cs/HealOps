import { useRef, useState } from "react";
import { useApi } from "./api";
import { clock, duration, humanize } from "./format";
import { link } from "./router";
import type { AgentPageData } from "./types";
import { Header, Loading, Page, Tag } from "./ui";

const VERDICT: Record<string, { text: string; color: string }> = {
  allow: { text: "Runs on its own", color: "var(--ok)" },
  needs_approval: { text: "Asks a person first", color: "var(--warn)" },
  deny: { text: "Never runs", color: "var(--bad)" },
};

function setupRows(s: AgentPageData["setup"]): [string, string][] {
  const out: [string, string][] = [];
  const has = (k: string) => s[k] !== undefined;
  if (has("mode")) out.push(["Mode", s.mode === "Dry run" ? "Dry run: proposes and asks, changes nothing" : "Live: allowed actions are carried out"]);
  if (has("diagnosis")) out.push(["Diagnosis by", String(s.diagnosis)]);
  if (has("check_interval_seconds")) out.push(["Checks the application every", duration(Number(s.check_interval_seconds))]);
  if (has("consecutive_breaches")) out.push(["Opens an incident after", `${s.consecutive_breaches} readings in a row over the limit`]);
  if (has("min_confidence_for_auto")) out.push(["Acts alone only above confidence", String(s.min_confidence_for_auto)]);
  if (has("max_recovery_attempts")) out.push(["Recovery attempts per incident", String(s.max_recovery_attempts)]);
  if (has("recovery_checks")) {
    out.push(["Closes an escalated incident by itself after", Number(s.recovery_checks) === 0 ? "never (switched off)" : `${s.recovery_checks} normal checks in a row`]);
  }
  if (has("approval_timeout_seconds")) out.push(["An approval request expires after", duration(Number(s.approval_timeout_seconds))]);
  if (has("email_alerts")) out.push(["Email alerts", s.email_alerts ? "configured" : "not configured"]);
  return out;
}

export function AgentPolicies() {
  const [q, setQ] = useState("");
  const { data, error } = useApi<AgentPageData>(`/api/agent?q=${encodeURIComponent(q.trim())}`);
  const last = useRef<AgentPageData | null>(null);
  if (data) last.current = data;
  const view = data ?? last.current;
  const shell = view?.shell ?? null;
  const rows = view ? setupRows(view.setup) : [];
  return (
    <Page active="agent" shell={shell} error={error}>
      <Header
        crumb={`${shell?.environment || "local"} / agent and policies`}
        title="Agent and policies"
        shell={shell}
        lede="How much the agent may do alone, and everything it did. Every action is checked against these rules and written to the audit log."
      />
      {!view ? (
        <Loading />
      ) : (
        <>
          <div className="row" style={{ alignItems: "flex-start" }}>
            <section className="card" style={{ flex: "1 1 420px" }}>
              <h2>How the agent is set up</h2>
              {rows.length === 0 ? (
                <div className="empty">The dashboard was started without a config file, so it cannot show the setup. Start it with --config.</div>
              ) : (
                <div className="kv" style={{ marginTop: 12 }}>
                  {rows.map(([a, b]) => (
                    <div key={a} style={{ borderTopColor: "var(--line-soft)" }}>
                      <span className="a" style={{ color: "var(--soft)" }}>{a}</span>
                      <span className="b">{b}</span>
                    </div>
                  ))}
                </div>
              )}
            </section>

            <section className="card" style={{ flex: "1 1 420px" }}>
              <h2>Action policies</h2>
              {view.policies.length === 0 ? (
                <div className="empty">No policies to show without a config file.</div>
              ) : (
                <div className="kv" style={{ marginTop: 12 }}>
                  {view.policies.map((p) => {
                    const v = VERDICT[p.verdict] ?? { text: p.verdict, color: "var(--soft)" };
                    return (
                      <div key={p.action} style={{ borderTopColor: "var(--line-soft)", alignItems: "center" }}>
                        <span className="a mono" style={{ color: "var(--text)" }}>{humanize(p.action)}</span>
                        <span className="b">
                          <Tag color={v.color}>{v.text.toUpperCase()}</Tag>
                          <span className="hint" style={{ display: "block", marginTop: 4 }}>
                            {p.max_retries} {p.max_retries === 1 ? "retry" : "retries"} · cooldown {duration(p.cooldown_seconds)}
                          </span>
                        </span>
                      </div>
                    );
                  })}
                </div>
              )}
            </section>
          </div>

          <section className="card" style={{ marginTop: 16 }}>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center", justifyContent: "space-between" }}>
              <h2 style={{ margin: 0 }}>Audit log</h2>
              <input
                className="field"
                placeholder="Search the audit log"
                aria-label="Search the audit log"
                value={q}
                onChange={(e) => setQ(e.target.value)}
              />
            </div>
            <div className="hint" style={{ marginTop: 8 }}>
              {view.query
                ? `${view.matched} of ${view.total} events match "${view.query}"`
                : `${view.total} events recorded`}
              {view.matched > view.events.length ? `, newest ${view.events.length} shown` : ""}
            </div>
            <div className="tbl-wrap">
              <div className="tbl" role="table" aria-label="Audit log">
                <div className="tr th" role="row" style={{ gridTemplateColumns: "130px 140px 110px 90px minmax(0, 1fr)" }}>
                  <span>TIME</span><span>STEP</span><span>MODEL</span><span>INCIDENT</span><span>WHAT HAPPENED</span>
                </div>
                {view.events.map((e, i) => (
                  <div className="tr" role="row" key={`${e.at}|${i}`} style={{ gridTemplateColumns: "130px 140px 110px 90px minmax(0, 1fr)", alignItems: "start" }}>
                    <span className="mono" style={{ color: "var(--muted)" }}>{clock(e.at)}</span>
                    <span>{e.stage}</span>
                    <span className="mono" style={{ overflowWrap: "anywhere" }}>{e.app}</span>
                    <a className="mono" href={link(`/incidents/${e.incident_id}`)}>{e.incident}</a>
                    <span style={{ color: "var(--soft)", overflowWrap: "anywhere" }}>{e.text}</span>
                  </div>
                ))}
              </div>
              {view.events.length === 0 && <div className="empty">{view.query ? "Nothing matches that search." : "No events yet."}</div>}
            </div>
          </section>
        </>
      )}
    </Page>
  );
}
