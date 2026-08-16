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
      screenshots: {
        available: false,
        content_type: 'image/png',
        max_input_bytes: 0,
        max_output_bytes: 0,
        max_width: 0,
        max_height: 0,
        max_pixels: 0,
        sanitizer_version: '',
      },
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
      screenshots: {
        available: false,
        content_type: 'image/png',
        max_input_bytes: 0,
        max_output_bytes: 0,
        max_width: 0,
        max_height: 0,
        max_pixels: 0,
        sanitizer_version: '',
      },
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
      screenshot: null,
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

  it('projects only bounded screenshot capability and receipt metadata', () => {
    const capability = projectSupportReportCapability({
      available: true,
      purpose: 'Product troubleshooting only.',
      terms_version: 'support-v2',
      retention_policy_version: 'retention-v2',
      terms_text: 'Screenshot support terms.',
      retention_text: 'Retained for 30 days.',
      max_report_bytes: 32_768,
      screenshots: {
        available: true,
        content_type: 'image/png',
        max_input_bytes: 2 * 1024 * 1024,
        max_output_bytes: 2 * 1024 * 1024,
        max_width: 4096,
        max_height: 4096,
        max_pixels: 4 * 1024 * 1024,
        sanitizer_version: 'png-rgb-v1',
        upload_url: 'must not survive projection',
      },
      message: null,
    });
    expect(capability?.screenshots).toEqual({
      available: true,
      content_type: 'image/png',
      max_input_bytes: 2 * 1024 * 1024,
      max_output_bytes: 2 * 1024 * 1024,
      max_width: 4096,
      max_height: 4096,
      max_pixels: 4 * 1024 * 1024,
      sanitizer_version: 'png-rgb-v1',
    });

    const status = {
      client_report_id: '123e4567-e89b-42d3-a456-426614174000',
      state: 'accepted',
      receipt_id: 'support-receipt-123',
      message: null,
      created_at: '2026-08-16T00:00:00Z',
      expires_at: '2026-09-15T00:00:00Z',
      screenshot: {
        state: 'accepted',
        source_sha256: 'a'.repeat(64),
        source_bytes: 1234,
        width: 1280,
        height: 720,
        message: 'received',
        sanitized_sha256: 'b'.repeat(64),
        sanitized_bytes: 1000,
        sanitizer_version: 'png-rgb-v1',
        hidden_url: 'must not survive projection',
      },
    };
    expect(projectSupportReportStatus(status)?.screenshot).toEqual({
      state: 'accepted',
      source_sha256: 'a'.repeat(64),
      source_bytes: 1234,
      width: 1280,
      height: 720,
      message: 'received',
      sanitized_sha256: 'b'.repeat(64),
      sanitized_bytes: 1000,
      sanitizer_version: 'png-rgb-v1',
    });
    expect(projectSupportReportStatus({
      ...status,
      screenshot: { ...status.screenshot, source_sha256: 'not-a-hash' },
    })).toBeNull();
  });
});
