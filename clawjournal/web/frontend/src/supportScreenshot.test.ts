import { describe, expect, it } from 'vitest';
import {
  SUPPORT_CAPTURE_EXCLUDE_ATTRIBUTE,
  SUPPORT_CAPTURE_ROOT_SELECTOR,
  SUPPORT_CAPTURE_SAFE_ATTRIBUTE,
  finalizeSupportCaptureClone,
  markdownSha256,
  sanitizeSupportCaptureClone,
  shouldIncludeSupportCaptureNode,
} from './supportScreenshot.ts';

describe('support screenshot privacy policy', () => {
  it('default-denies text and strips form, URL, media, and accessibility payloads', () => {
    const container = document.createElement('div');
    container.innerHTML = `
      <div id="dynamic" title="private title" aria-label="private aria" style="background-image:url(https://private.example/token)">private text</div>
      <div id="safe" ${SUPPORT_CAPTURE_SAFE_ATTRIBUTE}>Static heading</div>
      <input id="input" value="secret value" placeholder="secret placeholder" />
      <textarea id="textarea">secret textarea</textarea>
      <img id="image" src="https://private.example/image?token=secret" alt="private alt" />
      <iframe id="frame" src="https://private.example/frame"></iframe>
      <svg id="svg"><text>private vector text</text></svg>
    `;
    for (const node of Array.from(container.querySelectorAll('*'))) {
      sanitizeSupportCaptureClone(node);
    }

    const dynamic = container.querySelector<HTMLElement>('#dynamic')!;
    expect(dynamic.title).toBe('');
    expect(dynamic.getAttribute('aria-label')).toBeNull();
    expect(dynamic.style.getPropertyValue('background-image')).toBe('none');
    expect(dynamic.style.getPropertyPriority('background-image')).toBe('important');
    expect(dynamic.style.getPropertyValue('color')).toBe('transparent');

    const safe = container.querySelector<HTMLElement>('#safe')!;
    expect(safe.style.getPropertyValue('color')).not.toBe('transparent');
    expect(container.querySelector<HTMLInputElement>('#input')!.value).toBe('');
    expect(container.querySelector<HTMLTextAreaElement>('#textarea')!.value).toBe('');
    expect(container.querySelector<HTMLImageElement>('#image')!.alt).toBe('');
    expect(container.querySelector<HTMLImageElement>('#image')!.src).toMatch(/^data:image\/gif/);
    expect(container.querySelector<HTMLIFrameElement>('#frame')!.getAttribute('src')).toBeNull();
    expect(container.querySelector<SVGElement>('#svg')!.childElementCount).toBe(0);
  });

  it('uses explicit root/exclusion markers and computes the consent digest deterministically', async () => {
    expect(SUPPORT_CAPTURE_ROOT_SELECTOR).toBe('#root[data-support-capture-root]');
    expect(SUPPORT_CAPTURE_EXCLUDE_ATTRIBUTE).toBe('data-support-capture-exclude');
    expect(await markdownSha256('exact markdown')).toMatch(/^[0-9a-f]{64}$/);
    expect(await markdownSha256('exact markdown')).not.toBe(await markdownSha256('changed markdown'));
  });

  it('physically removes denied text and source-filters embedded documents', () => {
    const clone = document.createElement('div');
    clone.innerHTML = `
      <div id="denied" style="-webkit-text-stroke: 3px red; text-shadow: 0 0 2px red; filter:contrast(2)">CANARY_DYNAMIC_SECRET</div>
      <div id="safe" ${SUPPORT_CAPTURE_SAFE_ATTRIBUTE}>Static label</div>
      <div id="nested-safe" ${SUPPORT_CAPTURE_SAFE_ATTRIBUTE}><span>CANARY_NESTED_SECRET</span></div>
    `;
    finalizeSupportCaptureClone(clone);
    expect(clone.textContent).not.toContain('CANARY_DYNAMIC_SECRET');
    expect(clone.textContent).not.toContain('CANARY_NESTED_SECRET');
    expect(clone.textContent).toContain('Static label');
    expect(clone.querySelector<HTMLElement>('#denied')!.textContent).toBe('');

    const frame = document.createElement('iframe');
    frame.srcdoc = '<p>CANARY_IFRAME_SECRET</p>';
    const object = document.createElement('object');
    object.data = 'https://private.example/object';
    const embed = document.createElement('embed');
    embed.src = 'https://private.example/embed';
    expect(shouldIncludeSupportCaptureNode(frame)).toBe(false);
    expect(shouldIncludeSupportCaptureNode(object)).toBe(false);
    expect(shouldIncludeSupportCaptureNode(embed)).toBe(false);
    expect(shouldIncludeSupportCaptureNode(document.createElement('div'))).toBe(true);
  });
});
