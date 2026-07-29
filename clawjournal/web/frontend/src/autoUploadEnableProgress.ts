import { api } from './api.ts';
import type { AutoUploadEnableProgress } from './types.ts';

export const AUTO_UPLOAD_ENABLE_PROGRESS_POLL_MS = 300;

export function createAutoUploadEnableProgressId(): string {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }
  return `progress-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export function startAutoUploadEnableProgressPolling(
  progressId: string,
  onProgress: (progress: AutoUploadEnableProgress) => void,
): () => void {
  let active = true;
  onProgress({
    progress_id: progressId,
    stage: 'checking_hosted_service',
    message: 'Checking the hosted service and current terms...',
    source: null,
    current_project: null,
    total_projects: null,
    updated_at: null,
  });

  const timer = window.setInterval(() => {
    void api.autoUpload.enableProgress(progressId)
      .then(progress => {
        if (active) onProgress(progress);
      })
      .catch(() => {
        // The POST and first poll can race. Keep the last known stage and try
        // again rather than turning a transient local 404 into a user error.
      });
  }, AUTO_UPLOAD_ENABLE_PROGRESS_POLL_MS);

  return () => {
    active = false;
    window.clearInterval(timer);
  };
}
