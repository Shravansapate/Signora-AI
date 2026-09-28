import { apiRequest } from './api.mjs';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u;

/** Persistent progress contains IDs/cursors only; tokens and announcement text stay in memory. */
export class DisplayProgress {
  constructor(storage, displayId) {
    this.storage = storage; this.key = `signora-display-v1:${displayId}`;
    this.cursor = 0; this.completed = new Set();
    try {
      const saved = JSON.parse(storage.getItem(this.key) ?? '{}');
      if (Number.isSafeInteger(saved.cursor) && saved.cursor >= 0) this.cursor = saved.cursor;
      if (Array.isArray(saved.completed)) this.completed = new Set(saved.completed.filter(id => UUID.test(id)).slice(-128));
    } catch { /* Server snapshot remains authoritative if local progress is unavailable. */ }
  }
  save() {
    this.completed = new Set([...this.completed].slice(-128));
    this.storage.setItem(this.key, JSON.stringify({ cursor: this.cursor, completed: [...this.completed] }));
  }
}

/** Complete current state replaces the queue. A lost cursor never replays obsolete events. */
export function selectQueue(sync, progress, now = Date.now()) {
  if (sync?.type !== 'SYNC' || !Number.isSafeInteger(sync.cursor) || sync.cursor < 0
    || !Array.isArray(sync.active) || sync.active.length > 32 || !Array.isArray(sync.events)) throw new Error('Invalid live snapshot.');
  const ids = new Set();
  for (const item of sync.active) {
    if (!UUID.test(item.manifest_id) || !UUID.test(item.message_id) || ids.has(item.message_id)
      || !Number.isInteger(item.revision) || item.revision < 1 || !Number.isInteger(item.priority)
      || item.priority < 0 || item.priority > 3 || !Number.isFinite(Date.parse(item.valid_until))) throw new Error('Invalid live message.');
    ids.add(item.message_id);
  }
  return sync.active.filter(item => Date.parse(item.valid_until) > now
    && !progress.completed.has(item.manifest_id) && item.progress?.state !== 'COMPLETED');
}

export class LiveDisplay {
  constructor({ displayId, token, player, onStatus, storage = localStorage, socketFactory = url => new WebSocket(url),
    request = apiRequest, origin = location.origin }) {
    if (!UUID.test(displayId)) throw new Error('Enter a valid registered display ID.');
    this.id = displayId; this.token = token; this.player = player; this.onStatus = onStatus;
    this.progress = new DisplayProgress(storage, displayId); this.socketFactory = socketFactory;
    this.request = request; this.origin = origin; this.queue = []; this.current = null;
    this.acks = []; this.pending = null; this.generation = 0; this.closed = false;
    this.retries = 0; this.running = false; this.state = 'IDLE'; this.leaseUntil = 0;
    this.recovering = new Set();
  }
  connect() {
    const url = new URL(`/api/v1/displays/${this.id}/events`, this.origin);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    this.socket = this.socketFactory(url.href);
    const socket = this.socket;
    socket.onopen = () => {
      this.sentAt = performance.now();
      socket.send(JSON.stringify({ type: 'HELLO', token: this.token, cursor: this.progress.cursor }));
    };
    socket.onmessage = event => {
      if (socket !== this.socket || this.closed) return;
      try { this.receive(JSON.parse(event.data)); }
      catch (error) { this.onStatus({ state: 'ERROR', detail: error.message }); socket.close(); }
    };
    socket.onclose = event => {
      if (this.closed || socket !== this.socket) return;
      this.generation++; this.running = false;
      this.player.requestBoundaryStop('DISCONNECTED');
      if (this.state !== 'PLAYING') this.current = null;
      const error = new Error('Live connection interrupted');
      this.pending?.reject(error); this.pending = null;
      for (const ack of this.acks.splice(0)) ack.reject(error);
      this.onStatus({ state: 'DISCONNECTED', detail: 'Reconnecting; live signing requires a fresh lease.' });
      if (event.code === 1008) {
        this.onStatus({ state: 'REJECTED', detail: 'Display authorization or session was rejected. Reconnect with current credentials.' });
        return;
      }
      this.timer = setTimeout(() => this.connect(), Math.min(15000, 500 * 2 ** Math.min(this.retries++, 5)) + Math.random() * 250);
    };
  }
  receive(data) {
    if (data.type === 'ACKNOWLEDGED') {
      const ack = this.pending; this.pending = null;
      if (data.accepted === false) ack?.reject(new Error(data.detail ?? 'Playback acknowledgement rejected'));
      else ack?.resolve();
      return;
    }
    if (data.type !== 'SYNC') throw new Error('Unexpected live protocol message');
    const remaining = Date.parse(data.lease_until) - Date.parse(data.server_time)
      - Math.max(0, performance.now() - this.sentAt);
    if (!(remaining > 0 && remaining <= 60000)) throw new Error('Live freshness lease is invalid');
    this.leaseUntil = performance.now() + remaining;
    this.player.grantLease(this.leaseUntil);
    this.retries = 0;
    this.queue = selectQueue(data, this.progress, Date.parse(data.server_time));
    for (const item of data.active) {
      if (this.progress.completed.has(item.manifest_id) && item.progress?.state === 'STARTED'
        && !this.recovering.has(item.manifest_id) && Number.isInteger(item.last_boundary)) {
        this.recovering.add(item.manifest_id);
        this.ack({ manifest_id: item.manifest_id, state: 'COMPLETED', boundary: item.last_boundary })
          .catch(() => {}).finally(() => this.recovering.delete(item.manifest_id));
      }
    }
    this.progress.cursor = data.cursor;
    this.progress.save();
    // Correct captions update immediately; signing switches only at approved boundaries.
    this.onStatus({ state: 'CONNECTED', detail: '', caption: this.queue[0]?.caption_text ?? '', queued: this.queue.length });
    const valid = this.current && this.queue.some(item => item.manifest_id === this.current.manifest_id);
    const urgent = this.current && this.queue[0]?.priority < this.current.priority;
    if (this.current && (!valid || urgent)) {
      this.player.requestBoundaryStop(valid ? 'PRIORITY' : 'SUPERSEDED');
      if (this.state !== 'PLAYING') this.releaseCurrent();
    }
    if (this.pending) throw new Error('Server failed to acknowledge the prior request');
    this.pending = this.acks.shift() ?? null;
    this.sentAt = performance.now();
    this.socket.send(JSON.stringify({ type: 'HEARTBEAT', cursor: data.cursor, ...this.pending?.body }));
    this.schedule();
  }
  ack(body) {
    return new Promise((resolve, reject) => {
      if (this.closed || this.socket?.readyState !== 1 || this.acks.length >= 128) return reject(new Error('Live acknowledgement unavailable'));
      this.acks.push({ body: { type: 'ACK', ...body }, resolve, reject });
    });
  }
  releaseCurrent() { this.generation++; this.current = null; this.running = false; }
  async schedule() {
    this.queue = this.queue.filter(item => !this.progress.completed.has(item.manifest_id));
    if (this.closed || this.running || this.current || !this.queue.length || performance.now() >= this.leaseUntil) return;
    const item = this.queue[0];
    this.current = item; this.running = true;
    const generation = ++this.generation;
    const current = () => !this.closed && generation === this.generation;
    try {
      if (item.progress?.state && !['FAILED', 'RECEIVED'].includes(item.progress.state)) {
        await this.ack({ manifest_id: item.manifest_id, state: 'FAILED', error_code: 'RESTART_COMPLETE_MESSAGE' });
      }
      await this.ack({ manifest_id: item.manifest_id, state: 'RECEIVED' });
      if (!current()) return;
      const plan = await this.request(`/api/v1/playback/${item.manifest_id}`, { token: this.token });
      if (!current()) return;
      if (plan.manifest_hash !== item.manifest_hash || plan.delivery?.message_id !== item.message_id) throw new Error('Live manifest identity changed');
      this.plan = plan;
      if (!await this.player.prepare(plan)) throw new Error('Complete asset preparation failed');
      if (!current()) return;
      await this.ack({ manifest_id: item.manifest_id, state: 'ASSETS_READY' });
      if (!current()) return;
      if (!await this.player.start()) throw new Error('Live signing could not start');
      await this.ack({ manifest_id: item.manifest_id, state: 'STARTED' });
    } catch (error) {
      if (current()) {
        this.player.requestBoundaryStop('PLAYBACK_FAILED');
        this.onStatus({ state: 'ERROR', detail: error.message });
        await this.ack({ manifest_id: item.manifest_id, state: 'FAILED', error_code: 'PLAYBACK_FAILED' }).catch(() => {});
        // Remain held until a fresh connection or a corrected message; no retry loop.
      }
    } finally { if (current()) this.running = false; }
  }
  onPlayback(snapshot) {
    this.state = snapshot.state;
    const item = this.current;
    if (!item) return;
    if (snapshot.state === 'COMPLETE') {
      this.progress.completed.add(item.manifest_id); this.progress.save();
      this.ack({ manifest_id: item.manifest_id, state: 'COMPLETED', boundary: snapshot.total - 1 })
        .then(() => { if (this.current === item) { this.releaseCurrent(); this.schedule(); } }).catch(() => {});
    } else if (snapshot.state === 'INTERRUPTED' || snapshot.state === 'ERROR') {
      if (this.socket?.readyState !== 1) { this.releaseCurrent(); return; }
      const boundary = this.plan?.safe_boundaries.includes(snapshot.index) ? snapshot.index : -1;
      this.ack({ manifest_id: item.manifest_id, state: 'FAILED', boundary, error_code: snapshot.error?.code ?? 'INTERRUPTED' })
        .then(() => { if (this.current === item) this.releaseCurrent(); }).catch(() => {});
    }
  }
  close() {
    this.closed = true; this.generation++; clearTimeout(this.timer); this.socket?.close();
    this.player.cancel(); this.token = '';
    this.pending?.reject(new Error('Display disconnected'));
    for (const ack of this.acks.splice(0)) ack.reject(new Error('Display disconnected'));
  }
}
