import { BookOpen, Mail, Rocket, Wrench } from "lucide-react";
import { useEffect, useState } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import guideSource from "../../../docs/USER_GUIDE.md?raw";

// The same document is used in the repository and in the application.
const headings = Array.from(guideSource.matchAll(/<a id="([a-z-]+)"><\/a>\s*\n(#{2,3}) ([^\n]+)/g),
  ([, id, level, title]) => ({ id, level: level.length, title }));
const headingIds = new Map(headings.map(({ id, title }) => [title, `guide-${id}`]));
const contents = headings.filter(({ level }) => level === 2);
const introduction = guideSource.slice(guideSource.indexOf("\n") + 1, guideSource.indexOf("## Содержание"));
const body = guideSource.slice(guideSource.indexOf('<a id="first-start">'));
const markdown = `${introduction}\n${body}`;

const components: Components = {
  h2: ({ children }) => <h2 id={headingIds.get(String(children))} tabIndex={-1}>{children}</h2>,
  h3: ({ children }) => <h3 id={headingIds.get(String(children))} tabIndex={-1}>{children}</h3>,
  a: ({ href, children }) => {
    const internal = href !== undefined && href.startsWith("#");
    return <a href={internal ? `#guide/${href.slice(1)}` : href}
      target={internal ? undefined : "_blank"} rel={internal ? undefined : "noopener noreferrer"}>{children}</a>;
  },
  table: ({ children }) => <div className="guide-table-scroll" tabIndex={0} role="region" aria-label="Таблица из инструкции"><table>{children}</table></div>,
};

function sectionFromHash() {
  return window.location.hash.split("/")[1] || "";
}

export default function UserGuidePage() {
  const [section, setSection] = useState(sectionFromHash);

  useEffect(() => {
    let frame = 0;
    const navigate = () => {
      const id = sectionFromHash();
      setSection(id);
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const target = document.getElementById(id ? `guide-${id}` : "guide-title");
        target?.scrollIntoView({ block: "start" });
        target?.focus({ preventScroll: true });
      });
    };
    navigate();
    window.addEventListener("hashchange", navigate);
    return () => { cancelAnimationFrame(frame); window.removeEventListener("hashchange", navigate); };
  }, []);

  return <div className="content-page user-guide-page">
    <header className="page-header">
      <div><h1 id="guide-title" tabIndex={-1}>Инструкция</h1><p>От первого входа до ответа клиенту — по шагам и простым языком</p></div>
      <span className="guide-label"><BookOpen size={18} /> Руководство FuelLead</span>
    </header>
    <nav className="guide-shortcuts" aria-label="Быстрые ответы">
      <a href="#guide/first-start"><Rocket size={19} /><span>С чего начать</span></a>
      <a href="#guide/mailboxes"><Mail size={19} /><span>Почтовые ящики</span></a>
      <a href="#guide/troubleshooting"><Wrench size={19} /><span>Если что-то не работает</span></a>
    </nav>
    <div className="guide-layout">
      <aside className="guide-contents">
        <details open>
          <summary>Содержание</summary>
          <nav aria-label="Содержание инструкции">
            {contents.map(({ id, title }) => <a key={id} href={`#guide/${id}`} aria-current={section === id ? "location" : undefined}>{title}</a>)}
          </nav>
        </details>
        <p>Для поиска слова используйте поиск на странице в меню браузера.</p>
      </aside>
      <article className="guide-article" aria-label="Руководство пользователя FuelLead">
        <Markdown remarkPlugins={[remarkGfm]} components={components} skipHtml>{markdown}</Markdown>
      </article>
    </div>
  </div>;
}
