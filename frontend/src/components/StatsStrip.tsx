import type { Stats } from "../types";

interface StatsStripProps {
  stats: Stats;
  loading: boolean;
}

const items: Array<{ key: keyof Stats; label: string; tone?: string }> = [
  { key: "total", label: "Всего компаний" },
  { key: "new", label: "Новые", tone: "orange" },
  { key: "with_email", label: "С email", tone: "green" },
  { key: "without_email", label: "Без email", tone: "muted" },
];

export function StatsStrip({ stats, loading }: StatsStripProps) {
  return (
    <section className="stats-strip" aria-label="Сводка по компаниям">
      {items.map((item) => (
        <div className="stat" key={item.key}>
          <span className="stat-label">{item.label}</span>
          <strong className={`stat-value stat-value--${item.tone || "default"}`}>
            {loading ? "—" : stats[item.key].toLocaleString("ru-RU")}
          </strong>
        </div>
      ))}
    </section>
  );
}

