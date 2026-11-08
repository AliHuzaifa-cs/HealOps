import { AgentPolicies } from "./AgentPolicies";
import { IncidentDetail } from "./IncidentDetail";
import { Incidents } from "./Incidents";
import { Models } from "./Models";
import { Overview } from "./Overview";
import { Integrations } from "./Integrations";
import { PLANNED, PlannedPage } from "./PlannedPage";
import { useRoute } from "./router";
import { Signals } from "./Signals";

export function App() {
  const route = useRoute();
  const path = route.path.replace(/\/+$/, "") || "/";
  if (path === "/") return <Overview route={route} />;
  if (path === "/incidents") return <Incidents route={route} />;
  if (path.startsWith("/incidents/")) return <IncidentDetail id={decodeURIComponent(path.slice("/incidents/".length))} />;
  if (path === "/drift") return <Signals route={route} />;
  if (path === "/models") return <Models />;
  if (path === "/agent") return <AgentPolicies />;
  if (path === "/integrations") return <Integrations />;
  if (PLANNED[path]) return <PlannedPage info={PLANNED[path]} />;
  return <Overview route={route} />;
}
