import type {
  Session,
  SessionDetail,
  Share,
  SharePreview,
  Policy,
  Stats,
  ProjectSummary,
  DashboardData,
  HighlightsData,
  RedactionReport,
  AllowlistEntry,
  InsightsData,
  AdvisorData,
  Finding,
  FindingEntityGroup,
  FindingStatus,
  FindingsAllowlistEntry,
  HoldHistoryEntry,
  HoldState,
  Benchmark,
  BenchmarkSummary,
  BenchmarkTrend,
  Features,
  WorkbenchConfig,
  AutoUploadAgent,
  AutoUploadCandidateReport,
  AutoUploadHookDiagnostic,
  AutoUploadStatus,
} from './types.ts';

const BASE = '/api';

export class ApiError extends Error {
  status: number;
  body: Record<string, unknown>;
  constructor(status: number, message: string, body: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

// The server-side AI review is capped at 180 seconds, and its agent runner gets
// another 10 seconds to terminate and collect output. Leave a further 10
// seconds for the daemon to serialize the report and the browser to receive it.
// A wedged daemon or dropped response still gets a finite browser deadline.
export const REDACTION_REPORT_TIMEOUT_MS = 200_000;

declare global {
  interface Window {
    __CLAWJOURNAL_API_TOKEN__?: string;
  }
}

function authHeader(): Record<string, string> {
  // The daemon injects the per-install API token into index.html at
  // serve time; same-origin fetches pick it up here. No token → no
  // header → 401 (expected on non-daemon-hosted dev setups).
  const token = typeof window !== 'undefined' ? window.__CLAWJOURNAL_API_TOKEN__ : '';
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { ...authHeader() };
  if (init?.headers) {
    const extra = init.headers as Record<string, string>;
    for (const key of Object.keys(extra)) {
      headers[key] = extra[key];
    }
  }
  const res = await fetch(`${BASE}${path}`, { ...init, headers });
  if (res.status === 204) {
    return undefined as T;
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body.error || `HTTP ${res.status}`, body);
  }
  return res.json();
}

function qs(params: Record<string, string | number | string[] | number[] | null | undefined>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (Array.isArray(v)) {
      v.forEach(item => {
        if (item != null && item !== '') p.append(k, String(item));
      });
    } else if (v != null && v !== '') {
      p.set(k, String(v));
    }
  }
  const s = p.toString();
  return s ? `?${s}` : '';
}

function normalizeAutoUploadScopeEntries(value: unknown): Array<[string, string]> {
  if (!Array.isArray(value)) return [];
  return value.flatMap(item => {
    if (
      !Array.isArray(item)
      || item.length !== 2
      || typeof item[0] !== 'string'
      || !item[0].trim()
      || typeof item[1] !== 'string'
      || !item[1].trim()
    ) {
      return [];
    }
    return [[item[0].trim(), item[1].trim()] as [string, string]];
  });
}

function normalizeAutoUploadStatus(raw: Partial<AutoUploadStatus>): AutoUploadStatus {
  const scope = raw.scope ?? { sources: [], projects: [], entries: [] };
  const ai = raw.ai ?? { enabled: false, backend: null };
  const authorization = raw.authorization ?? { version: null, text: null };
  const retention = raw.retention ?? { version: null, text: null };
  const eligibility = raw.eligibility ?? {
    selected_count: 0,
    eligible_count: 0,
    exclusion_counts: {},
  };
  return {
    mode: raw.mode === 'enabled' || raw.mode === 'paused' ? raw.mode : 'off',
    health: raw.health === 'action_required' || raw.health === 'retrying'
      ? raw.health
      : 'ready',
    run_now_allowed: raw.run_now_allowed === true,
    overlay: raw.overlay === 'running' || raw.overlay === 'revocation_pending'
      ? raw.overlay
      : null,
    pending_submission_state: raw.pending_submission_state === 'sealed'
      || raw.pending_submission_state === 'submitting'
      ? raw.pending_submission_state
      : null,
    ui_visible: raw.ui_visible === true,
    offer_available: raw.offer_available === true,
    enrollment_grant_available: raw.enrollment_grant_available === true,
    scope: {
      sources: Array.isArray(scope.sources) ? scope.sources : [],
      projects: Array.isArray(scope.projects) ? scope.projects : [],
      entries: normalizeAutoUploadScopeEntries(scope.entries),
    },
    cap: typeof raw.cap === 'number' ? raw.cap : 5,
    cadence_days: typeof raw.cadence_days === 'number' ? raw.cadence_days : 1,
    ai: {
      enabled: ai.enabled === true,
      backend: typeof ai.backend === 'string' ? ai.backend : null,
    },
    authorization: {
      version: typeof authorization.version === 'string' ? authorization.version : null,
      text: typeof authorization.text === 'string' ? authorization.text : null,
    },
    retention: {
      version: typeof retention.version === 'string' ? retention.version : null,
      text: typeof retention.text === 'string' ? retention.text : null,
    },
    enrolled_at: typeof raw.enrolled_at === 'string' ? raw.enrolled_at : null,
    next_due_at: typeof raw.next_due_at === 'string' ? raw.next_due_at : null,
    next_retry_at: typeof raw.next_retry_at === 'string' ? raw.next_retry_at : null,
    hooks: normalizeAutoUploadHooks(raw.hooks),
    eligibility: {
      selected_count: typeof eligibility.selected_count === 'number'
        ? eligibility.selected_count
        : 0,
      eligible_count: typeof eligibility.eligible_count === 'number'
        ? eligibility.eligible_count
        : 0,
      exclusion_counts: eligibility.exclusion_counts ?? {},
    },
    last_result: raw.last_result ?? null,
  };
}

function normalizeAutoUploadHooks(value: unknown): AutoUploadHookDiagnostic[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap(item => {
    if (!item || typeof item !== 'object') return [];
    const hook = item as Record<string, unknown>;
    if (typeof hook.agent !== 'string' || !hook.agent.trim()) return [];
    return [{
      agent: hook.agent,
      selected: hook.selected === true,
      configured: hook.configured === true,
      installed: hook.installed === true,
      last_observed_at: typeof hook.last_observed_at === 'string'
        ? hook.last_observed_at
        : null,
      ...(typeof hook.diagnostic === 'string' ? { diagnostic: hook.diagnostic } : {}),
    }];
  });
}

function normalizeAutoUploadPreview(
  raw: Partial<AutoUploadCandidateReport>,
): AutoUploadCandidateReport {
  return {
    eligible: Array.isArray(raw.eligible) ? raw.eligible : [],
    selected: Array.isArray(raw.selected) ? raw.selected : [],
    eligible_count: typeof raw.eligible_count === 'number' ? raw.eligible_count : 0,
    selected_count: typeof raw.selected_count === 'number' ? raw.selected_count : 0,
    deferred_by_cap: typeof raw.deferred_by_cap === 'number' ? raw.deferred_by_cap : 0,
    exclusion_counts: raw.exclusion_counts ?? {},
    exclusions: Array.isArray(raw.exclusions) ? raw.exclusions : [],
    scope_blockers: Array.isArray(raw.scope_blockers) ? raw.scope_blockers : [],
    limit: typeof raw.limit === 'number' ? raw.limit : 5,
  };
}

export const api = {
  sessions: {
    list(params: {
      status?: string | string[] | null;
      source?: string | null;
      project?: string | null;
      task_type?: string | null;
      recovery_label?: string | null;
      failure_attribution?: string | null;
      failure_mode?: string | null;
      q?: string | null;
      sort?: string;
      order?: string;
      limit?: number;
      offset?: number;
    } = {}): Promise<Session[]> {
      return request(`/sessions${qs(params)}`);
    },

    get(id: string): Promise<SessionDetail> {
      return request(`/sessions/${encodeURIComponent(id)}`);
    },

    redacted(id: string): Promise<SessionDetail> {
      return request(`/sessions/${encodeURIComponent(id)}/redacted`);
    },

    async redactionReport(id: string, opts?: { aiPii?: boolean; signal?: AbortSignal; timeoutMs?: number }): Promise<RedactionReport> {
      const q = opts?.aiPii ? '?ai_pii=1' : '';
      const controller = new AbortController();
      let timedOut = false;
      const abortFromParent = () => controller.abort(opts?.signal?.reason);
      if (opts?.signal?.aborted) abortFromParent();
      else opts?.signal?.addEventListener('abort', abortFromParent, { once: true });
      const timeout = globalThis.setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, opts?.timeoutMs ?? REDACTION_REPORT_TIMEOUT_MS);
      try {
        return await request(`/sessions/${encodeURIComponent(id)}/redaction-report${q}`, {
          signal: controller.signal,
        });
      } catch (error) {
        if (timedOut) throw new ApiError(408, 'Redaction report timed out');
        throw error;
      } finally {
        globalThis.clearTimeout(timeout);
        opts?.signal?.removeEventListener('abort', abortFromParent);
      }
    },

    update(id: string, body: { status?: string; notes?: string; reason?: string; ai_quality_score?: number; ai_score_reason?: string; ai_failure_value_score?: number; ai_failure_evidence?: string[]; ai_recovery_labels?: string[]; ai_failure_attribution?: string; ai_failure_modes?: string[]; ai_learning_summary?: string; hold_state?: HoldState; embargo_until?: string | null }): Promise<{ ok: boolean }> {
      return request(`/sessions/${encodeURIComponent(id)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    },

    findings(id: string, opts: { groupBy?: 'entity'; status?: FindingStatus } = {}): Promise<{ total: number; entities?: FindingEntityGroup[]; findings?: Finding[] }> {
      const params: Record<string, string> = {};
      if (opts.groupBy) params.group_by = opts.groupBy;
      if (opts.status) params.status = opts.status;
      return request(`/sessions/${encodeURIComponent(id)}/findings${qs(params)}`);
    },

    holdHistory(id: string): Promise<{ total: number; history: HoldHistoryEntry[] }> {
      return request(`/sessions/${encodeURIComponent(id)}/hold-history`);
    },

    forceScan(id: string): Promise<{ status: string; revision?: string; count?: number }> {
      return request(`/sessions/${encodeURIComponent(id)}/scan`, { method: 'POST' });
    },

    score(id: string, body?: { backend?: string; model?: string }): Promise<{
      ok: boolean;
      ai_quality_score?: number;
      ai_failure_value_score?: number;
      ai_recovery_labels?: string[];
      ai_failure_attribution?: string;
      ai_failure_modes?: string[];
      ai_learning_summary?: string;
      reason?: string;
      task_type?: string;
      outcome?: string;
      summary?: string;
    }> {
      return request(`/sessions/${encodeURIComponent(id)}/score`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body ?? {}),
      });
    },
  },

  search(q: string, limit = 50, offset = 0): Promise<Session[]> {
    return request(`/search${qs({ q, limit, offset })}`);
  },

  stats(params: { start?: string; end?: string } = {}): Promise<Stats> {
    return request(`/stats${qs(params)}`);
  },

  features(): Promise<Features> {
    return request('/features');
  },

  config: {
    get(): Promise<WorkbenchConfig> {
      return request('/config');
    },
    update(body: Partial<{
      source: string;
      scorer_backend: string;
      confirm_projects: boolean;
      ai_pii_review_enabled: boolean;
      benchmark_tab_enabled: boolean;
      scoring_warmup_declined: boolean;
    }>): Promise<WorkbenchConfig> {
      return request('/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    },
  },

  autoUpload: {
    async status(): Promise<AutoUploadStatus> {
      const status = await request<Partial<AutoUploadStatus>>('/auto-upload/status');
      return normalizeAutoUploadStatus(status);
    },

    async preview(opts?: { refresh?: boolean }): Promise<AutoUploadCandidateReport> {
      const report = await request<Partial<AutoUploadCandidateReport>>(
        '/auto-upload/preview',
        opts?.refresh
          ? {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ refresh: true }),
            }
          : undefined,
      );
      return normalizeAutoUploadPreview(report);
    },

    async enable(body: {
      agent: AutoUploadAgent;
      accepted_authorization_version?: string;
      accepted_retention_version?: string;
      accepted_ownership_certification_version?: string;
      accepted_authorization_profile_hash?: string;
      challenge_only?: boolean;
    }): Promise<AutoUploadStatus> {
      const status = await request<Partial<AutoUploadStatus>>('/auto-upload/enable', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      return normalizeAutoUploadStatus(status);
    },

    async run(): Promise<AutoUploadStatus> {
      const status = await request<Partial<AutoUploadStatus>>('/auto-upload/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
      return normalizeAutoUploadStatus(status);
    },

    async pause(): Promise<AutoUploadStatus> {
      const status = await request<Partial<AutoUploadStatus>>('/auto-upload/pause', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
      return normalizeAutoUploadStatus(status);
    },

    async resume(): Promise<AutoUploadStatus> {
      const status = await request<Partial<AutoUploadStatus>>('/auto-upload/resume', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
      return normalizeAutoUploadStatus(status);
    },

    async disable(): Promise<AutoUploadStatus> {
      const status = await request<Partial<AutoUploadStatus>>('/auto-upload/disable', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
      return normalizeAutoUploadStatus(status);
    },
  },

  dashboard(params: { start?: string; end?: string } = {}): Promise<DashboardData> {
    return request(`/dashboard${qs(params)}`);
  },

  highlights(params: { days?: number; top?: number; min_quality?: number; min_failure_value?: number } = {}): Promise<HighlightsData> {
    return request(`/dashboard/highlights${qs(params)}`);
  },

  projects(): Promise<ProjectSummary[]> {
    return request('/projects');
  },

  shareReady(opts?: { includeUnapproved?: boolean }): Promise<{ count: number; total_approved: number; projects: string[]; models: string[]; recommended_session_ids: string[]; sessions: Array<{ session_id: string; project: string; model: string | null; source: string; display_title: string; ai_quality_score: number | null; ai_failure_value_score: number | null; ai_recovery_labels: string[]; ai_failure_attribution: string | null; ai_failure_modes: string[]; ai_learning_summary: string | null; user_messages: number; assistant_messages: number; tool_uses: number; input_tokens: number; output_tokens: number; outcome_badge: string | null; client_origin: string | null; runtime_channel: string | null; start_time: string | null; review_status?: string; revision_hash?: string | null; last_shared_revision_hash?: string | null; updated_since_last_share?: boolean }> }> {
    const q = opts?.includeUnapproved ? '?include_unapproved=1' : '';
    return request(`/share-ready${q}`);
  },

  shareDestination(): Promise<{
    configured: boolean;
    daemon_upload_supported: boolean;
    submissions_open: boolean;
    preferred_upload_flow: string;
    cli_ingest_supported: boolean;
    share_page_url: string | null;
    submit_page_url?: string | null;
    maximum_bundle_size?: number | null;
    accepted_manifest_schema_versions?: string[];
    supported_institution_email_policy?: {
      domain_suffixes?: string[];
      explicit_collaborators_supported?: boolean;
    } | null;
    support_contact?: string | null;
    message?: string;
  }> {
    return request('/share-destination');
  },

  share: {
    consent(): Promise<{
      consent_text: string;
      retention_text: string;
      consent_version: string;
      retention_policy_version: string;
      support_contact?: string;
      [key: string]: unknown;
    }> {
      return request('/share/consent');
    },

    verifyEmail(email: string): Promise<{ ok: boolean; email: string; expires_at?: string; dev_code?: string }> {
      return request('/share/verify-email', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email }),
      });
    },

    verifyConfirm(code: string): Promise<{ verified: boolean; verified_email: string; expires_at?: string | number }> {
      return request('/share/verify-confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code }),
      });
    },

    uploadStatus(): Promise<{
      verified_email: string | null;
      token_valid: boolean;
      expires_at: string | number | null;
      pending_email: string | null;
    }> {
      return request('/share/upload-status');
    },

    installScanners(): Promise<{
      ok: boolean;
      missing: string[];
      scanners: Record<string, {
        ok: boolean;
        status: string;
        install_attempted: boolean;
        available: boolean;
        managed: boolean;
      }>;
    }> {
      return request('/share/scanners/install', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
    },
  },

  quickShare(sessionIds: string[], note?: string, opts?: { aiPii?: boolean }): Promise<{
    ok: boolean; share_id: string; next_step: 'submit';
    export_path: string; session_count: number; zip_size_bytes?: number | null;
    redaction_summary: { total_redactions: number; by_type: Record<string, number> };
  }> {
    return request('/quick-share', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_ids: sessionIds, note, ai_pii: opts?.aiPii }),
    });
  },

  scoringBackend(): Promise<{ backend: string | null; display_name: string | null; confirmed?: boolean; needs_confirmation?: boolean }> {
    return request('/scoring/backend');
  },

  scoringWarmup(body?: { confirm_backend?: boolean; backend?: string | null; decline?: boolean }): Promise<{
    status: 'started' | 'already_running' | 'needs_confirmation' | 'disabled' | 'declined';
    backend?: string | null;
    display_name?: string | null;
    reason?: string;
    limit?: number;
  }> {
    return request('/scoring/warmup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body ?? {}),
    });
  },

  shares: {
    list(): Promise<Share[]> {
      return request('/shares');
    },

    get(id: string): Promise<Share> {
      return request(`/shares/${encodeURIComponent(id)}`);
    },

    create(
      sessionIds: string[],
      note?: string,
      attestation?: string,
      expectedRevisions?: Record<string, string>,
    ): Promise<{ share_id: string }> {
      return request('/shares', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_ids: sessionIds,
          note,
          attestation,
          expected_revisions: expectedRevisions,
        }),
      });
    },

    export(id: string, outputPath?: string): Promise<{ ok: boolean; export_path: string; session_count: number }> {
      return request(`/shares/${encodeURIComponent(id)}/export`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ output_path: outputPath }),
      });
    },

    seal(id: string, opts?: { aiPii?: boolean }): Promise<{
      ok: boolean;
      export_path: string;
      session_count: number;
      zip_size_bytes?: number | null;
      redaction_summary: { total_redactions: number; by_type: Record<string, number> };
    }> {
      return request(`/shares/${encodeURIComponent(id)}/seal`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ai_pii: opts?.aiPii }),
      });
    },

    preview(id: string): Promise<SharePreview> {
      return request(`/shares/${encodeURIComponent(id)}/preview`);
    },

    downloadUrl(id: string): string {
      return `${BASE}/shares/${encodeURIComponent(id)}/download`;
    },

    async download(id: string, opts?: { aiPii?: boolean }): Promise<void> {
      // `window.open` can't attach the Bearer auth header the daemon
      // requires, so fetch the zip through the same auth'd path as every
      // other API call and hand the browser a blob URL to save.
      const q = opts && typeof opts.aiPii === 'boolean' ? qs({ ai_pii: opts.aiPii ? 1 : 0 }) : '';
      const res = await fetch(`${BASE}/shares/${encodeURIComponent(id)}/download${q}`, {
        headers: authHeader(),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new ApiError(res.status, body.error || `HTTP ${res.status}`, body);
      }
      const disposition = res.headers.get('Content-Disposition') || '';
      // Prefer RFC 5987 `filename*=charset'lang'percent-encoded` when present,
      // else plain `filename="..."`. Fall back to a name that still carries the
      // .zip extension so the OS can open the saved file.
      const extMatch = /filename\*=[^']*'[^']*'([^";]+)/i.exec(disposition);
      const plainMatch = /filename="?([^";]+)"?/i.exec(disposition);
      let filename = plainMatch ? plainMatch[1] : `clawjournal-share-${id}.zip`;
      if (extMatch) {
        // Malformed percent-encoding must not abort an otherwise valid
        // download — fall back to the plain form / default instead of throwing.
        try { filename = decodeURIComponent(extMatch[1]); } catch { /* keep fallback */ }
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      a.rel = 'noopener';
      document.body.appendChild(a);
      a.click();
      a.remove();
      // Revoke on a later tick, not synchronously. Chromium processes a blob
      // download asynchronously after click(); revoking the object URL right
      // away drops the suggested filename (the browser then saves a bare UUID
      // with no .zip extension) and can truncate large downloads.
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    },

    upload(id: string, body: {
      accept_terms: boolean;
      ownership_certification: boolean;
      consent_version: string;
      retention_policy_version: string;
      ai_pii?: boolean;
    }): Promise<{
      ok: boolean; shared_at: string; receipt_id: string; hosted_status?: string | null;
      session_count: number; bundle_hash: string;
      zip_size_bytes?: number;
      redaction_summary: { total_redactions: number; by_type: Record<string, number> };
    }> {
      return request(`/shares/${encodeURIComponent(id)}/upload`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    },
  },

  policies: {
    list(): Promise<Policy[]> {
      return request('/policies');
    },

    add(policyType: string, value: string, reason?: string): Promise<{ policy_id: string }> {
      return request('/policies', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ policy_type: policyType, value, reason }),
      });
    },

    remove(id: string): Promise<{ ok: boolean }> {
      return request(`/policies/${encodeURIComponent(id)}`, { method: 'DELETE' });
    },
  },

  allowlist: {
    list(): Promise<AllowlistEntry[]> {
      return request('/allowlist');
    },

    add(entry: { type: string; text?: string; regex?: string; match_type?: string; reason?: string }): Promise<{ ok: boolean; entry: AllowlistEntry }> {
      return request('/allowlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(entry),
      });
    },

    remove(id: string): Promise<{ ok: boolean }> {
      return request(`/allowlist/${encodeURIComponent(id)}`, { method: 'DELETE' });
    },
  },

  insights(params: { start?: string; end?: string } = {}): Promise<InsightsData> {
    return request(`/insights${qs(params)}`);
  },

  advisor(params: { days?: number } = {}): Promise<AdvisorData> {
    return request(`/advisor${qs(params)}`);
  },

  desktopOpened(): Promise<{ ok: boolean; scheduled: boolean }> {
    return request('/desktop/opened', { method: 'POST' });
  },

  scan(opts: { force?: boolean } = {}): Promise<{ ok: boolean; status?: 'already_running'; new_sessions: Record<string, number>; updated_sessions?: Record<string, number>; unchanged_sessions?: Record<string, number>; force_rescan?: { processed: number; errored: { session_id: string; error: string }[] } }> {
    const path = opts.force ? '/scan?force=true' : '/scan';
    return request(path, { method: 'POST' });
  },

  findings: {
    patch(findingIds: string[], status: 'accepted' | 'ignored', opts: { reason?: string; global?: boolean } = {}): Promise<{ updated: number; allowlisted: boolean }> {
      return request('/findings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          finding_ids: findingIds,
          status,
          reason: opts.reason,
          global: opts.global,
        }),
      });
    },

    allowlist: {
      list(): Promise<{ total: number; entries: FindingsAllowlistEntry[] }> {
        return request('/findings/allowlist');
      },

      add(body: { entity_text: string; entity_type?: string | null; entity_label?: string | null; reason?: string | null }): Promise<{
        entry: FindingsAllowlistEntry;
        retroactive_updates: number;
        retroactive_sessions: number;
      }> {
        return request('/findings/allowlist', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
      },

      remove(id: string): Promise<{ removed: boolean; reverted: number; reassigned: number }> {
        return request(`/findings/allowlist/${encodeURIComponent(id)}`, { method: 'DELETE' });
      },
    },
  },

  benchmarks: {
    list(): Promise<{ benchmarks: BenchmarkSummary[] }> {
      return request('/benchmarks');
    },
    latest(): Promise<{ benchmark: Benchmark | null; stale: boolean }> {
      return request('/benchmarks/latest');
    },
    get(id: string): Promise<Benchmark> {
      return request(`/benchmarks/${encodeURIComponent(id)}`);
    },
    trend(): Promise<BenchmarkTrend> {
      return request('/benchmarks/trend');
    },
    status(id: string): Promise<{ benchmark_id: string; status: string; stage: string | null; error: string | null }> {
      return request(`/benchmarks/${encodeURIComponent(id)}/status`);
    },
    generate(body: { window_days?: number; cap?: number; backend?: string; model?: string } = {}): Promise<{ status: string; benchmark_id?: string; error?: string }> {
      return request('/benchmarks/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    },
    export(id: string, kind: string): Promise<{ benchmark_id: string; kind: string; path: string; pii_scan_hits: number; content: string }> {
      return request(`/benchmarks/${encodeURIComponent(id)}/export`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind }),
      });
    },
  },
};
