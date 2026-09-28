/* Capture is bounded in the audio thread, even if the page stops responding. */
class PcmCapture extends AudioWorkletProcessor {
  constructor() { super(); this.frames = 0; this.stopped = false; }
  process(inputs) {
    if (this.stopped) return false;
    const channels = inputs[0];
    if (!channels?.length) return true;
    const count = Math.min(channels[0].length, 960000 - this.frames);
    const mono = new Float32Array(count);
    for (let i = 0; i < count; i++) {
      for (const channel of channels) mono[i] += channel[i] / channels.length;
    }
    this.frames += count;
    this.port.postMessage({ samples: mono }, [mono.buffer]);
    if (this.frames >= 960000) {
      this.stopped = true;
      this.port.postMessage({ complete: true });
      return false;
    }
    return true;
  }
}
registerProcessor('signora-pcm-capture', PcmCapture);
