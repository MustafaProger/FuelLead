import { Fuel, RefreshCw, Search, Send } from "lucide-react";
import type { DiscoveryProvider } from "../types";

interface HeaderProps {
  mode: DiscoveryProvider;
  searching: boolean;
  onOpenOutreach: () => void;
  onSearch: () => void;
}

export function Header({ mode, searching, onOpenOutreach, onSearch }: HeaderProps) {
  const providerLabel = mode === "combined"
    ? "Checko → API-ФНС подключены"
    : mode === "api_fns"
      ? "API-ФНС подключён"
      : mode === "checko"
        ? "Checko подключён"
        : "Демо-режим";
  return (
    <header className="app-header">
      <div className="header-inner">
        <div className="brand-block">
          <span className="brand-mark" aria-hidden="true">
            <Fuel size={22} strokeWidth={2.2} />
          </span>
          <div>
            <div className="brand-name">FuelLead</div>
            <div className="brand-subtitle">Поиск клиентов для топливных карт</div>
          </div>
        </div>

        <div className="header-actions">
          <span className={`mode-indicator mode-indicator--${mode}`}>
            <span className="mode-dot" aria-hidden="true" />
            {providerLabel}
          </span>
          <button className="button button--secondary" type="button" onClick={onOpenOutreach} aria-label="Открыть безопасную отправку писем">
            <Send size={17} />
            <span>Отправить письма</span>
          </button>
          <button
            className="button button--primary"
            type="button"
            onClick={onSearch}
            disabled={searching}
            aria-label={searching ? "Поиск компаний выполняется" : "Найти компании"}
          >
            {searching ? <RefreshCw className="spin" size={17} /> : <Search size={17} />}
            <span>{searching ? "Идёт поиск…" : "Найти компании"}</span>
          </button>
        </div>
      </div>
    </header>
  );
}
