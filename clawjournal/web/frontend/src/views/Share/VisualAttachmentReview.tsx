import { useEffect, useRef, useState } from 'react';
import type { PointerEvent } from 'react';
import { api } from '../../api.ts';
import type { VisualBox, VisualPage, VisualReview } from '../../types.ts';

function PageImage({ sessionId, review, page, variant, drawing, onBox, onLoaded }: {
  sessionId: string; review: VisualReview; page: VisualPage; variant: 'original' | 'redacted';
  drawing?: boolean; onBox?: (box: VisualBox) => void;
  onLoaded?: () => void;
}) {
  const [url, setUrl] = useState('');
  const [error, setError] = useState('');
  const [drag, setDrag] = useState<{ start: [number, number]; end: [number, number] } | null>(null);
  const start = useRef<[number, number] | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    const timeout = globalThis.setTimeout(() => controller.abort(), 20_000);
    let active = true;
    let objectUrl = '';
    api.sessions.visualPage(sessionId, { attachment_id: review.attachment_id,
      expected_revision: review.reviewed_revision, review_id: review.review_id,
      page: String(page.index), version: page.version, variant }, controller.signal)
      .then(blob => {
        if (!active) return;
        objectUrl = URL.createObjectURL(blob); setUrl(objectUrl);
      })
      .catch(err => { if (active) setError(err instanceof Error ? err.message : 'Page preview failed.'); })
      .finally(() => globalThis.clearTimeout(timeout));
    return () => {
      active = false; controller.abort(); globalThis.clearTimeout(timeout);
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [sessionId, review.attachment_id, review.reviewed_revision, review.review_id, page.index, page.version, variant]);
  function point(event: PointerEvent<HTMLImageElement>): [number, number] {
    const bounds = event.currentTarget.getBoundingClientRect();
    return [Math.max(0, Math.min(page.width, Math.round((event.clientX - bounds.left) / bounds.width * page.width))),
      Math.max(0, Math.min(page.height, Math.round((event.clientY - bounds.top) / bounds.height * page.height)))];
  }
  if (error) return <p role="alert">{error}</p>;
  if (!url) return <p role="status">Loading {variant} page…</p>;
  return <div style={{ position: 'relative', lineHeight: 0 }}>
    <img src={url} alt={`${variant === 'original' ? 'Original' : 'Redacted'} attachment page ${page.index + 1}`}
      draggable={false} onLoad={onLoaded} onError={() => setError('The page could not be displayed.')}
      style={{ width: '100%', height: 'auto', cursor: drawing ? 'crosshair' : 'default', touchAction: drawing ? 'none' : 'auto' }}
      onPointerDown={e => {
        if (!drawing) return;
        e.preventDefault(); start.current = point(e);
        setDrag({ start: start.current, end: start.current });
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={e => { if (drawing && start.current) setDrag({ start: start.current, end: point(e) }); }}
      onPointerCancel={() => { start.current = null; setDrag(null); }}
      onPointerUp={e => {
        if (!drawing || !start.current) return;
        const end = point(e); const origin = start.current;
        start.current = null; setDrag(null);
        const box: VisualBox = [Math.min(origin[0], end[0]), Math.min(origin[1], end[1]), Math.max(origin[0], end[0]), Math.max(origin[1], end[1])];
        if (box[2] > box[0] && box[3] > box[1]) onBox?.(box);
      }} />
    {drag && <div aria-hidden="true" style={{ position: 'absolute', pointerEvents: 'none', border: '2px solid #be123c', background: '#be123c33', boxSizing: 'border-box',
      left: `${Math.min(drag.start[0], drag.end[0]) / page.width * 100}%`, top: `${Math.min(drag.start[1], drag.end[1]) / page.height * 100}%`,
      width: `${Math.abs(drag.end[0] - drag.start[0]) / page.width * 100}%`, height: `${Math.abs(drag.end[1] - drag.start[1]) / page.height * 100}%` }} />}
  </div>;
}

export function VisualAttachmentReview({ sessionId, attachmentId, revision, onApproved, onInvalidate, disabled }: {
  sessionId: string; attachmentId: string; revision?: string;
  onApproved?: (reviewId: string) => void; onInvalidate?: () => void; disabled?: boolean;
}) {
  const [review, setReview] = useState<VisualReview | null>(null);
  const [pageIndex, setPageIndex] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [drawing, setDrawing] = useState(false);
  const [loadedPages, setLoadedPages] = useState<Record<number, string>>({});
  const [reviewedPages, setReviewedPages] = useState<Record<number, string>>({});
  const active = useRef(true);
  const requestController = useRef<AbortController | null>(null);
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; requestController.current?.abort(); };
  }, []);

  async function run(action: 'start' | 'update' | 'prepare' | 'approve', changes: { selected_candidate_ids?: number[]; manual_boxes?: VisualBox[] } = {}) {
    if (!revision || busy || disabled) return;
    setBusy(true); setError(''); setDrawing(false);
    if (action !== 'approve') { setReviewedPages({}); onInvalidate?.(); }
    const controller = new AbortController(); requestController.current = controller;
    const timeout = globalThis.setTimeout(() => controller.abort(), action === 'prepare' ? 200_000 : 60_000);
    const page = review?.pages[pageIndex];
    try {
      const result = await api.sessions.visualReview(sessionId, {
        action, attachment_id: attachmentId, expected_revision: revision,
        ...(action === 'update' && page ? { review_id: review!.review_id, page: page.index, version: page.version, ...changes } : {}),
        ...(action === 'prepare' || action === 'approve' ? { review_id: review!.review_id, versions: review!.pages.map(p => p.version) } : {}),
        ...(action === 'approve' ? { reviewed_pages: review!.pages.filter(p => reviewedPages[p.index] === p.version).map(p => p.index) } : {}),
      }, controller.signal);
      if (active.current) {
        setReview(result);
        if (action === 'start') { setPageIndex(0); setLoadedPages({}); }
        if (action === 'approve' && result.approved) onApproved?.(result.review_id);
      }
    } catch (err) {
      if (active.current) setError(controller.signal.aborted ? 'Local processing timed out. Reopen the review to retry.' : err instanceof Error ? err.message : 'Visual review failed.');
    } finally {
      globalThis.clearTimeout(timeout);
      if (active.current) setBusy(false);
    }
  }
  const page = review?.pages[pageIndex];
  return <section aria-label="Local visual redaction review" style={{ gridColumn: '1 / -1', borderTop: '1px solid #ddd', paddingTop: 12 }}>
    <h3>Review image or PDF before sharing</h3>
    <p>First, scan each page for private details. Then check the suggested changes and hide anything else needed. Finally, run the privacy check and approve every page before including the attachment. Nothing is selected automatically. Text recognition reads words from images; it does not understand every chart or detect every private detail.</p>
    {!revision && <p role="alert">Refresh the conversation before opening a visual review.</p>}
    <button type="button" disabled={busy || !revision} onClick={() => void run('start')}>{review ? 'Start page review again' : 'Start page review'}</button>
    {busy && <p role="status">Processing locally…</p>}
    {error && <p role="alert">{error}</p>}
    {review && page && <>
      <p>Page {pageIndex + 1} of {review.pages.length} · {review.pages.filter(p => p.status === 'scanned').length} pages scanned</p>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <button type="button" disabled={busy || pageIndex === 0} onClick={() => { setPageIndex(pageIndex - 1); setDrawing(false); }}>Previous page</button>
        <button type="button" disabled={busy || pageIndex + 1 === review.pages.length} onClick={() => { setPageIndex(pageIndex + 1); setDrawing(false); }}>Next page</button>
        {page.status === 'unscanned' && <button type="button" disabled={busy} onClick={() => void run('update')}>Scan this page</button>}
        {page.status === 'scanned' && <>
          <button type="button" disabled={busy} aria-pressed={drawing} onClick={() => setDrawing(!drawing)}>{drawing ? 'Cancel drawing' : 'Hide an area'}</button>
          <button type="button" disabled={busy || !page.manual_boxes.length} onClick={() => void run('update', { manual_boxes: page.manual_boxes.slice(0, -1) })}>Undo last hidden area</button>
        </>}
      </div>
      {drawing && <p>Drag over the area to hide on the original page. The redacted preview will update after you release.</p>}
      {page.warnings.map(warning => <p key={warning}>{warning}</p>)}
      {review.privacy_checked && <p>Full privacy check passed. Attachment label to share: {review.redacted_name}. Review the final pages below.</p>}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 320px), 1fr))', gap: 16 }}>
        <div><h4>Original page · stays on your computer</h4><PageImage key={`${review.review_id}-original-${page.index}-${page.version}`} sessionId={sessionId} review={review} page={page} variant="original"
          drawing={drawing && !busy} onBox={box => void run('update', { manual_boxes: [...page.manual_boxes, box] })} /></div>
        <div><h4>Page to share · private details removed</h4>{page.status === 'scanned'
          ? <PageImage key={`${review.review_id}-redacted-${page.index}-${page.version}`} sessionId={sessionId} review={review} page={page} variant="redacted"
              onLoaded={() => setLoadedPages(previous => previous[page.index] === page.version ? previous : { ...previous, [page.index]: page.version })} />
          : <p>This page has not been scanned.</p>}</div>
      </div>
      {page.status === 'scanned' && <>
        <h4>Suggested redactions</h4>
        {page.candidates.length ? page.candidates.map(candidate => <label key={candidate.id} style={{ display: 'block', overflowWrap: 'anywhere' }}>
          <input type="checkbox" disabled={busy} checked={page.selected_candidate_ids.includes(candidate.id)} onChange={event => void run('update', {
            selected_candidate_ids: event.target.checked ? [...page.selected_candidate_ids, candidate.id] : page.selected_candidate_ids.filter(id => id !== candidate.id),
          })} /> Hide “{candidate.text}”
        </label>) : <p>No text redactions were suggested. Check the image for private details the scanner may have missed.</p>}
        <details><summary>Text read from the page after hiding private details</summary><pre style={{ whiteSpace: 'pre-wrap' }}>{page.redacted_text || 'No text recognized.'}</pre></details>
      </>}
      <div style={{ borderTop: '1px solid #ddd', marginTop: 16, paddingTop: 12 }}>
        <button type="button" disabled={busy || disabled || review.pages.some(p => p.status !== 'scanned')}
          onClick={() => void run('prepare')}>Run full privacy check</button>
        {review.privacy_checked && <>
          <label style={{ display: 'block', margin: '12px 0' }}>
            <input type="checkbox" disabled={busy || disabled || loadedPages[page.index] !== page.version}
              checked={reviewedPages[page.index] === page.version}
              onChange={event => setReviewedPages(previous => ({ ...previous, [page.index]: event.target.checked ? page.version : '' }))} />
            I inspected the final redacted page {page.index + 1}, including private details text recognition may miss.
          </label>
          <button type="button" disabled={busy || disabled || review.pages.some(p => reviewedPages[p.index] !== p.version)}
            onClick={() => void run('approve')}>Approve and include attachment</button>
        </>}
        {review.approved && <p role="status">These pages are approved for sharing. Now review and approve the conversation below.</p>}
      </div>
    </>}
  </section>;
}
