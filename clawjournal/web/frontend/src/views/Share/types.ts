import type { ToolUse } from '../../types.ts';

export type StepKey = 'queue' | 'redact' | 'review' | 'package' | 'submit' | 'done';

export const STEPS = [
  { key: 'queue', label: 'Queue' },
  { key: 'redact', label: 'Redact' },
  { key: 'review', label: 'Review' },
  { key: 'package', label: 'Package' },
  { key: 'submit', label: 'Submit' },
  { key: 'done', label: 'Done' },
];

export interface BlockedShareFinding {
  line?: number | null;
  detector?: string | null;
  status?: string | null;
  masked?: string | null;
}

export interface BlockedShareSession {
  session_id: string;
  project?: string | null;
  source?: string | null;
  model?: string | null;
  line?: number | null;
  findings?: BlockedShareFinding[];
}

export interface ReadySession {
  session_id: string;
  project: string;
  model: string | null;
  source: string;
  display_title: string;
  ai_quality_score: number | null;
  ai_failure_value_score: number | null;
  ai_recovery_labels: string[];
  ai_failure_attribution: string | null;
  ai_failure_modes: string[];
  ai_learning_summary: string | null;
  user_messages: number;
  assistant_messages: number;
  tool_uses: number;
  input_tokens: number;
  output_tokens: number;
  outcome_badge: string | null;
  client_origin?: string | null;
  runtime_channel?: string | null;
  start_time?: string | null;
  review_status?: string;
  /** Current normalized trace-content revision. */
  revision_hash?: string | null;
  /** Revision included in the latest successful share, when one exists. */
  last_shared_revision_hash?: string | null;
  /** True when this stable trace has new content after its latest successful share. */
  updated_since_last_share?: boolean;
}

export interface ShareReadyStats {
  count: number;
  total_approved: number;
  projects: string[];
  models: string[];
  recommended_session_ids: string[];
  sessions: ReadySession[];
}

export interface ShareDestination {
  configured: boolean;
  daemon_upload_supported: boolean;
  submissions_open: boolean;
  preferred_upload_flow: string;
  cli_ingest_supported: boolean;
  share_page_url: string | null;
  submit_page_url?: string | null;
  maximum_bundle_size?: number | null;
  accepted_manifest_schema_versions?: string[];
  supported_institution_email_policy?: { domain_suffixes?: string[] } | null;
  support_contact?: string | null;
  message?: string;
}

export interface HostedConsent {
  consent_text: string;
  retention_text: string;
  consent_version: string;
  retention_policy_version: string;
  support_contact?: string;
  [key: string]: unknown;
}

export type RedactionBucket = 'tokens' | 'emails' | 'paths' | 'timestamps' | 'urls' | 'other';

export interface BucketCounts {
  tokens: number;
  emails: number;
  paths: number;
  timestamps: number;
  urls: number;
  other: number;
}

export interface RedactedReviewMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
  thinking?: string;
  tool_uses?: ToolUse[];
  timestamp?: string;
}

export interface AiPiiFindingLocal {
  entity_type: string;
  entity_text: string;
  confidence: number;
  field: string;
  source: string;
}

export interface RedactedSessionData {
  messages: RedactedReviewMessage[];
  loading: boolean;
  redactionCount?: number;
  aiPiiFindings?: AiPiiFindingLocal[];
  aiCoverage?: 'full' | 'rules_only' | 'disabled';
  buckets?: BucketCounts;
  trufflehogHits?: number;
}

export const CONFIDENCE_THRESHOLD = 0.85;
export const LARGE_BUNDLE_CONFIRM_THRESHOLD = 100;
