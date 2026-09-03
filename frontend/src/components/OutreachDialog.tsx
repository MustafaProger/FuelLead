import { AlertTriangle, CheckCircle2, Clock3, LoaderCircle, MailCheck, Pause, Play, Send, ShieldCheck, Square, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import type { Filters, OutreachCampaign, OutreachPreflight } from "../types";


interface OutreachDialogProps {
  open: boolean;
  filters: Filters;
  mailConfigured: boolean;
  onClose: () => void;
  onChanged: () => void;
}

function formatDateTime(value: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

const campaignLabels: Record<OutreachCampaign["status"], string> = {
  draft: "Ожидает подтверждения",
  running: "Рассылка выполняется",
  paused: "Рассылка на паузе",
  cooldown: "Общий отдых после круга",
  interrupted: "Требуется ручное решение",
  completed: "Рассылка завершена",
  stopped: "Рассылка остановлена",
};

export function OutreachDialog({ open, filters, mailConfigured, onClose, onChanged }: OutreachDialogProps) {
  const [preflight, setPreflight] = useState<OutreachPreflight | null>(null);
  const [campaign, setCampaign] = useState<OutreachCampaign | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [loading, setLoading] = useState(false);
  const [acting, setActing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true); setError(null); setConfirmed(false);
    Promise.all([api.activeOutreachCampaign(), api.outreachPreflight(filters)])
      .then(([active, check]) => { if (!cancelled) { setCampaign(active); setPreflight(check); } })
      .catch((requestError) => { if (!cancelled) setError(requestError instanceof Error ? requestError.message : "Не удалось проверить рассылку"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [open, filters]);

  useEffect(() => {
    if (!open || !campaign || ["completed", "stopped"].includes(campaign.status)) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      api.outreachCampaign(campaign.id).then((current) => { if (!cancelled) { setCampaign(current); onChanged(); } }).catch((requestError) => { if (!cancelled) setError(requestError instanceof Error ? requestError.message : "Не удалось обновить прогресс"); });
    }, 4000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [campaign?.id, campaign?.status, onChanged, open]);

  useEffect(() => {
    if (!open) return;
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    document.body.classList.add("dialog-open");
    window.addEventListener("keydown", closeOnEscape);
    return () => { document.body.classList.remove("dialog-open"); window.removeEventListener("keydown", closeOnEscape); };
  }, [onClose, open]);

  if (!open) return null;

  const runAction = async (action: () => Promise<OutreachCampaign>) => {
    setActing(true); setError(null);
    try { const current = await action(); setCampaign(current); onChanged(); }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : "Не удалось изменить рассылку"); }
    finally { setActing(false); }
  };

  const start = async () => {
    if (!confirmed || !preflight?.snapshot_id) return;
    await runAction(() => api.startOutreachCampaign(preflight.snapshot_id as number));
  };

  return (
    <div className="outreach-dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="outreach-dialog" role="dialog" aria-modal="true" aria-labelledby="outreach-dialog-title">
        <header className="outreach-dialog-header"><span className="outreach-dialog-icon"><Send size={20} /></span><div><h2 id="outreach-dialog-title">Отправка писем</h2><p>Последовательные Mail.ru-пачки с сохранёнными таймерами</p></div><button type="button" className="dialog-close" onClick={onClose} aria-label="Закрыть"><X size={19} /></button></header>
        {loading ? <div className="outreach-loading"><LoaderCircle className="spin" size={24} /><span>Фиксируем снимок получателей и ящиков…</span></div> : null}
        {error ? <div className="outreach-error"><AlertTriangle size={18} /><span>{error}</span></div> : null}
        {!loading && campaign ? <CampaignProgress campaign={campaign} acting={acting} onAction={runAction} /> : null}
        {!loading && !campaign && preflight ? (
          <>
            <div className="outreach-summary-grid"><article><small>По фильтрам</small><strong>{preflight.matched_count}</strong></article><article><small>Можно отправить</small><strong>{preflight.eligible_count}</strong></article><article className="outreach-summary-primary"><small>В снимке</small><strong>{preflight.selected_count}</strong></article></div>
            <div className="outreach-safety-panel"><div className="outreach-panel-title"><ShieldCheck size={18} /><strong>Снимок действует 10 минут</strong></div><ul><li>ящиков в круге: {preflight.sender_count} ({preflight.sender_emails.join(" → ") || "нет проверенных"});</li><li>между письмами случайно 60–85 секунд, после круга 77–93 минуты;</li><li>дневной лимит и размер пачки применяются отдельно к каждому ящику;</li><li>перед SMTP повторно проверяются компания, email, статус и глобальные исключения;</li><li>после неопределённого результата автоматический повтор запрещён.</li></ul></div>
            <div className="outreach-skips"><span>Причины исключения могут пересекаться:</span><small>другой статус — {preflight.skipped.not_new}</small>{preflight.skipped.inactive ? <small>не действуют — {preflight.skipped.inactive}</small> : null}<small>без email — {preflight.skipped.without_email}</small><small>уже была попытка — {preflight.skipped.already_contacted}</small><small>в исключениях — {preflight.skipped.suppressed}</small><small>повтор адреса — {preflight.skipped.duplicate_address}</small></div>
            {preflight.sample ? <details className="outreach-preview"><summary><MailCheck size={16} /> Пример первого письма</summary><div><small>{preflight.sample.company_name} · {preflight.sample.recipient}</small><strong>{preflight.sample.subject}</strong><pre>{preflight.sample.body}</pre></div></details> : null}
            <p className="outreach-auto-note">Статус <strong>accepted</strong> означает только приём письма SMTP-сервером. Он не подтверждает доставку или попадание во «Входящие».</p>
            <label className="outreach-confirmation"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /><span>Я проверил снимок получателей и подтверждаю запуск именно с указанными ящиками. После 10 минут нужно создать новый снимок.</span></label>
            <footer className="outreach-dialog-actions"><button className="button button--secondary" type="button" onClick={onClose}>Отмена</button><button className="button button--primary" type="button" disabled={!mailConfigured || !confirmed || !preflight.snapshot_id || acting} onClick={start}>{acting ? <LoaderCircle className="spin" size={17} /> : <Send size={17} />}{acting ? "Запускаем…" : `Запустить на ${preflight.selected_count}`}</button></footer>
          </>
        ) : null}
      </section>
    </div>
  );
}

function CampaignProgress({ campaign, acting, onAction }: { campaign: OutreachCampaign; acting: boolean; onAction: (action: () => Promise<OutreachCampaign>) => Promise<void> }) {
  const terminal = campaign.status === "completed" || campaign.status === "stopped";
  const stop = () => {
    if (window.confirm("Остановить рассылку необратимо? Все ещё не начатые письма станут cancelled.")) void onAction(() => api.cancelOutreachCampaign(campaign.id));
  };
  return (
    <div className="campaign-progress">
      <div className={`campaign-state campaign-state--${campaign.status}`}>{campaign.status === "completed" ? <CheckCircle2 size={20} /> : campaign.status === "paused" ? <Pause size={20} /> : campaign.status === "stopped" ? <Square size={20} /> : campaign.status === "cooldown" ? <Clock3 size={20} /> : campaign.status === "interrupted" ? <AlertTriangle size={20} /> : <LoaderCircle className="spin" size={20} />}<div><strong>{campaignLabels[campaign.status]}</strong><small>{campaign.pause_reason || `Следующее действие: ${formatDateTime(campaign.next_send_at)}`}</small></div></div>
      <div className="campaign-observability"><span>Круг <strong>{campaign.current_round}</strong></span><span>Ящик <strong>{campaign.active_sender_email || "—"}</strong></span><span>Позиция <strong>{campaign.batch_position} / {campaign.current_batch_target || "—"}</strong></span><span>Следующий запуск <strong>{formatDateTime(campaign.next_send_at)}</strong></span><span>Отдых до <strong>{formatDateTime(campaign.round_rest_until)}</strong></span></div>
      <div className="campaign-progress-track"><span style={{ width: `${campaign.progress_percent}%` }} /></div>
      <div className="campaign-counters campaign-counters--full"><span><small>queued</small><strong>{campaign.queued_count}</strong></span><span><small>sending</small><strong>{campaign.sending_count}</strong></span><span><small>accepted</small><strong>{campaign.accepted_count}</strong></span><span><small>failed</small><strong>{campaign.failed_count}</strong></span><span><small>bounced</small><strong>{campaign.bounced_count}</strong></span><span><small>uncertain</small><strong>{campaign.uncertain_count}</strong></span><span><small>suppressed</small><strong>{campaign.suppressed_count}</strong></span><span><small>cancelled</small><strong>{campaign.cancelled_count}</strong></span></div>
      <p className="campaign-acceptance-note">{campaign.acceptance_notice}</p>
      {campaign.uncertain_deliveries.map((delivery) => <div className="uncertain-resolution" key={delivery.id}><span>{delivery.recipient}: укажите подтверждённый вручную результат</span><button className="button button--secondary" type="button" disabled={acting} onClick={() => onAction(() => api.resolveUncertainDelivery(delivery.id, "accepted"))}>SMTP принял</button><button className="button button--secondary" type="button" disabled={acting} onClick={() => onAction(() => api.resolveUncertainDelivery(delivery.id, "failed"))}>Не принято</button></div>)}
      {!terminal ? <div className="campaign-actions">{campaign.status === "running" || campaign.status === "cooldown" ? <button className="button button--secondary" disabled={acting} type="button" onClick={() => onAction(() => api.pauseOutreachCampaign(campaign.id))}><Pause size={16} /> Пауза</button> : campaign.status === "paused" ? <button className="button button--primary" disabled={acting} type="button" onClick={() => onAction(() => api.resumeOutreachCampaign(campaign.id))}><Play size={16} /> Продолжить</button> : null}<button className="button button--danger" disabled={acting} type="button" onClick={stop}><Square size={15} /> Остановить необратимо</button></div> : null}
    </div>
  );
}
