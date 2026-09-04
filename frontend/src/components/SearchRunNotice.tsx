import { discoveryProviderLabels } from "../discoveryProviders";
import { Search } from "lucide-react";
import type { SearchRun } from "../types";
import { Notice } from "./Notice";

interface SearchRunNoticeProps {
  error: string | null;
  run: SearchRun | null;
  onCloseError: () => void;
  onCloseRun: () => void;
  onStop: () => void;
  stopping: boolean;
}

function splitMessage(message: string) {
  const separator = message.indexOf(". ");
  if (separator === -1) return { title: message, description: undefined };
  return {
    title: message.slice(0, separator),
    description: message.slice(separator + 2),
  };
}

function providerName(run: SearchRun) {
  return discoveryProviderLabels[run.active_provider ?? run.mode];
}

const resultLabels: Record<string, string> = {
  results_exhausted: "доступная выдача пройдена",
  daily_limit: "суточный лимит исчерпан",
  quota_exhausted: "лимит исчерпан",
  reserve_not_needed: "резерв не использован: не все основные квоты исчерпаны",
  partial: "частичный результат, есть ошибки",
  rate_limit: "ограничение частоты после повторных попыток",
  timeout: "не ответил вовремя",
  connection_error: "ошибка соединения",
  pagination_stalled: "провайдер повторяет страницу",
};

function stageSummary(run: SearchRun) {
  return Object.entries(run.provider_results ?? {}).map(([provider, reason]) =>
    `${discoveryProviderLabels[provider as keyof typeof discoveryProviderLabels]}: ${resultLabels[reason] ?? "ошибка доступа или ответа API"}.`,
  ).join(" ");
}

function progressDescription(run: SearchRun) {
  if (run.status === "pending") {
    return "Запуск принят. Ожидаем начало обработки.";
  }

  if (run.cancel_requested) return "Останавливаем поиск после текущего запроса. Найденные компании сохранятся.";

  const processed = run.companies_created + run.companies_updated;
  const errors = run.errors_count
    ? ` Ошибок провайдера: ${run.errors_count}.${run.error_message ? ` Последняя: ${run.error_message}` : ""}`
    : "";

  if (run.search_scope === "full") {
    return `${run.progress_message ?? "Последовательно обрабатываем доступную выдачу."} Поисковых вызовов: ${run.search_requests}, запросов карточек: ${run.company_requests}. Добавлено: ${run.companies_created}.${errors}`;
  }

  if (!run.candidates_found) {
    const stage = run.mode === "combined"
      ? "Проверяем Checko, Okvedo и DaData. API-ФНС подключится только после исчерпания суточных лимитов всех настроенных источников."
      : `Проверяем целевые ОКВЭД и доступность данных в ${providerName(run)}.`;
    return `${stage}${errors}`;
  }

  return `Найдено кандидатов: ${run.candidates_found}. Обработано: ${processed}.${errors}`;
}

function completedDescription(run: SearchRun) {
  const summary = `Найдено ${run.candidates_found}, добавлено ${run.companies_created}, обновлено ${run.companies_updated}. ${stageSummary(run)}`;
  if (!run.errors_count) return summary;

  const errorDetails = run.error_message ? ` Последняя ошибка ${providerName(run)}: ${run.error_message}` : "";
  return `${summary} Ошибок провайдера: ${run.errors_count}.${errorDetails}`;
}

export function SearchRunNotice({ error, run, onCloseError, onCloseRun, onStop, stopping }: SearchRunNoticeProps) {
  const searching = run?.status === "pending" || run?.status === "running";
  const failedResult = run?.error_message ? splitMessage(run.error_message) : null;
  const processed = run ? run.companies_created + run.companies_updated : 0;

  return (
    <>
      {run && searching ? (
        <div className="search-run-experience">
          <div className="search-space-motion" aria-hidden="true">
            <span />
            <span />
            <span />
          </div>
          <section className="search-run-card" role="status" aria-live="polite">
            <span className="search-run-radar" aria-hidden="true">
              <Search size={22} strokeWidth={2.2} />
            </span>
            <div className="search-run-card-copy">
              <span className="search-run-provider">{run.search_scope === "full" ? "Полный поиск" : "Поиск"} · {providerName(run)}</span>
              <strong>{run.status === "pending" ? "Готовим поиск компаний" : "Ищем и проверяем компании"}</strong>
              <p>{progressDescription(run)}</p>
              <small>До лимита API или конца выдачи. Можно закрыть вкладку — поиск продолжится, пока работает сервер.</small>
              <button className="button button--secondary" type="button" onClick={onStop} disabled={stopping || run.cancel_requested}>
                {stopping || run.cancel_requested ? "Останавливаем…" : "Остановить поиск"}
              </button>
            </div>
            <div className="search-run-counters" aria-label="Прогресс поиска">
              <span><small>Найдено</small><strong>{run.candidates_found}</strong></span>
              <span><small>Обработано</small><strong>{processed}</strong></span>
            </div>
          </section>
        </div>
      ) : null}

      <div className="search-run-feedback">
        {error ? (
          <Notice
            tone="error"
            title="Не удалось выполнить поиск"
            description={error}
            onClose={onCloseError}
          />
        ) : null}

        {run && !searching ? (
          run.status === "completed" || run.status === "cancelled" ? (
            <Notice
              tone={run.errors_count ? "warning" : "success"}
              title={run.status === "cancelled" ? "Поиск остановлен, результат сохранён" : run.errors_count ? "Поиск завершён с предупреждениями" : "Поиск завершён"}
              description={completedDescription(run)}
              onClose={onCloseRun}
            />
          ) : (
            <Notice
              tone="error"
              title={failedResult?.title || "Поиск не выполнен"}
              description={failedResult?.description || `${providerName(run)} не вернул компании.`}
              onClose={onCloseRun}
            />
          )
        ) : null}
      </div>
    </>
  );
}
