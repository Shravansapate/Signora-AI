import test from 'node:test';
import assert from 'node:assert/strict';
import { pcmWav } from '../src/services/recorder.mjs';

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
