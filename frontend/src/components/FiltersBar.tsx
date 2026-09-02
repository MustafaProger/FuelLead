import { Search, X } from "lucide-react";
import type { CompanyStatus, Filters } from "../types";

interface FiltersBarProps {
  filters: Filters;
  total: number;
  loading: boolean;
  onChange: (filters: Filters) => void;
}

const statusTabs: Array<{ value: "" | CompanyStatus; label: string }> = [
  { value: "", label: "Все" },
  { value: "new", label: "Новые" },
  { value: "sent", label: "Отправленные" },
  { value: "answered", label: "Ответили" },
  { value: "interested", label: "Заинтересованы" },
  { value: "customer", label: "Работают с нами" },
  { value: "rejected", label: "Отказ" },
  { value: "error", label: "Ошибки отправки" },
];

export function FiltersBar({ filters, total, loading, onChange }: FiltersBarProps) {
  const set = <K extends keyof Filters>(key: K, value: Filters[K]) => onChange({ ...filters, [key]: value });
  const hasFilters = Object.values(filters).some(Boolean);

  return (
    <section className="filters-section" aria-label="Фильтры компаний">
      <div className="status-tabs" role="tablist" aria-label="Фильтр по статусу">
        {statusTabs.map((tab) => (
          <button
            className={`status-tab ${filters.status === tab.value ? "status-tab--active" : ""}`}
            type="button"
            role="tab"
            aria-selected={filters.status === tab.value}
            key={tab.value || "all"}
            onClick={() => set("status", tab.value)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className="filter-controls">
        <label className="search-field">
          <Search size={17} aria-hidden="true" />
          <span className="sr-only">Поиск по названию или ИНН</span>
          <input
            type="search"
            value={filters.search}
            onChange={(event) => set("search", event.target.value)}
            placeholder="Название или ИНН"
          />
        </label>

        <label className="select-field">
          <span className="sr-only">Наличие email</span>
          <select value={filters.hasEmail} onChange={(event) => set("hasEmail", event.target.value as Filters["hasEmail"])}>
            <option value="">Любой email</option>
            <option value="true">С email</option>
            <option value="false">Без email</option>
          </select>
        </label>

        <label className="select-field select-field--provider">
          <span className="sr-only">Тип почты</span>
          <select
            value={filters.emailProvider}
            onChange={(event) => set("emailProvider", event.target.value as Filters["emailProvider"])}
          >
            <option value="">Все типы почты</option>
            <option value="yandex">Яндекс</option>
            <option value="google">Google</option>
            <option value="mail_ru">Mail.ru</option>
            <option value="rambler">Rambler</option>
            <option value="other">Другие</option>
          </select>
        </label>

        <label className="select-field select-field--wide">
          <span className="sr-only">Категория деятельности</span>
          <select value={filters.category} onChange={(event) => set("category", event.target.value)}>
            <option value="">Все виды деятельности</option>
            <option value="freight">Грузоперевозки</option>
            <option value="road_construction">Дорожное строительство</option>
            <option value="construction">Строительство</option>
            <option value="agriculture">Сельское хозяйство</option>
            <option value="machinery">Спецтехника</option>
          </select>
        </label>

        <label className="date-field">
          <span className="sr-only">Дата обнаружения</span>
          <input
            type="date"
            value={filters.discoveredOn}
            onChange={(event) => set("discoveredOn", event.target.value)}
          />
        </label>

        {hasFilters && (
          <button
            type="button"
            className="clear-filters"
            onClick={() => onChange({ status: "", hasEmail: "", emailProvider: "", category: "", discoveredOn: "", search: "" })}
          >
            <X size={15} />
            Сбросить
          </button>
        )}
      </div>

      <div
        className={`filter-results ${loading ? "filter-results--loading" : ""}`}
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        <span>Найдено компаний</span>
        <strong>{total.toLocaleString("ru-RU")}</strong>
      </div>
    </section>
  );
}
