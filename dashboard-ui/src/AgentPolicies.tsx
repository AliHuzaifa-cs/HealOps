import { useRef, useState } from "react";
import { useApi } from "./api";
import { agoShort, clock, humanize } from "./format";
import { link } from "./router";
import type { AgentPageData } from "./types";
import { Header, Loading, Page, SEVERITY_COLOR } from "./ui";

const LEVELS = [
  { code: "L0", name: "Observe", text: "Watches and reports. Changes nothing." },
  { code: "L1", name: "Suggest", text: "Diagnoses and proposes a fix." },
  { code: "L2", name: "Act with approval", text: "Runs the fix after a human says yes." },
  { code: "L3", name: "Act within limits", text: "Runs safe fixes alone, inside guardrails." },
  { code: "L4", name: "Autonomous", text: "Handles everything. Not offered by HealOps." },
];
function span(seconds: number): string {
  const s = Math.round(seconds);
  if (s === 0) return "none";
  const unit = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
  if (s % 86400 === 0) return unit(s / 86400, "day");
  if (s % 3600 === 0) return unit(s / 3600, "hour");
  if (s % 60 === 0) return `${s / 60} min`;
  return `${s} s`;
}
const GRID = "minmax(190px, 1.5fr) repeat(5, minmax(96px, 1fr))";

/** What an action policy means in the five steps of the design. */
function levelOf(verdict: string, dryRun: boolean | null): number {
  if (verdict === "deny") return 0;
  if (dryRun === true) return 1;
  return verdict === "needs_approval" ? 2 : 3;
}

function guardrails(s: AgentPageData["setup"]): { title: string; text: string; value: string }[] {
  const out: { title: string; text: string; value: string }[] = [];
  const has = (k: string) => s[k] !== undefined;
  if (has("mode")) out.push({ title: "Mode", text: "Dry run proposes and asks. Live carries out what is allowed.", value: String(s.mode).toLowerCase() });
  if (has("check_interval_seconds")) out.push({ title: "Check interval", text: "How often the agent looks at the application.", value: `every ${span(Number(s.check_interval_seconds))}` });
  if (has("consecutive_breaches")) out.push({ title: "Noise filter", text: "One noisy reading cannot open an incident.", value: `${s.consecutive_breaches} in a row` });
  if (has("min_confidence_for_auto")) out.push({ title: "Confidence to act alone", text: "Below this the agent asks a person.", value: `above ${s.min_confidence_for_auto}` });
  if (has("max_recovery_attempts")) out.push({ title: "Recovery attempts", text: "How often it may retry one incident.", value: `max ${s.max_recovery_attempts} per incident` });
  if (has("recovery_checks")) {
    out.push({
      title: "Self-recovery",
      text: "Closes an escalated incident that went back to normal.",
      value: Number(s.recovery_checks) === 0 ? "off" : `after ${s.recovery_checks} normal checks`,
    });
  }
  if (has("approval_timeout_seconds")) out.push({ title: "Approval expiry", text: "A request nobody answers is dropped.", value: `after ${span(Number(s.approval_timeout_seconds))}` });
  if (has("diagnosis")) out.push({ title: "Diagnosis", text: "What works out the likely cause.", value: String(s.diagnosis) });
  if (has("email_alerts")) out.push({ title: "Email alerts", text: "Sent when a person has to act.", value: s.email_alerts ? "configured" : "not configured" });
  return out;
}

const PILL = {
  fontFamily: "var(--mono)",
  fontSize: 12,
  border: "1px solid var(--line-strong)",
  borderRadius: 6,
  padding: "6px 10px",
  background: "var(--bg)",
  whiteSpace: "nowrap" as const,
};

export function AgentPolicies() {
  const [q, setQ] = useState("");
  const [all, setAll] = useState(false);
  const { data, error } = useApi<AgentPageData>(`/api/agent?q=${encodeURIComponent(q.trim())}`);
  const last = useRef<AgentPageData | null>(null);
  if (data) last.current = data;
  const view = data ?? last.current;
  const shell = view?.shell ?? null;
  const rules = view ? guardrails(view.setup) : [];
  const used = new Set((view?.policies ?? []).map((p) => levelOf(p.verdict, view?.dry_run ?? null)));
  const shown = view ? (all ? view.events : view.events.slice(0, 5)) : [];
  return (
    <Page active="agent" shell={shell} error={error}>
      <Header
        crumb={`${shell?.environment || "local"} / agent`}
        title="Agent and policies"
        shell={shell}
        lede="Decide how much the agent may do alone. Every action it takes is checked against these rules and written to the audit log."
      />
      {!view ? (
        <Loading />
      ) : (
        <>
          <section className="card" style={{ marginTop: 24, padding: "22px 24px" }}>
            <h2>Autonomy level, per action</h2>
            {view.policies.length === 0 ? (
              <div className="empty">The dashboard was started without a config file, so it cannot show the policies. Start it with --config.</div>
            ) : (
              <div style={{ overflowX: "auto", marginTop: 18 }}>
                <div style={{ minWidth: 820 }}>
                  <div style={{ display: "grid", gridTemplateColumns: GRID, gap: 12 }}>
                    <span />
                    {LEVELS.map((l, i) => (
                      <div
                        key={l.code}
                        style={{
                          border: `1px solid ${used.has(i) ? "var(--accent)" : "var(--line)"}`,
                          background: used.has(i) ? "rgba(139,124,246,0.08)" : "transparent",
                          opacity: i === 4 ? 0.55 : 1,
                          borderRadius: 10,
                          padding: "12px 14px",
                        }}
                      >
                        <div className="mono hint">{l.code}</div>
                        <div style={{ fontWeight: 600, marginTop: 6, fontSize: 14 }}>{l.name}</div>
                        <div className="hint" style={{ marginTop: 6, lineHeight: 1.5 }}>{l.text}</div>
                      </div>
                    ))}
                  </div>
                  {view.policies.map((p) => {
                    const at = levelOf(p.verdict, view.dry_run);
                    return (
                      <div key={p.action} style={{ display: "grid", gridTemplateColumns: GRID, gap: 12, alignItems: "center", borderTop: "1px solid var(--line-soft)", marginTop: 12, paddingTop: 12 }}>
                        <div>
                          <div style={{ fontWeight: 600, fontSize: 14 }}>{humanize(p.action)}</div>
                          <div className="hint">
                            {p.max_retries} {p.max_retries === 1 ? "retry" : "retries"} · cooldown {span(p.cooldown_seconds)}
                          </div>
                        </div>
                        {LEVELS.map((l, i) => (
                          <div key={l.code} style={{ display: "flex", justifyContent: "center" }} aria-label={i === at ? `${l.name} for ${humanize(p.action)}` : undefined}>
                            <span style={{ width: 20, height: 20, borderRadius: "50%", border: "2px solid var(--line-strong)", background: i === at ? "var(--accent)" : "transparent", borderColor: i === at ? "var(--accent)" : undefined }} />
                          </div>
                        ))}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
            <div className="hint" style={{ marginTop: 16, lineHeight: 1.6 }}>
              Read only here: the levels follow the policies in the config file.
              {view.dry_run === true ? " Dry run is on, so an action that would run is only proposed (L1)." : ""}
            </div>
          </section>

          <div className="row" style={{ alignItems: "flex-start" }}>
            <section className="card" style={{ flex: "3 1 460px" }}>
              <h2>Guardrails</h2>
              {rules.length === 0 ? (
                <div className="empty">The dashboard was started without a config file, so it cannot show the setup.</div>
              ) : (
                <div style={{ marginTop: 14 }}>
                  {rules.map((r) => (
                    <div key={r.title} style={{ display: "flex", justifyContent: "space-between", gap: 16, alignItems: "center", borderTop: "1px solid var(--line-soft)", padding: "14px 0" }}>
                      <div>
                        <div style={{ fontWeight: 600, fontSize: 14 }}>{r.title}</div>
                        <div className="hint" style={{ marginTop: 3 }}>{r.text}</div>
                      </div>
                      <span style={PILL}>{r.value}</span>
                    </div>
                  ))}
                </div>
              )}
            </section>

            <div style={{ flex: "2 1 340px", display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
              <section className="card" style={{ borderColor: "rgba(245,183,59,0.45)", background: "rgba(245,183,59,0.05)" }}>
                <h2 style={{ color: "var(--warn)" }}>Waiting for you · {view.waiting.length}</h2>
                {view.waiting.length === 0 ? (
                  <div className="empty">Nothing is waiting for a decision.</div>
                ) : (
                  view.waiting.map((w, i) => (
                    <div key={w.id} style={{ borderTop: i === 0 ? "1px solid rgba(245,183,59,0.25)" : "1px solid var(--line-soft)", marginTop: i === 0 ? 14 : 0, padding: "14px 0 6px" }}>
                      <div style={{ fontWeight: 600, fontSize: 14 }}>{w.title}</div>
                      <div className="hint" style={{ margin: "4px 0 12px" }}>
                        <span className="mono">{w.short}</span> · <span style={{ color: SEVERITY_COLOR[w.severity] }}>{w.severity}</span> · {w.app} · {agoShort(view.shell.now, w.opened)} ago
                      </div>
                      <a className="btn" style={{ display: "block", background: "var(--warn)", color: "var(--bg)" }} href={link(`/incidents/${w.id}`)}>Review</a>
                    </div>
                  ))
                )}
              </section>

              <section className="card">
                <div className="card-head">
                  <h2 style={{ margin: 0 }}>Audit log</h2>
                  <span className="mono" style={{ fontSize: 10, color: view.audit_ok ? "var(--ok)" : "var(--bad)" }}>
                    {view.audit_ok ? "READ OK" : "LOG UNREADABLE"} · {view.total} EVENTS
                  </span>
                </div>
                {all && (
                  <input className="field" style={{ marginTop: 12, width: "100%" }} placeholder="Search the audit log" aria-label="Search the audit log" value={q} onChange={(e) => setQ(e.target.value)} />
                )}
                <div style={{ marginTop: 10, maxHeight: all ? 520 : undefined, overflowY: all ? "auto" : undefined }}>
                  {shown.map((e, i) => (
                    <div key={`${e.at}|${i}`} style={{ borderTop: "1px solid var(--line-soft)", padding: "12px 0" }}>
                      <div style={{ display: "flex", justifyContent: "space-between" }} className="mono">
                        <span style={{ color: "var(--muted)" }}>{clock(e.at)}</span>
                        <a href={link(`/incidents/${e.incident_id}`)}>{e.incident}</a>
                      </div>
                      <div style={{ marginTop: 4, fontSize: 13, color: "var(--soft)", lineHeight: 1.5, overflowWrap: "anywhere" }}>{e.text}</div>
                    </div>
                  ))}
                  {shown.length === 0 && <div className="empty">{view.query ? "Nothing matches that search." : "No events yet."}</div>}
                </div>
                {view.total > 5 && (
                  <button className="btn ghost" style={{ width: "100%", marginTop: 8 }} onClick={() => { setAll(!all); if (all) setQ(""); }}>
                    {all ? "Show fewer" : `Show all ${view.total} events`}
                  </button>
                )}
              </section>
            </div>
          </div>
        </>
      )}
    </Page>
  );
}
