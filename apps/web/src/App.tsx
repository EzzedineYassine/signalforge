import {
  Component,
  useCallback,
  useEffect,
  useState,
  type ErrorInfo,
  type ReactNode,
} from "react";
import { Layout, type RoutePath } from "./components/Layout";
import { InvestigationsPage } from "./pages/InvestigationsPage";
import { KnowledgePage } from "./pages/KnowledgePage";
import { OverviewPage } from "./pages/OverviewPage";
import { ServicesPage } from "./pages/ServicesPage";
import { TelemetryAnomaliesPage } from "./pages/TelemetryAnomaliesPage";
import { TimelinePage } from "./pages/TimelinePage";
import { ApiError, getHealth } from "./services/api";
import type { HealthResponse } from "./types/api";

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

class AppErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("[SignalForge ErrorBoundary]", error, info.componentStack);
  }

  private handleReset = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) {
      return this.props.children;
    }

    return (
      <div
        style={{
          minHeight: "100vh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: 24,
        }}
      >
        <section className="card" style={{ maxWidth: 640, width: "100%" }} role="alert">
          <div className="card-header">
            <div>
              <h1 className="card-title">SignalForge UI Runtime Error</h1>
              <p className="card-subtitle">
                An unexpected rendering error occurred. Check the browser console for full stack
                details.
              </p>
            </div>
            <span className="pill pill-danger">CRASH CAUGHT</span>
          </div>

          <pre className="kb-excerpt" style={{ marginBottom: 14 }}>
            {error.name}: {error.message}
          </pre>

          <div className="chip-row">
            <button type="button" className="btn btn-primary" onClick={this.handleReset}>
              Try Again
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => window.location.reload()}
            >
              Reload Page
            </button>
          </div>
        </section>
      </div>
    );
  }
}

function parseRoute(pathname: string): RoutePath {
  switch (pathname) {
    case "/services":
      return "/services";
    case "/telemetry":
      return "/telemetry";
    case "/timeline":
      return "/timeline";
    case "/knowledge":
      return "/knowledge";
    case "/investigations":
      return "/investigations";
    default:
      return "/";
  }
}

function AppContent() {
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
      {currentPath === "/timeline" && <TimelinePage />}
      {currentPath === "/knowledge" && <KnowledgePage />}
      {currentPath === "/investigations" && <InvestigationsPage />}
    </Layout>
  );
}

export function App() {
  return (
    <AppErrorBoundary>
      <AppContent />
    </AppErrorBoundary>
  );
}
export default App;
