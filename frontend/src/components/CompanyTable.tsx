import { ChevronDown, ChevronLeft, ChevronRight, Mail } from "lucide-react";
import type { Company, CompanyDetail, CompanyStatus } from "../types";
import { CompanyDetails } from "./CompanyDetails";

interface CompanyTableProps {
  companies: Company[];
  total: number;
  page: number;
  pageSize: number;
  loading: boolean;
  expandedId: number | null;
  detail: CompanyDetail | null;
  detailLoading: boolean;
  onToggle: (id: number) => void;
  onStatusChange: (id: number, status: CompanyStatus) => void;
  onPageChange: (page: number) => void;
}

const statusLabels: Record<CompanyStatus, string> = {
  new: "Новая",
  checked: "Проверена",
  ready: "Готова",
};

function formatDiscovered(value: string) {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

export function CompanyTable(props: CompanyTableProps) {
  const totalPages = Math.max(1, Math.ceil(props.total / props.pageSize));

  if (!props.loading && !props.companies.length) {
    return (
      <div className="empty-state">
        <span className="empty-state-icon"><Mail size={22} /></span>
        <h2>Компаний пока нет</h2>
        <p>Запустите поиск или измените выбранные фильтры.</p>
      </div>
    );
  }

  return (
    <section className={`table-shell ${props.loading ? "table-shell--loading" : ""}`} aria-busy={props.loading}>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Компания</th>
              <th>ИНН / ОГРН</th>
              <th>Основной ОКВЭД</th>
              <th>Email</th>
              <th>Обнаружена</th>
              <th>Статус</th>
            </tr>
          </thead>
          <tbody>
            {props.companies.map((company) => (
              <CompanyRow key={company.id} company={company} {...props} />
            ))}
          </tbody>
        </table>
      </div>

      <footer className="pagination">
        <span>
          {props.total ? (props.page - 1) * props.pageSize + 1 : 0}–{Math.min(props.page * props.pageSize, props.total)} из {props.total}
        </span>
        <div className="pagination-actions">
          <button type="button" aria-label="Предыдущая страница" disabled={props.page <= 1} onClick={() => props.onPageChange(props.page - 1)}>
            <ChevronLeft size={18} />
          </button>
          <span>{props.page} / {totalPages}</span>
          <button type="button" aria-label="Следующая страница" disabled={props.page >= totalPages} onClick={() => props.onPageChange(props.page + 1)}>
            <ChevronRight size={18} />
          </button>
        </div>
      </footer>
    </section>
  );
}

function CompanyRow({
  company,
  expandedId,
  detail,
  detailLoading,
  onToggle,
  onStatusChange,
}: CompanyTableProps & { company: Company }) {
  const expanded = expandedId === company.id;
  return (
    <>
      <tr className={expanded ? "company-row company-row--expanded" : "company-row"}>
        <td data-label="Компания">
          <button className="company-name-button" type="button" onClick={() => onToggle(company.id)} aria-expanded={expanded}>
            <span className={`row-chevron ${expanded ? "row-chevron--open" : ""}`}><ChevronDown size={17} /></span>
            <span>
              <strong>{company.name}</strong>
              <small>{company.activity_category === "other" ? "Другая деятельность" : categoryLabel(company.activity_category)}</small>
            </span>
          </button>
        </td>
        <td data-label="ИНН / ОГРН" className="requisites-cell">
          <span>ИНН {company.inn}</span>
          <span>ОГРН {company.ogrn || "—"}</span>
        </td>
        <td data-label="Основной ОКВЭД" className="okved-cell">
          <strong>{company.primary_okved.code || "—"}</strong>
          <span>{company.primary_okved.name || "Не указан"}</span>
        </td>
        <td data-label="Email" className="email-cell">
          {company.emails.length ? company.emails.map((email) => (
            <span key={email.id}>
              <a href={`mailto:${email.email}`}>{email.email}</a>
              <small>{email.source}</small>
            </span>
          )) : <span className="not-found">Не найден</span>}
        </td>
        <td data-label="Обнаружена" className="date-cell">{formatDiscovered(company.first_discovered_at)}</td>
        <td data-label="Статус">
          <select
            className={`status-select status-select--${company.status}`}
            value={company.status}
            aria-label={`Статус ${company.name}`}
            onChange={(event) => onStatusChange(company.id, event.target.value as CompanyStatus)}
          >
            {Object.entries(statusLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select>
        </td>
      </tr>
      {expanded && (
        <tr className="details-row">
          <td colSpan={6}>
            <CompanyDetails detail={detail?.id === company.id ? detail : null} loading={detailLoading} />
          </td>
        </tr>
      )}
    </>
  );
}

function categoryLabel(category: string) {
  const labels: Record<string, string> = {
    freight: "Грузоперевозки",
    road_construction: "Дорожное строительство",
    construction: "Строительство",
    agriculture: "Сельское хозяйство",
    machinery: "Спецтехника",
  };
  return labels[category] || "Другая деятельность";
}
