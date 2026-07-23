import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { DoneStep, type DoneStepProps } from './DoneStep.tsx';
import { globalStyles } from './styles.tsx';

vi.mock('../../components/AutoUploadControls.tsx', () => ({
  AutoUploadOffer: () => null,
}));

function props(receiptId: string | null): DoneStepProps {
  return {
    stepperHeader: null,
    bundle: { traces: 3, created: 'Jul 23', approxSize: '1.2 MB' },
    receiptId,
    hostedStatus: receiptId ? 'received' : null,
    supportContact: null,
    onDownloadAgain: vi.fn(),
    onNew: vi.fn(),
    globalStyles,
    shareDestination: null,
    destinationLoading: false,
    destinationFailed: false,
    onRetryDestination: vi.fn(),
  };
}

describe('DoneStep success celebration', () => {
  it('showers viewport confetti after a hosted submission succeeds', () => {
    render(<DoneStep {...props('receipt-123')} />);

    const confetti = screen.getByTestId('success-confetti');
    expect(confetti).toHaveAttribute('aria-hidden', 'true');
    expect(confetti).toHaveStyle({
      position: 'fixed',
      inset: '0',
      overflow: 'hidden',
    });
    expect(confetti.querySelectorAll('span')).toHaveLength(144);
    expect(confetti.querySelectorAll('.claw-confetti-later')).toHaveLength(96);
  });

  it('does not celebrate a bundle that stayed local', () => {
    render(<DoneStep {...props(null)} />);

    expect(screen.queryByTestId('success-confetti')).not.toBeInTheDocument();
  });

  it('keeps static confetti visible when reduced motion is requested', () => {
    const { container } = render(<DoneStep {...props('receipt-123')} />);
    const css = container.querySelector('style')?.textContent ?? '';

    expect(css).toContain('@media (prefers-reduced-motion: reduce)');
    expect(css).toContain('.claw-success-confetti .claw-confetti-later { display: none; }');
    expect(css).toContain('opacity: .72 !important;');
    expect(css).toContain('transform: translate3d(0,var(--cstatic-y),0) rotate(var(--cr));');
  });
});
