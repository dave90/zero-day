import { useEffect, useRef, type ReactNode } from "react";
import {
  X,
  ArrowUpRight,
  ShieldCheck,
  AlertCircle,
  FolderOpen,
  LoaderCircle,
} from "lucide-react";
export function Badge({
  children,
  tone = "",
}: {
  children: ReactNode;
  tone?: string;
}) {
  return (
    <span className={`badge ${tone}`}>
      <span className="badge-dot" />
      {children}
    </span>
  );
}
export function Empty({
  title,
  children,
  icon = "folder",
}: {
  title: string;
  children?: ReactNode;
  icon?: string;
}) {
  return (
    <div className="empty-state">
      <div className="empty-icon">
        {icon === "shield" ? <ShieldCheck /> : <FolderOpen />}
      </div>
      <h2>{title}</h2>
      <div className="empty-copy">{children}</div>
    </div>
  );
}
export function Loading() {
  return (
    <div className="loading">
      <LoaderCircle className="spin" size={18} /> Loading workspace…
    </div>
  );
}
export function ErrorBox({ error }: { error: unknown }) {
  return error ? (
    <div role="alert" className="error-banner">
      <AlertCircle size={17} />
      <span>{error instanceof Error ? error.message : String(error)}</span>
    </div>
  ) : null;
}
export function Dialog({
  title,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    const focused = document.activeElement as HTMLElement | null;
    d?.showModal();
    return () => {
      d?.close();
      focused?.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      className={`dialog ${wide ? "sheet" : ""}`}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        if (e.target === ref.current) onClose();
      }}
      aria-labelledby="dialog-title"
    >
      <div className="dialog-heading">
        <div>
          <span className="eyebrow">ZERO STUDIO</span>
          <h2 id="dialog-title">{title}</h2>
        </div>
        <button
          className="icon-button"
          aria-label="Close dialog"
          onClick={onClose}
        >
          <X size={19} />
        </button>
      </div>
      {children}
    </dialog>
  );
}
export function ExternalHint() {
  return (
    <span className="external-hint">
      Local artifact <ArrowUpRight size={12} />
    </span>
  );
}
export const modeName = (c: Record<string, unknown>) =>
  c.ralph
    ? "Ralph"
    : Number(c.swarm_size) > 1
      ? "Swarm"
      : c.execution_mode === "a"
        ? "Single agent"
        : c.execution_mode === "b"
          ? "Legacy session"
          : "Orchestrated";
export const active = (state?: string) =>
  !!state && !["draft", "closed", "failed", "interrupted"].includes(state);
export const displayTime = (t: number) =>
  new Date(t * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
export const bytes = (n: number) =>
  n < 1024 ? n + " B" : (n / 1024).toFixed(1) + " KB";
