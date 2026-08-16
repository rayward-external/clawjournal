import { describe, expect, it } from 'vitest';
import {
  SUPPORT_REPORT_STATE_PRESENTATION,
  isSupportReportStatus,
  projectSupportReportList,
  projectSupportReportCapability,
  projectSupportReportStatus,
  reportUtf8Bytes,
} from './supportReport.ts';

describe('private support report boundary', () => {
  it('accepts only a bounded explicit capability and counts UTF-8 bytes', () => {
    expect(projectSupportReportCapability({
      available: true,
      purpose: 'Product troubleshooting only.',
      terms_version: 'support-v1',
      retention_policy_version: 'retention-v1',
      terms_text: 'Private support terms.',
      retention_text: 'Retained for 30 days.',
      max_report_bytes: 32_768,
      message: null,
      hidden_remote_url: 'https://example.invalid/should-not-be-projected',
    })).toEqual({
      available: true,
      purpose: 'Product troubleshooting only.',
      terms_version: 'support-v1',
      retention_policy_version: 'retention-v1',
      terms_text: 'Private support terms.',
      retention_text: 'Retained for 30 days.',
      max_report_bytes: 32_768,
      message: null,
    });
    expect(projectSupportReportCapability({ available: true, purpose: 'missing terms' })).toBeNull();
    expect(projectSupportReportCapability({ available: false, message: null })).toMatchObject({ available: false });
    expect(reportUtf8Bytes('A界')).toBe(4);
  });

  it('accepts only known, bounded receipt states and has truthful UI labels for all states', () => {
    const status = {
      client_report_id: '123e4567-e89b-42d3-a456-426614174000',
      state: 'accepted',
      receipt_id: 'support-receipt-123',
      message: null,
      created_at: '2026-08-16T00:00:00Z',
      expires_at: '2026-09-15T00:00:00Z',
    };
    expect(isSupportReportStatus(status)).toBe(true);
    expect(isSupportReportStatus({ ...status, state: 'uploaded' })).toBe(false);
    expect(isSupportReportStatus({ ...status, receipt_id: '<unsafe receipt>' })).toBe(false);
    expect(projectSupportReportList({ reports: [status], truncated: false })).toEqual({
      reports: [status],
      truncated: false,
    });
    expect(projectSupportReportList({ reports: [status, status], truncated: false })).toBeNull();
    expect(projectSupportReportList({ reports: [{ ...status, secret: 'must-not-survive' }], truncated: false }))
      .toEqual({ reports: [status], truncated: false });
    expect(projectSupportReportList({ reports: [status], truncated: 'no' })).toBeNull();
    expect(projectSupportReportStatus({ ...status, manage_secret: 'must-not-survive' }))
      .toEqual(status);
    expect(projectSupportReportStatus(status, '223e4567-e89b-42d3-a456-426614174001'))
      .toBeNull();
    expect(Object.values(SUPPORT_REPORT_STATE_PRESENTATION).map(item => item.label)).toEqual([
      'Queued', 'Sending', 'Confirming', 'Received', 'Rejected',
    ]);
  });
});
