// Shared local-first privacy caveat for the onboarding surfaces (ZeroState and
// GettingStartedGuide). Kept in one place so the two banners can't drift into
// contradicting each other (one said "nothing leaves your machine" while the
// other disclosed AI-scoring egress). Honest about background AI scoring/review,
// which sends an anonymized, redacted trace to the configured backend
// independent of any sharing approval.
export const LOCAL_FIRST_CAVEAT =
  'Everything stays on your machine by default — AI scoring/review (when enabled) sends an anonymized, redacted trace to your configured AI backend; nothing is uploaded for sharing until you approve it.';
