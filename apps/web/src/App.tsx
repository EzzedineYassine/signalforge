import { useCallback, useEffect, useState } from "react";
import { Layout, type RoutePath } from "./components/Layout";
import { OverviewPage } from "./pages/OverviewPage";
import { ServicesPage } from "./pages/ServicesPage";
import { TelemetryAnomaliesPage } from "./pages/TelemetryAnomaliesPage";
import { ApiError, getHealth } from "./services/api";
import type { HealthResponse } from "./types/api";

function parseRoute(pathname: string): RoutePath {
  if (pathname === "/services") {
    return "/services";
  }
  if (pathname === "/telemetry") {
    return "/telemetry";
  }
  return "/";
}

export function App() {
  const [currentPath, setCurrentPath] = useState<RoutePath>(() =>
    parseRoute(window.location.pathname),
  );
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);

  const checkHealth = useCallback(async () => {
    setHealthError(null);
    try {
      const res = await getHealth();
      setHealth(res);
    } catch (err) {
      setHealth(null);
      setHealthError(err instanceof ApiError ? err.detail : "Unable to reach SignalForge API.");
    }
  }, []);

  useEffect(() => {
    void checkHealth();
  }, [checkHealth]);

  useEffect(() => {
    const onPopState = () => {
      setCurrentPath(parseRoute(window.location.pathname));
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const navigate = useCallback((path: RoutePath) => {
    if (window.location.pathname !== path) {
      window.history.pushState({}, "", path);
    }
    setCurrentPath(path);
  }, []);

  return (
    <Layout
      currentPath={currentPath}
      onNavigate={navigate}
      health={health}
      healthError={healthError}
      onRefreshHealth={() => void checkHealth()}
    >
      {currentPath === "/" && <OverviewPage health={health} healthError={healthError} />}
      {currentPath === "/services" && <ServicesPage />}
      {currentPath === "/telemetry" && <TelemetryAnomaliesPage />}
    </Layout>
  );
}
export default App;
