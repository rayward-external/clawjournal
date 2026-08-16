import { Component, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { BUG_REPORT_URL } from '../bugReportDraft.ts';
import type { BugReportSurface } from '../bugReportDraft.ts';
import { colors, fontFamily } from '../theme.ts';
import { ErrorBoundary } from './ErrorBoundary.tsx';
import { BugReportDialog } from './BugReportDialog.tsx';

class BugReportSafetyBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <a
        href={BUG_REPORT_URL}
        target="_blank"
        rel="noopener noreferrer"
        style={launcherStyle}
      >
        Open GitHub issues
      </a>
    );
  }
}

const launcherStyle: CSSProperties = {
  position: 'fixed',
  right: 20,
  // Toasts occupy bottom/right=20 at z-index 9999.
  bottom: 72,
  zIndex: 9_500,
  padding: '8px 12px',
  border: `1px solid ${colors.gray300}`,
  borderRadius: 999,
  background: colors.white,
  color: colors.gray700,
  boxShadow: '0 4px 16px rgba(42, 40, 37, 0.12)',
  cursor: 'pointer',
  fontFamily,
  fontSize: 12.5,
  fontWeight: 600,
  lineHeight: 1.2,
  textDecoration: 'none',
};

function BugReportLauncher({ surface }: { surface: BugReportSurface }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)} style={launcherStyle}>
        Report a problem
      </button>
      <BugReportDialog open={open} onClose={() => setOpen(false)} surface={surface} />
    </>
  );
}

/**
 * Keep the reporter outside the workbench error boundary so ready, recovery,
 * unavailable, and render-crash screens all retain the same local-only entry.
 */
export function GlobalBugReport({ children }: { children: ReactNode }) {
  const [surface, setSurface] = useState<BugReportSurface>('workbench');
  return (
    <>
      <ErrorBoundary onError={() => setSurface('ui_render_error')}>
        {children}
      </ErrorBoundary>
      <BugReportSafetyBoundary>
        <BugReportLauncher surface={surface} />
      </BugReportSafetyBoundary>
    </>
  );
}
