import { discoveryProviderLabels } from "../discoveryProviders";
import { useEffect, useRef, useState } from "react";
import { Ban, Building2, CheckCircle2, Inbox, LayoutDashboard, LogOut, Mail, MessagesSquare } from "lucide-react";
import type { DiscoveryProvider } from "../types";

export type AppPage = "dashboard" | "companies" | "template" | "mailboxes" | "suppressions" | "conversations";

interface AppSidebarProps {
  activePage: AppPage;
  mode: DiscoveryProvider;
  mailboxesConfigured: boolean;
  userEmail: string;
  onLogout: () => void;
  unreadReplies?: number;
}

const navigation = [
  { page: "dashboard" as const, label: "Обзор", icon: LayoutDashboard },
  { page: "companies" as const, label: "Компании", icon: Building2 },
  { page: "conversations" as const, label: "Переписка", icon: MessagesSquare },
  { page: "template" as const, label: "Шаблон письма", icon: Mail },
  { page: "mailboxes" as const, label: "Почтовые ящики", icon: Inbox },
  { page: "suppressions" as const, label: "Исключения", icon: Ban },
];

export function AppSidebar({ activePage, mode, mailboxesConfigured, userEmail, onLogout, unreadReplies = 0 }: AppSidebarProps) {
  const providerLabel = discoveryProviderLabels[mode];
  const [menuOpen, setMenuOpen] = useState(false);
  const sidebarRef = useRef<HTMLElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);

  const closeMenu = () => {
    setMenuOpen(false);
    toggleRef.current?.focus();
  };

  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 901px)");
    const resetMenu = () => setMenuOpen(false);
    desktop.addEventListener("change", resetMenu);
    window.addEventListener("hashchange", resetMenu);
    return () => {
      desktop.removeEventListener("change", resetMenu);
      window.removeEventListener("hashchange", resetMenu);
    };
  }, []);

  useEffect(() => {
    if (!menuOpen) return;
    const handleOutsidePress = (event: PointerEvent) => {
      if (event.target instanceof Node && !sidebarRef.current?.contains(event.target)) setMenuOpen(false);
    };
    document.addEventListener("pointerdown", handleOutsidePress);
    return () => document.removeEventListener("pointerdown", handleOutsidePress);
  }, [menuOpen]);

  return (
    <aside
      ref={sidebarRef}
      className={`workspace-sidebar ${menuOpen ? "workspace-sidebar--menu-open" : ""}`}
      onKeyDown={(event) => {
        if (event.key === "Escape" && menuOpen) {
          event.preventDefault();
          closeMenu();
        }
      }}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setMenuOpen(false);
      }}
    >
      <div className="sidebar-brand">
        <span className="sidebar-brand-mark" aria-hidden="true"><img className="app-icon" src="/icons/fuellead-192.png" alt="" width={48} height={48} /></span>
        <span>
          <strong>FuelLead</strong>
          <small>Поиск клиентов для<br />топливных карт</small>
        </span>
      </div>

      <button
        ref={toggleRef}
        className="sidebar-menu-toggle"
        type="button"
        aria-label={menuOpen ? "Закрыть меню" : "Открыть меню"}
        aria-expanded={menuOpen}
        aria-controls="workspace-navigation"
        onClick={() => setMenuOpen((open) => !open)}
      >
        <span aria-hidden="true" />
        <span aria-hidden="true" />
      </button>

      <div className="sidebar-menu" id="workspace-navigation">
        <nav className="sidebar-nav" aria-label="Основная навигация">
          {navigation.map(({ page, label, icon: Icon }) => (
            <a
              key={page}
              className={`sidebar-nav-item ${activePage === page ? "sidebar-nav-item--active" : ""}`}
              href={`#${page}`}
              aria-current={activePage === page ? "page" : undefined}
              onClick={() => { if (menuOpen) closeMenu(); }}
            >
              <Icon size={20} strokeWidth={1.9} />
              <span>{label}</span>
              {page === "conversations" && unreadReplies > 0 ? <span className="mail-unread-badge" aria-label={`${unreadReplies} непрочитанных ответов`}>{unreadReplies}</span> : null}
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
            <span>{mailboxesConfigured ? "Почта готова" : "Почта не настроена"}</span>
          </div>
        </div>

        <div className="sidebar-account">
          <span className="sidebar-account-avatar" aria-hidden="true">{userEmail.slice(0, 1).toUpperCase()}</span>
          <span className="sidebar-account-copy"><strong>В системе</strong><small>{userEmail}</small></span>
          <button type="button" onClick={onLogout} aria-label="Выйти из FuelLead" title="Выйти">
            <LogOut size={18} />
            <span className="sidebar-logout-label">Выйти</span>
          </button>
        </div>
      </div>
    </aside>
  );
}
