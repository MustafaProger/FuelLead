import { AlertTriangle, Ban, LoaderCircle, Plus, Search, ShieldOff } from "lucide-react";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { EmailSuppression } from "../types";


export function SuppressionsPage() {
  const [items, setItems] = useState<EmailSuppression[]>([]);
  const [search, setSearch] = useState("");
  const [email, setEmail] = useState("");
  const [reason, setReason] = useState("");
  const [comment, setComment] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setItems(await api.emailSuppressions(search));
      setError(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Не удалось загрузить исключения");
    } finally {
      setLoading(false);
    }
  }, [search]);

  useEffect(() => { const timer = window.setTimeout(() => void load(), 220); return () => window.clearTimeout(timer); }, [load]);

  const add = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await api.addEmailSuppression(email, reason, comment);
      setEmail(""); setReason(""); setComment("");
      await load();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Не удалось добавить исключение");
    }
  };

  const lift = async (item: EmailSuppression) => {
    const note = window.prompt(`Почему снимается исключение для ${item.email}?`);
    if (!note || note.trim().length < 3) return;
    if (!window.confirm("Подтвердить осторожное снятие исключения? Автоматический повтор старой отправки всё равно не произойдёт.")) return;
    try {
      await api.liftEmailSuppression(item.id, note.trim());
      await load();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Не удалось снять исключение");
    }
  };

  return (
    <div className="content-page suppressions-page">
      <header className="page-heading"><div><span className="page-icon"><Ban size={19} /></span><div><h1>Глобальные исключения</h1><p>Адреса, на которые SMTP-вызов запрещён</p></div></div></header>
      {error ? <div className="outreach-error"><AlertTriangle size={18} /><span>{error}</span></div> : null}
      <form className="suppression-form" onSubmit={add}>
        <label><span>Email</span><input type="email" required value={email} onChange={(event) => setEmail(event.target.value)} /></label>
        <label><span>Причина</span><input required value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Отказ от писем" /></label>
        <label><span>Комментарий</span><input value={comment} onChange={(event) => setComment(event.target.value)} /></label>
        <button className="button button--primary" type="submit"><Plus size={16} /> Добавить</button>
      </form>
      <div className="suppression-search"><Search size={16} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Поиск по email, причине или комментарию" /></div>
      {loading ? <div className="outreach-loading"><LoaderCircle className="spin" size={22} /> Загружаем…</div> : null}
      {!loading && !items.length ? <div className="empty-state">Исключения не найдены.</div> : null}
      <div className="suppression-list">
        {items.map((item) => <article key={item.id} className={item.active ? "" : "suppression-lifted"}><div><strong>{item.email}</strong><span>{item.reason}</span><small>{item.source}{item.smtp_code ? ` · ${item.smtp_code}` : ""}</small>{item.comment ? <p>{item.comment}</p> : null}</div>{item.active ? <button className="button button--secondary" type="button" onClick={() => lift(item)}><ShieldOff size={15} /> Снять осторожно</button> : <span className="lifted-label">Снято</span>}</article>)}
      </div>
    </div>
  );
}
