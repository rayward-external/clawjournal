type BadgeKind = 'outcome' | 'value' | 'risk' | 'status' | 'failure_mode' | 'meta_label' | 'recovery' | 'attribution';

const COLORS: Record<BadgeKind, Record<string, { bg: string; fg: string }>> = {
  outcome: {
    // New resolution labels
    resolved: { bg: '#dcfce7', fg: '#166534' },
    partial: { bg: '#fef3c7', fg: '#92400e' },
    failed: { bg: '#fee2e2', fg: '#991b1b' },
    abandoned: { bg: '#f3f4f6', fg: '#6b7280' },
    exploratory: { bg: '#e0f2fe', fg: '#075985' },
    trivial: { bg: '#f3f4f6', fg: '#6b7280' },
    // Legacy outcome labels (backward compat)
    tests_passed: { bg: '#dcfce7', fg: '#166534' },
    tests_failed: { bg: '#fee2e2', fg: '#991b1b' },
    build_failed: { bg: '#fee2e2', fg: '#991b1b' },
    analysis_only: { bg: '#e0f2fe', fg: '#075985' },
    completed: { bg: '#dcfce7', fg: '#166534' },
    errored: { bg: '#fee2e2', fg: '#991b1b' },
    unknown: { bg: '#f3f4f6', fg: '#6b7280' },
  },
  value: {
    // Session tags (organizational)
    multi_file: { bg: '#faf5ff', fg: '#7e22ce' },
    single_file: { bg: '#f3f4f6', fg: '#6b7280' },
    cross_project: { bg: '#faf5ff', fg: '#7e22ce' },
    infrastructure: { bg: '#fff7ed', fg: '#c2410c' },
    quick_fix: { bg: '#f0fdf4', fg: '#15803d' },
    deep_dive: { bg: '#eff6ff', fg: '#1d4ed8' },
    marathon_session: { bg: '#fff7ed', fg: '#c2410c' },
    debugging_cycle: { bg: '#fefce8', fg: '#a16207' },
    greenfield: { bg: '#f0fdf4', fg: '#15803d' },
    legacy_code: { bg: '#f3f4f6', fg: '#6b7280' },
    incident_response: { bg: '#fee2e2', fg: '#991b1b' },
    pair_programming: { bg: '#eff6ff', fg: '#1d4ed8' },
    // Legacy value labels (backward compat)
    novel_domain: { bg: '#faf5ff', fg: '#7e22ce' },
    long_horizon: { bg: '#fff7ed', fg: '#c2410c' },
    tool_rich: { bg: '#f0fdf4', fg: '#15803d' },
    scientific_workflow: { bg: '#eff6ff', fg: '#1d4ed8' },
    debugging: { bg: '#fefce8', fg: '#a16207' },
  },
  risk: {
    secrets_detected: { bg: '#fee2e2', fg: '#991b1b' },
    names_detected: { bg: '#fef3c7', fg: '#92400e' },
    private_url: { bg: '#fef3c7', fg: '#92400e' },
    pii_detected: { bg: '#fee2e2', fg: '#991b1b' },
    manual_review: { bg: '#fce7f3', fg: '#9d174d' },
  },
  status: {
    new: { bg: '#e0f2fe', fg: '#075985' },
    draft: { bg: '#f3f4f6', fg: '#6b7280' },
    shortlisted: { bg: '#f3f4f6', fg: '#6b7280' },
    approved: { bg: '#dcfce7', fg: '#166534' },
    blocked: { bg: '#f3f4f6', fg: '#6b7280' },
    exported: { bg: '#e0f2fe', fg: '#075985' },
    shared: { bg: '#f3e8ff', fg: '#7e22ce' },
    uploaded: { bg: '#f3e8ff', fg: '#7e22ce' },
  },
  failure_mode: {},
  meta_label: {
    evaluation_measurement: { bg: '#f3e8ff', fg: '#7e22ce' },
  },
  recovery: {
    self_recovered: { bg: '#dcfce7', fg: '#166534' },
    user_corrected_recovery: { bg: '#e0f2fe', fg: '#075985' },
    unrecovered: { bg: '#fee2e2', fg: '#991b1b' },
    blocked: { bg: '#f3f4f6', fg: '#6b7280' },
  },
  attribution: {
    agent_caused: { bg: '#fee2e2', fg: '#991b1b' },
    environment: { bg: '#f3f4f6', fg: '#6b7280' },
    preexisting_problem: { bg: '#fef3c7', fg: '#92400e' },
    user_redirect: { bg: '#e0f2fe', fg: '#075985' },
    unclear: { bg: '#f3f4f6', fg: '#6b7280' },
  },
};

export const LABELS: Record<string, string> = {
  // Normalized outcome labels (what the dashboard shows)
  resolved: 'Resolved',
  partial: 'Partial',
  interrupted: 'Interrupted',
  failed: 'Failed',
  abandoned: 'Abandoned',
  exploratory: 'Exploratory',
  inconclusive: 'Inconclusive',
  trivial: 'Trivial',
  unscored: 'Unscored',
  unknown: 'Unknown',
  // Raw badges kept so session-detail views still render correctly when
  // they show the source-of-truth label instead of the normalized one.
  tests_passed: 'Tests Passed',
  tests_failed: 'Tests Failed',
  build_failed: 'Build Failed',
  analysis_only: 'Analysis',
  completed: 'Completed',
  errored: 'Errored',
  // Session tags
  multi_file: 'Multi-File',
  single_file: 'Single File',
  cross_project: 'Cross-Project',
  infrastructure: 'Infrastructure',
  quick_fix: 'Quick Fix',
  deep_dive: 'Deep Dive',
  marathon_session: 'Marathon',
  iterative: 'Iterative',
  frontend: 'Frontend',
  backend: 'Backend',
  devops: 'DevOps',
  database: 'Database',
  api: 'API',
  docs: 'Docs',
  debugging_cycle: 'Debugging Cycle',
  greenfield: 'Greenfield',
  legacy_code: 'Legacy Code',
  dependency_upgrade: 'Dep Upgrade',
  incident_response: 'Incident',
  pair_programming: 'Pair Programming',
  delegation: 'Delegation',
  // Legacy value labels
  novel_domain: 'Novel Domain',
  long_horizon: 'Long Horizon',
  tool_rich: 'Tool Rich',
  scientific_workflow: 'Scientific',
  debugging: 'Debugging',
  // Privacy flags
  secrets_detected: 'Secrets',
  names_detected: 'Names',
  private_url: 'Private URL',
  pii_detected: 'PII',
  manual_review: 'Review Needed',
  // Status labels
  new: 'New',
  shortlisted: 'To Review',
  approved: 'Approved',
  blocked: 'Skipped',
  draft: 'Draft',
  exported: 'Exported',
  shared: 'Shared',
  uploaded: 'Uploaded',
  // Task types
  feature: 'Feature',
  refactor: 'Refactor',
  analysis: 'Analysis',
  testing: 'Testing',
  documentation: 'Docs',
  exploration: 'Exploration',
  review: 'Review',
  configuration: 'Config',
  migration: 'Migration',
  planning: 'Planning',
  incident: 'Incident',
  learning: 'Learning',
  // Failure modes (categories 1-12)
  task_framing: 'Task framing',
  method_selection: 'Method selection',
  context_handling: 'Context handling',
  execution_error: 'Execution error',
  reasoning_fabrication: 'Reasoning / fabrication',
  revision_failure: 'Revision failure',
  verification_skipped: 'Verification skipped',
  deliverable_defect: 'Deliverable defect',
  communication_error: 'Communication error',
  collaboration_error: 'Collaboration error',
  safety_security: 'Safety / security',
  efficiency_waste: 'Efficiency / waste',
  // Meta labels (category 13)
  evaluation_measurement: 'Evaluation / measurement',
  // Recovery
  self_recovered: 'Self-recovered',
  user_corrected_recovery: 'User-corrected',
  unrecovered: 'Unrecovered',
  // Attribution
  agent_caused: 'Agent-caused',
  environment: 'Environment',
  preexisting_problem: 'Preexisting',
  user_redirect: 'User redirect',
  unclear: 'Unclear',
};

const DESCRIPTIONS: Record<string, string> = {
  // Resolution
  resolved: 'Task completed successfully',
  partial: 'Some progress but not fully completed',
  failed: 'Task attempted but did not succeed',
  abandoned: 'User gave up or redirected',
  exploratory: 'Information-gathering, no specific task',
  trivial: 'No real task (greeting, warmup, slash command)',
  // Legacy outcomes
  tests_passed: 'Session ended with passing tests',
  tests_failed: 'Session ended with failing tests',
  build_failed: 'Session ended with a build failure',
  analysis_only: 'Analysis without code changes',
  completed: 'Session completed without detected errors',
  errored: 'Session ended with errors in tool outputs',
  // Session tags
  multi_file: 'Work across multiple files',
  deep_dive: 'Extended, thorough investigation',
  debugging_cycle: 'Iterative debugging pattern',
  greenfield: 'New code from scratch',
  incident_response: 'Responding to a production issue',
  // Privacy flags
  secrets_detected: 'Potential secrets found in content',
  names_detected: 'Personal names detected in content',
  private_url: 'Private or internal URLs detected',
  pii_detected: 'Personally identifiable information detected',
  manual_review: 'Flagged for manual review',
  // Task types
  feature: 'New feature implementation',
  refactor: 'Code refactoring session',
  analysis: 'Code analysis or investigation',
  testing: 'Writing or fixing tests',
  documentation: 'Documentation work',
  exploration: 'Codebase exploration',
  review: 'Code review or validation session',
  configuration: 'Setup or configuration task',
  migration: 'Migration or upgrade task',
  planning: 'Designing an approach without implementing',
  incident: 'Responding to a production issue',
  learning: 'Understanding something new',
  // Failure modes
  task_framing: 'Misread the goal, scope, deliverable, or constraints',
  method_selection: 'Wrong approach, tool, model, or order of steps',
  context_handling: 'Failed to gather, read, or preserve needed context',
  execution_error: 'Mechanical failure: bad tool call, code bug, env issue',
  reasoning_fabrication: 'Hallucinated APIs/files, unsupported inference, wrong math',
  revision_failure: "Had the signal to course-correct but didn't revise",
  verification_skipped: 'Declared done without testing or sanity-checking',
  deliverable_defect: 'Artifact missing, partial, malformed, or unauditable',
  communication_error: 'Misleading summary, missing caveats, over/underclaiming',
  collaboration_error: 'Mishandled human loop: when to ask, when to pause',
  safety_security: 'Insecure code, destructive action, privacy leak, policy issue',
  efficiency_waste: 'Disproportionate cost relative to value produced',
  evaluation_measurement: 'Evaluation/measurement setup misjudges the agent',
  // Recovery
  self_recovered: 'Agent detected and fixed the mistake itself',
  user_corrected_recovery: 'User supplied a correction; agent recovered',
  unrecovered: 'Meaningful failure remains unresolved at the end',
  // Attribution
  agent_caused: 'Failure caused by the agent',
  environment: 'Failure caused by tools, deps, or environment',
  preexisting_problem: 'Pre-existing issue in the codebase or task',
  user_redirect: 'User redirected away from the original task',
  unclear: 'Failure happened but cause cannot be assigned',
};

/** Deterministic color from a string hash — ensures unknown LLM-generated badges get distinct colors. */
const HASH_PALETTE = [
  { bg: '#faf5ff', fg: '#7e22ce' },  // purple
  { bg: '#fff7ed', fg: '#c2410c' },  // orange
  { bg: '#f0fdf4', fg: '#15803d' },  // green
  { bg: '#eff6ff', fg: '#1d4ed8' },  // blue
  { bg: '#fefce8', fg: '#a16207' },  // yellow
  { bg: '#fdf2f8', fg: '#9d174d' },  // pink
  { bg: '#f0fdfa', fg: '#0f766e' },  // teal
  { bg: '#fef2f2', fg: '#b91c1c' },  // red
];

function hashColor(value: string): { bg: string; fg: string } {
  let hash = 0;
  for (let i = 0; i < value.length; i++) {
    hash = value.charCodeAt(i) + ((hash << 5) - hash);
  }
  return HASH_PALETTE[Math.abs(hash) % HASH_PALETTE.length];
}

function titleCase(s: string): string {
  return s.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

export function BadgeChip({ kind, value }: { kind: BadgeKind; value: string }) {
  const palette = COLORS[kind]?.[value] ?? hashColor(value);
  const label = LABELS[value] ?? titleCase(value);

  return (
    <span
      title={DESCRIPTIONS[value]}
      style={{
        display: 'inline-block',
        padding: '1px 8px',
        borderRadius: '9999px',
        fontSize: '12px',
        fontWeight: 500,
        lineHeight: '20px',
        background: palette.bg,
        color: palette.fg,
        whiteSpace: 'nowrap',
      }}
    >
      {label}
    </span>
  );
}
