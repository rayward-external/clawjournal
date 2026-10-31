import { useEffect, useState } from 'react';
import { api } from '../../api.ts';
import type { RedactedSessionData } from './types.ts';
import { colors } from '../../theme.ts';
import { VisualAttachmentReview } from './VisualAttachmentReview.tsx';

const labels = { original_file: 'Original file available', extracted_text: 'Text available; original file missing', reference_only: 'File mentioned; contents unavailable' };

function AttachmentImagePreview({ sessionId, attachmentId, revision, name }: {
  sessionId: string; attachmentId: string; revision?: string; name: string;
}) {
  const [url, setUrl] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {
    setUrl('');
    setError('');
    if (!revision) {
      setError('Refresh the conversation before previewing this file.');
      return;
    }
    let active = true;
    let objectUrl = '';
    const controller = new AbortController();
    const timeout = globalThis.setTimeout(() => controller.abort(), 15_000);
    api.sessions.attachmentPreview(sessionId, attachmentId, revision, controller.signal)
      .then(blob => {
        if (!active) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(err => {
        if (active) setError(controller.signal.aborted ? 'Image preview timed out. Close and reopen it to retry.'
          : err instanceof Error ? err.message : 'Image preview failed.');
      })
      .finally(() => globalThis.clearTimeout(timeout));
    return () => {
      active = false;
      controller.abort();
      globalThis.clearTimeout(timeout);
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [sessionId, attachmentId, revision]);
  if (error) return <p role="alert">{error}</p>;
  return <div>
    <p>Local image preview · may be scaled down. This image has not been redacted and is not included for sharing.</p>
    {url ? <img src={url} alt={`Local preview of ${name}`} onError={() => setError('The image preview could not be displayed.')}
      style={{ maxWidth: '100%', maxHeight: 480, objectFit: 'contain' }} /> : <p role="status">Loading local image…</p>}
  </div>;
}

export function AttachmentsPanel({ sessionId, title, data, onChange }: {
  sessionId: string;
  title: string;
  data: RedactedSessionData;
  onChange: (ids: string[], visuals?: Record<string, string>) => void;
}) {
  const [preview, setPreview] = useState<string | null>(null);
  const attachments = data.attachments || [];
  const selected = data.selectedAttachmentIds || [];
  if (!attachments.length) return null;
  const records = data.messages.flatMap(m => m.extra?.attachment_review?.items || []);
  return <section aria-label={`Attachments for ${title}`} style={{ background: colors.white, padding: 16, marginBottom: 16, border: `1px solid ${colors.gray200}`, borderRadius: 8, fontSize: 14, lineHeight: 1.5, overflowWrap: 'anywhere' }}>
    <h2 style={{ fontSize: 16, marginTop: 0 }}>Attachments · {title}</h2>
    <p>Choose which attachments to include. Nothing is included by default. For text, select the checkbox and compare the original with the version to share. For images and PDFs, open the preview and review each page first.</p>
    <p>If you leave a file out, or its contents are missing, the research pipeline will be told that information is missing. Text already quoted in the conversation stays there. Changing an attachment choice means you must approve the conversation again.</p>
    {attachments.map((a, i) => {
      const record = records.find(r => r.attachment_id === `attachment-${i + 1}`);
      return <div key={a.id} style={{ borderTop: `1px solid ${colors.gray200}`, padding: '12px 0' }}>
        <label style={{ display: 'flex', gap: 10, alignItems: 'start' }}>
          <input type="checkbox" checked={selected.includes(a.id)} disabled={data.loading || !a.can_include}
            onChange={e => onChange(e.target.checked ? [...selected, a.id] : selected.filter(id => id !== a.id))} />
          <span>{a.name} — {labels[a.availability]} · {a.message_index === null ? 'Conversation attachment (message unknown)' : `Message ${a.message_index + 1}`}</span>
        </label>
        {a.reason && <p>{a.reason}</p>}
        {a.visual_context_missing && <p>Only this file’s text can be included. Missing figures or layout will be flagged in STEMTraceBench.</p>}
        {a.availability === 'original_file' && a.can_include && !data.visualReviewIds?.[a.id] && <p>Only the text with private details removed will be shared. The original file stays on your computer.</p>}
        {(a.original_text || a.can_preview_image || a.can_review_visual) && <button type="button" onClick={() => setPreview(preview === a.id ? null : a.id)}>{preview === a.id ? 'Hide contents' : `Preview ${a.name}`}</button>}
        {preview === a.id && <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 240px), 1fr))', gap: 16 }}>
          {(a.original_text || !a.can_review_visual) && <><div><h3>Original · stays on your computer</h3>
            {a.original_text && <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 300, overflow: 'auto' }}>{a.original_text}</pre>}
            {a.can_preview_image && !a.can_review_visual && <AttachmentImagePreview sessionId={sessionId} attachmentId={a.id} revision={data.reviewedRevision} name={a.name} />}
          </div>
          <div><h3>Text to share · private details removed</h3>{data.loading ? <p>Scanning…</p> : record?.status === 'included' ? <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 300, overflow: 'auto' }}>{record.text}</pre> : <p>Not included.</p>}</div></>}
          {a.can_review_visual && <VisualAttachmentReview key={`${a.id}-${data.reviewedRevision}`} sessionId={sessionId} attachmentId={a.id} revision={data.reviewedRevision} disabled={data.loading}
            onInvalidate={() => {
              if (data.visualReviewIds?.[a.id]) onChange(selected.filter(id => id !== a.id), Object.fromEntries(Object.entries(data.visualReviewIds).filter(([id]) => id !== a.id)));
            }} onApproved={reviewId => onChange([...new Set([...selected, a.id])], { ...data.visualReviewIds, [a.id]: reviewId })} />}
        </div>}
      </div>;
    })}
    {data.loading && <p role="status">Scanning selected attachments…</p>}
    {data.previewError && <p role="alert">{data.previewError}</p>}
  </section>;
}
