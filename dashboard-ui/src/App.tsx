import { IncidentDetail } from "./IncidentDetail";
import { Incidents } from "./Incidents";
import { Overview } from "./Overview";
import { PLANNED, PlannedPage } from "./PlannedPage";
import { useRoute } from "./router";

export function App() {
  const route = useRoute();
  const path = route.path.replace(/\/+$/, "") || "/";
  if (path === "/") return <Overview route={route} />;
  if (path === "/incidents") return <Incidents route={route} />;
  if (path.startsWith("/incidents/")) return <IncidentDetail id={decodeURIComponent(path.slice("/incidents/".length))} />;
  if (PLANNED[path]) return <PlannedPage info={PLANNED[path]} />;
  return <Overview route={route} />;
}
