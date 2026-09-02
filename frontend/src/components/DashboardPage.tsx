import {
  ArrowRight,
  AlertTriangle,
  Building2,
  CheckCircle2,
  Handshake,
  Mail,
  RefreshCw,
  Search,
  Send,
  Users,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import { api } from "../api";
import type { CompanyStatus, DashboardResponse } from "../types";
import { Notice } from "./Notice";

interface DashboardPageProps {
  searching: boolean;
  refreshToken: number;
  onSearch: () => void;
  onOpenOutreach: () => void;
}

const statusLabels: Record<CompanyStatus, string> = {
  new: "Новая",
  sent: "Письмо отправлено",
  answered: "Ответила",
  interested: "Заинтересована",
  customer: "Работает с нами",
  rejected: "Отказ",
  error: "Ошибка отправки",
};

const funnel = [
  { key: "new" as const, label: "Новые", tone: "orange", icon: Users },
  { key: "sent" as const, label: "Отправлено", tone: "green", icon: Send },
  { key: "answered" as const, label: "Ответили", tone: "teal", icon: Mail },
  { key: "interested" as const, label: "Заинтересованы", tone: "amber", icon: CheckCircle2 },
  { key: "customer" as const, label: "Работают с нами", tone: "green", icon: Handshake },
  { key: "error" as const, label: "Ошибки отправки", tone: "red", icon: AlertTriangle },
];

function formatShortDate(value: string) {
  return new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", year: "numeric" })
    .format(new Date(value))
    .replace(" г.", "");
}

export function DashboardPage({
  searching,
  refreshToken,
  onSearch,
  onOpenOutreach,
}: DashboardPageProps) {
  const [data, setData] = useState<DashboardResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.dashboard()
      .then((response) => {
        if (!cancelled) {
          setData(response);
          setError(null);
        }
      })
      .catch((requestError) => {
        if (!cancelled) setError(requestError instanceof Error ? requestError.message : "Не удалось загрузить обзор");
      });
    return () => { cancelled = true; };
  }, [refreshToken]);

  const maxFunnel = Math.max(1, ...funnel.map((item) => data?.status_counts[item.key] || 0));
  const heatmap = useMemo(() => data?.daily_discoveries || [], [data?.daily_discoveries]);
  const heatmapColumns = Math.max(1, Math.ceil(heatmap.length / 7));
  const heatmapMinWidth = heatmapColumns * 20 + Math.max(0, heatmapColumns - 1) * 4;
  const maxDaily = Math.max(1, ...(data?.daily_discoveries.map((item) => item.count) || [0]));

  return (
    <div className="content-page dashboard-page">
      <header className="page-header">
        <div>
          <h1>Обзор</h1>
          <p>Воронка поиска и работы с потенциальными клиентами</p>
        </div>
        <div className="page-actions">
          <button className="button button--secondary" type="button" onClick={onOpenOutreach}>
            <Send size={17} /> Отправить письма
          </button>
          <button className="button button--primary" type="button" onClick={onSearch} disabled={searching}>
            {searching ? <RefreshCw className="spin" size={17} /> : <Search size={17} />}
            {searching ? "Идёт поиск…" : "Найти компании"}
          </button>
        </div>
      </header>

      {error ? <Notice tone="error" title="Не удалось загрузить дашборд" description={error} /> : null}

      <section className="dashboard-metrics" aria-label="Ключевые показатели">
        <Metric icon={Building2} label="Всего компаний" value={data?.metrics.total} tone="orange" />
        <Metric icon={Mail} label="С email" value={data?.metrics.with_email} tone="teal" />
        <Metric icon={Send} label="Новые для отправки" value={data?.metrics.new} tone="orange" />
        <Metric icon={CheckCircle2} label="Писем отправлено" value={data?.metrics.sent_emails} tone="teal" />
      </section>

      <section className="dashboard-main-grid">
        <article className="dashboard-panel activity-panel">
          <div className="panel-heading">
            <div><h2>Новые компании</h2><p>За последние 3 месяца</p></div>
          </div>
          <div className="heatmap-layout">
            <div className="heatmap-days" aria-hidden="true">
              {['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'].map((day) => <span key={day}>{day}</span>)}
            </div>
            <div className="heatmap-scroll">
              <div
                className="heatmap-chart"
                style={{ minWidth: `${heatmapMinWidth}px`, "--heatmap-columns": heatmapColumns } as CSSProperties}
              >
                <div className="heatmap-grid" role="img" aria-label="Количество найденных компаний по дням">
                  {heatmap.map((item) => (
                    <span
                      key={item.date}
                      className={`heatmap-cell heatmap-cell--has-tooltip heatmap-cell--${item.count ? Math.max(1, Math.ceil((item.count / maxDaily) * 4)) : 0}`}
                      title={`${formatShortDate(item.date)}. Найдено компаний: ${item.count}`}
                      aria-label={`${formatShortDate(item.date)}. Найдено компаний: ${item.count}`}
                      tabIndex={0}
                    />
                  ))}
                </div>
                <div className="heatmap-legend">
                  <span>Меньше</span>
                  {[0, 1, 2, 3, 4].map((level) => <i className={`heatmap-cell heatmap-cell--${level}`} key={level} />)}
                  <span>Больше</span>
                </div>
              </div>
            </div>
          </div>
        </article>

        <article className="dashboard-panel funnel-panel">
          <div className="panel-heading"><div><h2>Воронка</h2><p>Текущее состояние компаний</p></div></div>
          <div className="funnel-list">
            {funnel.map(({ key, label, tone, icon: Icon }) => {
              const count = data?.status_counts[key] || 0;
              return (
                <div className="funnel-row" key={key}>
                  <span className={`funnel-icon funnel-icon--${tone}`}><Icon size={18} /></span>
                  <div className="funnel-track-block">
                    <div><span>{label}</span><strong>{count.toLocaleString("ru-RU")}</strong></div>
                    <span className="funnel-track"><i className={`funnel-fill funnel-fill--${tone}`} style={{ width: `${(count / maxFunnel) * 100}%` }} /></span>
                  </div>
                </div>
              );
            })}
          </div>
        </article>
      </section>

      <section className="dashboard-panel recent-panel">
        <div className="panel-heading">
          <div><h2>Последние компании</h2><p>Недавно добавленные потенциальные клиенты</p></div>
          <a className="panel-link" href="#companies">Показать все <ArrowRight size={16} /></a>
        </div>
        <div className="recent-table-wrap">
          <table className="recent-table">
            <thead><tr><th>Компания</th><th>Дата добавления</th><th>Email</th><th>Статус</th><th /></tr></thead>
            <tbody>
              {(data?.recent_companies || []).map((company) => (
                <tr key={company.id}>
                  <td><span className="recent-company-icon"><Building2 size={16} /></span><strong>{company.name}</strong></td>
                  <td>{formatShortDate(company.first_discovered_at)}</td>
                  <td className={company.emails.length ? "email-available" : "email-missing"}>{company.emails.length ? "Есть" : "Нет"}</td>
                  <td><span className={`status-badge status-badge--${company.status}`}>{statusLabels[company.status]}</span></td>
                  <td><a href="#companies" aria-label={`Открыть ${company.name}`}><ArrowRight size={16} /></a></td>
                </tr>
              ))}
              {data && !data.recent_companies.length ? <tr><td colSpan={5} className="recent-empty">Компании появятся после первого поиска.</td></tr> : null}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function Metric({ icon: Icon, label, value, tone }: { icon: typeof Building2; label: string; value?: number; tone: "orange" | "teal" }) {
  return (
    <article className="metric-card">
      <span className={`metric-icon metric-icon--${tone}`}><Icon size={21} /></span>
      <span><small>{label}</small><strong>{value === undefined ? "—" : value.toLocaleString("ru-RU")}</strong></span>
    </article>
  );
}
