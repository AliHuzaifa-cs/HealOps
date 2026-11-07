export type Severity = "critical" | "high" | "medium" | "low";
export type Tone = "ok" | "warn" | "bad" | "heal" | "info" | "idle";

export interface Shell {
  now: string;
  workspace: string;
  environment: string;
  user: string;
  demo: boolean;
  actions_enabled: boolean;
  audit_ok: boolean;
  agent: { running: boolean; age_s: number | null; note: string };
  badges: { incidents: number; models: number; agent: number };
}

export interface IncidentRow {
  id: string;
  short: string;
  severity: Severity;
  title: string;
  app: string;
  type: string;
  status: string;
  state: string;
  state_tone: Tone;
  by: "agent" | "human" | "self" | "pending";
  detected_at: string;
  updated_at: string;
  closed_at: string | null;
  dry_run?: boolean;
}

export interface Overview {
  shell: Shell;
  range: string;
  loop: { no: number; name: string; note: string; n: number }[];
  kpis: {
    healthy: { healthy: number; total: number; attention: number; spark: number[] | null };
    open: { n: number; awaiting: number; spark: number[] | null };
    healed: { n: number; resolved: number; spark: number[] | null };
    mttr: { seconds: number | null; previous_seconds: number | null; samples: number };
  };
  tiles: { name: string; status: string; drift: number | null }[];
  live: IncidentRow[];
  drift_pulse: { at: string; value: number | null }[];
  drift_threshold: number | null;
  activity: { at: string; text: string }[];
}

export interface IncidentsPage {
  shell: Shell;
  range: string;
  counts: { open: number; awaiting: number; by_agent: number; by_human: number; by_self: number };
  rows: IncidentRow[];
  causes: { label: string; n: number; w: number }[];
  note: string | null;
}

export interface Detail {
  shell: Shell;
  incident: IncidentRow;
  can_resolve: boolean;
  steps: { name: string; state: "done" | "current" | "todo"; sub: string; at: string | null }[];
  series: {
    name: string;
    kind: string;
    points: { at: string; value: number }[];
    threshold: number | null;
    baseline: number | null;
    detected_at: string;
    lookback_hours: number;
  };
  evidence: { name: string; kind: string; value: number | null; at: string }[];
  timeline: { at: string; kind: string; text: string; tool: string }[];
  action: {
    type: string;
    title: string;
    rationale: string;
    expected: string;
    params: Record<string, string>;
    risks: string[];
    proposal_id: string;
    expires_at: string | null;
    can_decide: boolean;
  } | null;
  checks: { tag: string; text: string }[];
  alternatives: string[];
  reason: string | null;
}

export interface SignalItem {
  app: string;
  kind: string;
  name: string;
  label: string;
  latest: number;
  latest_at: string;
  min: number;
  max: number;
  avg: number;
  readings: number;
  threshold: number | null;
  breaches: number;
  points: { at: string; value: number }[];
}
export interface SignalsPage {
  shell: Shell;
  range: string;
  drift: SignalItem[];
  service: SignalItem[];
}

export interface AppRow {
  app: string;
  status: string;
  last_seen: string | null;
  readings_24h: number;
  signals: string[];
  drift: number | null;
  incidents: { total: number; open: number; by_agent: number; by_human: number; by_self: number };
}
export interface AppsPage {
  shell: Shell;
  apps: AppRow[];
  limits: { drift: number | null; metrics: Record<string, number> };
}

export interface AgentEvent {
  at: string;
  stage: string;
  app: string;
  incident: string;
  incident_id: string;
  text: string;
}
export interface AgentPageData {
  shell: Shell;
  setup: Record<string, string | number | boolean>;
  policies: { action: string; verdict: string; max_retries: number; cooldown_seconds: number }[];
  events: AgentEvent[];
  matched: number;
  total: number;
  query: string;
}
