import type { ReactNode } from "react";
import { useApi } from "./api";
import { ago } from "./format";
import type { IntegrationsPage } from "./types";
import { Header, Loading, Page } from "./ui";

function Status({ label, color }: { label: string; color: string }) {
  return (
    <span style={{ display: "flex", alignItems: "center", gap: 7, color, fontSize: 12 }}>
      <span className="dot" style={{ width: 7, height: 7, background: color }} />
      {label}
    </span>
  );
}

function Card({ kind, status, title, children }: { kind: string; status: ReactNode; title: string; children: ReactNode }) {
  return (
    <section className="card" style={{ flex: "1 1 300px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span className="mono hint">{kind}</span>
        {status}
      </div>
      <h2 style={{ fontSize: 22, margin: "14px 0 8px" }}>{title}</h2>
      <div style={{ color: "var(--soft)", fontSize: 14, lineHeight: 1.6 }}>{children}</div>
    </section>
  );
}

function Switch({ on }: { on: boolean }) {
  return (
    <span aria-hidden style={{ width: 40, height: 22, borderRadius: 11, background: on ? "var(--ok)" : "var(--line-strong)", position: "relative", flex: "0 0 auto" }}>
      <span style={{ position: "absolute", top: 3, left: on ? 21 : 3, width: 16, height: 16, borderRadius: "50%", background: "#fff" }} />
    </span>
  );
}

export function Integrations() {
  const { data, error } = useApi<IntegrationsPage>("/api/integrations");
  const shell = data?.shell ?? null;
  const a = data?.adapter;
  const live = a?.status === "connected";
  return (
    <Page active="integrations" shell={shell} error={error}>
      <Header
        crumb={`${shell?.environment || "local"} / integrations`}
        title="Integrations"
        shell={shell}
        lede="Adapters teach HealOps how to read your metrics and how to retrain, roll back or restart your models."
      />
      {!data || !a ? (
        <Loading />
      ) : (
        <>
          <div className="row" style={{ marginTop: 24 }}>
            <Card
              kind="ADAPTER"
              title="Your application"
              status={
                <Status
                  label={live ? "Connected" : a.status === "silent" ? "No recent readings" : "Nothing seen yet"}
                  color={live ? "var(--ok)" : a.status === "silent" ? "var(--warn)" : "var(--muted)"}
                />
              }
            >
              {a.apps.length === 0
                ? "HealOps has not received a reading from an application yet."
                : `Reads accuracy and drift for ${a.apps.length} ${a.apps.length === 1 ? "model" : "models"} and runs the fixes you allow. Last reading ${ago(data.shell.now, a.last_seen)}.`}
            </Card>
            <Card kind="SDK" title="Python SDK" status={<Status label="Available" color="var(--ok)" />}>
              Report metrics and drift straight from your own code with <span className="mono">pip install healops</span>.
            </Card>
            <Card kind="ADAPTER" title="Prometheus" status={<Status label="Planned" color="var(--muted)" />}>
              Pull infrastructure metrics so the agent can tell code faults from data faults.
            </Card>
          </div>

          <section className="card" style={{ marginTop: 16 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span className="mono hint">WEBHOOK</span>
              <Status label="Not available yet" color="var(--muted)" />
            </div>
            <h2 style={{ fontSize: 22, margin: "14px 0 8px" }}>Custom webhook</h2>
            <div style={{ color: "var(--soft)", fontSize: 14 }}>Send incidents and agent actions to any system that accepts HTTP requests. This is planned and not built yet.</div>
          </section>

          <div className="row">
            <section className="card" style={{ flex: "3 1 420px" }}>
              <h2>Dashboard access</h2>
              <div className="kv">
                <div style={{ borderTopColor: "var(--line-soft)" }}>
                  <span className="a" style={{ color: "var(--soft)" }}>Signed in as</span>
                  <span className="b">{data.user}</span>
                </div>
                <div style={{ borderTopColor: "var(--line-soft)" }}>
                  <span className="a" style={{ color: "var(--soft)" }}>Approve, reject and mark resolved</span>
                  <span className="b" style={{ color: data.actions_enabled ? "var(--ok)" : "var(--warn)" }}>{data.actions_enabled ? "on" : "off"}</span>
                </div>
              </div>
              <div className="hint" style={{ marginTop: 14, lineHeight: 1.6 }}>
                {data.actions_enabled
                  ? "Anyone who can open this page can act on incidents. Keep it on a trusted network."
                  : "The dashboard was started read only. Start it with --allow-actions to let people act from here."}
                {" "}Access keys are not part of the dashboard yet.
              </div>
            </section>
            <section className="card" style={{ flex: "2 1 320px" }}>
              <h2>Notifications</h2>
              {[
                { name: "Email", text: "Critical incidents and approvals", on: data.email === true, note: data.email === null ? "unknown without a config file" : "" },
                { name: "Telegram", text: "Approvals waiting for you", on: false, note: "planned" },
                { name: "Slack", text: "Everything the agent does", on: false, note: "planned" },
              ].map((n) => (
                <div key={n.name} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, borderTop: "1px solid var(--line-soft)", padding: "14px 0", marginTop: n.name === "Email" ? 14 : 0 }}>
                  <div>
                    <div style={{ fontWeight: 600, fontSize: 14 }}>{n.name}</div>
                    <div className="hint">{n.text}{n.note ? ` · ${n.note}` : ""}</div>
                  </div>
                  <Switch on={n.on} />
                </div>
              ))}
              <div className="hint">Email is set in the config file. These switches only show the current state.</div>
            </section>
          </div>
        </>
      )}
    </Page>
  );
}
