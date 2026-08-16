import type {
  SupportReportCapability,
  SupportReportListResponse,
  SupportReportState,
  SupportReportStatus,
  SupportScreenshotCapability,
  SupportScreenshotStatus,
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

const UNAVAILABLE_SCREENSHOTS: SupportScreenshotCapability = {
  available: false,
  content_type: 'image/png',
  max_input_bytes: 0,
  max_output_bytes: 0,
  max_width: 0,
  max_height: 0,
  max_pixels: 0,
  sanitizer_version: '',
};

function projectScreenshotCapability(value: unknown): SupportScreenshotCapability {
  const raw = record(value);
  if (!raw || raw.available !== true) return { ...UNAVAILABLE_SCREENSHOTS };
  const sanitizerVersion = boundedString(raw.sanitizer_version, 128);
  const numeric = [
    raw.max_input_bytes,
    raw.max_output_bytes,
    raw.max_width,
    raw.max_height,
    raw.max_pixels,
  ];
  if (
    raw.content_type !== 'image/png'
    || !sanitizerVersion
    || numeric.some(candidate => !Number.isInteger(candidate) || (candidate as number) <= 0)
    || (raw.max_input_bytes as number) > 2 * 1024 * 1024
    || (raw.max_output_bytes as number) > 2 * 1024 * 1024
    || (raw.max_width as number) > 4096
    || (raw.max_height as number) > 4096
    || (raw.max_pixels as number) > 4 * 1024 * 1024
  ) return { ...UNAVAILABLE_SCREENSHOTS };
  return {
    available: true,
    content_type: 'image/png',
    max_input_bytes: raw.max_input_bytes as number,
    max_output_bytes: raw.max_output_bytes as number,
    max_width: raw.max_width as number,
    max_height: raw.max_height as number,
    max_pixels: raw.max_pixels as number,
    sanitizer_version: sanitizerVersion,
  };
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
      screenshots: projectScreenshotCapability(raw.screenshots),
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
    screenshots: projectScreenshotCapability(raw.screenshots),
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
  const screenshotIsSafe = raw.screenshot === undefined
    || raw.screenshot === null
    || projectSupportScreenshotStatus(raw.screenshot) !== null;
  return (
    typeof raw.client_report_id === 'string'
    && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(raw.client_report_id)
    && receiptIsSafe
    && messageIsSafe
    && createdAtIsSafe
    && expiresAtIsSafe
    && screenshotIsSafe
  );
}

function projectSupportScreenshotStatus(value: unknown): SupportScreenshotStatus | null {
  const raw = record(value);
  if (!raw || !Object.hasOwn(SUPPORT_REPORT_STATE_PRESENTATION, String(raw.state))) return null;
  const digest = (candidate: unknown) => typeof candidate === 'string'
    && /^[0-9a-f]{64}$/.test(candidate);
  const positiveInteger = (candidate: unknown, maximum: number) => Number.isInteger(candidate)
    && (candidate as number) > 0 && (candidate as number) <= maximum;
  const nullableDigest = raw.sanitized_sha256 === null || digest(raw.sanitized_sha256);
  const nullableBytes = raw.sanitized_bytes === null
    || positiveInteger(raw.sanitized_bytes, 2 * 1024 * 1024);
  const nullableVersion = raw.sanitizer_version === null || (
    typeof raw.sanitizer_version === 'string'
    && raw.sanitizer_version.length > 0
    && raw.sanitizer_version.length <= 128
  );
  const message = raw.message === null || (
    typeof raw.message === 'string' && raw.message.length <= 500
  );
  if (
    !Object.hasOwn(SUPPORT_REPORT_STATE_PRESENTATION, String(raw.state))
    || !digest(raw.source_sha256)
    || !positiveInteger(raw.source_bytes, 2 * 1024 * 1024)
    || !positiveInteger(raw.width, 4096)
    || !positiveInteger(raw.height, 4096)
    || (raw.width as number) * (raw.height as number) > 4 * 1024 * 1024
    || !message || !nullableDigest || !nullableBytes || !nullableVersion
  ) return null;
  const accepted = raw.state === 'accepted';
  if (accepted !== (
    raw.sanitized_sha256 !== null
    && raw.sanitized_bytes !== null
    && raw.sanitizer_version !== null
  )) return null;
  return {
    state: raw.state as SupportScreenshotStatus['state'],
    source_sha256: raw.source_sha256 as string,
    source_bytes: raw.source_bytes as number,
    width: raw.width as number,
    height: raw.height as number,
    message: raw.message as string | null,
    sanitized_sha256: raw.sanitized_sha256 as string | null,
    sanitized_bytes: raw.sanitized_bytes as number | null,
    sanitizer_version: raw.sanitizer_version as string | null,
  };
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
    screenshot: value.screenshot === undefined || value.screenshot === null
      ? null
      : projectSupportScreenshotStatus(value.screenshot),
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
