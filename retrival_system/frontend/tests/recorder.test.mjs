import test from 'node:test';
import assert from 'node:assert/strict';
import { pcmWav, PushToTalkRecorder } from '../src/services/recorder.mjs';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

function worklet(rate) {
  let Processor;
  const messages = [];
  vm.runInNewContext(readFileSync(new URL('../public/pcm-recorder-worklet.js', import.meta.url), 'utf8'), {
    sampleRate: rate, Float32Array,
    AudioWorkletProcessor: class { constructor() { this.port = { postMessage: value => messages.push(value) }; } },
    registerProcessor: (_, value) => { Processor = value; },
  });
  return { capture: new Processor(), messages };
}

test('Stopping flushes every final audio frame before the completion acknowledgement', () => {
  const { capture, messages } = worklet(48000);
  for (let i = 0; i < 19; i++) capture.process([[new Float32Array(128).fill(i / 20)]]);
  capture.port.onmessage({ data: { command: 'stop' } });
  const samples = messages.filter(m => m.samples).flatMap(m => [...m.samples]);
  assert.equal(samples.length, 19 * 128);
  assert.equal(samples.at(-1), Math.fround(18 / 20));
  assert.equal(messages.at(-1).reason, 'stopped');
  assert.equal(capture.process([[new Float32Array(128)]]), false);
});

test('Native microphone rates use a 60-second frame limit, not a fixed 16 kHz count', () => {
  for (const rate of [44100, 48000]) {
    const { capture, messages } = worklet(rate);
    const input = [[new Float32Array(128)]];
    while (capture.process(input)) { /* audio thread advances */ }
    assert.equal(messages.filter(m => m.samples).reduce((n, m) => n + m.samples.length, 0), rate * 60);
    assert.equal(messages.at(-1).reason, 'limit');
  }
});

test('Recorder waits for final worklet samples and cancels safely while stopping', async () => {
  const recorder = new PushToTalkRecorder();
  recorder.context = { sampleRate: 16000, close: async () => {} };
  recorder.chunks = [new Float32Array(4000)];
  recorder.node = { disconnect() {}, port: { postMessage() {
    queueMicrotask(() => { recorder.chunks.push(new Float32Array(128).fill(0.5)); recorder.flush.resolve(); });
  } } };
  const wav = await recorder.stop();
  assert.equal(wav.size, 44 + 4128 * 2);
  assert.equal(new DataView(await wav.arrayBuffer()).getInt16(wav.size - 2, true), 16384);
  recorder.context = { sampleRate: 16000, close: async () => {} };
  recorder.node = { disconnect() {}, port: { postMessage() {} } };
  const pending = recorder.stop();
  recorder.cancel();
  await assert.rejects(pending, { name: 'AbortError' });
});

test('PCM recording encodes exact mono 16 kHz samples and clips amplitude', async () => {
  const samples = new Float32Array(4000); samples[0] = -2; samples[1] = 2; samples[2] = 0.5;
  const wav = pcmWav([samples]);
  const view = new DataView(await wav.arrayBuffer());
  assert.equal(wav.type, 'audio/wav');
  assert.equal(view.getUint32(4, true), 8036);
  assert.equal(view.getUint16(22, true), 1);
  assert.equal(view.getUint32(24, true), 16000);
  assert.equal(view.getInt16(44, true), -32768);
  assert.equal(view.getInt16(46, true), 32767);
  assert.equal(view.getInt16(48, true), 16384);
});

test('Empty, short, excessive and nonfinite audio never becomes an upload', () => {
  for (const count of [0, 3999, 960001]) assert.throws(() => pcmWav([new Float32Array(count)]));
  const invalid = new Float32Array(4000); invalid[30] = NaN;
  assert.throws(() => pcmWav([invalid]), /invalid audio/);
});
