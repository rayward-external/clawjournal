import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { WorkflowProgress } from './WorkflowProgress.tsx';
import { workflowProgressStageFor } from './workflowProgressStage.ts';

describe('WorkflowProgress', () => {
  it.each([
    { stage: 1 as const, percent: '33', text: 'Step 1 of 3: Review sessions', section: 'Sessions' },
    { stage: 2 as const, percent: '67', text: 'Step 2 of 3: Prepare share', section: 'Share' },
    { stage: 3 as const, percent: '100', text: 'Step 3 of 3: Submitted', section: 'Share' },
  ])('renders stage $stage as $percent percent', ({ stage, percent, text, section }) => {
    render(<WorkflowProgress stage={stage} />);

    expect(screen.getByText(`Progress ${stage}/3: ${section}`)).toBeInTheDocument();
    expect(screen.getByText('Done!')).toBeInTheDocument();
    expect(screen.queryByText('Prepare share')).not.toBeInTheDocument();
    expect(screen.queryByText('Submitted')).not.toBeInTheDocument();
    expect(screen.getByRole('progressbar', { name: 'Overall workflow progress' })).toHaveAttribute(
      'aria-valuenow',
      percent,
    );
    expect(screen.getByRole('progressbar', { name: 'Overall workflow progress' })).toHaveAttribute(
      'aria-valuetext',
      text,
    );
  });

  it('advances only a receipt-backed Done route to the submitted stage', () => {
    expect(workflowProgressStageFor('/', '', null)).toBe(1);
    expect(workflowProgressStageFor('/share', '', null)).toBe(2);
    expect(workflowProgressStageFor('/share', '?step=done&share=local-only', null)).toBe(2);
    expect(workflowProgressStageFor('/share', '?step=done&share=submitted', 'submitted')).toBe(3);
    expect(workflowProgressStageFor('/share', '?step=done&share=other', 'submitted')).toBe(2);
  });
});
