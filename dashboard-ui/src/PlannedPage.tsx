import { useApi } from "./api";
import type { Shell } from "./types";
import { Header, Page, Planned } from "./ui";

export interface PlannedInfo {
  key: "drift" | "models" | "agent" | "integrations";
  crumb: string;
  title: string;
  lede: string;
  items: string[];
  when: string;
}

export function PlannedPage({ info }: { info: PlannedInfo }) {
  const { data, error } = useApi<{ shell: Shell } & Record<string, unknown>>("/api/overview?range=24h");
  const shell = data?.shell ?? null;
  return (
    <Page active={info.key} shell={shell} error={error}>
      <Header crumb={`${shell?.environment || "local"} / ${info.crumb}`} title={info.title} shell={shell} lede={info.lede}>
        <Planned>{info.when}</Planned>
      </Header>
      <section className="card" style={{ marginTop: 24 }}>
        <h2>What this page will show</h2>
        <ul style={{ margin: "14px 0 0", paddingLeft: 20, color: "var(--soft)", fontSize: 14, lineHeight: 1.7 }}>
          {info.items.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
        <div className="hint" style={{ marginTop: 16, lineHeight: 1.5 }}>
          Nothing is faked here: this page stays empty until the feature exists and has real data to show.
        </div>
      </section>
    </Page>
  );
}

export const PLANNED: Record<string, PlannedInfo> = {
  "/drift": {
    key: "drift",
    crumb: "drift and data",
    title: "Drift and data quality",
    lede: "Which inputs have moved away from what the model was trained on, and whether the data feeding it is healthy.",
    items: [
      "A heat map of drift per feature over the last 14 days",
      "Data checks: schema, freshness, missing values, values out of range",
      "Training and live distributions side by side for one feature",
    ],
    when: "PLANNED FOR 0.3",
  },
  "/models": {
    key: "models",
    crumb: "models",
    title: "Model registry",
    lede: "Every model HealOps watches, its version, stage and how it compares with its baseline.",
    items: [
      "Stage (production, canary, shadow), version and last retrain",
      "Accuracy against the baseline each model must beat",
      "Version lineage with a ready rollback, and canary traffic share",
    ],
    when: "PLANNED FOR 0.3",
  },
  "/agent": {
    key: "agent",
    crumb: "agent",
    title: "Agent and policies",
    lede: "Decide how much the agent may do alone. Every action it takes is checked against these rules and written to the audit log.",
    items: [
      "Autonomy level per model class, from watch-only to act-alone",
      "Guardrails: retrain limits, cooldowns, cost budget",
      "The audit log, searchable",
    ],
    when: "PLANNED FOR 0.3",
  },
  "/integrations": {
    key: "integrations",
    crumb: "integrations",
    title: "Integrations",
    lede: "Adapters teach HealOps how to read your metrics and how to retrain, roll back or restart your models.",
    items: [
      "Connected adapters and their status",
      "Notification channels (email and others) with a test button",
      "Access keys for the dashboard",
    ],
    when: "PLANNED FOR 0.3",
  },
};
