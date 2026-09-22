import { CheckCircle2, Code2, Download, FileText, Info, Paperclip, Save, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { EmailAttachment, EmailPreview, EmailTemplate, EmailTemplateDraft } from "../types";
import { Notice } from "./Notice";
import { EmailContentPreview } from "./EmailContentPreview";

type TemplateField = "subject_template" | "body_template" | "html_template";
const emptyDraft: EmailTemplateDraft = { subject_template: "", body_template: "", body_format: "text", html_template: "", attachment_ids: [] };
const fileSize = (size: number) => size < 1024 * 1024 ? `${Math.max(1, Math.ceil(size / 1024))} КБ` : `${(size / 1024 / 1024).toFixed(1)} МБ`;

export function EmailTemplatePage() {
  const [template, setTemplate] = useState<EmailTemplate | null>(null);
  const [draft, setDraft] = useState<EmailTemplateDraft>(emptyDraft);
  const [attachments, setAttachments] = useState<EmailAttachment[]>([]);
  const [activeField, setActiveField] = useState<TemplateField>("body_template");
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [saved, setSaved] = useState(true);
  const [preview, setPreview] = useState<EmailPreview | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [previewPending, setPreviewPending] = useState(false);
  const [notice, setNotice] = useState<{ tone: "error" | "success" | "warning"; title: string; description?: string } | null>(null);
  const subjectRef = useRef<HTMLInputElement>(null);
  const bodyRef = useRef<HTMLTextAreaElement>(null);
  const htmlRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let cancelled = false;
    api.emailTemplate().then((response) => {
      if (cancelled) return;
      setTemplate(response);
      setDraft({ subject_template: response.subject_template, body_template: response.body_template, body_format: response.body_format, html_template: response.html_template, attachment_ids: response.attachments.map((file) => file.id) });
      setAttachments(response.attachments);
      setActiveField(response.body_format === "html" ? "html_template" : "body_template");
    }).catch((error) => {
      if (!cancelled) setNotice({ tone: "error", title: "Не удалось открыть шаблон", description: error instanceof Error ? error.message : undefined });
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!template) return;
    let cancelled = false;
    setPreviewPending(true);
    const timer = window.setTimeout(() => {
      api.previewTemplateDraft(draft).then((response) => {
        if (!cancelled) { setPreview(response); setPreviewError(""); }
      }).catch((error) => {
        if (!cancelled) { setPreview(null); setPreviewError(error instanceof Error ? error.message : "Не удалось загрузить предпросмотр"); }
      }).finally(() => { if (!cancelled) setPreviewPending(false); });
    }, 450);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [draft, template]);

  const update = (changes: Partial<EmailTemplateDraft>) => { setDraft((current) => ({ ...current, ...changes })); setSaved(false); };
  const insertVariable = (token: string) => {
    const ref = activeField === "subject_template" ? subjectRef.current : activeField === "html_template" ? htmlRef.current : bodyRef.current;
    const value = draft[activeField];
    const start = ref?.selectionStart ?? value.length;
    const end = ref?.selectionEnd ?? value.length;
    update({ [activeField]: `${value.slice(0, start)}${token}${value.slice(end)}` });
    window.setTimeout(() => { ref?.focus(); ref?.setSelectionRange(start + token.length, start + token.length); }, 0);
  };

  const saveTemplate = async () => {
    setSaving(true);
    try {
      const response = await api.saveEmailTemplate(draft);
      setTemplate(response); setSaved(true);
      setNotice({ tone: "success", title: "Шаблон сохранён", description: "Формат и вложения применятся к новым письмам и новым рассылкам. Ранее созданные рассылки сохраняют своё содержимое." });
    } catch (error) {
      setNotice({ tone: "error", title: "Не удалось сохранить шаблон", description: error instanceof Error ? error.message : undefined });
    } finally { setSaving(false); }
  };

  const loadOffer = async () => {
    setSaving(true);
    try {
      update(await api.artelOffer()); setActiveField("html_template");
      setNotice({ tone: "success", title: "Предложение АРТЭЛЬ добавлено в редактор", description: "Проверьте предпросмотр и сохраните шаблон." });
    } catch (error) { setNotice({ tone: "error", title: "Не удалось загрузить предложение", description: error instanceof Error ? error.message : undefined }); }
    finally { setSaving(false); }
  };

  const uploadFiles = async (files: FileList | null) => {
    if (!files?.length) return;
    const incoming = Array.from(files);
    if (attachments.length + incoming.length > 5 || [...attachments, ...incoming].reduce((sum, file) => sum + file.size, 0) > 10 * 1024 * 1024) {
      setNotice({ tone: "error", title: "Можно прикрепить до 5 файлов, суммарно до 10 МБ" });
      if (fileRef.current) fileRef.current.value = "";
      return;
    }
    setUploading(true);
    try {
      for (const file of incoming) {
        const uploaded = await api.uploadEmailAttachment(file);
        setAttachments((current) => [...current, uploaded]);
        setDraft((current) => ({ ...current, attachment_ids: [...current.attachment_ids, uploaded.id] }));
        setSaved(false);
      }
    } catch (error) { setNotice({ tone: "error", title: "Не удалось прикрепить файл", description: error instanceof Error ? error.message : undefined }); }
    finally { setUploading(false); if (fileRef.current) fileRef.current.value = ""; }
  };

  const downloadHtml = () => {
    const url = URL.createObjectURL(new Blob([draft.html_template], { type: "text/html;charset=utf-8" }));
    const link = document.createElement("a"); link.href = url; link.download = "Коммерческое предложение АРТЭЛЬ.html"; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  return <div className="content-page template-page">
    <header className="page-header"><div><h1>Шаблон письма</h1><p>Текст, HTML и файлы для персональных предложений каждой компании</p></div></header>
    {notice ? <Notice {...notice} onClose={() => setNotice(null)} /> : null}
    {!template ? <p>{notice ? "Обновите страницу, чтобы повторить загрузку." : "Загружаем шаблон…"}</p> : <div className="template-workspace template-workspace--preview">
      <section className="template-panel template-editor-panel">
        <div className="template-panel-heading"><h2>Основной шаблон</h2><p>Используется для новых писем и новых рассылок.</p></div>
        <fieldset className="template-fields" disabled={saving || uploading}>
          <button className="button button--secondary template-preset" type="button" onClick={loadOffer}><FileText size={17} /> Использовать предложение АРТЭЛЬ</button>
          <label className="form-field"><span>Тема письма</span><input aria-label="Тема письма" ref={subjectRef} maxLength={998} value={draft.subject_template} onFocus={() => setActiveField("subject_template")} onChange={(e) => update({ subject_template: e.target.value })} /></label>
          <div className="template-format-switch" role="group" aria-label="Формат письма">
            <button type="button" aria-pressed={draft.body_format === "text"} onClick={() => { update({ body_format: "text" }); setActiveField("body_template"); }}><FileText size={17} /> Обычный текст</button>
            <button type="button" aria-pressed={draft.body_format === "html"} onClick={() => { update({ body_format: "html" }); setActiveField("html_template"); }}><Code2 size={17} /> HTML-код</button>
          </div>
          {draft.body_format === "text" ? <label className="form-field"><span>Текст письма</span><textarea aria-label="Текст письма" ref={bodyRef} className="template-body-input" maxLength={20000} value={draft.body_template} onFocus={() => setActiveField("body_template")} onChange={(e) => update({ body_template: e.target.value })} /></label> : <>
            <label className="form-field"><span>HTML-код письма</span><textarea aria-label="HTML-код письма" ref={htmlRef} className="template-body-input template-html-input" spellCheck={false} maxLength={200000} value={draft.html_template} placeholder="Вставьте HTML-код коммерческого предложения" onFocus={() => setActiveField("html_template")} onChange={(e) => update({ html_template: e.target.value })} /></label>
            <p className="template-help">Клиент получит оформленное письмо. Текстовая версия создаётся автоматически. Используйте стили внутри тегов; скрипты и формы удаляются.</p>
            <button className="button button--secondary" type="button" onClick={downloadHtml} disabled={!draft.html_template.trim()}><Download size={16} /> Скачать HTML-шаблон</button>
          </>}
          <div className="variables-section"><h3>Подстановки</h3><p>Нажмите, чтобы вставить в позицию курсора. Работают в теме, тексте и HTML.</p><div className="variable-list">{template.variables.map((variable) => <button type="button" key={variable.key} onClick={() => insertVariable(variable.token)}><code>{variable.token}</code><span>{variable.label}</span></button>)}</div></div>
          <section className="template-attachments" aria-label="Вложения"><h3><Paperclip size={17} /> Вложения</h3><p>Прикрепляются к каждому письму в любом формате. До 5 файлов, суммарно 10 МБ.</p>
            <input ref={fileRef} type="file" multiple hidden accept=".pdf,.doc,.docx,.xls,.xlsx,.pptx,.txt,.csv,.png,.jpg,.jpeg,.zip" onChange={(e) => void uploadFiles(e.target.files)} />
            <button className="button button--secondary" type="button" disabled={attachments.length >= 5} onClick={() => fileRef.current?.click()}><Paperclip size={16} /> {uploading ? "Загружаем…" : "Прикрепить файл"}</button>
            {attachments.length ? <ul className="template-file-list">{attachments.map((file) => <li key={file.id}><a href={api.attachmentUrl(file.id)} download>{file.filename}<small>{fileSize(file.size)}</small></a><button type="button" aria-label={`Убрать ${file.filename}`} onClick={() => { setAttachments((current) => current.filter((item) => item.id !== file.id)); update({ attachment_ids: draft.attachment_ids.filter((id) => id !== file.id) }); }}><Trash2 size={17} /></button></li>)}</ul> : <p className="template-help">Файлы пока не прикреплены.</p>}
          </section>
          <div className="template-save-row"><button className="button button--primary" type="button" onClick={saveTemplate} disabled={saved || !draft.subject_template.trim() || !(draft.body_format === "html" ? draft.html_template : draft.body_template).trim()}><Save size={17} /> {saving ? "Сохраняем…" : "Сохранить шаблон"}</button><span className={saved ? "save-state save-state--saved" : "save-state"}>{saved ? <CheckCircle2 size={16} /> : <Info size={16} />}{saved ? "Все изменения сохранены" : "Есть несохранённые изменения"}</span></div>
        </fieldset>
      </section>
      <section className="template-panel template-preview-panel" aria-label="Предпросмотр письма">
        <div className="template-panel-heading"><h2>Как выглядит письмо</h2><p>Пример для ООО «Пример». Компания и дата подставятся при подготовке письма.</p></div>
        <p className="template-preview-status" role="status">{previewPending ? "Обновляем предпросмотр…" : previewError || "Предпросмотр готов"}</p>
        {preview ? <div className={previewPending ? "template-preview-content is-pending" : "template-preview-content"}><div className="template-preview-subject"><small>Тема</small><strong>{preview.subject}</strong></div><EmailContentPreview html={preview.html_body} text={preview.body} />{preview.attachments.length ? <p className="template-preview-files"><Paperclip size={15} /> {preview.attachments.map((file) => file.filename).join(" · ")}</p> : null}</div> : null}
      </section>
    </div>}
  </div>;
}
