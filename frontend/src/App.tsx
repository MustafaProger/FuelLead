import { AlertCircle, CheckCircle2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { CompanyTable } from "./components/CompanyTable";
import { FiltersBar } from "./components/FiltersBar";
import { Header } from "./components/Header";
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

export default function App() {
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
    <div className="app-shell">
      <Header
        mode={health?.mode || "demo"}
        searching={Boolean(searching)}
        exportUrl={exportUrl}
        onSearch={handleSearch}
      />
      <main className="main-content">
        <div className="page-intro">
          <div>
            <h1>Компании</h1>
            <p>Потенциальные клиенты с повышенным расходом топлива</p>
          </div>
          <span className="last-sync">Данные сохраняются в PostgreSQL</span>
        </div>

        {error && (
          <div className="notice notice--error" role="alert">
            <AlertCircle size={18} />
            <span>{error}</span>
            <button type="button" onClick={() => setError(null)} aria-label="Закрыть"><X size={17} /></button>
          </div>
        )}

        {searchRun && !searching && (
          <div className={`notice ${searchRun.status === "completed" ? "notice--success" : "notice--error"}`}>
            {searchRun.status === "completed" ? <CheckCircle2 size={18} /> : <AlertCircle size={18} />}
            <span>
              {searchRun.status === "completed"
                ? `Поиск завершён: добавлено ${searchRun.companies_created}, обновлено ${searchRun.companies_updated}`
                : searchRun.error_message || "Поиск завершился с ошибкой"}
            </span>
            <button type="button" onClick={() => setSearchRun(null)} aria-label="Закрыть"><X size={17} /></button>
          </div>
        )}

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
      </main>
    </div>
  );
}
