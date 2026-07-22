import { useEffect, useRef } from "react";

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

export function ConfirmModal({
  open, title, message, confirmLabel = "Confirm", danger,
  onConfirm, onCancel, loading,
}: ConfirmModalProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);

  // Focus the confirm button when the modal opens
  useEffect(() => {
    if (open) {
      // Small delay to allow the DOM to render
      const id = setTimeout(() => confirmRef.current?.focus(), 50);
      return () => clearTimeout(id);
    }
  }, [open]);

  if (!open) return null;

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape" && !loading) {
      e.preventDefault();
      onCancel();
      return;
    }
    if (e.key === "Tab") {
      // Cycle focus between cancel and confirm buttons only
      const focusable = [cancelRef.current, confirmRef.current].filter(Boolean) as HTMLButtonElement[];
      if (focusable.length < 2) return;
      const currentIdx = focusable.indexOf(document.activeElement as HTMLButtonElement);
      if (e.shiftKey) {
        // Backward
        const next = currentIdx <= 0 ? focusable.length - 1 : currentIdx - 1;
        e.preventDefault();
        focusable[next].focus();
      } else {
        // Forward
        const next = currentIdx >= focusable.length - 1 ? 0 : currentIdx + 1;
        e.preventDefault();
        focusable[next].focus();
      }
    }
  };

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onKeyDown={handleKeyDown}
    >
      <div className="modal-box" style={{ width: 380 }}>
        <h3 style={{ fontSize: 16 }}>{title}</h3>
        <p style={{ margin: 0, fontSize: 13, color: "var(--text-dim)", lineHeight: 1.5 }}>{message}</p>
        <div className="modal-actions" style={{ marginTop: 20 }}>
          <button
            ref={cancelRef}
            className="btn-ghost"
            type="button"
            onClick={onCancel}
            disabled={loading}
          >
            Cancel
          </button>
          <button
            ref={confirmRef}
            className={danger ? "btn-reject" : "btn-primary"}
            type="button"
            onClick={onConfirm}
            disabled={loading}
          >
            {loading ? "Processing..." : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
