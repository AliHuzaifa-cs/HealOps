export type Severity = "critical" | "high" | "medium" | "low";
export type Tone = "ok" | "warn" | "bad" | "heal" | "info" | "idle";

export interface Shell {
  now: string;
  workspace: string;
  environment: string;
  user: string;
  demo: boolean;
  actions_enabled: boolean;
  needs_token?: boolean;
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
  watched: boolean;
  points: { at: string; value: number }[];
}
export interface CheckTile {
  label: string;
  value: string;
  note: string;
  tag: string;
  tone: "ok" | "warn" | "bad" | "idle";
}
export interface HeatRow {
  app: string;
  name: string;
  cells: (number | null)[];
  latest: number | null;
}
export interface SignalsPage {
  shell: Shell;
  range: string;
  app: string | null;
  apps: string[];
  checks: CheckTile[];
  heatmap: { days: string[]; rows: HeatRow[]; hidden: number; limit: number };
  service: SignalItem[];
  more_service: number;
  related_incident: string | null;
}

export interface AppRow {
  app: string;
  status: string;
  last_seen: string | null;
  readings_24h: number;
  signals: string[];
  drift: number | null;
  last_retrain: string | null;
  changes: { at: string; text: string }[];
  incidents: { total: number; open: number; by_agent: number; by_human: number; by_self: number };
}
export interface AppsPage {
  shell: Shell;
  apps: AppRow[];
  gates: { label: string; tag: string; tone: "ok" | "warn" | "bad" }[];
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
export interface WaitingItem {
  id: string;
  short: string;
  title: string;
  severity: string;
  app: string;
  opened: string;
}
export interface AgentPageData {
  shell: Shell;
  waiting: WaitingItem[];
  audit_ok: boolean;
  dry_run: boolean | null;
  setup: Record<string, string | number | boolean>;
  policies: { action: string; verdict: string; max_retries: number; cooldown_seconds: number }[];
  events: AgentEvent[];
  matched: number;
  total: number;
  query: string;
}

export interface IntegrationsPage {
  shell: Shell;
  adapter: { apps: string[]; last_seen: string | null; status: "connected" | "silent" | "none" };
  email: boolean | null;
  actions_enabled: boolean;
  user: string;
  dry_run: boolean | null;
}
