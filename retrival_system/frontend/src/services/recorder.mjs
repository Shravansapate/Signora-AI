export function pcmWav(chunks) {
  const count = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  if (count < 4000 || count > 960000) throw new Error('Record between 0.25 and 60 seconds.');
  const buffer = new ArrayBuffer(44 + count * 2);
  const view = new DataView(buffer);
  const label = (offset, text) => [...text].forEach((letter, index) => view.setUint8(offset + index, letter.charCodeAt(0)));
  label(0, 'RIFF'); view.setUint32(4, buffer.byteLength - 8, true); label(8, 'WAVE');
  label(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, 16000, true); view.setUint32(28, 32000, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); label(36, 'data'); view.setUint32(40, count * 2, true);
  let index = 0;
  for (const chunk of chunks) for (const sample of chunk) {
    if (!Number.isFinite(sample)) throw new Error('The microphone returned invalid audio.');
    const value = Math.max(-1, Math.min(1, sample));
    view.setInt16(44 + index++ * 2, Math.round(value * (value < 0 ? 32768 : 32767)), true);
  }
  return new Blob([buffer], { type: 'audio/wav' });
}

export class PushToTalkRecorder {
  constructor(onLimit = () => {}, onLevel = () => {}) {
    this.onLimit = onLimit; this.onLevel = onLevel; this.generation = 0; this.chunks = [];
  }
  async start() {
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) throw new Error('Microphone capture requires a secure browser with AudioWorklet support.');
    this.cancel();
    const generation = this.generation;
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false });
    if (generation !== this.generation) { stream.getTracks().forEach(track => track.stop()); return false; }
    this.stream = stream;
    try {
      this.context = new AudioContext();
      this.finished = false; this.samples = 0; this.lastLevel = 0;
      await this.context.audioWorklet.addModule('/pcm-recorder-worklet.js');
      if (generation !== this.generation) return false;
      this.node = new AudioWorkletNode(this.context, 'signora-pcm-capture');
      this.node.port.onmessage = ({ data }) => {
        if (generation !== this.generation) return;
        if (data.samples) {
          this.samples += data.samples.length;
          if (this.samples > this.context.sampleRate * 60) { this.cancel(); return; }
          this.chunks.push(data.samples);
          if (performance.now() - this.lastLevel > 150) {
            const rms = Math.sqrt(data.samples.reduce((sum, value) => sum + value * value, 0) / data.samples.length);
            this.onLevel(Math.min(1, rms * 5)); this.lastLevel = performance.now();
          }
        }
        if (data.complete) {
          this.finished = true; this.flush?.resolve();
          if (data.reason === 'limit') this.onLimit();
        }
      };
      this.source = this.context.createMediaStreamSource(stream);
      this.source.connect(this.node);
      this.node.connect(this.context.destination); // Worklet emits silence; microphone is never monitored.
      await this.context.resume();
      this.timer = setTimeout(() => { if (generation === this.generation) this.onLimit(); }, 60000);
      return true;
    } catch (error) { this.cancel(); throw error; }
  }
  async stop() {
    if (!this.context || !this.node || this.flush) throw new Error('No active recording to finish.');
    const generation = this.generation;
    let timeout;
    try {
      if (!this.finished) {
        await new Promise((resolve, reject) => {
          this.flush = { resolve, reject };
          timeout = setTimeout(() => reject(new Error('Microphone did not finish recording. Please record again.')), 2000);
          this.node.port.postMessage({ command: 'stop' });
        });
      }
      if (generation !== this.generation) throw new DOMException('Recording cancelled', 'AbortError');
      const chunks = this.chunks, rate = this.context.sampleRate;
      this.flush = null; clearTimeout(timeout); this.cancel();
      return pcmWav([await resampleRecording(chunks, rate)]);
    } finally {
      clearTimeout(timeout);
      if (generation === this.generation) { this.flush = null; this.cancel(); }
    }
  }
  cancel() {
    this.generation++;
    clearTimeout(this.timer);
    this.flush?.reject(new DOMException('Recording cancelled', 'AbortError')); this.flush = null;
    this.stream?.getTracks().forEach(track => track.stop());
    this.source?.disconnect(); this.node?.disconnect();
    if (this.node) this.node.port.onmessage = null;
    this.context?.close().catch(() => {});
    this.stream = this.source = this.node = this.context = null;
    this.chunks = [];
    this.onLevel(0);
  }
}

/** Use the browser's band-limited resampler; never relabel 44.1/48 kHz samples as 16 kHz. */
export async function resampleRecording(chunks, sampleRate) {
  const count = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  if (!Number.isFinite(sampleRate) || sampleRate < 8000 || sampleRate > 192000 ||
      count < sampleRate * 0.25 || count > sampleRate * 60) throw new Error('Record between 0.25 and 60 seconds.');
  const samples = new Float32Array(count);
  let offset = 0;
  for (const chunk of chunks) { samples.set(chunk, offset); offset += chunk.length; }
  if (sampleRate === 16000) return samples;
  const offline = new OfflineAudioContext(1, Math.round(count * 16000 / sampleRate), 16000);
  const buffer = offline.createBuffer(1, count, sampleRate);
  buffer.copyToChannel(samples, 0);
  const source = offline.createBufferSource(); source.buffer = buffer;
  source.connect(offline.destination); source.start();
  return (await offline.startRendering()).getChannelData(0);
}
