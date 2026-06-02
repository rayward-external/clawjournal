import { useEffect, useRef } from 'react';
import { colors, btnPrimary, btnSecondary, btnDanger } from '../theme.ts';

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  variant?: 'danger' | 'primary';
  onConfirm: () => void;
  onCancel: () => void;
  // Backdrop click / Escape. Defaults to onCancel so existing call sites whose
  // onCancel is a harmless state-clear keep working. Pass a distinct onDismiss
  // when onCancel has a side effect (e.g. persisting a decision) that a stray
  // backdrop misclick must NOT trigger.
  onDismiss?: () => void;
}

export function ConfirmDialog({ open, title, message, confirmLabel = 'Confirm', variant = 'primary', onConfirm, onCancel, onDismiss }: ConfirmDialogProps) {
  const dismiss = onDismiss ?? onCancel;
  const confirmRef = useRef<HTMLButtonElement>(null);

  // While open, behave like a real modal: a capture-phase listener swallows page
  // keystrokes so route-level shortcuts (e.g. Inbox j/k/Enter) don't fire behind
  // the dialog (the native window.confirm this replaced blocked them). Enter
  // confirms, Escape dismisses — matching window.confirm's OK/Cancel keys.
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.stopPropagation(); dismiss(); return; }
      if (e.key === 'Enter') { e.stopPropagation(); e.preventDefault(); onConfirm(); return; }
      e.stopPropagation();
    };
    window.addEventListener('keydown', handler, true);
    confirmRef.current?.focus();
    return () => window.removeEventListener('keydown', handler, true);
  }, [open, dismiss, onConfirm]);

  if (!open) return null;

  const confirmStyle = variant === 'danger' ? btnDanger : btnPrimary;

  return (
    <div
      onClick={dismiss}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.35)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 9998,
        animation: 'fade-in 0.15s ease-out',
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          background: colors.white,
          borderRadius: 10,
          padding: '24px 28px',
          minWidth: 340,
          maxWidth: 440,
          boxShadow: '0 8px 30px rgba(0,0,0,0.18)',
          animation: 'dialog-in 0.15s ease-out',
        }}
      >
        <h3 style={{ margin: '0 0 8px', fontSize: 16, fontWeight: 600, color: colors.gray900 }}>
          {title}
        </h3>
        <p style={{ margin: '0 0 20px', fontSize: 14, color: colors.gray500, lineHeight: 1.5 }}>
          {message}
        </p>
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button onClick={onCancel} style={btnSecondary}>Cancel</button>
          <button ref={confirmRef} onClick={onConfirm} style={{ ...confirmStyle, fontWeight: 600 }}>
            {confirmLabel}
          </button>
        </div>
      </div>
      <style>{`
        @keyframes fade-in { from { opacity: 0; } to { opacity: 1; } }
        @keyframes dialog-in { from { opacity: 0; transform: scale(0.95); } to { opacity: 1; transform: scale(1); } }
      `}</style>
    </div>
  );
}
