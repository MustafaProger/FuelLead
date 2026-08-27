import { AlertCircle, AlertTriangle, CheckCircle2, LoaderCircle, X } from "lucide-react";

type NoticeTone = "error" | "success" | "warning" | "progress";

interface NoticeProps {
  tone: NoticeTone;
  title: string;
  description?: string;
  onClose?: () => void;
}

const icons = {
  error: AlertCircle,
  success: CheckCircle2,
  warning: AlertTriangle,
  progress: LoaderCircle,
};

export function Notice({ tone, title, description, onClose }: NoticeProps) {
  const Icon = icons[tone];
  return (
    <div
      className={`notice notice--${tone}`}
      role={tone === "error" ? "alert" : "status"}
      aria-live={tone === "progress" ? "polite" : undefined}
    >
      <span className={`notice-icon ${tone === "progress" ? "spin" : ""}`} aria-hidden="true">
        <Icon size={20} strokeWidth={2} />
      </span>
      <div className="notice-copy">
        <strong>{title}</strong>
        {description ? <p>{description}</p> : null}
      </div>
      {onClose ? (
        <button type="button" onClick={onClose} aria-label="Закрыть уведомление">
          <X size={18} />
        </button>
      ) : null}
    </div>
  );
}
