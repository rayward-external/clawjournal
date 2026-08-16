import type { SupportScreenshotCapability } from './types.ts';

export const SUPPORT_CAPTURE_ROOT_SELECTOR = '#root[data-support-capture-root]';
export const SUPPORT_CAPTURE_EXCLUDE_ATTRIBUTE = 'data-support-capture-exclude';
export const SUPPORT_CAPTURE_SAFE_ATTRIBUTE = 'data-support-capture-safe';

const TRANSPARENT_PIXEL = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==';
const MAX_CAPTURE_ATTEMPTS = 6;

export interface CapturedSupportScreenshot {
  blob: Blob;
  png_base64: string;
  sha256: string;
  width: number;
  height: number;
  bytes: number;
}

function stripSensitiveAttributes(element: Element): void {
  for (const attribute of Array.from(element.attributes)) {
    const name = attribute.name.toLowerCase();
    if (
      name.startsWith('on')
      || name === 'value'
      || name === 'placeholder'
      || name === 'title'
      || name === 'alt'
      || name === 'href'
      || name === 'src'
      || name === 'srcset'
      || name === 'action'
      || name === 'formaction'
      || name === 'poster'
      || name === 'data'
      || name === 'srcdoc'
      || name.startsWith('aria-')
    ) element.removeAttribute(attribute.name);
  }
}

/**
 * Default-deny the cloned DOM before modern-screenshot serializes it.
 * Unmarked text is rendered transparent, form/media content is destroyed,
 * URL-bearing styles are removed, and only explicitly marked static leaves
 * may render text.
 */
export function sanitizeSupportCaptureClone(node: Node): void {
  if (node.nodeType === Node.TEXT_NODE) return;
  if (!(node instanceof Element)) return;
  stripSensitiveAttributes(node);
  if (!(node instanceof HTMLElement || node instanceof SVGElement)) return;

  const style = node.style;
  style.setProperty('background-image', 'none', 'important');
  style.setProperty('border-image', 'none', 'important');
  style.setProperty('list-style-image', 'none', 'important');
  style.setProperty('cursor', 'default', 'important');
  style.setProperty('caret-color', 'transparent', 'important');
  style.setProperty('text-shadow', 'none', 'important');
  style.setProperty('-webkit-text-stroke', '0 transparent', 'important');
  style.setProperty('filter', 'none', 'important');
  style.setProperty('backdrop-filter', 'none', 'important');

  const safeLeaf = node.hasAttribute(SUPPORT_CAPTURE_SAFE_ATTRIBUTE)
    && node.childElementCount === 0;
  if (!safeLeaf) {
    style.setProperty('color', 'transparent', 'important');
    style.setProperty('-webkit-text-fill-color', 'transparent', 'important');
  }

  if (node instanceof HTMLInputElement) {
    node.value = '';
    node.checked = false;
  } else if (node instanceof HTMLTextAreaElement) {
    node.value = '';
    node.textContent = '';
  } else if (node instanceof HTMLSelectElement) {
    for (const option of Array.from(node.options)) option.text = '';
  }

  if (
    node instanceof HTMLImageElement
    || node instanceof HTMLVideoElement
    || node instanceof HTMLAudioElement
    || node instanceof HTMLIFrameElement
    || node instanceof HTMLObjectElement
    || node instanceof HTMLEmbedElement
  ) {
    if (node instanceof HTMLImageElement) node.src = TRANSPARENT_PIXEL;
    else node.removeAttribute('src');
    style.setProperty('opacity', '0', 'important');
    style.setProperty('background', '#d7dbe2', 'important');
  }

  if (node instanceof HTMLCanvasElement) {
    const context = node.getContext('2d');
    if (context) {
      context.save();
      context.setTransform(1, 0, 0, 1, 0, 0);
      context.fillStyle = '#d7dbe2';
      context.fillRect(0, 0, node.width, node.height);
      context.restore();
    }
  }
  if (node instanceof SVGElement) {
    node.replaceChildren();
    style.setProperty('background', '#d7dbe2', 'important');
  }
}

export function shouldIncludeSupportCaptureNode(node: Node): boolean {
  if (!(node instanceof Element)) return true;
  return !node.hasAttribute(SUPPORT_CAPTURE_EXCLUDE_ATTRIBUTE)
    && !node.matches('iframe, object, embed');
}

export function finalizeSupportCaptureClone(clonedRoot: Node): void {
  if (!(clonedRoot instanceof Element)) return;
  const textWalker = clonedRoot.ownerDocument.createTreeWalker(
    clonedRoot,
    NodeFilter.SHOW_TEXT,
  );
  const deniedTextNodes: Text[] = [];
  for (let current = textWalker.nextNode(); current; current = textWalker.nextNode()) {
    const parent = current.parentElement;
    const safeStaticLeaf = parent?.hasAttribute(SUPPORT_CAPTURE_SAFE_ATTRIBUTE) === true
      && parent.childElementCount === 0;
    if (!safeStaticLeaf) deniedTextNodes.push(current as Text);
  }
  for (const textNode of deniedTextNodes) textNode.data = '';

  const owner = clonedRoot.ownerDocument;
  const policy = owner.createElement('style');
  policy.textContent = `
    [${SUPPORT_CAPTURE_EXCLUDE_ATTRIBUTE}] { display: none !important; }
    * {
      text-shadow: none !important;
      -webkit-text-stroke: 0 transparent !important;
      filter: none !important;
      backdrop-filter: none !important;
      caret-color: transparent !important;
    }
    *:not([${SUPPORT_CAPTURE_SAFE_ATTRIBUTE}]) {
      color: transparent !important;
      -webkit-text-fill-color: transparent !important;
    }
    [${SUPPORT_CAPTURE_SAFE_ATTRIBUTE}]:empty { color: transparent !important; }
    *::before, *::after { content: none !important; }
    input, textarea, select, [contenteditable] {
      color: transparent !important;
      -webkit-text-fill-color: transparent !important;
      background-image: none !important;
    }
  `;
  clonedRoot.prepend(policy);
}

function pngDimensions(buffer: ArrayBuffer): { width: number; height: number } {
  const bytes = new Uint8Array(buffer);
  const signature = [137, 80, 78, 71, 13, 10, 26, 10];
  if (bytes.byteLength < 24 || signature.some((value, index) => bytes[index] !== value)) {
    throw new Error('Capture did not produce a PNG image.');
  }
  const view = new DataView(buffer);
  return { width: view.getUint32(16), height: view.getUint32(20) };
}

async function blobToImage(blob: Blob): Promise<CanvasImageSource> {
  if (typeof createImageBitmap === 'function') return createImageBitmap(blob);
  return new Promise<HTMLImageElement>((resolve, reject) => {
    const url = URL.createObjectURL(blob);
    const image = new Image();
    image.onload = () => {
      URL.revokeObjectURL(url);
      resolve(image);
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error('Could not decode the captured PNG.'));
    };
    image.src = url;
  });
}

async function resizeOpaquePng(
  blob: Blob,
  width: number,
  height: number,
): Promise<Blob> {
  const source = await blobToImage(blob);
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext('2d', { alpha: false });
  if (!context) throw new Error('Canvas capture is unavailable.');
  context.fillStyle = '#ffffff';
  context.fillRect(0, 0, width, height);
  context.drawImage(source, 0, 0, width, height);
  if ('close' in source && typeof source.close === 'function') source.close();
  return new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(result => {
      if (result) resolve(result);
      else reject(new Error('Could not encode the captured PNG.'));
    }, 'image/png');
  });
}

async function sha256Hex(buffer: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', buffer);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}

function base64FromBytes(bytes: Uint8Array): string {
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 32_768) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 32_768));
  }
  return btoa(binary);
}

export async function markdownSha256(markdown: string): Promise<string> {
  return sha256Hex(new TextEncoder().encode(markdown).buffer);
}

export async function captureSupportScreenshot(
  capability: SupportScreenshotCapability,
): Promise<CapturedSupportScreenshot> {
  if (!capability.available) throw new Error('Private screenshot capture is unavailable.');
  const root = document.querySelector<HTMLElement>(SUPPORT_CAPTURE_ROOT_SELECTOR);
  if (!root) throw new Error('The support capture surface is unavailable.');

  const viewportWidth = Math.max(1, Math.floor(document.documentElement.clientWidth || window.innerWidth));
  const viewportHeight = Math.max(1, Math.floor(document.documentElement.clientHeight || window.innerHeight));
  const maximumScale = Math.min(
    capability.max_width / viewportWidth,
    capability.max_height / viewportHeight,
    Math.sqrt(capability.max_pixels / (viewportWidth * viewportHeight)),
  );
  const scale = Math.min(Math.max(1, window.devicePixelRatio || 1), maximumScale);
  if (!Number.isFinite(scale) || scale <= 0) throw new Error('The viewport is larger than the screenshot limit.');

  // Kept as an explicit dynamic import: no capture code or dependency is
  // loaded until the reporter presses the capture button.
  const { domToBlob } = await import('modern-screenshot');
  let blob = await domToBlob(root, {
    type: 'image/png',
    width: viewportWidth,
    height: viewportHeight,
    scale,
    backgroundColor: '#ffffff',
    timeout: 5_000,
    font: false,
    fetchFn: async () => TRANSPARENT_PIXEL,
    filter: shouldIncludeSupportCaptureNode,
    features: { restoreScrollPosition: true },
    onCloneEachNode: sanitizeSupportCaptureClone,
    onCloneNode: finalizeSupportCaptureClone,
  });
  let encoded = await blob.arrayBuffer();
  let dimensions = pngDimensions(encoded);

  for (let attempt = 0; blob.size > capability.max_input_bytes; attempt += 1) {
    if (attempt >= MAX_CAPTURE_ATTEMPTS || dimensions.width <= 1 || dimensions.height <= 1) {
      throw new Error('The reviewed screenshot is larger than private support accepts.');
    }
    const ratio = Math.min(0.9, Math.sqrt(capability.max_input_bytes / blob.size) * 0.9);
    const width = Math.max(1, Math.floor(dimensions.width * ratio));
    const height = Math.max(1, Math.floor(dimensions.height * ratio));
    blob = await resizeOpaquePng(blob, width, height);
    encoded = await blob.arrayBuffer();
    dimensions = pngDimensions(encoded);
  }

  if (
    dimensions.width > capability.max_width
    || dimensions.height > capability.max_height
    || dimensions.width * dimensions.height > capability.max_pixels
  ) throw new Error('The reviewed screenshot exceeds private support dimensions.');

  return {
    blob,
    png_base64: base64FromBytes(new Uint8Array(encoded)),
    sha256: await sha256Hex(encoded),
    width: dimensions.width,
    height: dimensions.height,
    bytes: blob.size,
  };
}
