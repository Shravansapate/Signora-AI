/* Capture is bounded in the audio thread, even if the page stops responding. */
class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super(); this.frames = 0; this.stopped = false;
    this.buffer = new Float32Array(2048); this.offset = 0;
    this.port.onmessage = ({ data }) => { if (data?.command === 'stop') this.finish('stopped'); };
  }
  flush() {
    if (!this.offset) return;
    const samples = this.buffer.slice(0, this.offset);
    this.port.postMessage({ samples }, [samples.buffer]);
    this.offset = 0;
  }
  finish(reason) {
    this.flush(); this.stopped = true;
    this.port.postMessage({ complete: true, reason });
  }
  process(inputs) {
    if (this.stopped) return false;
    const channels = inputs[0];
    if (!channels?.length) return true;
    const count = Math.min(channels[0].length, sampleRate * 60 - this.frames);
    for (let i = 0; i < count; i++) {
      let mono = 0;
      for (const channel of channels) mono += channel[i] / channels.length;
      this.buffer[this.offset++] = mono;
      if (this.offset === this.buffer.length) this.flush();
    }
    this.frames += count;
    if (this.frames >= sampleRate * 60) {
      this.finish('limit');
      return false;
    }
    return true;
  }
}
registerProcessor('signora-pcm-capture', PcmCapture);
