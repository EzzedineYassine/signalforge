import type { ReactNode } from "react";
import { API_BASE_URL } from "../services/api";
import type { HealthResponse } from "../types/api";

export type RoutePath = "/" | "/services" | "/telemetry";

interface NavItem {
  path: RoutePath;
  label: string;
  tag: string;
}

const NAV_ITEMS: NavItem[] = [
  { path: "/", label: "Operations Overview", tag: "KPI" },
  { path: "/services", label: "Services & Dependencies", tag: "GRAPH" },
  { path: "/telemetry", label: "Telemetry & Anomalies", tag: "ML" },
];

interface LayoutProps {
  currentPath: RoutePath;
  onNavigate: (path: RoutePath) => void;
  health: HealthResponse | null;
  healthError: string | null;
  onRefreshHealth: () => void;
  children: ReactNode;
}

function getPageTitle(path: RoutePath): string {
  const found = NAV_ITEMS.find((item) => item.path === path);
  return found ? found.label : "SignalForge";
}

export function Layout({
  currentPath,
  onNavigate,
  health,
  healthError,
  onRefreshHealth,
  children,
}: LayoutProps) {
  let pillClass = "pill pill-neutral";
  let pillText = "Checking API...";

  if (healthError) {
    pillClass = "pill pill-danger";
    pillText = "API Unreachable";
  } else if (health) {
    if (health.status === "ok" && health.database === "connected") {
      pillClass = "pill pill-ok";
      pillText = "API OK · DB Connected";
    } else {
      pillClass = "pill pill-warn";
      pillText = `API ${health.status} · DB ${health.database}`;
    }
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">SF</span>
          <div>
            <div className="brand-title">SignalForge</div>
            <div className="brand-subtitle">Local AIOps Console</div>
          </div>
        </div>

        <nav className="nav-list" aria-label="Primary navigation">
          {NAV_ITEMS.map((item) => (
            <button
              key={item.path}
              type="button"
              className={`nav-item${currentPath === item.path ? " active" : ""}`}
              onClick={() => onNavigate(item.path)}
            >
              <span>{item.label}</span>
              <span className="mono">{item.tag}</span>
            </button>
          ))}
        </nav>

        <div className="sidebar-footer">
          <div className="kpi-label">Backend Endpoint</div>
          <div className="api-meta">{API_BASE_URL}</div>
        </div>
      </aside>

      <div className="main-area">
        <header className="topbar">
          <h1 className="topbar-title">{getPageTitle(currentPath)}</h1>
          <div className="topbar-actions">
            <span className={pillClass} title={healthError ?? "Live /health status"}>
              <span className="pill-dot" />
              {pillText}
            </span>
            <button type="button" className="btn" onClick={onRefreshHealth}>
              Check Health
            </button>
          </div>
        </header>

        <main className="content">{children}</main>
      </div>
    </div>
  );
}
