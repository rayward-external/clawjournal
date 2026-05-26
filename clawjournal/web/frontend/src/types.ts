export type HoldState = 'auto_redacted' | 'pending_review' | 'released' | 'embargoed';

export interface Session {
  session_id: string;
  project: string;
  source: string;
  model: string | null;
  model_effort: string | null;
  start_time: string | null;
  end_time: string | null;
  duration_seconds: number | null;
  git_branch: string | null;
  user_messages: number;
  assistant_messages: number;
  tool_uses: number;
  input_tokens: number;
  output_tokens: number;
  display_title: string;
  outcome_label: string | null;
  value_labels: string[];
  risk_level: string[];
  sensitivity_score: number;
  task_type: string | null;
  files_touched: string[];
  commands_run: string[];
  review_status: string;
  selection_reason: string | null;
  reviewer_notes: string | null;
  reviewed_at: string | null;
  ai_quality_score: number | null;
  ai_failure_value_score: number | null;
  ai_recovery_labels: string[];
  ai_failure_attribution: string | null;
  ai_failure_modes: string[];
  ai_learning_summary: string | null;
  ai_score_reason: string | null;
  ai_summary: string | null;
  ai_effort_estimate: number | null;
  blob_path: string | null;
  raw_source_path: string | null;
  client_origin: string | null;
  runtime_channel: string | null;
  outer_session_id: string | null;
  indexed_at: string;
  updated_at: string | null;
  share_id: string | null;
  estimated_cost_usd: number | null;
  parent_session_id: string | null;
  subagent_session_ids: string | null;
  user_interrupts: number | null;
  hold_state: HoldState | null;
  embargo_until: string | null;
  findings_revision: string | null;
}

export type FindingStatus = 'open' | 'accepted' | 'ignored';

export interface FindingPreview {
  before: string;
  after: string;
  match_placeholder: string;
  field?: string;
  message_index?: number;
}

/** A single persisted finding row. Never carries raw match text — only the salted hash. */
export interface Finding {
  finding_id: string;
  engine: string;
  rule: string | null;
  entity_type: string | null;
  entity_hash: string;
  entity_length: number;
  field: string;
  /** Diagnostic offsets only. DO NOT index content by these — Python code points
   * and JS UTF-16 do not agree on astral characters. Use `preview` strings. */
  message_index: number | null;
  tool_field: string | null;
  offset: number;
  length: number;
  confidence: number;
  status: FindingStatus;
  decided_by: string | null;
  decided_at: string | null;
  decision_reason: string | null;
  preview?: FindingPreview;
}

/** Grouped finding — one row per distinct `(engine, entity_type, entity_hash)`. */
export interface FindingEntityGroup {
  engine: string;
  rule: string | null;
  entity_type: string | null;
  entity_hash: string;
  entity_length: number;
  occurrences: number;
  finding_ids: string[];
  max_confidence: number;
  status: FindingStatus;
  sample: {
    field: string;
    message_index: number | null;
    tool_field: string | null;
    offset: number;
    length: number;
  };
  sample_preview?: FindingPreview;
}

export interface FindingsAllowlistEntry {
  allowlist_id: string;
  entity_type: string | null;
  entity_label: string | null;
  scope: string;
  reason: string | null;
  added_by: string;
  added_at: string;
}

export interface HoldHistoryEntry {
  history_id: string;
  session_id: string;
  from_state: HoldState | null;
  to_state: HoldState;
  embargo_until: string | null;
  changed_by: 'auto' | 'user' | 'migration';
  changed_at: string;
  reason: string | null;
}

export interface SessionDetail extends Session {
  messages: Message[];
  ai_scoring_detail?: string | null;
}

export interface Message {
  role: 'user' | 'assistant';
  content: string;
  thinking?: string;
  tool_uses?: ToolUse[];
  timestamp?: string;
}

export interface ToolUse {
  tool: string;
  id?: string;
  input: Record<string, unknown> | string;
  output: Record<string, unknown> | string;
  status: string;
}

export interface Share {
  share_id: string;
  created_at: string;
  session_count: number;
  status: string;
  attestation: string | null;
  submission_note: string | null;
  bundle_hash: string | null;
  manifest: Record<string, unknown> | null;
  shared_at: string | null;
  gcs_uri?: string | null;
  sessions?: Session[];
}

export interface SharePreviewSession {
  session_id: string;
  project: string;
  source: string;
  model: string | null;
  display_title: string;
  message_count: number;
  input_tokens: number;
  output_tokens: number;
  first_user_message: string;
  ai_quality_score: number | null;
  ai_failure_value_score: number | null;
  ai_failure_attribution: string | null;
  ai_recovery_labels: string[];
  ai_failure_modes: string[];
}

export interface SharePreview {
  share_id: string;
  status: string;
  session_count: number;
  total_tokens: number;
  total_messages: number;
  file_size_bytes: number;
  export_path: string;
  manifest: Record<string, unknown>;
  sessions: SharePreviewSession[];
}

export interface Policy {
  policy_id: string;
  policy_type: string;
  value: string;
  reason: string | null;
  created_at: string;
}

export interface Stats {
  total: number;
  by_status: Record<string, number>;
  by_source: Record<string, number>;
  by_project: Record<string, number>;
  by_task_type: Record<string, number>;
}

export interface ProjectSummary {
  project: string;
  source: string;
  session_count: number;
  total_tokens: number;
}

export type ReviewStatus = 'new' | 'shortlisted' | 'approved' | 'blocked';  // shortlisted kept for DB compat

export interface RedactionLogEntry {
  type: string;
  confidence: number;
  original_length: number;
  field: string;
  message_index?: number;
  context_before?: string;
  context_after?: string;
}

export interface AiPiiFinding {
  entity_type: string;
  entity_text: string;
  confidence: number;
  field: string;
  source: string;
}

export interface RedactionReport {
  session_id: string;
  redaction_count: number;
  redaction_log: RedactionLogEntry[];
  ai_pii_findings?: AiPiiFinding[];
  ai_coverage?: 'full' | 'rules_only';
  redacted_session: SessionDetail;
}

export interface AllowlistEntry {
  id: string;
  type: 'exact' | 'pattern' | 'category';
  text?: string;
  regex?: string;
  match_type?: string;
  reason?: string;
  added: string;
}

export interface DashboardData {
  summary: {
    total_sessions: number;
    total_tokens: number;
    unique_projects: number;
    unique_sources: number;
    total_cost: number;
  };
  activity: { day: string; count: number }[];
  weekly_activity: { week: string; week_start: string; count: number }[];
  by_outcome_label: { outcome_label: string; count: number }[];
  by_value_label: { badge: string; count: number }[];
  by_risk_level: { badge: string; count: number }[];
  by_task_type: { task_type: string; count: number }[];
  by_model: { model: string; count: number }[];
  by_agent: { agent: string; count: number }[];
  tokens_by_source: { source: string; input_tokens: number; output_tokens: number }[];
  by_quality_score: { score: number; count: number }[];
  by_failure_value_score?: { score: number; count: number }[];
  high_value_failure_count?: number;
  by_failure_attribution?: { attribution: string; count: number }[];
  by_recovery_label?: { recovery_label: string; count: number }[];
  by_failure_mode?: { failure_mode: string; count: number }[];
  unscored_count: number;
  resolve_rate: number | null;
  resolve_rate_previous: number | null;
  read_edit_ratio: number | null;
  top_tools: { tool: string; calls: number }[];
  avg_interrupts: number | null;
}

export interface InsightsHeatmapCell {
  day: string;
  hour: number;
  sessions: number;
  tokens: number;
  cost: number;
}

export interface InsightsFocusRow {
  project: string;
  sessions: number;
  tokens: number;
  cost: number;
  task_types: Record<string, number>;
}

export interface InsightsModelEffectivenessRow {
  model: string;
  sessions: number;
  avg_score: number;
  resolve_rate: number;
  avg_cost: number;
  total_cost: number;
}

export interface InsightsTrendRow {
  day: string;
  sessions: number;
  avg_cost: number;
  avg_duration: number;
  resolve_rate: number;
}

export interface InsightsCostByModelRow {
  model: string;
  cost: number;
}

export interface InsightsCostByProjectRow {
  project: string;
  cost: number;
}

export interface InsightsDurationVsScoreRow {
  session_id: string;
  duration_seconds: number;
  ai_quality_score: number;
  resolution: string | null;
  cost: number | null;
}

export interface HighlightItem {
  session_id: string;
  title: string;
  project: string | null;
  source: string | null;
  model: string | null;
  start_time: string | null;
  end_time: string | null;
  duration_seconds: number | null;
  ai_quality_score: number | null;
  ai_effort_estimate: number | null;
  outcome: string | null;
  summary_teaser: string;
  rationale: string;
}

export interface HighlightsData {
  highlights: HighlightItem[];
  window_days: number;
  min_quality: number;
  candidate_count: number;
}

export interface InsightsData {
  heatmap: InsightsHeatmapCell[];
  focus: InsightsFocusRow[];
  model_effectiveness: InsightsModelEffectivenessRow[];
  trends: InsightsTrendRow[];
  duration_vs_score: InsightsDurationVsScoreRow[];
  cost_by_model: InsightsCostByModelRow[];
  cost_by_project: InsightsCostByProjectRow[];
}

export interface AdvisorRecommendation {
  type: string;
  priority: string;
  title: string;
  detail: string;
  estimated_savings_usd?: number;
}

export interface AdvisorData {
  generated_at: string;
  period: string;
  headline: string;
  recommendations: AdvisorRecommendation[];
  summary_stats: {
    total_cost_usd: number;
    total_sessions: number;
    cost_per_session: number;
    most_efficient_model: string | null;
    highest_quality_model: string | null;
    potential_savings_usd: number;
  };
}
