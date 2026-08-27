import { Building2, CheckCircle2, Fuel, LayoutDashboard, Mail } from "lucide-react";

export type AppPage = "dashboard" | "companies" | "template";

interface AppSidebarProps {
  activePage: AppPage;
  mode: "checko" | "demo";
  gmailConfigured: boolean;
}

const navigation = [
  { page: "dashboard" as const, label: "Обзор", icon: LayoutDashboard },
  { page: "companies" as const, label: "Компании", icon: Building2 },
  { page: "template" as const, label: "Шаблон письма", icon: Mail },
];

export function AppSidebar({ activePage, mode, gmailConfigured }: AppSidebarProps) {
  return (
    <aside className="workspace-sidebar">
      <div className="sidebar-brand">
        <span className="sidebar-brand-mark" aria-hidden="true"><Fuel size={23} strokeWidth={2.3} /></span>
        <span>
          <strong>FuelLead</strong>
          <small>Поиск клиентов для<br />топливных карт</small>
        </span>
      </div>

      <nav className="sidebar-nav" aria-label="Основная навигация">
        {navigation.map(({ page, label, icon: Icon }) => (
          <a
            key={page}
            className={`sidebar-nav-item ${activePage === page ? "sidebar-nav-item--active" : ""}`}
            href={`#${page}`}
            aria-current={activePage === page ? "page" : undefined}
          >
            <Icon size={20} strokeWidth={1.9} />
            <span>{label}</span>
          </a>
        ))}
      </nav>

      <div className="sidebar-statuses">
        <div className="sidebar-status">
          <CheckCircle2 size={17} />
          <span>{mode === "checko" ? "Checko подключён" : "Демо-режим"}</span>
        </div>
        <div className={`sidebar-status ${gmailConfigured ? "" : "sidebar-status--muted"}`}>
          <Mail size={17} />
          <span>{gmailConfigured ? "Gmail подключён" : "Gmail не настроен"}</span>
        </div>
      </div>
    </aside>
  );
}
