import { Component, type ReactNode } from 'react';
import { colors, fontFamily, btnPrimary } from '../theme.ts';

interface State {
  hasError: boolean;
  message: string;
}

/**
 * App-wide error boundary. A render-time exception anywhere in the tree would
 * otherwise white-screen the workbench; this shows a styled fallback with the
 * error message and a reload affordance instead. Must be a class component —
 * React error boundaries cannot be function components.
 */
export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { hasError: false, message: '' };

  static getDerivedStateFromError(err: unknown): State {
    return { hasError: true, message: err instanceof Error ? err.message : String(err) };
  }

  componentDidCatch(err: unknown): void {
    console.error('ClawJournal UI error:', err);
  }

  render(): ReactNode {
    if (!this.state.hasError) return this.props.children;
    return (
      <div style={{ padding: 40, maxWidth: 560, margin: '80px auto', textAlign: 'center', fontFamily }}>
        <h2 style={{ color: colors.gray900, fontSize: 20, margin: '0 0 8px' }}>Something went wrong</h2>
        <p style={{ color: colors.gray500, fontSize: 14, margin: '0 0 16px' }}>
          The workbench hit an unexpected error. Reloading usually fixes it.
        </p>
        <pre style={{
          textAlign: 'left',
          background: colors.gray50,
          border: `1px solid ${colors.gray200}`,
          padding: 12,
          borderRadius: 8,
          fontSize: 12,
          color: colors.red700,
          overflow: 'auto',
          marginBottom: 16,
        }}>{this.state.message}</pre>
        <button onClick={() => window.location.reload()} style={{ ...btnPrimary, fontWeight: 600 }}>
          Reload
        </button>
      </div>
    );
  }
}
