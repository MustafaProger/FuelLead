import { Search } from "lucide-react";
import type { SearchRun } from "../types";
import { Notice } from "./Notice";

interface SearchRunNoticeProps {
  error: string | null;
  run: SearchRun | null;
  onCloseError: () => void;
  onCloseRun: () => void;
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
  return run.mode === "combined"
    ? "Checko → API-ФНС"
    : run.mode === "api_fns"
      ? "API-ФНС"
      : run.mode === "checko"
        ? "Checko"
        : "демо-провайдер";
}

function progressDescription(run: SearchRun) {
  if (run.status === "pending") {
    return "Запуск принят. Ожидаем начало обработки.";
  }

  if (!run.candidates_found) {
    return run.mode === "combined"
      ? "Сначала проверяем целевые ОКВЭД через Checko, затем продолжаем через API-ФНС."
      : `Проверяем целевые ОКВЭД и доступность данных в ${providerName(run)}.`;
  }

  const processed = run.companies_created + run.companies_updated;
  const errors = run.errors_count ? ` Ошибок провайдера: ${run.errors_count}.` : "";
  return `Найдено кандидатов: ${run.candidates_found}. Обработано: ${processed}.${errors}`;
}

function completedDescription(run: SearchRun) {
  const summary = `Найдено ${run.candidates_found}, добавлено ${run.companies_created}, обновлено ${run.companies_updated}.`;
  if (!run.errors_count) return summary;

  const errorDetails = run.error_message ? ` Последняя ошибка ${providerName(run)}: ${run.error_message}` : "";
  return `${summary} Ошибок провайдера: ${run.errors_count}.${errorDetails}`;
}

export function SearchRunNotice({ error, run, onCloseError, onCloseRun }: SearchRunNoticeProps) {
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
              <span className="search-run-provider">Живой поиск · {providerName(run)}</span>
              <strong>{run.status === "pending" ? "Готовим поиск компаний" : "Ищем и проверяем компании"}</strong>
              <p>{progressDescription(run)}</p>
              <small>Можно перейти в другой раздел — поиск продолжится в фоне.</small>
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
          run.status === "completed" ? (
            <Notice
              tone={run.errors_count ? "warning" : "success"}
              title={run.errors_count ? "Поиск завершён с предупреждениями" : "Поиск завершён"}
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
