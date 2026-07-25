import { ApiError } from '../api.ts';
import type { AutoUploadAuthorizationChallenge } from '../types.ts';

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object'
    ? value as Record<string, unknown>
    : null;
}

function stringField(
  record: Record<string, unknown> | null,
  key: string,
): string | null {
  const value = record?.[key];
  return typeof value === 'string' && value.trim() ? value : null;
}

function stringList(
  record: Record<string, unknown> | null,
  key: string,
): string[] {
  const value = record?.[key];
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : [];
}

function scopeEntryList(
  record: Record<string, unknown> | null,
  key: string,
): Array<[string, string]> | null {
  const value = record?.[key];
  if (!Array.isArray(value) || value.length === 0) return null;
  const entries: Array<[string, string]> = [];
  for (const item of value) {
    if (
      !Array.isArray(item) || item.length !== 2
      || typeof item[0] !== 'string' || item[0].length === 0
      || typeof item[1] !== 'string' || item[1].length === 0
    ) {
      return null;
    }
    entries.push([item[0], item[1]]);
  }
  return entries;
}

export function challengeFromError(
  error: unknown,
): AutoUploadAuthorizationChallenge | null {
  if (
    !(error instanceof ApiError)
    || error.status !== 409
    || error.body.code !== 'authorization_required'
  ) {
    return null;
  }
  const authorization = asRecord(error.body.authorization);
  const retention = asRecord(error.body.retention);
  const ownership = asRecord(error.body.ownership_certification);
  const scope = asRecord(error.body.scope);
  const ai = asRecord(error.body.ai);
  const authorizationVersion = stringField(authorization, 'version');
  const authorizationText = stringField(authorization, 'text');
  const retentionVersion = stringField(retention, 'version');
  const retentionText = stringField(retention, 'text');
  const ownershipVersion = stringField(ownership, 'version');
  const ownershipText = stringField(ownership, 'text');
  const authorizationProfileHash = stringField(
    error.body,
    'authorization_profile_hash',
  );
  const maximumBundleSize =
    typeof error.body.maximum_bundle_size === 'number'
    && error.body.maximum_bundle_size > 0
      ? error.body.maximum_bundle_size
      : null;
  const scopeEntries = scopeEntryList(scope, 'entries');
  if (
    !authorizationVersion || !authorizationText
    || !retentionVersion || !retentionText
    || !ownershipVersion || !ownershipText
    || !authorizationProfileHash
    || maximumBundleSize === null
    || scopeEntries === null
  ) {
    return null;
  }
  return {
    authorization_profile_hash: authorizationProfileHash,
    authorization: {
      version: authorizationVersion,
      text: authorizationText,
    },
    retention: {
      version: retentionVersion,
      text: retentionText,
    },
    ownership_certification: {
      version: ownershipVersion,
      text: ownershipText,
    },
    scope: {
      sources: stringList(scope, 'sources'),
      projects: stringList(scope, 'projects'),
      entries: scopeEntries,
    },
    ai: {
      enabled: ai?.enabled === true,
      backend: stringField(ai, 'backend'),
    },
    cap: typeof error.body.cap === 'number' ? error.body.cap : 5,
    cadence_days:
      typeof error.body.cadence_days === 'number'
        ? error.body.cadence_days
        : 7,
    maximum_bundle_size: maximumBundleSize,
    destination_origin:
      typeof error.body.destination_origin === 'string'
        ? error.body.destination_origin
        : null,
  };
}
