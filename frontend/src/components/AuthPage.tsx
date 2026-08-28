import {
  ArrowRight,
  BarChart3,
  Eye,
  EyeOff,
  Fuel,
  LockKeyhole,
  Mail,
  ShieldCheck,
} from "lucide-react";
import { type FormEvent, useState } from "react";
import { api, type AuthSession } from "../api";

interface AuthPageProps {
  onAuthenticated: (session: AuthSession) => void;
}

export function AuthPage({ onAuthenticated }: AuthPageProps) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!email.trim() || !password) return;
    setSubmitting(true);
    setError(null);
    try {
      onAuthenticated(await api.login(email, password));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Не удалось войти в FuelLead");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="auth-shell">
      <section className="auth-story" aria-label="О FuelLead">
        <div className="auth-story-rings" aria-hidden="true" />
        <div className="auth-story-content">
          <div className="auth-brand auth-brand--dark">
            <span className="auth-brand-mark" aria-hidden="true"><Fuel size={25} strokeWidth={2.25} /></span>
            <span>
              <strong>FuelLead</strong>
              <small>поиск клиентов для топливных карт</small>
            </span>
          </div>

          <div className="auth-story-copy">
            <h1>Клиенты с высоким<br />расходом топлива</h1>
            <p>Находите компании по целевым ОКВЭД, проверяйте контакты и ведите продажи в одном рабочем пространстве.</p>
          </div>

          <div className="auth-insight-card">
            <div className="auth-insight-label"><BarChart3 size={18} /> База для продаж</div>
            <strong>Москва и область</strong>
            <span>компании по целевым ОКВЭД</span>
          </div>
        </div>
      </section>

      <section className="auth-form-side">
        <div className="auth-mobile-brand">
          <div className="auth-brand">
            <span className="auth-brand-mark" aria-hidden="true"><Fuel size={22} strokeWidth={2.25} /></span>
            <span><strong>FuelLead</strong><small>внутренняя рабочая система</small></span>
          </div>
        </div>

        <div className="auth-form-wrap">
          <div className="auth-kicker"><ShieldCheck size={18} /> Закрытая рабочая зона</div>
          <h2>С возвращением</h2>
          <p className="auth-form-lead">Войдите, чтобы продолжить работу с потенциальными клиентами.</p>

          <form className="auth-form" onSubmit={handleSubmit} noValidate>
            <label className="auth-field">
              <span>Электронная почта</span>
              <span className="auth-input-wrap">
                <Mail size={20} aria-hidden="true" />
                <input
                  type="email"
                  name="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  placeholder="name@company.ru"
                  autoComplete="username"
                  inputMode="email"
                  required
                  autoFocus
                />
              </span>
            </label>

            <label className="auth-field">
              <span>Пароль</span>
              <span className="auth-input-wrap">
                <LockKeyhole size={20} aria-hidden="true" />
                <input
                  type={showPassword ? "text" : "password"}
                  name="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder="Введите пароль"
                  autoComplete="current-password"
                  required
                />
                <button
                  className="auth-password-toggle"
                  type="button"
                  onClick={() => setShowPassword((value) => !value)}
                  aria-label={showPassword ? "Скрыть пароль" : "Показать пароль"}
                >
                  {showPassword ? <EyeOff size={20} /> : <Eye size={20} />}
                </button>
              </span>
            </label>

            <p className="auth-session-note">Вход сохранится на этом устройстве</p>

            {error ? <div className="auth-error" role="alert">{error}</div> : null}

            <button className="auth-submit" type="submit" disabled={submitting || !email.trim() || !password}>
              <span>{submitting ? "Входим…" : "Войти"}</span>
              <ArrowRight size={21} aria-hidden="true" />
            </button>
          </form>

          <div className="auth-security-note">
            <LockKeyhole size={17} aria-hidden="true" />
            <span>Доступ к данным открыт только авторизованным сотрудникам.</span>
          </div>
        </div>
      </section>
    </main>
  );
}
