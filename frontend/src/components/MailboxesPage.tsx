import { AlertTriangle, CheckCircle2, KeyRound, LoaderCircle, Mail, Pause, Play, Plus, RefreshCw, Send, Trash2 } from "lucide-react";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { SenderAccount } from "../types";


function formatDateTime(value: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short" }).format(new Date(value));
}

const verificationLabels: Record<SenderAccount["verification_status"], string> = {
  unverified: "Не проверен",
  verified: "Подключён",
  failed: "Ошибка проверки",
  blocked: "Авторизация отклонена",
  temporary_error: "Временная ошибка",
};

export function MailboxesPage({ encryptionConfigured, onChanged }: { encryptionConfigured: boolean; onChanged: () => void }) {
  const [accounts, setAccounts] = useState<SenderAccount[]>([]);
  const [loading, setLoading] = useState(true);
  const [actingId, setActingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [dailyLimit, setDailyLimit] = useState(50);
  const [imapEnabled, setImapEnabled] = useState(false);
  const [replacementId, setReplacementId] = useState<number | null>(null);
  const [replacementPassword, setReplacementPassword] = useState("");
  const [testId, setTestId] = useState<number | null>(null);
  const [testRecipient, setTestRecipient] = useState("");
  const [testConfirmed, setTestConfirmed] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setAccounts(await api.senderAccounts());
      setError(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Не удалось загрузить ящики");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const create = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setNotice(null);
    try {
      await api.createSenderAccount({ email, display_name: displayName, password, daily_limit: dailyLimit, smtp_enabled: true, imap_enabled: imapEnabled });
      setEmail("");
      setDisplayName("");
      setPassword("");
      setDailyLimit(50);
      setImapEnabled(false);
      setNotice("Ящик сохранён. Пароль зашифрован и больше не отображается; теперь проверьте подключение.");
      await load();
      onChanged();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Не удалось добавить ящик");
    }
  };

  const act = async (accountId: number, action: () => Promise<unknown>, success?: string) => {
    setActingId(accountId);
    setError(null);
    setNotice(null);
    try {
      await action();
      if (success) setNotice(success);
      await load();
      onChanged();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Действие не выполнено");
    } finally {
      setActingId(null);
    }
  };

  const replacePassword = async (account: SenderAccount) => {
    if (!replacementPassword) return;
    await act(account.id, () => api.updateSenderAccount(account.id, { password: replacementPassword }), "Новый пароль зашифрован. Выполните проверку подключения ещё раз.");
    setReplacementPassword("");
    setReplacementId(null);
  };

  const sendTest = async (account: SenderAccount) => {
    if (!testConfirmed || !testRecipient.trim()) return;
    await act(account.id, () => api.sendSenderTestEmail(account.id, testRecipient.trim()), "Отправлено ровно одно тестовое письмо. SMTP подтвердил приём, но не доставку во «Входящие».");
    setTestRecipient("");
    setTestConfirmed(false);
    setTestId(null);
  };

  const remove = async (account: SenderAccount) => {
    if (!window.confirm(`Удалить ящик ${account.email}? Это возможно только вне активной кампании.`)) return;
    await act(account.id, () => api.deleteSenderAccount(account.id), "Почтовый ящик удалён.");
  };

  return (
    <div className="content-page mailboxes-page">
      <header className="page-heading">
        <div><span className="page-icon"><Mail size={19} /></span><div><h1>Почтовые ящики</h1><p>Mail.ru SMTP по очереди, с отдельными лимитами и прогревом</p></div></div>
      </header>

      {!encryptionConfigured ? <div className="settings-alert"><AlertTriangle size={18} /><span>Сначала задайте <code>MAIL_CREDENTIALS_ENCRYPTION_KEY</code> в локальном <code>.env</code> и пересоздайте backend. Значение ключа интерфейс не получает.</span></div> : null}
      {error ? <div className="outreach-error"><AlertTriangle size={18} /><span>{error}</span></div> : null}
      {notice ? <div className="settings-notice"><CheckCircle2 size={18} /><span>{notice}</span></div> : null}

      <form className="mailbox-form" onSubmit={create} autoComplete="off">
        <div className="section-heading"><div><h2>Добавить Mail.ru</h2><p>Используется пароль внешнего приложения, а не пароль от аккаунта.</p></div><span>SSL 465 · IMAP SSL 993</span></div>
        <div className="mailbox-form-grid">
          <label><span>Провайдер</span><select disabled value="mailru_smtp"><option value="mailru_smtp">Mail.ru</option></select></label>
          <label><span>Email</span><input type="email" required value={email} onChange={(event) => setEmail(event.target.value)} placeholder="name@mail.ru" autoComplete="off" /></label>
          <label><span>Отображаемое имя</span><input value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="Отдел продаж" /></label>
          <label><span>Пароль внешнего приложения</span><input type="password" required value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Вводится один раз" autoComplete="new-password" /></label>
          <label><span>Дневной лимит</span><input type="number" min={1} max={500} value={dailyLimit} onChange={(event) => setDailyLimit(Number(event.target.value))} /></label>
          <label className="toggle-label"><input type="checkbox" checked={imapEnabled} onChange={(event) => setImapEnabled(event.target.checked)} /><span>Включить IMAP-сбор возвратов</span></label>
        </div>
        <button className="button button--primary" type="submit" disabled={!encryptionConfigured || !email || !password}><Plus size={17} /> Добавить ящик</button>
      </form>

      <section className="mailbox-list" aria-busy={loading}>
        <div className="section-heading"><div><h2>Подключённые ящики</h2><p>Порядок карточек — порядок ящиков в каждом круге.</p></div><button className="icon-button" type="button" onClick={load} aria-label="Обновить"><RefreshCw size={17} /></button></div>
        {loading ? <div className="outreach-loading"><LoaderCircle className="spin" size={22} /> Загружаем…</div> : null}
        {!loading && !accounts.length ? <div className="empty-state">Почтовые ящики ещё не добавлены.</div> : null}
        {accounts.map((account) => (
          <article className={`mailbox-card ${account.is_active ? "" : "mailbox-card--paused"}`} key={account.id}>
            <header>
              <div><strong>{account.display_name || account.email}</strong><span>{account.email}</span></div>
              <span className={`verification-badge verification-badge--${account.verification_status}`}>{verificationLabels[account.verification_status]}</span>
            </header>
            <div className="mailbox-metrics">
              <span><small>Сегодня</small><strong>{account.sent_today} / {account.daily_limit}</strong></span>
              <span><small>Размер пачки</small><strong>{account.current_batch_size}</strong></span>
              <span><small>Полные пачки</small><strong>{account.successful_full_batches}</strong></span>
              <span><small>Последняя отправка</small><strong>{formatDateTime(account.last_sent_at)}</strong></span>
            </div>
            <div className="mailbox-flags">
              <button type="button" className={account.smtp_enabled ? "flag flag--on" : "flag"} onClick={() => act(account.id, () => api.updateSenderAccount(account.id, { smtp_enabled: !account.smtp_enabled }))}>SMTP {account.smtp_enabled ? "включён" : "выключен"}</button>
              <button type="button" className={account.imap_enabled ? "flag flag--on" : "flag"} onClick={() => act(account.id, () => api.updateSenderAccount(account.id, { imap_enabled: !account.imap_enabled }))}>IMAP {account.imap_enabled ? "включён" : "выключен"}</button>
              <span className="flag flag--saved"><KeyRound size={13} /> {account.password_saved ? "Пароль сохранён" : "Пароль отсутствует"}</span>
            </div>
            {account.verification_error ? <p className="mailbox-error">{account.verification_error}</p> : null}
            {account.blocked_until_round ? <p className="mailbox-block">Пропуск до конца круга {account.blocked_until_round}: {account.block_reason || "ошибка ящика"}</p> : null}
            <p className="mailbox-checked">Последняя проверка: {formatDateTime(account.verification_checked_at)}</p>
            <div className="mailbox-actions">
              <button className="button button--secondary" type="button" disabled={actingId === account.id} onClick={() => act(account.id, () => api.verifySenderAccount(account.id), "Проверка завершена без отправки письма.")}><RefreshCw size={15} /> Проверить</button>
              <button className="button button--secondary" type="button" onClick={() => { setReplacementId(account.id); setReplacementPassword(""); }}><KeyRound size={15} /> Заменить пароль</button>
              <button className="button button--secondary" type="button" onClick={() => { setTestId(account.id); setTestRecipient(""); setTestConfirmed(false); }}><Send size={15} /> Тестовое письмо</button>
              <button className="button button--secondary" type="button" onClick={() => act(account.id, () => api.updateSenderAccount(account.id, { is_active: !account.is_active }))}>{account.is_active ? <Pause size={15} /> : <Play size={15} />}{account.is_active ? "Приостановить" : "Активировать"}</button>
              <button className="button button--danger" type="button" onClick={() => remove(account)}><Trash2 size={15} /> Удалить</button>
            </div>
            {replacementId === account.id ? <div className="inline-mailbox-form"><label><span>Новый пароль внешнего приложения</span><input type="password" value={replacementPassword} onChange={(event) => setReplacementPassword(event.target.value)} autoComplete="new-password" /></label><button className="button button--primary" type="button" onClick={() => replacePassword(account)} disabled={!replacementPassword}>Сохранить</button><button className="button button--secondary" type="button" onClick={() => { setReplacementId(null); setReplacementPassword(""); }}>Отмена</button></div> : null}
            {testId === account.id ? <div className="inline-mailbox-form inline-mailbox-form--test"><label><span>Куда отправить ровно одно письмо</span><input type="email" value={testRecipient} onChange={(event) => setTestRecipient(event.target.value)} placeholder="recipient@example.com" /></label><label className="toggle-label"><input type="checkbox" checked={testConfirmed} onChange={(event) => setTestConfirmed(event.target.checked)} /><span>Подтверждаю введённый адрес</span></label><button className="button button--primary" type="button" onClick={() => sendTest(account)} disabled={!testRecipient || !testConfirmed}>Отправить одно</button><button className="button button--secondary" type="button" onClick={() => setTestId(null)}>Отмена</button></div> : null}
          </article>
        ))}
      </section>
    </div>
  );
}
