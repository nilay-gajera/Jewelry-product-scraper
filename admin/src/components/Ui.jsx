import { Icon } from "../icons.jsx";

export function Button({ children, icon, tone = "default", className = "", ...props }) {
  return (
    <button className={`button button--${tone} ${className}`} {...props}>
      {icon ? <Icon name={icon} /> : null}
      <span>{children}</span>
    </button>
  );
}

export function StatusMark({ state = "idle" }) {
  const normalized = state.toLowerCase();
  return (
    <span className={`status-mark status-mark--${normalized}`}>
      <span className="status-mark__dot" />
      {state}
    </span>
  );
}

export function EmptyState({ icon = "products", title, body }) {
  return (
    <div className="empty-state">
      <Icon name={icon} size={28} />
      <strong>{title}</strong>
      <p>{body}</p>
    </div>
  );
}

export function Field({ label, hint, children }) {
  return (
    <label className="field">
      <span className="field__label">{label}</span>
      {children}
      {hint ? <span className="field__hint">{hint}</span> : null}
    </label>
  );
}

export function LoadingLine() {
  return <div className="loading-line" aria-label="Loading" />;
}

export function formatNumber(value) {
  return Number(value || 0).toLocaleString();
}

export function formatDate(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}
