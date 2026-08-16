import type { SupportContext } from './types.ts';

export const BUG_REPORT_URL = 'https://github.com/rayward-external/clawjournal/issues/new';
export const BUG_REPORT_FILENAME = 'clawjournal-bug-report.md';
export const BUG_REPORT_CONTEXT_TIMEOUT_MS = 5_000;
export const MAX_BUG_REPORT_DRAFT_LENGTH = 20_000;

export type BugReportSurface = 'workbench' | 'ui_render_error';
export type BrowserName = 'Edge' | 'Chrome' | 'Firefox' | 'Safari' | 'unknown';

export interface BugReportLocation {
  pathname: string;
  search?: string;
}

export interface BrowserHost {
  browser: BrowserName;
  major_version: number | null;
  viewport_css_pixels: { width: number | null; height: number | null };
  device_pixel_ratio: number | null;
}

export interface BrowserHostSource {
  userAgent: string;
  innerWidth: number;
  innerHeight: number;
  devicePixelRatio: number;
}

export interface BugReportDraftInput {
  summary: string;
  whatHappened: string;
  expectedBehavior: string;
  routeTemplate: string;
  surface: BugReportSurface;
  browserHost: BrowserHost;
  supportContext: SupportContext | null;
}

const STATIC_ROUTE_TEMPLATES = new Set([
  '/', '/search', '/analytics', '/analytics/insights', '/analytics/benchmark',
  '/share', '/share/rules', '/settings',
  // Pre-regroup routes remain valid bookmarks in App.tsx.
  '/dashboard', '/insights', '/benchmark', '/bundles', '/policies',
]);
const SHARE_STEPS = new Set(['queue', 'redact', 'review', 'package', 'submit', 'done']);

/** Return a finite route label without decoding or echoing identifiers or URLs. */
export function safeRouteTemplate(location: BugReportLocation): string {
  const pathname = typeof location.pathname === 'string' ? location.pathname : '';
  if (/^\/session\/[^/]+\/?$/.test(pathname)) return '/session/:id';
  if (!STATIC_ROUTE_TEMPLATES.has(pathname)) return '/unknown';
  if (pathname !== '/share') return pathname;
  const step = new URLSearchParams(location.search ?? '').get('step');
  return step && SHARE_STEPS.has(step) ? `/share?step=${step}` : '/share';
}

type UnknownRecord = Record<string, unknown>;

function record(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : null;
}

function oneOf<const T extends string>(value: unknown, choices: readonly T[]): T | null {
  return typeof value === 'string' && choices.includes(value as T) ? value as T : null;
}

function nullableSchemaVersion(value: unknown): number | null | undefined {
  if (value === null) return null;
  return Number.isInteger(value) && (value as number) >= 0 && (value as number) <= 1_000_000
    ? value as number
    : undefined;
}

function safePackageVersion(value: unknown): string | null {
  return typeof value === 'string' && /^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$/.test(value)
    ? value
    : null;
}

function safeNumericVersion(value: unknown): string | null {
  return typeof value === 'string' && (value === 'unknown' || /^\d+(?:\.\d+){0,3}$/.test(value))
    ? value
    : null;
}

const OS_FAMILIES = ['Linux', 'Windows', 'macOS', 'FreeBSD', 'unknown'] as const;
const ARCHITECTURES = ['x86', 'x86_64', 'arm64', 'ppc64le', 'riscv64', 's390x', 'unknown'] as const;
const STORAGE_RISKS = ['network', 'local', 'unknown'] as const;
const INDEX_STATUSES = ['ready', 'checking', 'recovery_required', 'rebuilding', 'unavailable', 'unknown'] as const;
const INDEX_CONDITIONS = ['storage_migration_required', 'interrupted_recovery', 'recovery_required', 'unavailable'] as const;
const COLLECTION_STATUSES = ['complete', 'partial'] as const;
const UNAVAILABLE_SECTIONS = [
  'package_version', 'package_revision', 'runtime_python', 'runtime_sqlite',
  'runtime_os', 'runtime_architecture', 'expected_schema', 'cached_index_health',
] as const;
const FILESYSTEM_TYPES = new Set([
  'unknown',
  '9p', 'afs', 'beegfs', 'blobfuse', 'ceph', 'cifs', 'cvmfs', 'davfs',
  'gfs2', 'gcsfuse', 'glusterfs', 'gpfs', 'juicefs', 'lustre', 'nfs', 'nfs4',
  'ocfs2', 'orangefs', 'panfs', 'rclone', 's3fs', 'smbfs', 'sshfs', 'virtiofs', 'weka',
  'apfs', 'btrfs', 'exfat', 'ext2', 'ext3', 'ext4', 'f2fs', 'hfs', 'hfsplus',
  'jfs', 'ntfs', 'ntfs3', 'ramfs', 'reiserfs', 'tmpfs', 'ufs', 'vfat', 'xfs', 'zfs',
  'autofs', 'cgroup', 'cgroup2', 'devtmpfs', 'ecryptfs', 'fuse', 'fuseblk',
  'iso9660', 'nsfs', 'overlay', 'proc', 'squashfs', 'sysfs', 'udf',
]);

/**
 * Project the v1 endpoint again in the browser. A doctor payload, future
 * schema, extra object fields, and arbitrary strings can never reach a draft.
 */
export function projectSupportContext(value: unknown): SupportContext | null {
  const root = record(value);
  const packageInfo = record(root?.package);
  const runtime = record(root?.runtime);
  const schema = record(root?.schema);
  const storage = record(root?.storage);
  const index = record(root?.index);
  const collection = record(root?.collection);
  if (
    !root || root.support_context_schema_version !== 1 || root.kind !== 'workbench'
    || !packageInfo || !runtime || !schema || !storage || !index || !collection
  ) return null;

  const version = safePackageVersion(packageInfo.version);
  const revision = packageInfo.revision === null
    ? null
    : typeof packageInfo.revision === 'string' && /^[0-9a-fA-F]{7,12}$/.test(packageInfo.revision)
      ? packageInfo.revision.toLowerCase()
      : undefined;
  const pythonVersion = safeNumericVersion(runtime.python_version);
  const sqliteVersion = safeNumericVersion(runtime.sqlite_version);
  const osFamily = oneOf(runtime.os_family, OS_FAMILIES);
  const osRelease = safeNumericVersion(runtime.os_release);
  const architecture = oneOf(runtime.architecture, ARCHITECTURES);
  const expectedUserVersion = nullableSchemaVersion(schema.expected_user_version);
  const filesystemType = typeof storage.filesystem_type === 'string'
    && FILESYSTEM_TYPES.has(storage.filesystem_type) ? storage.filesystem_type : null;
  const storageRisk = oneOf(storage.storage_risk, STORAGE_RISKS);
  const indexStatus = oneOf(index.status, INDEX_STATUSES);
  const condition = index.condition === null
    ? null : oneOf(index.condition, INDEX_CONDITIONS) ?? undefined;
  const collectionStatus = oneOf(collection.status, COLLECTION_STATUSES);
  const unavailable = Array.isArray(collection.unavailable_sections)
    && collection.unavailable_sections.length <= UNAVAILABLE_SECTIONS.length
    && collection.unavailable_sections.every(item => typeof item === 'string')
    ? collection.unavailable_sections as string[] : null;
  const unavailableSet = unavailable ? new Set(unavailable) : null;
  const unavailableIsOrdered = unavailable !== null
    && unavailableSet?.size === unavailable.length
    && UNAVAILABLE_SECTIONS.filter(section => unavailableSet.has(section)).every(
      (section, index) => unavailable[index] === section,
    )
    && unavailable.every(item => (UNAVAILABLE_SECTIONS as readonly string[]).includes(item));
  const collectionIsConsistent = collectionStatus === 'complete'
    ? unavailable?.length === 0
    : (unavailable?.length ?? 0) > 0;

  if (
    !version || revision === undefined || !pythonVersion || !sqliteVersion
    || !osFamily || !osRelease || !architecture || expectedUserVersion === undefined
    || !filesystemType || !storageRisk || typeof storage.storage_migration_required !== 'boolean'
    || !indexStatus || condition === undefined || !collectionStatus
    || !unavailableIsOrdered || !collectionIsConsistent
  ) return null;

  return {
    support_context_schema_version: 1,
    kind: 'workbench',
    package: { version, revision },
    runtime: {
      python_version: pythonVersion,
      sqlite_version: sqliteVersion,
      os_family: osFamily,
      os_release: osRelease,
      architecture,
    },
    schema: { expected_user_version: expectedUserVersion },
    storage: {
      filesystem_type: filesystemType,
      storage_risk: storageRisk,
      storage_migration_required: storage.storage_migration_required,
    },
    index: { status: indexStatus, condition },
    collection: {
      status: collectionStatus,
      unavailable_sections: unavailable as SupportContext['collection']['unavailable_sections'],
    },
  };
}

function safeDimension(value: number): number | null {
  return Number.isFinite(value) && value >= 0 && value <= 20_000 ? Math.round(value) : null;
}

function safeDevicePixelRatio(value: number): number | null {
  return Number.isFinite(value) && value >= 0.1 && value <= 10
    ? Math.round(value * 100) / 100 : null;
}

function browserIdentity(userAgent: string): Pick<BrowserHost, 'browser' | 'major_version'> {
  const patterns: Array<[Exclude<BrowserName, 'unknown'>, RegExp]> = [
    ['Edge', /\bEdg(?:A|iOS)?\/(\d{1,3})/],
    ['Chrome', /\b(?:Chrome|CriOS)\/(\d{1,3})/],
    ['Firefox', /\b(?:Firefox|FxiOS)\/(\d{1,3})/],
  ];
  for (const [browser, pattern] of patterns) {
    const match = pattern.exec(userAgent);
    if (match) {
      const major = Number(match[1]);
      return { browser, major_version: major >= 1 && major <= 999 ? major : null };
    }
  }
  const safari = /\bSafari\//.test(userAgent) && /\bVersion\/(\d{1,3})/.exec(userAgent);
  if (safari) {
    const major = Number(safari[1]);
    return { browser: 'Safari', major_version: major >= 1 && major <= 999 ? major : null };
  }
  return { browser: 'unknown', major_version: null };
}

/** Derive only a fixed browser enum and bounded numeric display properties. */
export function deriveBrowserHost(source: BrowserHostSource): BrowserHost {
  const identity = browserIdentity(
    typeof source.userAgent === 'string' ? source.userAgent.slice(0, 512) : '',
  );
  return {
    ...identity,
    viewport_css_pixels: {
      width: safeDimension(source.innerWidth),
      height: safeDimension(source.innerHeight),
    },
    device_pixel_ratio: safeDevicePixelRatio(source.devicePixelRatio),
  };
}

function singleLine(value: string, limit: number): string {
  return value.replace(/[\r\n]+/g, ' ').split('\u0000').join('').trim().slice(0, limit);
}

function multiline(value: string, limit: number): string {
  return value.replace(/\r\n?/g, '\n').split('\u0000').join('').trim().slice(0, limit);
}

function safeRouteTemplateLabel(value: string): string {
  if (value === '/session/:id') return value;
  if (/^\/share\?step=(queue|redact|review|package|submit|done)$/.test(value)) return value;
  return safeRouteTemplate({ pathname: value });
}

export function buildBugReportDraft(input: BugReportDraftInput): string {
  const summary = singleLine(input.summary, 120);
  const whatHappened = multiline(input.whatHappened, 4_000);
  const expectedBehavior = multiline(input.expectedBehavior, 2_000);
  const surface = input.surface === 'ui_render_error' ? 'UI render error' : 'Workbench';
  const route = safeRouteTemplateLabel(input.routeTemplate);
  const daemonContext = input.supportContext
    ? `\`\`\`json\n${JSON.stringify(input.supportContext, null, 2)}\n\`\`\``
    : 'Daemon support context unavailable.';

  return [
    `# Bug report: ${summary}`,
    '',
    '## What happened / how to reproduce', '', whatHappened,
    '',
    '## Expected behavior', '', expectedBehavior || 'Not provided.',
    '',
    '## Report context', '',
    `- Surface: ${surface}`,
    `- Route: \`${route}\``,
    '- Screenshot: not captured',
    '',
    '### Browser host', '',
    `\`\`\`json\n${JSON.stringify(input.browserHost, null, 2)}\n\`\`\``,
    '',
    '### ClawJournal daemon runtime', '', daemonContext,
  ].join('\n').slice(0, MAX_BUG_REPORT_DRAFT_LENGTH);
}
