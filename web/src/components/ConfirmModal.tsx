/** Reusable confirm dialog — replaces browser confirm(). */
interface ConfirmModalProps {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  loading?: boolean;
}

export function ConfirmModal({ open, title, message, confirmLabel = "Confirm", danger, onConfirm, onCancel, loading }: ConfirmModalProps) {
  if (!open) return null;
  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" aria-label={title} onKeyDown={(e) => { if (e.key === "Escape" && !loading) onCancel(); if (e.key === "Tab") { e.preventDefault(); /* focus stays within dialog */ } }}>
      <div className="modal-box" style={{ width: 380 }}>
        <h3 style={{ fontSize: 16 }}>{title}</h3>
        <p style={{ margin: 0, fontSize: 13, color: "var(--text-dim)", lineHeight: 1.5 }}>{message}</p>
        <div className="modal-actions" style={{ marginTop: 20 }}>
          <button className="btn-ghost" type="button" onClick={onCancel} disabled={loading}>
            Cancel
          </button>
          <button className={danger ? "btn-reject" : "btn-primary"} type="button" onClick={onConfirm} disabled={loading}>
            {loading ? "Processing..." : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
