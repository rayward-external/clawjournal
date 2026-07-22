import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PackageStep } from './PackageStep.tsx';

function renderMissingScanners(overrides: Partial<React.ComponentProps<typeof PackageStep>> = {}) {
  const onInstallScannersAndRetry = vi.fn();
  render(
    <PackageStep
      stepperHeader={null}
      approvedCount={1}
      approvedList={[]}
      progress={46}
      log="Failed: required scanner missing"
      failed="Betterleaks is required"
      missingScanners
      installingScanners={false}
      blockedSessions={[]}
      onInstallScannersAndRetry={onInstallScannersAndRetry}
      onRetry={vi.fn()}
      onRemoveBlockedAndRetry={vi.fn()}
      onBack={vi.fn()}
      globalStyles={null}
      {...overrides}
    />,
  );
  return { onInstallScannersAndRetry };
}

describe('PackageStep scanner recovery', () => {
  it('offers a local managed install and retry when preflight reports a missing scanner', () => {
    const { onInstallScannersAndRetry } = renderMissingScanners();

    expect(screen.getByRole('heading', { name: 'Packaging failed' })).toBeInTheDocument();
    expect(screen.getByText('Local scanners required')).toBeInTheDocument();
    expect(screen.getByText(/Betterleaks and TruffleHog scan the redacted bundle on this computer/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Install secure scanners & retry' }));
    expect(onInstallScannersAndRetry).toHaveBeenCalledOnce();
  });

  it('disables the repair action while the managed installers are running', () => {
    renderMissingScanners({ installingScanners: true });

    expect(screen.getByRole('button', { name: 'Installing secure scanners…' })).toBeDisabled();
  });
});
