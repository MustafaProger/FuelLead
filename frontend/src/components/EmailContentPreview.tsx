export function EmailContentPreview({ html, text }: { html?: string | null; text: string }) {
  if (!html) return <pre className="email-text-preview">{text}</pre>;
  const document = `<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"><style>body{margin:0;font:15px/1.6 Arial,sans-serif;overflow-wrap:anywhere}img{max-width:100%}table{max-width:100%}</style></head><body>${html}</body></html>`;
  return <iframe className="email-html-preview" title="Предпросмотр HTML-письма" sandbox="" referrerPolicy="no-referrer" srcDoc={document} />;
}
