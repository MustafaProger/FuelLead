import { ArrowLeft, CheckCheck, ChevronDown, ChevronLeft, ChevronRight, Inbox, LoaderCircle, Mail, Maximize2, Minimize2, Paperclip, RefreshCw, Reply, Search, Send } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError } from "../api";
import { createRequestId } from "../requestId";
import type { ConversationDetail, ConversationMessage, ConversationSummary } from "../types";

function selectedFromHash() {
  const id = Number(window.location.hash.split("/")[1]);
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}
function dateTime(value: string) {
  return new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}
function errorText(error: unknown) { return error instanceof Error ? error.message : "Не удалось загрузить переписку"; }
const statusLabels = { received: "Входящее", accepted: "Отправлено", sending: "Отправляется…", failed: "Не отправлено", uncertain: "Результат неизвестен" };

export function ConversationsPage({ onChanged }: { onChanged: () => void }) {
  const [selected, setSelected] = useState<number | null>(selectedFromHash);
  const [items, setItems] = useState<ConversationSummary[]>([]);
  const [search, setSearch] = useState("");
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sync, setSync] = useState<Awaited<ReturnType<typeof api.mailSyncState>> | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const listRevision = useRef(0);
  const loadList = useCallback(async () => {
    const revision = ++listRevision.current;
    try {
      const [result, state] = await Promise.all([api.conversations(search, unreadOnly, page), api.mailSyncState()]);
      if (revision !== listRevision.current) return;
      setItems(result.items); setTotal(result.total); setError(null);
      setSync(state);
    } catch (error) { if (revision === listRevision.current) setError(errorText(error)); }
    finally { if (revision === listRevision.current) setLoading(false); }
  }, [search, unreadOnly, page]);
  useEffect(() => {
    const hashChanged = () => setSelected(selectedFromHash());
    window.addEventListener("hashchange", hashChanged);
    return () => window.removeEventListener("hashchange", hashChanged);
  }, []);
  useEffect(() => {
    const delay = window.setTimeout(() => void loadList(), 200);
    const timer = window.setInterval(() => { if (!document.hidden) void loadList(); }, 15000);
    return () => { listRevision.current++; window.clearTimeout(delay); window.clearInterval(timer); };
  }, [loadList]);
  const onRead = useCallback(() => {
    window.dispatchEvent(new Event("fuellead:mail-read"));
    void loadList();
  }, [loadList]);
  const refreshMail = async () => {
    setRefreshing(true);
    try { await api.syncMail(); setSync((current) => current ? { ...current, running: true } : current); await loadList(); window.dispatchEvent(new Event("fuellead:refresh-conversation")); }
    catch (error) { setError(errorText(error)); }
    finally { setRefreshing(false); }
  };
  return <div className="content-page conversations-page">
    <header className="page-header">
      <div><h1>Переписка</h1><p>Ответы клиентов из всех подключённых ящиков</p></div>
      <button type="button" className="button button--secondary" disabled={refreshing || sync?.running} onClick={() => void refreshMail()}><RefreshCw size={17} className={refreshing || sync?.running ? "spin" : ""} /> {refreshing || sync?.running ? "Проверяем почту…" : "Проверить почту"}</button>
    </header>
    <div className="conversation-helper">Список обновляется автоматически.{sync ? ` Проверка почты — каждые ${Math.ceil(sync.poll_seconds / 60)} мин.` : ""} <a href="#mailboxes">Состояние ящиков</a></div>
    {sync && !sync.mailboxes.length ? <p className="mail-error">Для получения ответов включите IMAP в разделе «Почтовые ящики».</p> : null}
    {sync?.error ? <p className="mail-error" role="alert">{sync.error}</p> : null}
    {sync?.mailboxes.filter((mailbox) => mailbox.status !== "verified").map((mailbox) => <p className="mail-error" key={mailbox.email}>{mailbox.email}: {mailbox.error || "Получение ответов пока не подключено"}</p>)}
    {error ? <p className="mail-error" role="alert">{error}</p> : null}
    <div className={`conversation-workspace ${selected ? "conversation-workspace--selected" : ""}`}>
      <aside className="conversation-list-panel" aria-label="Переписки с клиентами">
        <div className="conversation-list-tools">
          <label className="conversation-search"><Search size={17} /><input aria-label="Найти переписку" placeholder="Компания или почта" value={search} onChange={(e) => { setSearch(e.target.value); setPage(1); }} /></label>
          <div className="conversation-tabs"><button type="button" aria-pressed={!unreadOnly} onClick={() => { setUnreadOnly(false); setPage(1); }}>Все ответы</button><button type="button" aria-pressed={unreadOnly} onClick={() => { setUnreadOnly(true); setPage(1); }}>Непрочитанные</button></div>
        </div>
        <div className="conversation-list">
          {loading ? <p className="mail-empty"><LoaderCircle className="spin" size={22} /> Загружаем ответы…</p> : null}
          {!loading && !items.length ? <div className="mail-empty"><Inbox size={30} /><strong>{search || unreadOnly ? "Ответов по этому фильтру нет" : "Здесь появятся ответы клиентов"}</strong><span>Когда клиент ответит на письмо, вы сможете прочитать его здесь.</span></div> : null}
          {items.map((item) => <a href={`#conversations/${item.company_id}`} key={item.company_id} aria-current={selected === item.company_id ? "true" : undefined} className={`conversation-list-item ${item.unread_count ? "conversation-list-item--unread" : ""}`}>
            <div className="conversation-list-item-top"><strong>{item.company_name}</strong><time>{dateTime(item.received_at)}</time></div>
            <span className="conversation-list-subject">{item.subject}</span>
            <p>{item.preview}</p>
            <div className="conversation-list-item-bottom"><span>{item.sender}</span>{item.unread_count ? <span className="mail-unread-badge">{item.unread_count}</span> : <CheckCheck size={14} aria-label="Прочитано" />}</div>
          </a>)}
        </div>
        {total > 30 ? <div className="conversation-pagination"><button type="button" aria-label="Предыдущая страница" disabled={page === 1} onClick={() => setPage(page - 1)}><ChevronLeft size={18} /></button><span>{page} / {Math.ceil(total / 30)}</span><button type="button" aria-label="Следующая страница" disabled={page * 30 >= total} onClick={() => setPage(page + 1)}><ChevronRight size={18} /></button></div> : null}
      </aside>
      {selected ? <ConversationThread key={selected} companyId={selected} onRead={onRead} onChanged={onChanged} /> : <div className="conversation-placeholder"><span><Mail size={32} /></span><h2>Вся переписка под рукой</h2><p>Выберите компанию, чтобы прочитать письма и написать ответ.</p></div>}
    </div>
  </div>;
}

interface Draft { body: string; replyId: number | null; requestId: string; pending: boolean }
function newDraft(): Draft { return { body: "", replyId: null, requestId: createRequestId(), pending: false }; }
function readDraft(key: string): Draft {
  try { const value = JSON.parse(sessionStorage.getItem(key) || "null"); if (value && typeof value.body === "string" && typeof value.requestId === "string") return value; } catch { /* Storage may be unavailable. */ }
  return newDraft();
}

function ConversationThread({ companyId, onRead, onChanged }: { companyId: number; onRead: () => void; onChanged: () => void }) {
  const storageKey = `fuellead.reply-draft.${companyId}`;
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [draft, setDraft] = useState<Draft>(() => readDraft(storageKey));
  const [error, setError] = useState<string | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [composerExpanded, setComposerExpanded] = useState(false);
  const sendLock = useRef(false);
  const alive = useRef(true);
  const revision = useRef(0);
  const scroller = useRef<HTMLDivElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const initialScroll = useRef(true);
  const load = useCallback(async () => {
    const current = ++revision.current;
    try {
      const result = await api.conversation(companyId);
      if (!alive.current || current !== revision.current) return;
      setDetail(result); setError(null);
    } catch (error) { if (alive.current && current === revision.current) setError(errorText(error)); }
  }, [companyId]);
  useEffect(() => {
    alive.current = true;
    void load();
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 15000);
    window.addEventListener("fuellead:refresh-conversation", load);
    return () => { alive.current = false; revision.current++; window.clearInterval(timer); window.removeEventListener("fuellead:refresh-conversation", load); };
  }, [load]);
  useEffect(() => { try { sessionStorage.setItem(storageKey, JSON.stringify(draft)); } catch { /* Keep in-memory draft. */ } }, [draft, storageKey]);
  useEffect(() => {
    if (!detail) return;
    if (initialScroll.current && scroller.current) {
      const lastMessage = scroller.current.lastElementChild;
      if (lastMessage) scroller.current.scrollTop += lastMessage.getBoundingClientRect().top - scroller.current.getBoundingClientRect().top - 20;
      initialScroll.current = false;
    }
    const latest = detail.latest_reply_id;
    if (latest && detail.messages.some((m) => m.unread) && !document.hidden) {
      void api.readConversation(companyId, latest).then(() => {
        if (!alive.current) return;
        setDetail((current) => current ? { ...current, messages: current.messages.map((m) => m.reply_id && m.reply_id <= latest ? { ...m, unread: false } : m) } : current);
        onRead();
      }).catch(() => {});
    }
  }, [detail, companyId, onRead]);
  const reply = detail?.messages.find((m) => m.reply_id === (draft.replyId || detail.latest_reply_id));
  const composerVisible = Boolean(reply && (!reply.reply_disabled_reason || draft.pending));
  const resizeComposer = useCallback(() => {
    const input = textarea.current;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight + input.offsetHeight - input.clientHeight}px`;
  }, []);
  useLayoutEffect(resizeComposer, [draft.body, composerExpanded, composerVisible, resizeComposer]);
  useEffect(() => {
    const input = textarea.current;
    if (!input) return;
    let width = input.getBoundingClientRect().width;
    const observer = new ResizeObserver(() => {
      const nextWidth = input.getBoundingClientRect().width;
      if (nextWidth !== width) { width = nextWidth; resizeComposer(); }
    });
    observer.observe(input);
    window.addEventListener("resize", resizeComposer);
    return () => { observer.disconnect(); window.removeEventListener("resize", resizeComposer); };
  }, [composerVisible, resizeComposer]);
  const changeBody = (body: string) => setDraft((current) => ({ ...current, body, replyId: current.replyId || detail?.latest_reply_id || null }));
  const chooseReply = (message: ConversationMessage) => {
    if (draft.pending || sending) return;
    setDraft((current) => ({ ...current, replyId: message.reply_id || null }));
    textarea.current?.focus();
  };
  const send = async (event: FormEvent) => {
    event.preventDefault();
    if (!reply?.reply_id || !draft.body.trim() || sendLock.current) return;
    sendLock.current = true; setSending(true); setSendError(null); setNotice(null);
    const attempt = { ...draft, replyId: reply.reply_id, body: draft.body.trim(), pending: true };
    setDraft(attempt);
    // Persist before making the request, so reload/retry uses the same receipt.
    try { sessionStorage.setItem(storageKey, JSON.stringify(attempt)); } catch { /* In-memory receipt remains. */ }
    try {
      const result = await api.replyConversation(companyId, reply.reply_id, attempt.body, attempt.requestId);
      if (result.status === "accepted") {
        initialScroll.current = true;
        const cleared = newDraft();
        try { sessionStorage.removeItem(storageKey); } catch { /* Nothing to clear. */ }
        if (alive.current) { setDraft(cleared); setComposerExpanded(false); setNotice(result.sent_copy_saved === false ? "Письмо принято почтовым сервером. Копию в «Отправленные» сохранить не удалось; повторно отправлять письмо не нужно." : "Письмо принято почтовым сервером. Ответ клиента появится здесь."); }
        onChanged();
      } else if (alive.current) {
        if (result.status === "failed") setDraft({ ...attempt, pending: false, requestId: createRequestId() });
        setSendError(result.error || (result.status === "sending" ? "Письмо ещё отправляется. Нажмите «Проверить результат» через несколько секунд." : "Не удалось подтвердить отправку. Проверьте «Отправленные»; письмо не будет отправлено повторно."));
      }
      if (alive.current) await load();
    } catch (error) {
      if (alive.current) {
        // HTTP policy/validation errors occur before reservation. Network and 5xx
        // errors retain the original request ID until the server confirms outcome.
        if (error instanceof ApiError && [404, 409, 422, 429].includes(error.status)) setDraft({ ...attempt, pending: false });
        setSendError(errorText(error));
      }
    } finally { sendLock.current = false; if (alive.current) setSending(false); }
  };
  const resolve = async (message: ConversationMessage, outcome: "accepted" | "failed") => {
    if (!window.confirm(outcome === "accepted" ? "Вы проверили почту и подтверждаете, что письмо отправлено?" : "Вы проверили почту и подтверждаете, что письмо НЕ отправлено? После этого можно будет повторить отправку.")) return;
    try {
      await api.resolveReply(companyId, message.id, outcome);
      if (message.id === draft.requestId) setDraft(outcome === "accepted" ? newDraft() : { ...draft, pending: false, requestId: createRequestId() });
      setSendError(null); await load();
    } catch (error) { setSendError(errorText(error)); }
  };
  return <section className="conversation-thread" aria-label="Письма компании">
    <header className="conversation-thread-header"><a href="#conversations" className="conversation-back" aria-label="К списку переписок"><ArrowLeft size={20} /></a><div><h2>{detail?.company_name || "Загружаем переписку…"}</h2><p>{detail?.messages.length ? `${detail.messages.length} писем в истории` : "История сообщений"}</p></div></header>
    {error ? <p className="mail-error" role="alert">{error} <button type="button" onClick={() => void load()}>Повторить</button></p> : null}
    <div className="conversation-messages" ref={scroller}>
      {!detail && !error ? <p className="mail-empty"><LoaderCircle className="spin" size={24} /> Загружаем письма…</p> : null}
      {detail && !detail.messages.length ? <p className="mail-empty">Переписки пока нет. После отправки предложения и ответа клиента здесь появятся письма.</p> : null}
      {detail?.messages.map((message) => <article key={message.id} className={`conversation-message conversation-message--${message.direction}`}>
        <header><span>{message.direction === "incoming" ? <Mail size={15} /> : <Send size={15} />}{message.direction === "incoming" && message.is_automatic ? "Автоответ" : statusLabels[message.status]}</span><time dateTime={message.created_at}>{dateTime(message.created_at)}</time></header>
        <h3>{message.subject}</h3><div className="conversation-addresses"><span>От: {message.sender || "Исходный ящик"}</span><span>Кому: {message.recipient}</span></div>
        <MessageBody message={message} />
        {message.legacy ? <p className="mail-muted">Сохранён фрагмент старого письма.</p> : null}
        {message.attachments.length ? <p className="conversation-attachments"><Paperclip size={14} /> Вложения: {message.attachments.join(", ")}. Файлы доступны в почте.</p> : null}
        {message.error ? <p className="mail-error">{message.error}</p> : null}
        {message.status === "uncertain" ? <div className="conversation-resolution"><p className="mail-muted">Уточните результат после проверки почты:</p><button type="button" onClick={() => void resolve(message, "accepted")}>Письмо отправлено</button><button type="button" onClick={() => void resolve(message, "failed")}>Письмо не отправлено</button></div> : null}
        {message.status === "accepted" ? <small className="mail-muted">Принято почтовым сервером; доставка во входящие ещё не подтверждена.</small> : null}
        {message.direction === "incoming" ? <button type="button" className="conversation-reply-link" disabled={sending || draft.pending} onClick={() => chooseReply(message)}><Reply size={15} /> Ответить на это письмо</button> : null}
      </article>)}
    </div>
    {reply ? <form className={`conversation-composer${composerExpanded ? " conversation-composer--expanded" : ""}`} onSubmit={send}>
      <details className="conversation-reply-context">
        <summary><Reply size={15} /><span><strong>Ответ: {reply.reply_recipient || reply.sender}</strong><span>{reply.subject}</span></span><ChevronDown size={15} /></summary>
        <div>
          <p className="conversation-compose-route">С <strong>{reply.reply_sender || "исходного ящика"}</strong> → <strong>{reply.reply_recipient || reply.sender}</strong></p>
          <p className="mail-muted">На письмо: {reply.subject}</p>
          <p className="mail-muted">Черновик сохраняется в этой вкладке. Во время ответа рассылка подождёт и продолжится автоматически. Ручная пауза сохранится.</p>
        </div>
      </details>
      {reply.reply_disabled_reason ? <p className="mail-error" role="status">{reply.reply_disabled_reason}</p> : null}
      {composerVisible ? <>
        <div className="conversation-composer-input">
          <div className="conversation-composer-field">
            <label className="sr-only" htmlFor="client-reply">Текст ответа</label>
            <textarea ref={textarea} id="client-reply" value={draft.body} maxLength={20000} rows={1} placeholder="Сообщение…" disabled={sending || draft.pending} onChange={(event) => changeBody(event.target.value)} />
            <button type="button" className="conversation-composer-toggle" aria-label={composerExpanded ? "Свернуть поле ответа" : "Развернуть поле ответа"} title={composerExpanded ? "Свернуть поле ответа" : "Развернуть поле ответа"} aria-expanded={composerExpanded} aria-controls="client-reply" onClick={() => { setComposerExpanded((value) => !value); textarea.current?.focus(); }}>{composerExpanded ? <Minimize2 size={17} /> : <Maximize2 size={17} />}</button>
          </div>
          <button className={`conversation-composer-send${draft.pending && !sending ? " conversation-composer-send--pending" : ""}`} type="submit" aria-label={sending ? "Отправляем ответ" : draft.pending ? "Проверить результат" : "Отправить ответ"} title={sending ? "Отправляем ответ" : draft.pending ? "Проверить результат" : "Отправить ответ"} disabled={sending || !draft.body.trim() || Boolean(reply.reply_disabled_reason) && !draft.pending}>{sending ? <LoaderCircle className="spin" size={19} /> : draft.pending ? <RefreshCw size={19} /> : <Send size={19} />}{draft.pending && !sending ? <span>Проверить результат</span> : null}</button>
        </div>
        {draft.body.length ? <div className="conversation-composer-footer"><span>{draft.pending ? "Ожидает подтверждения отправки" : "Черновик сохраняется в этой вкладке"}</span><small>{draft.body.length.toLocaleString("ru-RU")} / 20 000</small></div> : null}
      </> : null}
      {sending ? <p className="mail-muted" role="status">Ожидаем завершения текущей отправки и отправляем ответ…</p> : null}
      {sendError ? <p className="mail-error" role="alert">{sendError}</p> : null}
      {notice ? <p className="mail-success" role="status">{notice}</p> : null}
    </form> : null}
  </section>;
}

function MessageBody({ message }: { message: ConversationMessage }) {
  const [expanded, setExpanded] = useState(false);
  const text = message.direction === "incoming" && message.preview && message.body !== message.preview ? message.preview : message.body;
  const collapsible = text !== message.body || message.body.length > 1800;
  return <><div className="conversation-message-body">{expanded ? message.body : text.slice(0, 1800)}</div>{collapsible ? <button type="button" className="conversation-expand" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? "Свернуть письмо" : "Показать письмо полностью"}</button> : null}</>;
}
