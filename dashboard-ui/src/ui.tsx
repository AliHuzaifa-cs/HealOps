import type { ReactNode } from "react";
import { initials } from "./format";
import { link } from "./router";
import type { Shell, Tone } from "./types";

export const TONE: Record<Tone, string> = {
  ok: "var(--ok)",
  warn: "var(--warn)",
  bad: "var(--bad)",
  heal: "var(--accent)",
  info: "var(--info)",
  idle: "var(--muted)",
};
export const SEVERITY_COLOR: Record<string, string> = {
  critical: "var(--bad)",
  high: "var(--warn)",
  medium: "var(--info)",
  low: "var(--muted)",
};

const ICONS: Record<string, string> = {
  overview: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  incidents: "M12 3l10 18H2z M12 10v5 M12 18v.5",
  drift: "M3 17l5-6 4 4 5-8 4 5",
  models: "M12 2l9 5v10l-9 5-9-5V7z M12 12l9-5 M12 12v10 M12 12L3 7",
  agent:
    "M12 3a4 4 0 0 1 4 4v2h2a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2h2V7a4 4 0 0 1 4-4z M9 14h.01 M15 14h.01",
  integrations: "M9 7V3 M15 7V3 M6 7h12v5a6 6 0 0 1-12 0z M12 18v3",
};

type NavKey = "overview" | "incidents" | "drift" | "models" | "agent" | "integrations";
const GROUPS: { title: string; items: { key: NavKey; label: string; path: string }[] }[] = [
  {
    title: "MONITOR",
    items: [
      { key: "overview", label: "Overview", path: "/" },
      { key: "incidents", label: "Incidents", path: "/incidents" },
      { key: "drift", label: "Drift and data", path: "/drift" },
    ],
  },
  {
    title: "OPERATE",
    items: [
      { key: "models", label: "Models", path: "/models" },
      { key: "agent", label: "Agent and policies", path: "/agent" },
    ],
  },
  { title: "CONNECT", items: [{ key: "integrations", label: "Integrations", path: "/integrations" }] },
];

export function Sidebar({ active, shell }: { active: NavKey; shell: Shell | null }) {
  const badges: Partial<Record<NavKey, number>> = shell
    ? { incidents: shell.badges.incidents, models: shell.badges.models, agent: shell.badges.agent }
    : {};
  const running = shell?.agent.running ?? false;
  const age = shell?.agent.age_s ?? null;
  return (
    <aside className="side" aria-label="Main">
      <div className="brand">
        <svg viewBox="0 0 32 32" width="30" height="30" aria-hidden="true">
          <circle cx="16" cy="16" r="13" fill="none" stroke="#2dd4a8" strokeWidth="2.5" />
          <path d="M8 17 L13 17 L16 10 L19 23 L22 17 L25 17" fill="none" stroke="#e6eaf0" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        <span>HealOps</span>
      </div>

      <div className="workspace">
        <div className="k">WORKSPACE</div>
        <div className="v">{shell?.workspace ?? "…"}</div>
        <div className="s">{shell ? shell.environment || (shell.demo ? "sample data" : "local") : ""}</div>
      </div>

      <nav className="nav">
        {GROUPS.map((group) => (
          <div key={group.title}>
            <div className="nav-title">{group.title}</div>
            <div className="nav-items">
              {group.items.map((item) => {
                const count = badges[item.key];
                return (
                  <a
                    key={item.key}
                    href={link(item.path)}
                    className={"nav-item" + (item.key === active ? " on" : "")}
                    aria-current={item.key === active ? "page" : undefined}
                  >
                    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                      <path d={ICONS[item.key]} />
                    </svg>
                    <span className="label">{item.label}</span>
                    <span className="badge">{count ? count : ""}</span>
                  </a>
                );
              })}
            </div>
          </div>
        ))}
      </nav>

      <div className="side-foot">
        <div className={"watch" + (running ? "" : " off")}>
          <div className="t">
            <span className={"dot" + (running ? " pulse" : "")} style={{ background: running ? "var(--accent)" : "var(--warn)", animation: running ? "pulse 2s ease-in-out infinite" : undefined }} />
            {shell === null ? "Checking the agent" : running ? "Agent is watching" : age === null ? "Agent has not reported" : "Agent is not running"}
          </div>
          <div className="d">
            {shell === null
              ? ""
              : running
                ? shell.agent.note
                : age === null
                  ? "HealOps has not written a heartbeat to this state file yet."
                  : `Last seen ${Math.max(1, Math.round(age / 60))} min ago. Nothing will act until it runs again.`}
          </div>
        </div>
        <div className="me">
          <div className="avatar">{initials(shell?.user ?? "?")}</div>
          <div>
            <div className="n">{shell?.user ?? ""}</div>
            <div className="r">{shell?.actions_enabled ? "Operator" : "Viewer"}</div>
          </div>
        </div>
      </div>
    </aside>
  );
}

export function Page({
  active,
  shell,
  error,
  children,
}: {
  active: NavKey;
  shell: Shell | null;
  error: string | null;
  children: ReactNode;
}) {
  return (
    <div className="app">
      <div className="side-slot">
        <Sidebar active={active} shell={shell} />
      </div>
      <main className="main">
        {error && (
          <div className="banner err" role="alert">
            Cannot read the latest data: {error}. The page keeps trying every 10 seconds.
          </div>
        )}
        {shell && !shell.audit_ok && (
          <div className="banner" role="status">
            The audit log could not be read (a line may be damaged), so step-by-step histories are empty.
          </div>
        )}
        {children}
      </main>
    </div>
  );
}

export function Header({
  crumb,
  title,
  shell,
  children,
  lede,
}: {
  crumb: ReactNode;
  title: string;
  shell: Shell | null;
  children?: ReactNode;
  lede?: string;
}) {
  return (
    <header className="head">
      <div>
        <div className="crumb">{crumb}</div>
        <h1>{title}</h1>
        {lede && <p className="lede">{lede}</p>}
      </div>
      <div className="tools">
        {shell?.demo && <span className="sample">SAMPLE DATA</span>}
        {children}
      </div>
    </header>
  );
}

export function Tag({ color, children, big }: { color: string; children: ReactNode; big?: boolean }) {
  return (
    <span className={"tag" + (big ? " big" : "")} style={{ color }}>
      {children}
    </span>
  );
}

export function Planned({ children }: { children?: ReactNode }) {
  return <span className="tag planned">{children ?? "PLANNED"}</span>;
}

export function Spark({ values, color }: { values: number[] | null; color: string }) {
  if (!values || values.length < 2) return <svg width="120" height="40" aria-hidden="true" />;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 1;
  const points = values
    .map((v, i) => `${((i / (values.length - 1)) * 120).toFixed(1)},${(34 - ((v - lo) / span) * 28).toFixed(1)}`)
    .join(" ");
  return (
    <svg viewBox="0 0 120 40" width="120" height="40" preserveAspectRatio="none" aria-hidden="true">
      <polyline points={points} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export function Loading() {
  return (
    <div aria-busy="true">
      <div className="skeleton" />
      <div className="skeleton" style={{ width: "70%" }} />
    </div>
  );
}
