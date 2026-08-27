import { Database, Download, Mail, RefreshCw, Search } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { AppSidebar, type AppPage } from "./components/AppSidebar";
import { CompanyTable } from "./components/CompanyTable";
import { DashboardPage } from "./components/DashboardPage";
import { EmailTemplatePage } from "./components/EmailTemplatePage";
import { FiltersBar } from "./components/FiltersBar";
import { Notice } from "./components/Notice";
import { StatsStrip } from "./components/StatsStrip";
import type {
  Company,
  CompanyDetail,
  CompanyStatus,
  Filters,
  Health,
  SearchRun,
  Stats,
} from "./types";

const defaultFilters: Filters = {
  status: "",
  hasEmail: "",
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

function splitMessage(message: string) {
  const separator = message.indexOf(". ");
  if (separator === -1) return { title: message, description: undefined };
  return {
    title: message.slice(0, separator),
    description: message.slice(separator + 2),
  };
}

export default function App() {
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
        if (["pending", "running"].includes(current.status)) {
          timer = window.setTimeout(poll, 1200);
        } else {
          setRefreshToken((value) => value + 1);
        }
      } catch (requestError) {
        if (!cancelled) setError(requestError instanceof Error ? requestError.message : "Не удалось проверить поиск");
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
  const searchResult = searchRun?.error_message ? splitMessage(searchRun.error_message) : null;

  const handleSearch = async () => {
    try {
      setError(null);
      const run = await api.startSearch(
        health?.default_okved_codes || [],
        health?.discovery_limit_per_code || 10,
      );
      setSearchRun(run);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Не удалось запустить поиск");
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

  return (
    <div className="workspace-shell">
      <AppSidebar
        activePage={activePage}
        mode={health?.mode || "demo"}
        gmailConfigured={Boolean(health?.gmail_oauth_configured)}
      />
      <main className="workspace-main">
        {activePage === "dashboard" ? (
          <DashboardPage
            exportUrl={exportUrl}
            searching={Boolean(searching)}
            refreshToken={refreshToken}
            onSearch={handleSearch}
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
            {searching ? (
              <Notice
                tone="progress"
                title="Поиск компаний запущен"
                description={searchRun?.candidates_found ? `Найдено кандидатов: ${searchRun.candidates_found}. Получаем карточки и контакты.` : "Проверяем целевые ОКВЭД и доступность данных в Checko."}
              />
            ) : null}
            {searchRun && !searching ? (
              searchRun.status === "completed" ? (
                <Notice
                  tone={searchRun.errors_count ? "warning" : "success"}
                  title={searchRun.errors_count ? "Поиск завершён с предупреждениями" : "Поиск завершён"}
                  description={`Добавлено ${searchRun.companies_created}, обновлено ${searchRun.companies_updated}${searchRun.errors_count ? `, ошибок провайдера: ${searchRun.errors_count}` : ""}.`}
                  onClose={() => setSearchRun(null)}
                />
              ) : (
                <Notice tone="error" title={searchResult?.title || "Поиск не выполнен"} description={searchResult?.description || "Checko не вернул компании."} onClose={() => setSearchRun(null)} />
              )
            ) : null}

            <StatsStrip stats={stats} loading={loading && !companies.length} />
            <FiltersBar filters={filters} onChange={handleFilters} />
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
