import { discoveryProviderLabels } from "../discoveryProviders";
import { Ban, Building2, CheckCircle2, Fuel, Inbox, LayoutDashboard, LogOut, Mail } from "lucide-react";
import type { DiscoveryProvider } from "../types";

export type AppPage = "dashboard" | "companies" | "template" | "mailboxes" | "suppressions";

interface AppSidebarProps {
  activePage: AppPage;
  mode: DiscoveryProvider;
  mailboxesConfigured: boolean;
  userEmail: string;
  onLogout: () => void;
}

const navigation = [
  { page: "dashboard" as const, label: "Обзор", icon: LayoutDashboard },
  { page: "companies" as const, label: "Компании", icon: Building2 },
  { page: "template" as const, label: "Шаблон письма", icon: Mail },
  { page: "mailboxes" as const, label: "Почтовые ящики", icon: Inbox },
  { page: "suppressions" as const, label: "Исключения", icon: Ban },
];

export function AppSidebar({ activePage, mode, mailboxesConfigured, userEmail, onLogout }: AppSidebarProps) {
  const providerLabel = discoveryProviderLabels[mode];
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
          <span>{providerLabel}</span>
        </div>
        <div className={`sidebar-status ${mailboxesConfigured ? "" : "sidebar-status--muted"}`}>
          <Mail size={17} />
          <span>{mailboxesConfigured ? "Mail.ru готов" : "Mail.ru не настроен"}</span>
        </div>
      </div>

      <div className="sidebar-account">
        <span className="sidebar-account-avatar" aria-hidden="true">{userEmail.slice(0, 1).toUpperCase()}</span>
        <span className="sidebar-account-copy"><strong>В системе</strong><small>{userEmail}</small></span>
        <button type="button" onClick={onLogout} aria-label="Выйти из FuelLead" title="Выйти">
          <LogOut size={18} />
        </button>
      </div>
    </aside>
  );
}
