import { describe, expect, it } from 'vitest';
import {
  buildBugReportDraft,
  deriveBrowserHost,
  projectSupportContext,
  safeRouteTemplate,
} from './bugReportDraft.ts';

const validContext = {
  support_context_schema_version: 1,
  kind: 'workbench',
  package: { version: '0.2.0', revision: 'abcdef123456' },
  runtime: {
    python_version: '3.13.7',
    sqlite_version: '3.49.1',
    os_family: 'Linux',
    os_release: '6.8.0',
    architecture: 'x86_64',
  },
  schema: { expected_user_version: 12 },
  storage: {
    filesystem_type: 'nfs4',
    storage_risk: 'network',
    storage_migration_required: true,
  },
  index: { status: 'unavailable', condition: 'storage_migration_required' },
  collection: { status: 'complete', unavailable_sections: [] },
};

describe('safeRouteTemplate', () => {
  it('removes session ids, hashes, and all non-allowlisted query values', () => {
    const canary = 'alice@purdue.edu-private-session';
    expect(safeRouteTemplate({
      pathname: `/session/${canary}`,
      search: `?token=${canary}`,
    })).toBe('/session/:id');
    expect(safeRouteTemplate({
      pathname: '/share',
      search: `?step=review&ids=${canary}&queue_ref=${canary}`,
    })).toBe('/share?step=review');
    expect(safeRouteTemplate({ pathname: `/unknown/${canary}` })).toBe('/unknown');
  });

  it('does not echo malformed or unrecognized share steps', () => {
    expect(safeRouteTemplate({ pathname: '/share', search: '?step=secret-project' })).toBe('/share');
    expect(safeRouteTemplate({ pathname: '//evil.example/secret' })).toBe('/unknown');
  });
});

describe('projectSupportContext', () => {
  it('accepts the exact v1 allowlist and ignores unknown response fields', () => {
    const canary = '/home/alice/session-123?token=secret';
    const projected = projectSupportContext({
      ...validContext,
      database_path: canary,
      error: canary,
      package: { ...validContext.package, checkout_path: canary },
    });
    expect(projected).not.toBeNull();
    expect(JSON.stringify(projected)).not.toContain(canary);
    expect(projected?.index).toEqual(validContext.index);
  });

  it('rejects doctor payloads and malicious values in known fields', () => {
    expect(projectSupportContext({
      support_diagnostics_schema_version: 1,
      kind: 'index',
      index: { quick_check: { status: 'ok' } },
    })).toBeNull();
    expect(projectSupportContext({
      ...validContext,
      storage: { ...validContext.storage, filesystem_type: '/home/alice/private' },
    })).toBeNull();
    expect(projectSupportContext({
      ...validContext,
      collection: { status: 'partial', unavailable_sections: ['private_path'] },
    })).toBeNull();
  });

  it('accepts safe partial context but rejects inconsistent collection status', () => {
    expect(projectSupportContext({
      ...validContext,
      package: { ...validContext.package, revision: null },
      collection: { status: 'partial', unavailable_sections: ['package_revision'] },
    })?.collection).toEqual({
      status: 'partial',
      unavailable_sections: ['package_revision'],
    });
    expect(projectSupportContext({
      ...validContext,
      collection: { status: 'complete', unavailable_sections: ['package_revision'] },
    })).toBeNull();
    expect(projectSupportContext({
      ...validContext,
      collection: { status: 'partial', unavailable_sections: [] },
    })).toBeNull();
  });
});

describe('browser-host projection and draft rendering', () => {
  it('derives only a fixed browser enum, major, and bounded display numbers', () => {
    const canary = 'private-user-token';
    const browser = deriveBrowserHost({
      userAgent: `Mozilla/5.0 ${canary} Chrome/142.0.1 Safari/537.36`,
      innerWidth: 1440.4,
      innerHeight: 900.2,
      devicePixelRatio: 1.257,
    });
    expect(browser).toEqual({
      browser: 'Chrome',
      major_version: 142,
      viewport_css_pixels: { width: 1440, height: 900 },
      device_pixel_ratio: 1.26,
    });
    expect(JSON.stringify(browser)).not.toContain(canary);

    expect(deriveBrowserHost({
      userAgent: 'private-only-agent',
      innerWidth: 99_999,
      innerHeight: -1,
      devicePixelRatio: 100,
    })).toEqual({
      browser: 'unknown',
      major_version: null,
      viewport_css_pixels: { width: null, height: null },
      device_pixel_ratio: null,
    });
  });

  it('keeps browser and daemon context separate without raw route data', () => {
    const routeCanary = 'session-alice-private';
    const errorCanary = 'render failed at /home/alice/private';
    const supportContext = projectSupportContext({ ...validContext, error: errorCanary });
    const draft = buildBugReportDraft({
      summary: 'Index unavailable',
      whatHappened: 'The recovery screen remained open.',
      expectedBehavior: 'The workbench should open.',
      routeTemplate: safeRouteTemplate({ pathname: `/session/${routeCanary}` }),
      surface: 'ui_render_error',
      browserHost: deriveBrowserHost({
        userAgent: `secret ${errorCanary} Firefox/141.0`,
        innerWidth: 1200,
        innerHeight: 800,
        devicePixelRatio: 2,
      }),
      supportContext,
    });

    expect(draft).toContain('### Browser host');
    expect(draft).toContain('### ClawJournal daemon runtime');
    expect(draft).toContain('- Route: `/session/:id`');
    expect(draft).toContain('- Screenshot: not captured');
    expect(draft).not.toContain(routeCanary);
    expect(draft).not.toContain(errorCanary);
  });
});
