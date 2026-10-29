import { useEffect, useRef, useState } from "react";
import { decide, useApi } from "./api";
import { EvidenceChart } from "./charts";
import { ago, clock, humanize, until } from "./format";
import { link } from "./router";
import type { Detail } from "./types";
import { Loading, Page, Planned, SEVERITY_COLOR, Tag, TONE } from "./ui";

const KIND_COLOR: Record<string, string> = {
  OBSERVE: "var(--info)",
  TOOL: "var(--muted)",
  HYPOTHESIS: "var(--accent)",
  CONCLUSION: "var(--ok)",
  POLICY: "var(--warn)",
};
const CHECK_COLOR: Record<string, string> = { PASS: "var(--ok)", HOLD: "var(--warn)", DENY: "var(--bad)" };

function Confirm({
  title,
  body,
  confirmLabel,
  askReason,
  onCancel,
  onConfirm,
  busy,
}: {
  title: string;
  body: string;
  confirmLabel: string;
  askReason: boolean;
  onCancel: () => void;
  onConfirm: (reason: string) => void;
  busy: boolean;
}) {
  const [reason, setReason] = useState("");
  const first = useRef<HTMLButtonElement>(null);
  useEffect(() => first.current?.focus(), []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onCancel();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);
  return (
    <div className="veil" role="dialog" aria-modal="true" aria-label={title}>
      <div className="dialog">
        <h3>{title}</h3>
        <p>{body}</p>
        {askReason && (
          <textarea placeholder="Why? (optional, kept in the audit trail)" maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
        )}
        <div className="btns">
          <button ref={first} className="btn ghost" onClick={onCancel} disabled={busy}>Cancel</button>
          <button className="btn primary" onClick={() => onConfirm(reason)} disabled={busy}>
            {busy ? "Saving…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export function IncidentDetail({ id }: { id: string }) {
  const { data, error, reload } = useApi<Detail>(`/api/incidents/${encodeURIComponent(id)}`);
  const [asking, setAsking] = useState<"approve" | "reject" | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<{ text: string; bad: boolean } | null>(null);
  const shell = data?.shell ?? null;

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 7000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  async function answer(action: "approve" | "reject", reason: string) {
    setBusy(true);
    try {
      const result = await decide(id, action, reason);
      setToast({ text: result.message, bad: !result.agent_running });
      setAsking(null);
      reload();
    } catch (e) {
      setToast({ text: e instanceof Error ? e.message : "Could not save your answer", bad: true });
      setAsking(null);
    } finally {
      setBusy(false);
    }
  }

  if (error && !data) {
    return (
      <Page active="incidents" shell={null} error={null}>
        <div className="crumb"><a href={link("/incidents")}>incidents</a> / {id.slice(0, 6)}</div>
        <h1>Incident not found</h1>
        <p className="lede">{error}. It may be older than the retention period, or the id may be wrong.</p>
      </Page>
    );
  }

  const inc = data?.incident;
  const action = data?.action;
  const canAnswer = !!action?.can_decide;
  const enabled = !!shell?.actions_enabled;
  const off = !enabled ? "Start the dashboard with --allow-actions to answer from here" : undefined;
  const now = shell?.now ?? new Date().toISOString();

  return (
    <Page active="incidents" shell={shell} error={error}>
      {!data || !inc ? (
        <Loading />
      ) : (
        <>
          <header>
            <div className="crumb"><a href={link("/incidents")}>incidents</a> / {inc.short}</div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 20, alignItems: "flex-start", justifyContent: "space-between", marginTop: 10 }}>
              <div style={{ flex: "1 1 520px", minWidth: 0 }}>
                <h1 style={{ margin: 0, lineHeight: 1.15 }}>{inc.title}</h1>
                <div className="chips">
                  <Tag color={SEVERITY_COLOR[inc.severity]}>{inc.severity.toUpperCase()}</Tag>
                  <Tag color={TONE[inc.state_tone]}>{inc.state.toUpperCase()}</Tag>
                  <span className="tag plain">{inc.app}</span>
                  <span className="tag plain">opened {ago(now, inc.detected_at)}</span>
                  {inc.dry_run && <span className="tag plain">DRY RUN</span>}
                  {shell?.demo && <span className="tag" style={{ color: "var(--warn)", borderColor: "#5a4417" }}>SAMPLE DATA</span>}
                </div>
              </div>
              {canAnswer && (
                <div className="hdr-btns">
                  <button className="btn ghost" disabled={!enabled} title={off} onClick={() => setAsking("reject")}>Reject plan</button>
                  <button className="btn primary" disabled={!enabled} title={off} onClick={() => setAsking("approve")}>Approve and run</button>
                </div>
              )}
            </div>
          </header>

          <section className="steps" aria-label="Where this incident is in the healing loop">
            {data.steps.map((s, i) => (
              <div key={s.name} className={`step ${s.state}`}>
                <div className="c">{s.state === "done" ? "✓" : i + 1}</div>
                <div>
                  <div className="nm">{s.name}</div>
                  <div className="sb">{s.state === "done" && s.at ? `done ${clock(s.at)}` : s.sub}</div>
                </div>
              </div>
            ))}
          </section>

          <section className="row" style={{ alignItems: "flex-start" }}>
            <div className="stack" style={{ flex: "3 1 520px" }}>
              <div className="card">
                <div className="card-head">
                  <h2>Evidence</h2>
                  <span className="hint">{humanize(data.series.name)}, last {data.series.lookback_hours >= 48 ? `${Math.round(data.series.lookback_hours / 24)} days` : `${data.series.lookback_hours} hours`}</span>
                </div>
                <EvidenceChart
                  name={data.series.name}
                  points={data.series.points}
                  threshold={data.series.threshold}
                  baseline={data.series.baseline}
                  detectedAt={data.series.detected_at}
                  hours={data.series.lookback_hours}
                />
              </div>

              <div className="card">
                <div className="card-head">
                  <h2>What triggered it</h2>
                  <Planned>DISTRIBUTION VIEW PLANNED</Planned>
                </div>
                <div style={{ marginTop: 12 }}>
                  {data.evidence.map((o, i) => (
                    <div className="obs" key={i}>
                      <span><span className="mono">{o.name}</span> <span className="hint">· {o.kind}</span></span>
                      <span className="mono" style={{ color: "var(--warn)" }}>{o.value === null ? "n/a" : o.value}</span>
                    </div>
                  ))}
                </div>
                <div className="hint" style={{ marginTop: 12, lineHeight: 1.5 }}>
                  A side-by-side of the training and live distributions needs the drift toolbox, which is not built yet.
                </div>
              </div>

              <div className="card">
                <h2>What the agent did, in order</h2>
                {data.timeline.length === 0 ? (
                  <div className="empty">
                    No audit steps are stored for this incident. Point the dashboard at the audit log with --audit.
                  </div>
                ) : (
                  <div style={{ marginTop: 16 }}>
                    {data.timeline.map((e, i) => (
                      <div className="ev" key={i}>
                        <div className="tm">{clock(e.at)}</div>
                        <div style={{ minWidth: 0 }}>
                          <Tag color={KIND_COLOR[e.kind] ?? "var(--muted)"}>{e.kind}</Tag>
                          <div className="tx">{e.text}</div>
                          <div className="tl">{e.tool}</div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>

            <div className="stack" style={{ flex: "2 1 340px" }}>
              {action ? (
                <div className="plan">
                  <div className="k">PROPOSED ACTION</div>
                  <h2>{action.title}</h2>
                  <div className="why">{action.rationale}</div>
                  <div className="kv">
                    {Object.entries(action.params).map(([k, v]) => (
                      <div key={k}><span className="a">{humanize(k)}</span><span className="b">{v}</span></div>
                    ))}
                    {action.expected && <div><span className="a">Expected result</span><span className="b">{action.expected}</span></div>}
                    {action.risks.length > 0 && <div><span className="a">Risks</span><span className="b">{action.risks.join(" ")}</span></div>}
                    {action.expires_at && <div><span className="a">Answer needed</span><span className="b">{until(now, action.expires_at)}</span></div>}
                  </div>
                  {canAnswer ? (
                    <>
                      <div className="btns">
                        <button className="btn primary" disabled={!enabled} title={off} onClick={() => setAsking("approve")}>Approve and run</button>
                        <button className="btn ghost on-plan" disabled title="Editing a plan is planned for a later release">Edit plan</button>
                      </div>
                      {!enabled && (
                        <div className="hint" style={{ marginTop: 12, lineHeight: 1.5, color: "var(--accent-soft)" }}>
                          Answers from this page are off. Use <span className="mono">healops approve {inc.id.slice(0, 8)} --by {shell?.user}</span>, or start the dashboard with <span className="mono">--allow-actions</span>.
                        </div>
                      )}
                    </>
                  ) : (
                    <div className="hint" style={{ marginTop: 14, color: "var(--accent-soft)" }}>This plan is not waiting for an answer.</div>
                  )}
                </div>
              ) : (
                <div className="card">
                  <h2>No action proposed yet</h2>
                  <div className="empty">The agent has not proposed a fix for this incident.</div>
                </div>
              )}

              {data.checks.length > 0 && (
                <div className="card">
                  <h2>Guardrail checks</h2>
                  <div className="checks">
                    {data.checks.map((c, i) => (
                      <div className="it" key={i}>
                        <Tag color={CHECK_COLOR[c.tag] ?? "var(--muted)"}>{c.tag}</Tag>
                        <span className="tx">{c.text}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {data.alternatives.length > 0 && (
                <div className="card">
                  <h2>Other options the agent weighed</h2>
                  <div>
                    {data.alternatives.map((a, i) => (
                      <div className="alt" key={i}>{a}</div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </section>

          {asking && action && (
            <Confirm
              title={asking === "approve" ? "Approve and run?" : "Reject this plan?"}
              body={
                asking === "approve"
                  ? `HealOps will carry out "${action.title}" on ${inc.app} through its usual policy, audit and verification path.`
                  : `The incident will be escalated to a person and "${action.title}" will not run.`
              }
              confirmLabel={asking === "approve" ? "Approve and run" : "Reject plan"}
              askReason={asking === "reject"}
              busy={busy}
              onCancel={() => setAsking(null)}
              onConfirm={(reason) => void answer(asking, reason)}
            />
          )}
          {toast && <div className={"toast" + (toast.bad ? " err" : "")} role="status">{toast.text}</div>}
        </>
      )}
    </Page>
  );
}
