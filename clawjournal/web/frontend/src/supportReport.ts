import type {
  SupportReportCapability,
  SupportReportListResponse,
  SupportReportState,
  SupportReportStatus,
} from './types.ts';

// Capability discovery may make two bounded 15-second hosted requests through
// the daemon. The editor remains usable while this independent check runs.
export const SUPPORT_REPORT_CAPABILITY_TIMEOUT_MS = 35_000;

type UnknownRecord = Record<string, unknown>;

function record(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : null;
}

function boundedString(value: unknown, maxLength: number, allowEmpty = false): string | null {
  if (typeof value !== 'string' || value.length > maxLength) return null;
  const trimmed = value.trim();
  return trimmed || allowEmpty ? trimmed : null;
}

/** Treat capability data as untrusted even though it came through the local daemon. */
export function projectSupportReportCapability(value: unknown): SupportReportCapability | null {
  const raw = record(value);
  if (!raw || typeof raw.available !== 'boolean') return null;
  const message = raw.message === null ? null : boundedString(raw.message, 500, true);
  if (message === null && raw.message !== null) return null;

  if (!raw.available) {
    return {
      available: false,
      purpose: '',
      terms_version: '',
      retention_policy_version: '',
      terms_text: '',
      retention_text: '',
      max_report_bytes: 0,
      message,
    };
  }

  const purpose = boundedString(raw.purpose, 1_000);
  const termsVersion = boundedString(raw.terms_version, 128);
  const retentionVersion = boundedString(raw.retention_policy_version, 128);
  const termsText = boundedString(raw.terms_text, 8_000);
  const retentionText = boundedString(raw.retention_text, 8_000);
  const maxReportBytes = raw.max_report_bytes;
  if (
    !purpose || !termsVersion || !retentionVersion || !termsText || !retentionText
    || !Number.isInteger(maxReportBytes) || (maxReportBytes as number) < 1
    || (maxReportBytes as number) > 1_000_000
  ) return null;

  return {
    available: true,
    purpose,
    terms_version: termsVersion,
    retention_policy_version: retentionVersion,
    terms_text: termsText,
    retention_text: retentionText,
    max_report_bytes: maxReportBytes as number,
    message,
  };
}

export function reportUtf8Bytes(markdown: string): number {
  return new TextEncoder().encode(markdown).byteLength;
}

export const SUPPORT_REPORT_STATE_PRESENTATION: Record<SupportReportState, {
  label: string;
  description: string;
}> = {
  queued: {
    label: 'Queued',
    description: 'Queued locally. ClawJournal will send this exact report privately.',
  },
  submitting: {
    label: 'Sending',
    description: 'Sending the report to private support.',
  },
  ambiguous: {
    label: 'Confirming',
    description: 'The send result is uncertain. ClawJournal is confirming the receipt before retrying.',
  },
  accepted: {
    label: 'Received',
    description: 'Private support received the report.',
  },
  rejected: {
    label: 'Rejected',
    description: 'Private support did not accept the report.',
  },
};

export function isSupportReportStatus(value: unknown): value is SupportReportStatus {
  const raw = record(value);
  if (!raw || !Object.hasOwn(SUPPORT_REPORT_STATE_PRESENTATION, String(raw.state))) return false;
  const receiptIsSafe = raw.receipt_id === null
    || (typeof raw.receipt_id === 'string' && /^[0-9A-Za-z._:-]{1,200}$/.test(raw.receipt_id));
  const messageIsSafe = raw.message === null
    || (typeof raw.message === 'string' && raw.message.length <= 500);
  const createdAtIsSafe = typeof raw.created_at === 'string'
    && raw.created_at.length <= 64 && Number.isFinite(Date.parse(raw.created_at));
  const expiresAtIsSafe = raw.expires_at === null || (
    typeof raw.expires_at === 'string'
    && raw.expires_at.length <= 64 && Number.isFinite(Date.parse(raw.expires_at))
  );
  return (
    typeof raw.client_report_id === 'string'
    && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(raw.client_report_id)
    && receiptIsSafe
    && messageIsSafe
    && createdAtIsSafe
    && expiresAtIsSafe
  );
}

/** Copy only the public receipt fields from one untrusted daemon response. */
export function projectSupportReportStatus(
  value: unknown,
  expectedClientReportId?: string,
): SupportReportStatus | null {
  if (!isSupportReportStatus(value)) return null;
  if (expectedClientReportId !== undefined
    && value.client_report_id !== expectedClientReportId) return null;
  return {
    client_report_id: value.client_report_id,
    state: value.state,
    receipt_id: value.receipt_id,
    message: value.message,
    created_at: value.created_at,
    expires_at: value.expires_at,
  };
}

/** Strictly project the DB-free local outbox listing before rendering it. */
export function projectSupportReportList(value: unknown): SupportReportListResponse | null {
  const raw = record(value);
  if (!raw || !Array.isArray(raw.reports) || raw.reports.length > 100
    || typeof raw.truncated !== 'boolean') return null;
  const reports: SupportReportStatus[] = [];
  for (const value of raw.reports) {
    const report = projectSupportReportStatus(value);
    if (!report) return null;
    reports.push(report);
  }
  if (new Set(reports.map(report => report.client_report_id)).size !== reports.length) return null;
  return { reports, truncated: raw.truncated };
}
