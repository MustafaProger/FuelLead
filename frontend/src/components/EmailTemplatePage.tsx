import { CheckCircle2, Info, Save } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { EmailTemplate } from "../types";
import { Notice } from "./Notice";

type TemplateField = "subject" | "body";

export function EmailTemplatePage() {
  const [template, setTemplate] = useState<EmailTemplate | null>(null);
  const [subjectTemplate, setSubjectTemplate] = useState("");
  const [bodyTemplate, setBodyTemplate] = useState("");
  const [activeField, setActiveField] = useState<TemplateField>("body");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(true);
  const [notice, setNotice] = useState<{ tone: "error" | "success" | "warning"; title: string; description?: string } | null>(null);
  const subjectRef = useRef<HTMLInputElement>(null);
  const bodyRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    let cancelled = false;
    api.emailTemplate().then((templateResponse) => {
      if (cancelled) return;
      setTemplate(templateResponse);
      setSubjectTemplate(templateResponse.subject_template);
      setBodyTemplate(templateResponse.body_template);
    }).catch((requestError) => {
      if (!cancelled) setNotice({ tone: "error", title: "Не удалось открыть шаблон", description: requestError instanceof Error ? requestError.message : undefined });
    });
    return () => { cancelled = true; };
  }, []);

  const updateSubject = (value: string) => { setSubjectTemplate(value); setSaved(false); };
  const updateBody = (value: string) => { setBodyTemplate(value); setSaved(false); };

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

  return (
    <div className="content-page template-page">
      <header className="page-header">
        <div><h1>Шаблон письма</h1><p>Один шаблон для персональных писем каждой компании</p></div>
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

      </div>
    </div>
  );
}
