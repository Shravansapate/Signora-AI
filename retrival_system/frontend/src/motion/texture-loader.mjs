import { Texture } from 'three';

const MAX_TEXTURE_SIZE = 1024;
const bitmapOptions = { premultiplyAlpha: 'none', colorSpaceConversion: 'none' };
const dimensions = (width, height) => {
  if (!(width > 0 && height > 0)) throw new Error('The embedded image has no pixels.');
  const scale = Math.min(1, MAX_TEXTURE_SIZE / Math.max(width, height));
  return [Math.max(1, Math.round(width * scale)), Math.max(1, Math.round(height * scale))];
};

async function decodeTexture(blob, url) {
  let original;
  if (typeof createImageBitmap === 'function') {
    try {
      original = await createImageBitmap(blob, bitmapOptions);
      const [width, height] = dimensions(original.width, original.height);
      if (width === original.width && height === original.height) {
        const result = original; original = null; return result;
      }
      return await createImageBitmap(original, { ...bitmapOptions, resizeWidth: width, resizeHeight: height, resizeQuality: 'high' });
    } catch {
      // Some browser/GPU combinations expose ImageBitmap but reject valid PNGs.
      // Decode the same verified bytes through the browser's image element instead.
    } finally { original?.close(); }
  }
  const image = new Image();
  let timeout;
  try {
    await new Promise((resolve, reject) => {
      timeout = setTimeout(() => reject(new Error('Embedded image decoding timed out.')), 30000);
      image.onload = resolve;
      image.onerror = () => reject(new Error('The embedded image is damaged or unsupported.'));
      image.src = url;
    });
    const [width, height] = dimensions(image.naturalWidth, image.naturalHeight);
    const canvas = document.createElement('canvas');
    canvas.width = width; canvas.height = height;
    const context = canvas.getContext('2d');
    if (!context) throw new Error('The browser cannot prepare embedded image pixels.');
    context.imageSmoothingEnabled = true; context.imageSmoothingQuality = 'high';
    context.drawImage(image, 0, 0, width, height);
    return canvas;
  } finally {
    clearTimeout(timeout); image.onload = image.onerror = null; image.removeAttribute('src');
  }
}

/** GLTFLoader TextureLoader contract; keep at most one full-size image decoding at a time. */
export class EmbeddedTextureLoader {
  constructor(manager) { this.manager = manager; this.pending = Promise.resolve(); }
  load(source, onLoad, _onProgress, onError) {
    const url = this.manager.resolveURL(source);
    if (!url.startsWith('blob:')) throw new Error('Only embedded texture URLs are supported.');
    this.manager.itemStart(url);
    const task = this.pending.then(async () => {
      const response = await fetch(url, { credentials: 'omit', signal: AbortSignal.timeout(30000) });
      if (!response.ok) throw new Error('The embedded image bytes are unavailable.');
      const image = await decodeTexture(await response.blob(), url);
      const texture = new Texture(image);
      texture.needsUpdate = true;
      onLoad(texture);
    });
    this.pending = task.catch(error => {
      this.manager.itemError(url); onError?.(error);
    }).finally(() => {
      // GLTFLoader also revokes on success, but omits revocation on decode failure.
      URL.revokeObjectURL(url); this.manager.itemEnd(url);
    });
  }
}
