import { Clock3 } from "lucide-react";
import type { CompanyDetail } from "../types";

interface CompanyDetailsProps {
  detail: CompanyDetail | null;
  loading: boolean;
}

function formatDateTime(value: string) {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "long",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

export function CompanyDetails({ detail, loading }: CompanyDetailsProps) {
  if (loading) {
    return <div className="details-loading">Загружаем историю компании…</div>;
  }
  if (!detail) return null;

  return (
    <div className="company-details">
      <section>
        <h3>Дополнительные ОКВЭД</h3>
        {detail.additional_okveds.length ? (
          <ul className="okved-list">
            {detail.additional_okveds.map((item) => (
              <li key={item.code || item.name}>
                <strong>{item.code}</strong>
                <span>{item.name}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted-copy">Дополнительные виды деятельности не указаны.</p>
        )}
        <dl className="timestamp-list">
          <div>
            <dt>Последняя проверка</dt>
            <dd>{formatDateTime(detail.last_checked_at)}</dd>
          </div>
          <div>
            <dt>Последнее обновление</dt>
            <dd>{formatDateTime(detail.last_updated_at)}</dd>
          </div>
        </dl>
      </section>

      <section className="history-section">
        <h3>История действий</h3>
        <ol className="timeline">
          {detail.history.map((event) => (
            <li key={event.id}>
              <span className="timeline-icon" aria-hidden="true"><Clock3 size={13} /></span>
              <div>
                <p>{event.description}</p>
                <time>{formatDateTime(event.created_at)}</time>
              </div>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}

