import { useEffect, useRef, useState } from 'react';
import { api } from '../../api.ts';
import type { HostedConsent } from './types.ts';

export function OriginalConversations({ shareId, offer, disabled, onChange, onPending }: {
  shareId: string | null;
  offer: HostedConsent['original_conversations'];
  disabled: boolean;
  onChange: (digest: string | null) => void;
  onPending: (pending: boolean) => void;
}) {
  const supported = offer?.schema === 'clawjournal-upload-originals-v1'
    && offer?.consent_version === 'redaction-improvement-upload-originals-v2';
  const [selected, setSelected] = useState(false);
  const [preview, setPreview] = useState<Awaited<ReturnType<typeof api.shares.originals>> | null>(null);
  const [error, setError] = useState('');
  const generation = useRef<object>({});
  useEffect(() => {
    generation.current = {};
    setSelected(false); setPreview(null); onChange(null); onPending(false);
    return () => { generation.current = {}; };
  }, [shareId, supported, offer?.consent_version, onChange, onPending]);

  async function choose(enabled: boolean) {
    const request = generation.current = {};
    setSelected(enabled); setPreview(null); setError(''); onChange(null); onPending(enabled);
    if (!enabled || !shareId) return;
    try {
      const result = await api.shares.originals(shareId);
      if (generation.current !== request) return;
      setPreview(result); onChange(result.preview_digest); onPending(false);
    } catch (e) {
      if (generation.current === request) setError(e instanceof Error ? e.message : 'Could not load originals');
      // Keep Submit blocked until the user retries or removes this choice.
    }
  }

  async function download() {
    if (!preview || !shareId) return;
    try { await api.shares.downloadOriginals(shareId, preview.preview_digest); }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not download originals'); }
  }

  // Same text size as the consent CheckboxRows that follow it.
  return <fieldset style={{ border: '1px solid #ddd', borderRadius: 8, padding: 12, fontSize: 12.5, lineHeight: 1.4 }}>
    <legend>Optional: help improve redaction</legend>
    <label><input type="checkbox" checked={selected} disabled={disabled || !supported || !shareId}
      onChange={e => void choose(e.target.checked)} /> Include my original conversation to help improve redaction.</label>
    <p>{offer?.purpose || 'Original conversations are used only to evaluate and improve the redaction pipeline, including redaction-specific model training.'}</p>
    <p>{offer?.retention_text || "Stored separately with no automatic expiration. You can withdraw your upload to delete the stored originals. Withdrawal does not undo completed training or automatically recall downloaded copies."} This choice applies only to this upload; it does not enable automatic sharing of originals.</p>
    {!supported && <p>This server needs an update before it can accept originals with these terms. You can still submit the redacted conversation.</p>}
    {selected && !preview && !error && <p role="status">Loading your local originals…</p>}
    {error && <p role="alert">{error}</p>}
    {preview && <>
      <p>{preview.document.sessions.length} original conversation(s) will be paired with the final redacted versions when you submit.</p>
      <details><summary>Inspect originals before sharing</summary>
        <pre style={{ whiteSpace: 'pre-wrap', maxHeight: 320, overflow: 'auto' }}>{JSON.stringify(preview.document.sessions, null, 2)}</pre>
      </details>
      <button type="button" disabled={disabled} onClick={() => void download()}>Download originals for the website upload</button>
      <p>If uploading on the website instead, download your redacted ZIP and this matching originals file. Nothing is sent to the website by this download.</p>
    </>}
  </fieldset>;
}
