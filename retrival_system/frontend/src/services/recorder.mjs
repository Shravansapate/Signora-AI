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
  constructor(onLimit = () => {}) { this.onLimit = onLimit; this.generation = 0; this.chunks = []; }
  async start() {
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) throw new Error('Microphone capture requires a secure browser with AudioWorklet support.');
    this.cancel();
    const generation = this.generation;
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false });
    if (generation !== this.generation) { stream.getTracks().forEach(track => track.stop()); return false; }
    this.stream = stream;
    try {
      this.context = new AudioContext({ sampleRate: 16000 });
      if (this.context.sampleRate !== 16000) throw new Error('This browser cannot capture the required 16 kHz audio.');
      await this.context.audioWorklet.addModule('/pcm-recorder-worklet.js');
      if (generation !== this.generation) return false;
      this.node = new AudioWorkletNode(this.context, 'signora-pcm-capture');
      this.node.port.onmessage = ({ data }) => {
        if (generation !== this.generation) return;
        if (data.samples) this.chunks.push(data.samples);
        if (data.complete) this.onLimit();
      };
      this.source = this.context.createMediaStreamSource(stream);
      this.source.connect(this.node);
      this.node.connect(this.context.destination); // Worklet emits silence; microphone is never monitored.
      await this.context.resume();
      this.timer = setTimeout(() => { if (generation === this.generation) this.onLimit(); }, 60000);
      return true;
    } catch (error) { this.cancel(); throw error; }
  }
  stop() { const chunks = this.chunks; this.cancel(); return pcmWav(chunks); }
  cancel() {
    this.generation++;
    clearTimeout(this.timer);
    this.stream?.getTracks().forEach(track => track.stop());
    this.source?.disconnect(); this.node?.disconnect();
    if (this.node) this.node.port.onmessage = null;
    this.context?.close().catch(() => {});
    this.stream = this.source = this.node = this.context = null;
    this.chunks = [];
  }
}
