import { colors, fontFamily } from '../theme.ts';
import type { WorkflowProgressStage } from './workflowProgressStage.ts';

const WORKFLOW_STEPS = [
  { stage: 1, label: 'Review sessions' },
  { stage: 2, label: 'Prepare share' },
  { stage: 3, label: 'Submitted' },
] as const;

export function WorkflowProgress({ stage }: { stage: WorkflowProgressStage }) {
  const percent = Math.round((stage / WORKFLOW_STEPS.length) * 100);
  const currentLabel = WORKFLOW_STEPS[stage - 1].label;
  const sectionLabel = stage === 1 ? 'Sessions' : 'Share';

  return (
    <footer
      aria-label="ClawJournal workflow progress"
      style={{
        flexShrink: 0,
        padding: '5px 18px 6px',
        background: colors.gray50,
        borderTop: `1px solid ${colors.gray200}`,
        fontFamily,
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16, marginBottom: 4 }}>
        <span style={{ fontSize: 11, fontWeight: 700, color: colors.gray700 }}>
          Progress {stage}/3: {sectionLabel}
        </span>
        <span style={{
          fontSize: 11,
          fontWeight: stage === 3 ? 700 : 500,
          color: stage === 3 ? colors.green700 : colors.gray400,
        }}>
          Done!
        </span>
      </div>
      <div
        role="progressbar"
        aria-label="Overall workflow progress"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
        aria-valuetext={`Step ${stage} of 3: ${currentLabel}`}
        style={{
          height: 4,
          overflow: 'hidden',
          borderRadius: 999,
          background: colors.gray200,
        }}
      >
        <div style={{
          width: `${percent}%`,
          height: '100%',
          borderRadius: 999,
          background: stage === 3 ? colors.green500 : colors.primary500,
          transition: 'width 280ms ease, background-color 280ms ease',
        }} />
      </div>
    </footer>
  );
}
