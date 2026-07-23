export type WorkflowProgressStage = 1 | 2 | 3;

export function workflowProgressStageFor(
  pathname: string,
  search: string,
  submittedShareId: string | null,
): WorkflowProgressStage {
  const inShare = pathname === '/share' || pathname.startsWith('/share/');
  if (!inShare) return 1;

  const params = new URLSearchParams(search);
  return params.get('step') === 'done'
    && params.get('share') !== null
    && params.get('share') === submittedShareId
    ? 3
    : 2;
}
