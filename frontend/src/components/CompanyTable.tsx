import { useEffect, useMemo, useRef, useState } from "react";
import { Building2, ChevronDown, ChevronLeft, ChevronRight, Columns3, LoaderCircle, Mail, MessageCircle, Phone, RotateCcw, Send } from "lucide-react";
import type { Company, CompanyDetail, CompanyStatus, ContactType } from "../types";
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
  onContactAdd: (companyId: number, contactType: ContactType, value: string) => Promise<void>;
  onContactDelete: (companyId: number, contactId: number) => Promise<void>;
  onCompanyDelete: (company: CompanyDetail) => Promise<void>;
  mailConfigured: boolean;
  sendingEmailId: number | null;
  onSendEmail: (company: Company) => Promise<void>;
  onPageChange: (page: number) => void;
}

const statusLabels: Record<CompanyStatus, string> = {
  new: "Новая",
  sent: "Письмо отправлено",
  answered: "Ответила",
  interested: "Заинтересована",
  customer: "Работает с нами",
  rejected: "Отказ",
};

type CompanyColumnId =
  | "company"
  | "requisites"
  | "primaryOkved"
  | "contacts"
  | "discovered"
  | "status"
  | "category"
  | "lastChecked"
  | "updated"
  | "activity"
  | "emailAction";

interface CompanyColumn {
  id: CompanyColumnId;
  label: string;
  width: number;
  defaultVisible: boolean;
  locked?: boolean;
}

const COMPANY_COLUMNS: CompanyColumn[] = [
  { id: "company", label: "Компания", width: 260, defaultVisible: true, locked: true },
  { id: "requisites", label: "ИНН / ОГРН", width: 155, defaultVisible: true },
  { id: "primaryOkved", label: "Основной ОКВЭД", width: 240, defaultVisible: true },
  { id: "contacts", label: "Связь", width: 245, defaultVisible: true },
  { id: "discovered", label: "Обнаружена", width: 135, defaultVisible: true },
  { id: "status", label: "Статус", width: 185, defaultVisible: true },
  { id: "category", label: "Категория", width: 170, defaultVisible: false },
  { id: "lastChecked", label: "Последняя проверка", width: 150, defaultVisible: false },
  { id: "updated", label: "Обновлена", width: 150, defaultVisible: false },
  { id: "activity", label: "Активность", width: 125, defaultVisible: false },
  { id: "emailAction", label: "Письмо", width: 140, defaultVisible: true, locked: true },
];

const COLUMN_STORAGE_KEY = "fuellead.company-table.columns.v1";

function defaultVisibleColumns() {
  return new Set<CompanyColumnId>(COMPANY_COLUMNS.filter((column) => column.defaultVisible).map((column) => column.id));
}

function initialVisibleColumns() {
  if (typeof window === "undefined") return defaultVisibleColumns();

  try {
    const stored = JSON.parse(window.localStorage.getItem(COLUMN_STORAGE_KEY) || "null");
    if (!Array.isArray(stored)) return defaultVisibleColumns();

    const availableIds = new Set(COMPANY_COLUMNS.map((column) => column.id));
    const restored = new Set<CompanyColumnId>(
      stored.filter((value): value is CompanyColumnId => typeof value === "string" && availableIds.has(value as CompanyColumnId)),
    );
    COMPANY_COLUMNS.filter((column) => column.locked).forEach((column) => restored.add(column.id));
    return restored;
  } catch {
    return defaultVisibleColumns();
  }
}

function formatDateTime(value: string | null | undefined) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";

  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function CompanyTable(props: CompanyTableProps) {
  const totalPages = Math.max(1, Math.ceil(props.total / props.pageSize));
  const [visibleColumns, setVisibleColumns] = useState<Set<CompanyColumnId>>(initialVisibleColumns);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const settingsRef = useRef<HTMLDivElement>(null);
  const orderedColumns = useMemo(
    () => COMPANY_COLUMNS.filter((column) => visibleColumns.has(column.id)),
    [visibleColumns],
  );
  const tableMinWidth = orderedColumns.reduce((total, column) => total + column.width, 0);

  useEffect(() => {
    window.localStorage.setItem(COLUMN_STORAGE_KEY, JSON.stringify(Array.from(visibleColumns)));
  }, [visibleColumns]);

  useEffect(() => {
    if (!settingsOpen) return;

    const handlePointerDown = (event: MouseEvent) => {
      if (!settingsRef.current?.contains(event.target as Node)) setSettingsOpen(false);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSettingsOpen(false);
    };

    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [settingsOpen]);

  const toggleColumn = (column: CompanyColumn) => {
    if (column.locked) return;
    setVisibleColumns((current) => {
      const next = new Set(current);
      if (next.has(column.id)) next.delete(column.id);
      else next.add(column.id);
      return next;
    });
  };

  const resetColumns = () => setVisibleColumns(defaultVisibleColumns());

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
      <div className="table-toolbar">
        <span className="table-toolbar-summary">Показано столбцов: {orderedColumns.length} из {COMPANY_COLUMNS.length}</span>
        <div className="column-settings" ref={settingsRef}>
          <button
            className="column-settings-trigger"
            type="button"
            aria-expanded={settingsOpen}
            aria-controls="company-column-settings"
            onClick={() => setSettingsOpen((value) => !value)}
          >
            <Columns3 size={16} />
            Столбцы
            <ChevronDown className={settingsOpen ? "column-settings-chevron column-settings-chevron--open" : "column-settings-chevron"} size={15} />
          </button>
          {settingsOpen ? (
            <div className="column-settings-menu" id="company-column-settings" aria-label="Настройка столбцов">
              <div className="column-settings-heading">
                <div>
                  <strong>Столбцы таблицы</strong>
                  <small>Выбор сохранится на этом устройстве</small>
                </div>
                <button type="button" className="column-settings-reset" onClick={resetColumns} aria-label="Сбросить столбцы">
                  <RotateCcw size={14} />
                </button>
              </div>
              <div className="column-settings-list">
                {COMPANY_COLUMNS.map((column) => (
                  <label className={column.locked ? "column-settings-option column-settings-option--locked" : "column-settings-option"} key={column.id}>
                    <input
                      type="checkbox"
                      checked={visibleColumns.has(column.id)}
                      disabled={column.locked}
                      onChange={() => toggleColumn(column)}
                    />
                    <span>{column.label}</span>
                    {!column.defaultVisible ? <small>доп.</small> : null}
                  </label>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      </div>
      <div className="table-scroll">
        <table className="company-table" style={{ minWidth: tableMinWidth }}>
          <colgroup>
            {orderedColumns.map((column) => <col key={column.id} style={{ width: column.width }} />)}
          </colgroup>
          <thead>
            <tr>
              {orderedColumns.map((column) => <th key={column.id}>{column.label}</th>)}
            </tr>
          </thead>
          <tbody>
            {props.companies.map((company) => (
              <CompanyRow key={company.id} company={company} visibleColumns={visibleColumns} columnCount={orderedColumns.length} {...props} />
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
  visibleColumns,
  columnCount,
  expandedId,
  detail,
  detailLoading,
  onToggle,
  onStatusChange,
  onContactAdd,
  onContactDelete,
  onCompanyDelete,
  mailConfigured,
  sendingEmailId,
  onSendEmail,
}: CompanyTableProps & { company: Company; visibleColumns: Set<CompanyColumnId>; columnCount: number }) {
  const expanded = expandedId === company.id;
  const sendingEmail = sendingEmailId === company.id;
  const hasEmail = company.emails.length > 0;
  const summaryLimit = 3;
  const visibleEmails = company.emails.slice(0, summaryLimit);
  const visibleContacts = company.contacts.slice(0, summaryLimit - visibleEmails.length);
  const hiddenContactCount = company.emails.length + company.contacts.length - visibleEmails.length - visibleContacts.length;

  return (
    <>
      <tr className={expanded ? "company-row company-row--expanded" : "company-row"}>
        <td data-label="Компания">
          <button className="company-name-button" type="button" onClick={() => onToggle(company.id)} aria-expanded={expanded}>
            <span className="company-icon" aria-hidden="true"><Building2 size={18} /></span>
            <span className="company-title">
              <strong>{company.name}</strong>
              <small>{company.activity_category === "other" ? "Другая деятельность" : categoryLabel(company.activity_category)}</small>
            </span>
            <span className={`row-chevron ${expanded ? "row-chevron--open" : ""}`}><ChevronDown size={17} /></span>
          </button>
        </td>
        {visibleColumns.has("requisites") ? (
          <td data-label="ИНН / ОГРН" className="requisites-cell">
            <span>ИНН {company.inn}</span>
            <span>ОГРН {company.ogrn || "—"}</span>
          </td>
        ) : null}
        {visibleColumns.has("primaryOkved") ? (
          <td data-label="Основной ОКВЭД" className="okved-cell">
            <strong>{company.primary_okved.code || "—"}</strong>
            <span>{company.primary_okved.name || "Не указан"}</span>
          </td>
        ) : null}
        {visibleColumns.has("contacts") ? (
          <td data-label="Связь" className="email-cell contact-summary-cell">
            {visibleEmails.map((email) => (
              <span key={email.id}>
                <a href={`mailto:${email.email}`}><Mail size={12} /> {email.email}</a>
                <small>{email.source}</small>
              </span>
            ))}
            {visibleContacts.map((contact) => (
              <span key={contact.id}>
                <a href={contact.href} target={contact.contact_type === "phone" ? undefined : "_blank"} rel="noreferrer">
                  {contact.contact_type === "phone" ? <Phone size={12} /> : null}
                  {contact.contact_type === "whatsapp" ? <MessageCircle size={12} /> : null}
                  {contact.contact_type === "telegram" ? <Send size={12} /> : null}
                  {contact.value}
                </a>
                <small>{contact.contact_type === "phone" ? "Телефон" : contact.contact_type === "whatsapp" ? "WhatsApp" : "Telegram"}</small>
              </span>
            ))}
            {hiddenContactCount > 0 ? (
              <button
                className="contact-overflow-button"
                type="button"
                aria-expanded={expanded}
                aria-label={`Показать все способы связи компании ${company.name}`}
                onClick={() => onToggle(company.id)}
              >
                Ещё {hiddenContactCount}
              </button>
            ) : null}
            {!company.emails.length && !company.contacts.length ? <span className="not-found">Не найдено</span> : null}
          </td>
        ) : null}
        {visibleColumns.has("discovered") ? <td data-label="Обнаружена" className="date-cell">{formatDateTime(company.first_discovered_at)}</td> : null}
        {visibleColumns.has("status") ? (
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
        ) : null}
        {visibleColumns.has("category") ? <td data-label="Категория">{categoryLabel(company.activity_category)}</td> : null}
        {visibleColumns.has("lastChecked") ? <td data-label="Последняя проверка" className="date-cell">{formatDateTime(company.last_checked_at)}</td> : null}
        {visibleColumns.has("updated") ? <td data-label="Обновлена" className="date-cell">{formatDateTime(company.last_updated_at)}</td> : null}
        {visibleColumns.has("activity") ? (
          <td data-label="Активность">
            <span className={company.is_active ? "company-activity company-activity--active" : "company-activity company-activity--inactive"}>
              {company.is_active ? "Действует" : "Неактивна"}
            </span>
          </td>
        ) : null}
        <td data-label="Письмо" className="send-email-cell">
          <button
            className="send-email-button"
            type="button"
            disabled={!hasEmail || !mailConfigured || sendingEmail}
            title={!hasEmail ? "У компании нет email" : !mailConfigured ? "Нет проверенного ящика Mail.ru" : "Отправить сохранённый шаблон на основной email компании"}
            aria-label={`Отправить письмо на основной email компании ${company.name}`}
            onClick={() => void onSendEmail(company)}
          >
            {sendingEmail ? <LoaderCircle className="spin" size={15} /> : <Send size={15} />}
            {sendingEmail ? "Отправка…" : "Отправить"}
          </button>
        </td>
      </tr>
      {expanded && (
        <tr className="details-row">
          <td colSpan={columnCount}>
            <CompanyDetails
              detail={detail?.id === company.id ? detail : null}
              loading={detailLoading}
              onContactAdd={onContactAdd}
              onContactDelete={onContactDelete}
              onCompanyDelete={onCompanyDelete}
            />
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
