import {
  Clock3,
  Mail,
  MessageCircle,
  Phone,
  Plus,
  Send,
  Trash2,
} from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import type { CompanyContact, CompanyDetail, ContactType } from "../types";

interface CompanyDetailsProps {
  detail: CompanyDetail | null;
  loading: boolean;
  onContactAdd: (companyId: number, contactType: ContactType, value: string) => Promise<void>;
  onContactDelete: (companyId: number, contactId: number) => Promise<void>;
  onCompanyDelete: (company: CompanyDetail) => Promise<void>;
}

const contactLabels: Record<ContactType, string> = {
  phone: "Телефон",
  whatsapp: "WhatsApp",
  telegram: "Telegram",
};

const contactPlaceholders: Record<ContactType, string> = {
  phone: "+7 999 123-45-67",
  whatsapp: "+7 999 123-45-67",
  telegram: "@company_name",
};

function formatDateTime(value: string) {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "long",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function ContactIcon({ type }: { type: ContactType }) {
  if (type === "phone") return <Phone size={15} />;
  if (type === "whatsapp") return <MessageCircle size={15} />;
  return <Send size={15} />;
}

export function CompanyDetails({
  detail,
  loading,
  onContactAdd,
  onContactDelete,
  onCompanyDelete,
}: CompanyDetailsProps) {
  const [contactType, setContactType] = useState<ContactType>("phone");
  const [contactValue, setContactValue] = useState("");
  const [savingContact, setSavingContact] = useState(false);
  const [deletingContactId, setDeletingContactId] = useState<number | null>(null);
  const [deletingCompany, setDeletingCompany] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    setContactType("phone");
    setContactValue("");
    setActionError(null);
  }, [detail?.id]);

  if (loading) {
    return <div className="details-loading">Загружаем историю компании…</div>;
  }
  if (!detail) return null;

  const handleContactSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSavingContact(true);
    setActionError(null);
    try {
      await onContactAdd(detail.id, contactType, contactValue);
      setContactValue("");
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Не удалось добавить контакт");
    } finally {
      setSavingContact(false);
    }
  };

  const handleContactDelete = async (contact: CompanyContact) => {
    setDeletingContactId(contact.id);
    setActionError(null);
    try {
      await onContactDelete(detail.id, contact.id);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Не удалось удалить контакт");
    } finally {
      setDeletingContactId(null);
    }
  };

  const handleCompanyDelete = async () => {
    const confirmed = window.confirm(
      `Удалить «${detail.name}»? Компания исчезнет из базы и больше не появится при следующих поисках.`,
    );
    if (!confirmed) return;
    setDeletingCompany(true);
    setActionError(null);
    try {
      await onCompanyDelete(detail);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Не удалось удалить компанию");
      setDeletingCompany(false);
    }
  };

  return (
    <div className="company-details">
      <div className="details-primary">
        <section className="contacts-section">
          <h3>Способы связи</h3>
          <div className="contact-list">
            {detail.emails.map((email) => (
              <a className="contact-item" href={`mailto:${email.email}`} key={`email-${email.id}`}>
                <span className="contact-item-icon"><Mail size={15} /></span>
                <span><strong>Email</strong><small>{email.email} · {email.source}</small></span>
              </a>
            ))}
            {detail.contacts.map((contact) => (
              <div className="contact-item" key={contact.id}>
                <a href={contact.href} target={contact.contact_type === "phone" ? undefined : "_blank"} rel="noreferrer">
                  <span className="contact-item-icon"><ContactIcon type={contact.contact_type} /></span>
                  <span>
                    <strong>{contactLabels[contact.contact_type]}</strong>
                    <small>{contact.value} · {contact.source}</small>
                  </span>
                </a>
                {contact.source === "Вручную" ? (
                  <button
                    className="contact-delete"
                    type="button"
                    aria-label={`Удалить ${contactLabels[contact.contact_type]} ${contact.value}`}
                    disabled={deletingContactId === contact.id}
                    onClick={() => handleContactDelete(contact)}
                  >
                    <Trash2 size={14} />
                  </button>
                ) : null}
              </div>
            ))}
            {!detail.emails.length && !detail.contacts.length ? (
              <p className="muted-copy">Контакты пока не найдены. Добавьте их вручную.</p>
            ) : null}
          </div>

          <form className="contact-form" onSubmit={handleContactSubmit}>
            <label>
              <span>Тип</span>
              <select value={contactType} onChange={(event) => setContactType(event.target.value as ContactType)}>
                {Object.entries(contactLabels).map(([value, label]) => (
                  <option value={value} key={value}>{label}</option>
                ))}
              </select>
            </label>
            <label className="contact-value-field">
              <span>Контакт</span>
              <input
                type="text"
                value={contactValue}
                placeholder={contactPlaceholders[contactType]}
                autoComplete="off"
                required
                onChange={(event) => setContactValue(event.target.value)}
              />
            </label>
            <button className="contact-add" type="submit" disabled={savingContact || !contactValue.trim()}>
              <Plus size={15} /> {savingContact ? "Добавляем…" : "Добавить"}
            </button>
          </form>
          {actionError ? <p className="details-action-error" role="alert">{actionError}</p> : null}
        </section>

        <section className="okved-section">
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

        <section className="danger-zone">
          <div>
            <h3>Удаление компании</h3>
            <p>Компания будет удалена вместе с историей и добавлена в стоп-лист по ИНН.</p>
          </div>
          <button type="button" disabled={deletingCompany} onClick={handleCompanyDelete}>
            <Trash2 size={15} /> {deletingCompany ? "Удаляем…" : "Удалить фирму"}
          </button>
        </section>
      </div>

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
