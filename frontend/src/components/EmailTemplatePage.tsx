import { CheckCircle2, Info, Mail, RotateCcw, Save, Send } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Company, EmailPreview, EmailTemplate } from "../types";
import { Notice } from "./Notice";

interface EmailTemplatePageProps {
  gmailConfigured: boolean;
  senderEmail: string;
  onSent: () => void;
}

type TemplateField = "subject" | "body";

export function EmailTemplatePage({ gmailConfigured, senderEmail, onSent }: EmailTemplatePageProps) {
  const [template, setTemplate] = useState<EmailTemplate | null>(null);
  const [subjectTemplate, setSubjectTemplate] = useState("");
  const [bodyTemplate, setBodyTemplate] = useState("");
  const [companies, setCompanies] = useState<Company[]>([]);
  const [companyId, setCompanyId] = useState<number | null>(null);
  const [recipient, setRecipient] = useState("");
  const [preview, setPreview] = useState<EmailPreview | null>(null);
  const [finalSubject, setFinalSubject] = useState("");
  const [finalBody, setFinalBody] = useState("");
  const [activeField, setActiveField] = useState<TemplateField>("body");
  const [saving, setSaving] = useState(false);
  const [sending, setSending] = useState(false);
  const [saved, setSaved] = useState(true);
  const [notice, setNotice] = useState<{ tone: "error" | "success" | "warning"; title: string; description?: string } | null>(null);
  const subjectRef = useRef<HTMLInputElement>(null);
  const bodyRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      api.emailTemplate(),
      api.companies({ status: "", hasEmail: "true", emailProvider: "", category: "", discoveredOn: "", search: "" }, 1, 100),
    ]).then(([templateResponse, companiesResponse]) => {
      if (cancelled) return;
      setTemplate(templateResponse);
      setSubjectTemplate(templateResponse.subject_template);
      setBodyTemplate(templateResponse.body_template);
      setCompanies(companiesResponse.items);
      const first = companiesResponse.items[0];
      if (first) {
        setCompanyId(first.id);
        setRecipient(first.emails[0]?.email || "");
      }
    }).catch((requestError) => {
      if (!cancelled) setNotice({ tone: "error", title: "Не удалось открыть шаблон", description: requestError instanceof Error ? requestError.message : undefined });
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!companyId || !subjectTemplate || !bodyTemplate) return;
    const timer = window.setTimeout(() => {
      api.previewEmail(companyId, subjectTemplate, bodyTemplate, recipient)
        .then((response) => {
          setPreview(response);
          setFinalSubject(response.subject);
          setFinalBody(response.body);
          setNotice((current) => current?.tone === "error" ? null : current);
        })
        .catch((requestError) => setNotice({
          tone: "error",
          title: "Не удалось собрать письмо",
          description: requestError instanceof Error ? requestError.message : undefined,
        }));
    }, 220);
    return () => window.clearTimeout(timer);
  }, [companyId, recipient, subjectTemplate, bodyTemplate]);

  const selectedCompany = companies.find((company) => company.id === companyId) || null;

  const updateSubject = (value: string) => { setSubjectTemplate(value); setSaved(false); };
  const updateBody = (value: string) => { setBodyTemplate(value); setSaved(false); };

  const handleCompanyChange = (nextId: number) => {
    const company = companies.find((item) => item.id === nextId);
    setCompanyId(nextId);
    setRecipient(company?.emails[0]?.email || "");
  };

  const insertVariable = (token: string) => {
    const ref = activeField === "subject" ? subjectRef.current : bodyRef.current;
    const value = activeField === "subject" ? subjectTemplate : bodyTemplate;
    const start = ref?.selectionStart ?? value.length;
    const end = ref?.selectionEnd ?? value.length;
    const next = `${value.slice(0, start)}${token}${value.slice(end)}`;
    if (activeField === "subject") updateSubject(next); else updateBody(next);
    window.setTimeout(() => {
      ref?.focus();
      ref?.setSelectionRange(start + token.length, start + token.length);
    }, 0);
  };

  const saveTemplate = async () => {
    setSaving(true);
    try {
      const response = await api.saveEmailTemplate(subjectTemplate, bodyTemplate);
      setTemplate(response);
      setSaved(true);
      setNotice({ tone: "success", title: "Шаблон сохранён", description: "Он будет использоваться для новых писем всем компаниям." });
    } catch (requestError) {
      setNotice({ tone: "error", title: "Не удалось сохранить шаблон", description: requestError instanceof Error ? requestError.message : undefined });
    } finally {
      setSaving(false);
    }
  };

  const sendEmail = async () => {
    if (!companyId || !preview) return;
    setSending(true);
    try {
      const response = await api.sendEmail(companyId, preview.recipient, finalSubject, finalBody);
      setNotice({ tone: "success", title: "Письмо отправлено", description: `${response.recipient} · отправлено одно персональное письмо.` });
      onSent();
    } catch (requestError) {
      setNotice({ tone: "error", title: "Не удалось отправить письмо", description: requestError instanceof Error ? requestError.message : undefined });
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="content-page template-page">
      <header className="page-header">
        <div><h1>Шаблон письма</h1><p>Один шаблон для персональных писем каждой компании</p></div>
        <div className={`integration-chip ${gmailConfigured ? "integration-chip--ready" : "integration-chip--warning"}`}>
          {gmailConfigured ? <CheckCircle2 size={17} /> : <Mail size={17} />}
          <span>{gmailConfigured ? "Gmail OAuth подключён" : "Gmail OAuth не настроен"}<small>{senderEmail}</small></span>
        </div>
      </header>

      {notice ? <Notice {...notice} onClose={() => setNotice(null)} /> : null}

      <div className="template-workspace">
        <section className="template-panel template-editor-panel">
          <div className="template-panel-heading"><h2>Основной шаблон</h2><p>Сохранённый текст применяется ко всем новым письмам.</p></div>
          <label className="form-field">
            <span>Тема письма</span>
            <input ref={subjectRef} value={subjectTemplate} onFocus={() => setActiveField("subject")} onChange={(event) => updateSubject(event.target.value)} />
          </label>
          <label className="form-field">
            <span>Текст письма</span>
            <textarea ref={bodyRef} className="template-body-input" value={bodyTemplate} onFocus={() => setActiveField("body")} onChange={(event) => updateBody(event.target.value)} />
          </label>

          <div className="variables-section">
            <h3>Переменные</h3>
            <p>Нажмите переменную, чтобы вставить её в позицию курсора.</p>
            <div className="variable-list">
              {(template?.variables || []).map((variable) => (
                <button type="button" key={variable.key} onClick={() => insertVariable(variable.token)}>
                  <code>{variable.token}</code><span>{variable.label}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="template-save-row">
            <button className="button button--primary" type="button" onClick={saveTemplate} disabled={saving || saved}>
              <Save size={17} /> {saving ? "Сохраняем…" : "Сохранить шаблон"}
            </button>
            <span className={saved ? "save-state save-state--saved" : "save-state"}>
              {saved ? <CheckCircle2 size={16} /> : <Info size={16} />}
              {saved ? "Все изменения сохранены" : "Есть несохранённые изменения"}
            </span>
          </div>
        </section>

        <section className="template-panel company-email-panel">
          <div className="template-panel-heading"><h2>Письмо компании</h2><p>Это письмо можно изменить отдельно перед отправкой.</p></div>
          {companies.length ? (
            <>
              <label className="form-field">
                <span>Компания</span>
                <select value={companyId || ""} onChange={(event) => handleCompanyChange(Number(event.target.value))}>
                  {companies.map((company) => <option key={company.id} value={company.id}>{company.name}</option>)}
                </select>
              </label>
              <label className="form-field">
                <span>Получатель</span>
                <select value={recipient} onChange={(event) => setRecipient(event.target.value)}>
                  {(selectedCompany?.emails || []).map((email) => <option key={email.id} value={email.email}>{email.email}</option>)}
                </select>
              </label>
              <div className="one-off-hint"><Info size={16} /><span>Правки ниже относятся только к этому письму и не меняют основной шаблон.</span></div>
              <label className="form-field">
                <span>Тема письма</span>
                <input value={finalSubject} onChange={(event) => setFinalSubject(event.target.value)} />
              </label>
              <label className="form-field form-field--grow">
                <span>Текст письма</span>
                <textarea className="final-body-input" value={finalBody} onChange={(event) => setFinalBody(event.target.value)} />
              </label>
              <div className="send-actions">
                <button className="button button--secondary" type="button" onClick={() => { setFinalSubject(preview?.subject || ""); setFinalBody(preview?.body || ""); }}>
                  <RotateCcw size={17} /> Вернуть шаблон
                </button>
                <button className="button button--primary" type="button" disabled={!gmailConfigured || !preview || sending || !finalSubject.trim() || !finalBody.trim()} onClick={sendEmail}>
                  <Send size={17} /> {sending ? "Отправляем…" : "Отправить письмо"}
                </button>
              </div>
              {!gmailConfigured ? <p className="send-disabled-copy">Подключите Gmail OAuth, чтобы активировать отправку. Редактор и предпросмотр уже работают.</p> : null}
            </>
          ) : (
            <div className="template-empty"><Mail size={24} /><h3>Нет компаний с email</h3><p>После поиска компании с найденным адресом появятся здесь.</p><a className="button button--secondary" href="#companies">Перейти к компаниям</a></div>
          )}
        </section>
      </div>
    </div>
  );
}
