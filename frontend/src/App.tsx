import { Database, Download, Mail, RefreshCw, Search } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { AppSidebar, type AppPage } from "./components/AppSidebar";
import { AuthPage } from "./components/AuthPage";
import { CompanyTable } from "./components/CompanyTable";
import { DashboardPage } from "./components/DashboardPage";
import { EmailTemplatePage } from "./components/EmailTemplatePage";
import { FiltersBar } from "./components/FiltersBar";
import { Notice } from "./components/Notice";
import { SearchRunNotice } from "./components/SearchRunNotice";
import { StatsStrip } from "./components/StatsStrip";
import type {
  Company,
  CompanyDetail,
  CompanyStatus,
  ContactType,
  Filters,
  Health,
  SearchRun,
  Stats,
} from "./types";

const defaultFilters: Filters = {
  status: "",
  hasEmail: "",
  emailProvider: "",
  category: "",
  discoveredOn: "",
  search: "",
};

const emptyStats: Stats = { total: 0, new: 0, with_email: 0, without_email: 0 };
const PAGE_SIZE = 20;

function pageFromHash(): AppPage {
  const page = window.location.hash.replace("#", "");
  return page === "companies" || page === "template" ? page : "dashboard";
}

interface WorkspaceProps {
  userEmail: string;
  onLogout: () => void;
}

function Workspace({ userEmail, onLogout }: WorkspaceProps) {
  const [activePage, setActivePage] = useState<AppPage>(pageFromHash);
  const [health, setHealth] = useState<Health | null>(null);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [stats, setStats] = useState<Stats>(emptyStats);
  const [total, setTotal] = useState(0);
  const [filters, setFilters] = useState<Filters>(defaultFilters);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<CompanyDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [searchRun, setSearchRun] = useState<SearchRun | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);

  useEffect(() => {
    const handleHashChange = () => setActivePage(pageFromHash());
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, []);

  const loadCompanies = useCallback(async () => {
    setLoading(true);
    try {
      const response = await api.companies(filters, page, PAGE_SIZE);
      setCompanies(response.items);
      setStats(response.stats);
      setTotal(response.total);
      setError(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Не удалось загрузить компании");
    } finally {
      setLoading(false);
    }
  }, [filters, page]);

  useEffect(() => {
    api.health().then(setHealth).catch((requestError) => {
      setError(requestError instanceof Error ? requestError.message : "Backend недоступен");
    });
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(loadCompanies, filters.search ? 280 : 0);
    return () => window.clearTimeout(timer);
  }, [loadCompanies, refreshToken, filters.search]);

  useEffect(() => {
    if (!searchRun || !["pending", "running"].includes(searchRun.status)) return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const current = await api.searchRun(searchRun.id);
        if (cancelled) return;
        setSearchRun(current);
        setSearchError(null);
        if (["pending", "running"].includes(current.status)) {
          timer = window.setTimeout(poll, 1200);
        } else {
          setRefreshToken((value) => value + 1);
        }
      } catch (requestError) {
        if (!cancelled) setSearchError(requestError instanceof Error ? requestError.message : "Не удалось проверить поиск");
      }
    };
    timer = window.setTimeout(poll, 800);
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [searchRun?.id, searchRun?.status]);

  const searching = searchRun?.status === "pending" || searchRun?.status === "running";
  const exportUrl = useMemo(() => api.exportUrl(filters), [filters]);

  const handleSearch = async () => {
    try {
      setSearchError(null);
      const run = await api.startSearch(
        health?.default_okved_codes || [],
        health?.discovery_limit_per_code || 10,
      );
      setSearchRun(run);
    } catch (requestError) {
      setSearchError(requestError instanceof Error ? requestError.message : "Не удалось запустить поиск");
    }
  };

  const handleFilters = (next: Filters) => {
    setFilters(next);
    setPage(1);
    setExpandedId(null);
    setDetail(null);
  };

  const handleToggle = async (id: number) => {
    if (expandedId === id) {
      setExpandedId(null);
      setDetail(null);
      return;
    }
    setExpandedId(id);
    setDetail(null);
    setDetailLoading(true);
    try {
      setDetail(await api.company(id));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Не удалось загрузить карточку компании");
    } finally {
      setDetailLoading(false);
    }
  };

  const handleStatusChange = async (id: number, status: CompanyStatus) => {
    try {
      const updated = await api.updateStatus(id, status);
      setCompanies((items) => items.map((company) => company.id === id ? { ...company, status } : company));
      if (detail?.id === id) setDetail(updated);
      setRefreshToken((value) => value + 1);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Не удалось изменить статус");
    }
  };

  const applyCompanyUpdate = (updated: CompanyDetail) => {
    setCompanies((items) => items.map((company) => company.id === updated.id ? updated : company));
    setDetail(updated);
  };

  const handleContactAdd = async (companyId: number, contactType: ContactType, value: string) => {
    applyCompanyUpdate(await api.addContact(companyId, contactType, value));
  };

  const handleContactDelete = async (companyId: number, contactId: number) => {
    applyCompanyUpdate(await api.deleteContact(companyId, contactId));
  };

  const handleCompanyDelete = async (company: CompanyDetail) => {
    await api.deleteCompany(company.id);
    setCompanies((items) => items.filter((item) => item.id !== company.id));
    setExpandedId(null);
    setDetail(null);
    if (companies.length === 1 && page > 1) {
      setPage((current) => current - 1);
    } else {
      setRefreshToken((value) => value + 1);
    }
  };

  return (
    <div className="workspace-shell">
      <AppSidebar
        activePage={activePage}
        mode={health?.mode || "demo"}
        gmailConfigured={Boolean(health?.gmail_oauth_configured)}
        userEmail={userEmail}
        onLogout={onLogout}
      />
      <main className="workspace-main">
        {activePage === "dashboard" ? (
          <DashboardPage
            exportUrl={exportUrl}
            searchError={searchError}
            searchRun={searchRun}
            searching={Boolean(searching)}
            refreshToken={refreshToken}
            onSearch={handleSearch}
            onCloseSearchError={() => setSearchError(null)}
            onCloseSearchRun={() => setSearchRun(null)}
          />
        ) : null}

        {activePage === "companies" ? (
          <div className="content-page companies-page">
            <header className="page-header">
              <div>
                <h1>Компании</h1>
                <p>Потенциальные клиенты с повышенным расходом топлива</p>
              </div>
              <div className="page-actions">
                <a className="button button--secondary" href={exportUrl} aria-label="Экспортировать компании в Excel">
                  <Download size={17} /> Экспорт
                </a>
                <button className="button button--primary" type="button" onClick={handleSearch} disabled={Boolean(searching)}>
                  {searching ? <RefreshCw className="spin" size={17} /> : <Search size={17} />}
                  {searching ? "Идёт поиск…" : "Найти компании"}
                </button>
              </div>
            </header>
            <div className="system-meta company-system-meta" aria-label="Состояние интеграций">
              <span><Database size={14} /> PostgreSQL</span>
              <span className={health?.gmail_oauth_configured ? "integration-ready" : "integration-pending"}>
                <Mail size={14} />
                <span>{health?.outreach_sender_email || "artel.office8@gmail.com"}<small>{health?.gmail_oauth_configured ? "Gmail OAuth подключён" : "Gmail OAuth ожидает настройки"}</small></span>
              </span>
            </div>

            {error ? <Notice tone="error" title="Не удалось выполнить действие" description={error} onClose={() => setError(null)} /> : null}
            <SearchRunNotice
              error={searchError}
              run={searchRun}
              onCloseError={() => setSearchError(null)}
              onCloseRun={() => setSearchRun(null)}
            />

            <StatsStrip stats={stats} loading={loading && !companies.length} />
            <FiltersBar filters={filters} total={total} loading={loading} onChange={handleFilters} />
            <CompanyTable
              companies={companies}
              total={total}
              page={page}
              pageSize={PAGE_SIZE}
              loading={loading}
              expandedId={expandedId}
              detail={detail}
              detailLoading={detailLoading}
              onToggle={handleToggle}
              onStatusChange={handleStatusChange}
              onContactAdd={handleContactAdd}
              onContactDelete={handleContactDelete}
              onCompanyDelete={handleCompanyDelete}
              onPageChange={setPage}
            />
          </div>
        ) : null}

        {activePage === "template" ? (
          <EmailTemplatePage
            gmailConfigured={Boolean(health?.gmail_oauth_configured)}
            senderEmail={health?.outreach_sender_email || "artel.office8@gmail.com"}
            onSent={() => setRefreshToken((value) => value + 1)}
          />
        ) : null}
      </main>
    </div>
  );
}

export default function App() {
  const [session, setSession] = useState<{ email: string } | null | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    api.authSession()
      .then((current) => {
        if (!cancelled) setSession({ email: current.email });
      })
      .catch(() => {
        if (!cancelled) setSession(null);
      });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    const handleUnauthorized = () => setSession(null);
    window.addEventListener("fuellead:unauthorized", handleUnauthorized);
    return () => window.removeEventListener("fuellead:unauthorized", handleUnauthorized);
  }, []);

  const handleLogout = async () => {
    try {
      await api.logout();
    } finally {
      setSession(null);
    }
  };

  if (session === undefined) {
    return (
      <main className="auth-loading" aria-label="Проверяем доступ">
        <span className="auth-brand-mark" aria-hidden="true"><RefreshCw className="spin" size={23} /></span>
        <strong>FuelLead</strong>
      </main>
    );
  }

  if (session === null) {
    return <AuthPage onAuthenticated={(current) => setSession({ email: current.email })} />;
  }

  return <Workspace userEmail={session.email} onLogout={handleLogout} />;
}
