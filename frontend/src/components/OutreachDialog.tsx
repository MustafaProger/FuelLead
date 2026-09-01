import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  LoaderCircle,
  MailCheck,
  Pause,
  Play,
  Send,
  ShieldCheck,
  Square,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import type { Filters, OutreachCampaign, OutreachPreflight } from "../types";

interface OutreachDialogProps {
  open: boolean;
  filters: Filters;
  gmailConfigured: boolean;
  onClose: () => void;
  onChanged: () => void;
}

function formatDelay(seconds: number) {
  if (seconds >= 60 && seconds % 60 === 0) return `${seconds / 60} мин`;
  return `${seconds} сек`;
}

function formatDateTime(value: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

const campaignLabels: Record<OutreachCampaign["status"], string> = {
  running: "Рассылка выполняется",
  paused: "Рассылка на паузе",
  completed: "Рассылка завершена",
  cancelled: "Рассылка отменена",
};

export function OutreachDialog({
  open,
  filters,
  gmailConfigured,
  onClose,
  onChanged,
}: OutreachDialogProps) {
  const [preflight, setPreflight] = useState<OutreachPreflight | null>(null);
  const [campaign, setCampaign] = useState<OutreachCampaign | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [loading, setLoading] = useState(false);
  const [acting, setActing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setConfirmed(false);
    Promise.all([api.activeOutreachCampaign(), api.outreachPreflight(filters)])
      .then(([active, check]) => {
        if (cancelled) return;
        setCampaign(active);
        setPreflight(check);
      })
      .catch((requestError) => {
        if (!cancelled) setError(requestError instanceof Error ? requestError.message : "Не удалось проверить рассылку");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [open, filters]);

  useEffect(() => {
    if (!open || !campaign || campaign.status !== "running") return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      api.outreachCampaign(campaign.id)
        .then((current) => {
          if (cancelled) return;
          setCampaign(current);
          if (current.status !== "running") onChanged();
        })
        .catch((requestError) => {
          if (!cancelled) setError(requestError instanceof Error ? requestError.message : "Не удалось обновить прогресс");
        });
    }, 4000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [campaign?.id, campaign?.status, onChanged, open]);

  useEffect(() => {
    if (!open) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.body.classList.add("dialog-open");
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.classList.remove("dialog-open");
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [onClose, open]);

  if (!open) return null;

  const runAction = async (action: () => Promise<OutreachCampaign>) => {
    setActing(true);
    setError(null);
    try {
      const current = await action();
      setCampaign(current);
      onChanged();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Не удалось изменить рассылку");
    } finally {
      setActing(false);
    }
  };

  const start = async () => {
    if (!confirmed) return;
    await runAction(() => api.startOutreachCampaign(filters));
  };

  return (
    <div className="outreach-dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="outreach-dialog" role="dialog" aria-modal="true" aria-labelledby="outreach-dialog-title">
        <header className="outreach-dialog-header">
          <span className="outreach-dialog-icon"><Send size={20} /></span>
          <div>
            <h2 id="outreach-dialog-title">Отправка писем</h2>
            <p>Персональная очередь с ограничениями для защиты почты</p>
          </div>
          <button type="button" className="dialog-close" onClick={onClose} aria-label="Закрыть"><X size={19} /></button>
        </header>

        {loading ? (
          <div className="outreach-loading"><LoaderCircle className="spin" size={24} /><span>Проверяем получателей и лимиты…</span></div>
        ) : null}

        {error ? <div className="outreach-error"><AlertTriangle size={18} /><span>{error}</span></div> : null}

        {!loading && campaign ? (
          <CampaignProgress campaign={campaign} acting={acting} onAction={runAction} />
        ) : null}

        {!loading && !campaign && preflight ? (
          <>
            <div className="outreach-summary-grid">
              <article><small>По текущим фильтрам</small><strong>{preflight.matched_count}</strong></article>
              <article><small>Можно отправить</small><strong>{preflight.eligible_count}</strong></article>
              <article className="outreach-summary-primary"><small>В этой очереди</small><strong>{preflight.selected_count}</strong></article>
            </div>

            <div className="outreach-safety-panel">
              <div className="outreach-panel-title"><ShieldCheck size={18} /><strong>Ограничения применяются на сервере</strong></div>
              <ul>
                <li>только действующие компании со статусом «Готова»;</li>
                <li>одно письмо на основной email, без повторов уже отправленным;</li>
                <li>до {preflight.policy.hourly_limit} писем в час и {preflight.policy.daily_limit} в день;</li>
                <li>пауза не меньше {formatDelay(preflight.policy.min_interval_seconds)} между письмами;</li>
                <li>до {preflight.policy.max_per_domain_per_day} писем на один домен в день;</li>
                <li>при ошибке или ограничении Gmail очередь автоматически встанет на паузу.</li>
              </ul>
            </div>

            <div className="outreach-skips">
              <span>Не войдут в очередь:</span>
              <small>не готовы — {preflight.skipped.not_ready}</small>
              <small>без email — {preflight.skipped.without_email}</small>
              <small>уже получали письмо — {preflight.skipped.already_contacted}</small>
              <small>повторяющийся адрес — {preflight.skipped.duplicate_address}</small>
              {preflight.deferred_by_campaign_limit ? <small>останутся на следующий запуск — {preflight.deferred_by_campaign_limit}</small> : null}
            </div>

            {preflight.sample ? (
              <details className="outreach-preview">
                <summary><MailCheck size={16} /> Пример первого письма</summary>
                <div><small>{preflight.sample.company_name} · {preflight.sample.recipient}</small><strong>{preflight.sample.subject}</strong><pre>{preflight.sample.body}</pre></div>
              </details>
            ) : null}

            <label className="outreach-confirmation">
              <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
              <span>Я проверил получателей: предложение релевантно, адресаты не отказывались от писем, а ответы «Не писать» будут сразу исключаться из следующих запусков.</span>
            </label>

            <footer className="outreach-dialog-actions">
              <button className="button button--secondary" type="button" onClick={onClose}>Отмена</button>
              <button className="button button--primary" type="button" disabled={!gmailConfigured || !confirmed || !preflight.selected_count || acting} onClick={start}>
                {acting ? <LoaderCircle className="spin" size={17} /> : <Send size={17} />}
                {acting ? "Создаём очередь…" : `Запустить на ${preflight.selected_count}`}
              </button>
            </footer>
          </>
        ) : null}
      </section>
    </div>
  );
}

function CampaignProgress({
  campaign,
  acting,
  onAction,
}: {
  campaign: OutreachCampaign;
  acting: boolean;
  onAction: (action: () => Promise<OutreachCampaign>) => Promise<void>;
}) {
  const terminal = campaign.status === "completed" || campaign.status === "cancelled";
  return (
    <div className="campaign-progress">
      <div className={`campaign-state campaign-state--${campaign.status}`}>
        {campaign.status === "completed" ? <CheckCircle2 size={20} /> : campaign.status === "paused" ? <Pause size={20} /> : campaign.status === "cancelled" ? <Square size={20} /> : <LoaderCircle className="spin" size={20} />}
        <div><strong>{campaignLabels[campaign.status]}</strong><small>{campaign.pause_reason || `Следующая отправка: ${formatDateTime(campaign.next_send_at)}`}</small></div>
      </div>
      <div className="campaign-progress-track"><span style={{ width: `${campaign.progress_percent}%` }} /></div>
      <div className="campaign-counters">
        <span><small>Отправлено</small><strong>{campaign.sent_count}</strong></span>
        <span><small>Осталось</small><strong>{campaign.remaining_count}</strong></span>
        <span><small>Ошибки</small><strong>{campaign.failed_count}</strong></span>
      </div>
      <div className="campaign-policy-line"><Clock3 size={15} /> {campaign.policy.hourly_limit}/час · {campaign.policy.daily_limit}/день · пауза {formatDelay(campaign.policy.min_interval_seconds)}</div>
      {!terminal ? (
        <div className="campaign-actions">
          {campaign.status === "running" ? (
            <button className="button button--secondary" disabled={acting} type="button" onClick={() => onAction(() => api.pauseOutreachCampaign(campaign.id))}><Pause size={16} /> Пауза</button>
          ) : (
            <button className="button button--primary" disabled={acting} type="button" onClick={() => onAction(() => api.resumeOutreachCampaign(campaign.id))}><Play size={16} /> Продолжить</button>
          )}
          <button className="button button--danger" disabled={acting} type="button" onClick={() => onAction(() => api.cancelOutreachCampaign(campaign.id))}><Square size={15} /> Отменить остаток</button>
        </div>
      ) : null}
    </div>
  );
}
