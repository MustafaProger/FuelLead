import { AlertTriangle, CheckCircle2, KeyRound, LoaderCircle, Mail, Pause, Play, Plus, RefreshCw, Send, Trash2 } from "lucide-react";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { SenderAccount, SmtpSenderProvider } from "../types";


const providers: Record<SmtpSenderProvider, { label: string; placeholder: string; hint: string; url: string }> = {
  mailru_smtp: {
    label: "Mail.ru", placeholder: "name@mail.ru",
    hint: "Для IMAP выберите при создании пароля «Полный доступ к Почте». Для отправки с выключенным IMAP подходит «Только отправка писем в Почте».",
    url: "https://help.mail.ru/mail/login/mailer/",
  },
  gmail_smtp: {
    label: "Gmail", placeholder: "name@gmail.com",
    hint: "Включите двухэтапную аутентификацию Google и создайте пароль приложения из 16 символов. Пробелы между четырьмя группами можно оставить. Если пароли приложений недоступны из-за защиты аккаунта или правил организации, подключение этим способом невозможно. Gmail сохраняет отправленные письма автоматически.",
    url: "https://support.google.com/accounts/answer/185833?hl=ru",
  },
  yandex_smtp: {
    label: "Яндекс Почта", placeholder: "name@yandex.ru",
    hint: "Создайте пароль приложения «Почта» в Яндекс ID. В настройках Почты → Почтовые программы разрешите IMAP и включите «Пароли приложений и OAuth-токены».",
    url: "https://yandex.ru/support/yandex-360/customers/mail/ru/mail-clients/others",
  },
};

function providerLabel(provider: SenderAccount["provider"]) {
  return provider === "gmail_api" ? "Gmail (старое подключение)" : providers[provider].label;
}


function formatDateTime(value: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short" }).format(new Date(value));
}

const verificationLabels: Record<SenderAccount["verification_status"], string> = {
  unverified: "Не проверен",
  verified: "SMTP подключён",
  failed: "Ошибка проверки",
  blocked: "Авторизация отклонена",
  temporary_error: "Временная ошибка",
};

function verificationHint(category: string | null, provider: SenderAccount["provider"]) {
  if (category === "timeout" || category === "connection") {
    return "Проверьте сеть и VPN на сервере FuelLead. Замена пароля не устраняет ошибку соединения.";
  }
  if (category === "auth") {
    return `Проверьте адрес, пароль приложения и разрешённый доступ к почте в настройках ${providerLabel(provider)}.`;
  }
  return "";
}

export function MailboxesPage({ encryptionConfigured, onChanged }: { encryptionConfigured: boolean; onChanged: () => void }) {
  const [accounts, setAccounts] = useState<SenderAccount[]>([]);
  const [loading, setLoading] = useState(true);
  const [actingId, setActingId] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [provider, setProvider] = useState<SmtpSenderProvider>("mailru_smtp");
  const selectedProvider = providers[provider];
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [dailyLimit, setDailyLimit] = useState(50);
  const [imapEnabled, setImapEnabled] = useState(true);
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
    if (creating || actingId !== null) return;
    setCreating(true);
    setError(null);
    setNotice(null);
    try {
      const saved = await api.createSenderAccount({ provider, email, display_name: displayName, password, daily_limit: dailyLimit, smtp_enabled: true, imap_enabled: imapEnabled });
      setEmail("");
      setDisplayName("");
      setPassword("");
      setDailyLimit(50);
      setImapEnabled(true);
      setNotice("Ящик сохранён, подключение проверено без отправки письма.");
      await load();
      if (saved.verification_status !== "verified") setError(saved.verification_error || "Подключение требует проверки");
      onChanged();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Не удалось добавить ящик");
    } finally {
      setCreating(false);
    }
  };

  const act = async (accountId: number, action: () => Promise<unknown>, success?: string): Promise<boolean> => {
    if (actingId !== null || creating) return false;
    setActingId(accountId);
    setError(null);
    setNotice(null);
    try {
      const result = await action();
      await load();
      onChanged();
      if (result && typeof result === "object" && "verification_status" in result) {
        const checked = result as SenderAccount;
        if (checked.verification_status !== "verified") {
          setError(checked.verification_error || "Настройки сохранены, но SMTP ещё не подключён. Выполните проверку.");
          return false;
        }
      }
      if (success) setNotice(success);
      return true;
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Действие не выполнено");
      return false;
    } finally {
      setActingId(null);
    }
  };

  const replacePassword = async (account: SenderAccount) => {
    if (!replacementPassword) return;
    const succeeded = await act(account.id, () => api.updateSenderAccount(account.id, { password: replacementPassword }), "Пароль сохранён. SMTP подключён; проверка выполнена без отправки письма.");
    if (succeeded) {
      setReplacementPassword("");
      setReplacementId(null);
    }
  };

  const sendTest = async (account: SenderAccount) => {
    if (!testConfirmed || !testRecipient.trim()) return;
    const succeeded = await act(account.id, async () => {
      const result = await api.sendSenderTestEmail(account.id, testRecipient.trim());
      setNotice(result.notice);
    });
    if (succeeded) {
      setTestRecipient("");
      setTestConfirmed(false);
      setTestId(null);
    }
  };

  const remove = async (account: SenderAccount) => {
    if (!window.confirm(`Удалить ящик ${account.email}? Это возможно только вне активной кампании.`)) return;
    await act(account.id, () => api.deleteSenderAccount(account.id), "Почтовый ящик удалён.");
  };

  return (
    <div className="content-page mailboxes-page">
      <header className="page-heading">
        <div><span className="page-icon"><Mail size={19} /></span><div><h1>Почтовые ящики</h1><p>Mail.ru, Gmail и Яндекс Почта — с отдельными лимитами и прогревом</p></div></div>
      </header>

      {!encryptionConfigured ? <div className="settings-alert"><AlertTriangle size={18} /><span>Сначала задайте <code>MAIL_CREDENTIALS_ENCRYPTION_KEY</code> в локальном <code>.env</code> и пересоздайте backend. Значение ключа интерфейс не получает.</span></div> : null}
      {error ? <div className="outreach-error"><AlertTriangle size={18} /><span>{error}</span></div> : null}
      {notice ? <div className="settings-notice"><CheckCircle2 size={18} /><span>{notice}</span></div> : null}

      <form className="mailbox-form" onSubmit={create} autoComplete="off">
        <div className="section-heading"><div><h2>Добавить почтовый ящик</h2><p>Используется пароль внешнего приложения, а не пароль от аккаунта.</p></div><span>SSL 465 · IMAP SSL 993</span></div>
        <div className="mailbox-form-grid">
          <label><span>Провайдер</span><select value={provider} disabled={creating || actingId !== null} onChange={(event) => { setProvider(event.target.value as SmtpSenderProvider); setPassword(""); }}>{Object.entries(providers).map(([value, item]) => <option key={value} value={value}>{item.label}</option>)}</select></label>
          <label><span>Email</span><input type="email" required value={email} onChange={(event) => setEmail(event.target.value)} placeholder={selectedProvider.placeholder} autoComplete="off" /></label>
          <label><span>Отображаемое имя</span><input value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="Отдел продаж" /></label>
          <label><span>Пароль внешнего приложения</span><input type="password" required value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Вводится один раз" autoComplete="new-password" /></label>
          <label><span>Дневной лимит</span><input type="number" min={1} max={500} value={dailyLimit} onChange={(event) => setDailyLimit(Number(event.target.value))} /></label>
          <label className="toggle-label"><input type="checkbox" checked={imapEnabled} onChange={(event) => setImapEnabled(event.target.checked)} /><span>IMAP: ответы и возвраты</span></label>
        </div>
        <p className="mailbox-checked">{selectedProvider.hint} <a href={selectedProvider.url} target="_blank" rel="noopener noreferrer">Инструкция {selectedProvider.label}</a></p>
        <button className="button button--primary" type="submit" disabled={creating || actingId !== null || !encryptionConfigured || !email || !password}><Plus size={17} /> {creating ? "Сохраняем и проверяем…" : "Добавить ящик"}</button>
      </form>

      <section className="mailbox-list" aria-busy={loading}>
        <div className="section-heading"><div><h2>Подключённые ящики</h2><p>Порядок карточек — порядок ящиков в каждом круге.</p></div><button className="icon-button" type="button" onClick={load} aria-label="Обновить"><RefreshCw size={17} /></button></div>
        {loading ? <div className="outreach-loading"><LoaderCircle className="spin" size={22} /> Загружаем…</div> : null}
        {!loading && !accounts.length ? <div className="empty-state">Почтовые ящики ещё не добавлены.</div> : null}
        {accounts.map((account) => (
          <article className={`mailbox-card ${account.is_active ? "" : "mailbox-card--paused"}`} key={account.id}>
            <header>
              <div><strong>{account.display_name || account.email}</strong><span>{providerLabel(account.provider)} · {account.email}</span></div>
              <span className={`verification-badge verification-badge--${account.verification_status}`}>{verificationLabels[account.verification_status]}</span>
            </header>
            <div className="mailbox-metrics">
              <span><small>Сегодня</small><strong>{account.sent_today} / {account.daily_limit}</strong></span>
              <span><small>Размер пачки</small><strong>{account.current_batch_size}</strong></span>
              <span><small>Полные пачки</small><strong>{account.successful_full_batches}</strong></span>
              <span><small>Последняя отправка</small><strong>{formatDateTime(account.last_sent_at)}</strong></span>
            </div>
            <div className="mailbox-flags" inert={actingId !== null || creating}>
              <button type="button" className={account.smtp_enabled ? "flag flag--on" : "flag"} onClick={() => act(account.id, () => api.updateSenderAccount(account.id, { smtp_enabled: !account.smtp_enabled }))}>SMTP {account.smtp_enabled ? "включён" : "выключен"}</button>
              <button type="button" className={account.imap_enabled ? "flag flag--on" : "flag"} onClick={() => act(account.id, () => api.updateSenderAccount(account.id, { imap_enabled: !account.imap_enabled }))}>IMAP {account.imap_enabled ? "включён" : "выключен"}</button>
              <span className="flag flag--saved"><KeyRound size={13} /> {account.password_saved ? "Пароль сохранён" : "Пароль отсутствует"}</span>
            </div>
            {account.verification_error ? <p className="mailbox-error">{account.verification_error} {verificationHint(account.verification_error_category, account.provider)}</p> : null}
            {account.imap_enabled ? <p className="mailbox-checked">IMAP: {account.imap_verification_status === "disabled" ? "Выключен" : account.imap_verification_status === "verified" ? "Подключён" : verificationLabels[account.imap_verification_status] || "Не проверен"} · {formatDateTime(account.imap_verification_checked_at)}</p> : null}
            {account.imap_verification_error ? <p className="mailbox-error">{account.imap_verification_error}. Состояние отправки определяется проверкой SMTP.</p> : null}
            {account.verification_retry_at ? <p className="mailbox-checked">Автопроверка входа без отправки письма после {formatDateTime(account.verification_retry_at)}, в том числе во время кампании. Перерыв ящика по кругам сохраняется.</p> : null}
            {account.blocked_until_round ? <p className="mailbox-block">Пропуск до конца круга {account.blocked_until_round}: {account.block_reason || "ошибка ящика"}</p> : null}
            <p className="mailbox-checked">Проверка SMTP: {formatDateTime(account.verification_checked_at)}{actingId === account.id ? " · Проверяем, дождитесь результата…" : ""}</p>
            <div className="mailbox-actions" inert={actingId !== null || creating}>
              <button className="button button--secondary" type="button" disabled={actingId !== null || creating} onClick={() => act(account.id, () => api.verifySenderAccount(account.id), "Проверка завершена без отправки письма.")}><RefreshCw size={15} /> Проверить</button>
              <button className="button button--secondary" type="button" onClick={() => { setReplacementId(account.id); setReplacementPassword(""); }}><KeyRound size={15} /> Заменить пароль</button>
              <button className="button button--secondary" type="button" onClick={() => { setTestId(account.id); setTestRecipient(""); setTestConfirmed(false); }}><Send size={15} /> Тестовое письмо</button>
              <button className="button button--secondary" type="button" onClick={() => act(account.id, () => api.updateSenderAccount(account.id, { is_active: !account.is_active }))}>{account.is_active ? <Pause size={15} /> : <Play size={15} />}{account.is_active ? "Приостановить" : "Активировать"}</button>
              <button className="button button--danger" type="button" onClick={() => remove(account)}><Trash2 size={15} /> Удалить</button>
            </div>
            {replacementId === account.id ? <div className="inline-mailbox-form"><label><span>Новый пароль внешнего приложения</span><input type="password" value={replacementPassword} onChange={(event) => setReplacementPassword(event.target.value)} autoComplete="new-password" disabled={actingId !== null || creating} /></label><button className="button button--primary" type="button" onClick={() => replacePassword(account)} disabled={actingId !== null || creating || !replacementPassword}>Сохранить</button><button className="button button--secondary" type="button" onClick={() => { setReplacementId(null); setReplacementPassword(""); }}>Отмена</button></div> : null}
            {testId === account.id ? <div className="inline-mailbox-form inline-mailbox-form--test"><label><span>Куда отправить ровно одно письмо</span><input type="email" value={testRecipient} onChange={(event) => setTestRecipient(event.target.value)} placeholder="recipient@example.com" disabled={actingId !== null || creating} /></label><label className="toggle-label"><input type="checkbox" checked={testConfirmed} onChange={(event) => setTestConfirmed(event.target.checked)} disabled={actingId !== null || creating} /><span>Подтверждаю введённый адрес</span></label><button className="button button--primary" type="button" onClick={() => sendTest(account)} disabled={actingId !== null || creating || !testRecipient || !testConfirmed}>Отправить одно</button><button className="button button--secondary" type="button" onClick={() => setTestId(null)}>Отмена</button></div> : null}
          </article>
        ))}
      </section>
    </div>
  );
}
