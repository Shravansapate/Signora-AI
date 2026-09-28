'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import AvatarViewer from './AvatarViewer';
import { Navigation } from './workspace.jsx';
import { apiRequest } from '../services/api.mjs';

const IDLE = { state: 'IDLE', index: -1, total: 0, completed: 0, error: null };
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const statusLabels = {
  IDLE: 'Awaiting a sequence', PRELOADING: 'Loading and checking every clip', READY: 'All clips ready',
  PLAYING: 'Playing sequence', TRANSITIONING: 'Changing clip', COMPLETE: 'Sequence complete', ERROR: 'Playback unavailable',
};

function humanize(value) { return String(value).replaceAll('_', ' ').toLowerCase(); }
function seconds(value) { return `${Number(value).toFixed(1)} s`; }
function errorMessage(error) { return `${error.message || 'The request failed.'}${error.requestId ? ` Request: ${error.requestId}` : ''}`; }

export default function ReviewConsole() {
  const [credential, setCredential] = useState('');
  const [token, setToken] = useState('');
  const [mode, setMode] = useState('review');
  const [motions, setMotions] = useState([]);
  const [hasMore, setHasMore] = useState(false);
  const [query, setQuery] = useState('');
  const [avatarId, setAvatarId] = useState('');
  const [profileId, setProfileId] = useState('');
  const [queue, setQueue] = useState([]);
  const [text, setText] = useState('');
  const [preparedId, setPreparedId] = useState('');
  const [caption, setCaption] = useState('Select content and prepare a complete sequence.');
  const [manifest, setManifest] = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [pending, setPending] = useState(null);
  const [locked, setLocked] = useState(false);
  const [session, setSession] = useState(0);
  const [snapshot, setSnapshot] = useState(IDLE);
  const viewer = useRef(null);
  const request = useRef(null);
  const generation = useRef(0);
  const sequenceCounter = useRef(0);
  const busy = !!pending || ['PRELOADING', 'PLAYING', 'TRANSITIONING'].includes(snapshot.state);
  const avatar = motions.find((motion) => motion.motion_version_id === avatarId);
  const filtered = useMemo(() => {
    const term = query.trim().toLowerCase();
    return motions.filter((motion) => `${motion.gloss} ${motion.semantic_key} ${motion.clip_name}`.toLowerCase().includes(term));
  }, [motions, query]);

  const onState = useCallback((state) => setSnapshot(state), []);
  useEffect(() => () => { generation.current++; request.current?.abort(); }, []);

  function invalidate() {
    generation.current++;
    request.current?.abort();
    request.current = null;
    viewer.current?.cancel();
    setManifest(null);
    setResult(null);
    setError(null);
    setPending(null);
    setSnapshot(IDLE);
  }

  function resetSession() {
    invalidate();
    setLocked(false);
    setSession((value) => value + 1);
    setCaption('Select content and prepare a complete sequence.');
  }

  function disconnect() {
    resetSession();
    setCredential('');
    setToken('');
    setMotions([]);
    setHasMore(false);
    setAvatarId('');
    setProfileId('');
    setQueue([]);
    setPreparedId('');
  }

  async function loadLibrary(accessToken = token, offset = motions.length) {
    const current = ++generation.current;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setPending('library');
    setError(null);
    try {
      const data = await apiRequest(`/api/v1/review/motions?limit=100&offset=${offset}`, { token: accessToken, signal: controller.signal });
      if (current !== generation.current) return;
      if (!Array.isArray(data.items)) throw new Error('The library response is invalid.');
      setMotions((previous) => offset === 0 ? data.items : [...previous, ...data.items]);
      setHasMore(data.items.length === 100);
      if (offset === 0 && data.items.length) {
        const first = data.items.find((motion) => motion.gloss === 'TRAIN') ?? data.items[0];
        setAvatarId(first.motion_version_id);
        setProfileId(first.avatar_profile_id);
      }
    } catch (cause) {
      if (current === generation.current && !controller.signal.aborted) setError(errorMessage(cause));
    } finally {
      if (current === generation.current) setPending(null);
    }
  }

  function connect(event) {
    event.preventDefault();
    const accessToken = credential.trim();
    if (!accessToken) return;
    resetSession();
    setToken(accessToken);
    setCredential('');
    if (mode === 'review') void loadLibrary(accessToken, 0);
  }

  function changeMode(next) {
    if (next === mode || busy) return;
    resetSession();
    setMode(next);
    if (next === 'review' && token && !motions.length) void loadLibrary(token, 0);
  }

  function editQueue(nextQueue) {
    invalidate();
    setQueue(nextQueue);
    setCaption('Sequence changed. Prepare again to validate every clip.');
  }

  function add(motion) {
    if (busy || queue.length >= 64) return;
    editQueue([...queue, { ...motion, occurrence: ++sequenceCounter.current }]);
  }

  function move(index, direction) {
    const updated = [...queue];
    [updated[index], updated[index + direction]] = [updated[index + direction], updated[index]];
    editQueue(updated);
  }

  async function prepare() {
    if (!token || busy) return;
    invalidate();
    const current = generation.current;
    const controller = new AbortController();
    request.current = controller;
    setPending('prepare');
    const sourceCaption = mode === 'exact' ? text : queue.map((item) => item.gloss).join(' · ');
    setCaption(sourceCaption);
    try {
      if (mode === 'prepared') {
        const plan = await apiRequest(`/api/v1/playback/${preparedId.trim()}`, { token, signal: controller.signal });
        if (current !== generation.current) return;
        if (plan.purpose !== 'CONTENT_REVIEW') throw new Error('Open a content review plan prepared for this reviewer.');
        setManifest(plan);
        setCaption(plan.caption_text);
        setLocked(true);
        await viewer.current.prepare(plan);
        return;
      }
      const path = mode === 'review' ? '/api/v1/review/prepare' : '/api/v1/playback/prepare';
      const body = mode === 'review' ? {
        avatar_motion_version_id: avatarId, motion_version_ids: queue.map((item) => item.motion_version_id),
      } : { text, avatar_profile_id: profileId, source_text_language: 'en' };
      const response = await apiRequest(path, { token, body, signal: controller.signal });
      if (current !== generation.current) return;
      setResult(response);
      if (response.status !== 'READY') return;
      if (!response.manifest) throw new Error('The server returned readiness without a playback plan.');
      setManifest(response.manifest);
      setCaption(response.manifest.caption_text);
      setLocked(true);
      await viewer.current.prepare(response.manifest);
    } catch (cause) {
      if (current === generation.current && !controller.signal.aborted) setError(errorMessage(cause));
    } finally {
      if (current === generation.current) setPending(null);
    }
  }

  async function play() {
    setError(null);
    try { await viewer.current.start(); }
    catch (cause) { setError(errorMessage(cause)); }
  }

  const prepareDisabled = !token || busy || (mode === 'review' ? !avatarId || !queue.length
    : mode === 'prepared' ? !UUID.test(preparedId.trim()) : !text.trim() || !UUID.test(profileId));
  const selectedItem = manifest?.items[snapshot.index];
  const progress = snapshot.total ? Math.min(100, (snapshot.completed / snapshot.total) * 100) : 0;

  return <>
    <a className="skip-link" href="#workspace">Skip to workspace</a>
    <Navigation />
    <main id="workspace">
      <div className="page-heading">
        <div><p className="eyebrow">INDIAN SIGN LANGUAGE / MOTION LIBRARY</p><h1>Content review</h1>
          <p className="subtitle">Inspect exact motion versions on one persistent avatar.</p></div>
        <span className="review-badge">Review workspace</span>
      </div>

      <section className="connection panel" aria-label="Access credentials">
        {!token ? <form onSubmit={connect} className="connection-form">
          <div><label htmlFor="access-token">Access token</label><p>Use reviewer or admin access for the library; operator access for exact lookup.</p></div>
          <input id="access-token" type="password" value={credential} onChange={(event) => setCredential(event.target.value)}
            autoComplete="off" spellCheck={false} required maxLength={2048} placeholder="Enter your access token" />
          <button className="primary" type="submit" disabled={!credential.trim()}>Connect workspace</button>
        </form> : <div className="connected-row"><div><span className="status-dot" /><strong>Session credential set</strong>
          <span className="muted">Held in memory until you disconnect or reload.</span></div>
          <button className="subtle" onClick={disconnect}>Disconnect</button></div>}
      </section>

      <div className="workspace-grid">
        <section className="library panel" aria-labelledby="library-heading">
          <div className="panel-heading"><h2 id="library-heading">Source content</h2><span className="counter">{motions.length} loaded</span></div>
          <div className="mode-switch" role="group" aria-label="Content source">
            <button aria-pressed={mode === 'review'} onClick={() => changeMode('review')} disabled={busy}>Motion library</button>
            <button aria-pressed={mode === 'exact'} onClick={() => changeMode('exact')} disabled={busy}>Exact text</button>
            <button aria-pressed={mode === 'prepared'} onClick={() => changeMode('prepared')} disabled={busy}>Prepared review</button>
          </div>
          {mode === 'review' ? <>
            <label htmlFor="search" className="sr-only">Search loaded motions</label>
            <input id="search" className="search-input" type="search" placeholder="Search loaded motions…" value={query} onChange={(event) => setQuery(event.target.value)} />
            <div className="motion-list" aria-label="Available motions" aria-busy={pending === 'library'}>
              {!token && <div className="empty-state"><strong>Your library starts here</strong><p>Connect with your access token to browse registered motions.</p></div>}
              {token && !motions.length && pending !== 'library' && <div className="empty-state"><strong>No motions loaded</strong><p>Refresh the library to request available review content.</p>
                <button className="secondary" disabled={busy} onClick={() => loadLibrary(token, 0)}>Refresh library</button></div>}
              {token && filtered.length === 0 && motions.length > 0 && <p className="empty-state">No loaded motions match “{query}”.</p>}
              {filtered.map((motion) => <article className="motion-row" key={motion.motion_version_id}>
                <div className="motion-detail"><strong>{motion.gloss}</strong><span className="motion-key" title={motion.semantic_key}>{motion.semantic_key}</span>
                  <span className="motion-meta">v{motion.version_no} · {seconds(motion.duration_seconds)} · {humanize(motion.linguistic_review_status)}</span></div>
                <button className="add-button" aria-label={`Add ${motion.gloss} version ${motion.version_no} to sequence`} disabled={busy || queue.length >= 64} onClick={() => add(motion)}>+</button>
              </article>)}
              {pending === 'library' && <p className="loading-text" role="status">Loading library…</p>}
            </div>
            {hasMore && <button className="load-more" disabled={busy} onClick={() => loadLibrary()}>Load more motions</button>}
            <div className="library-footnote">Exact versions · Original motion assets</div>
          </> : mode === 'prepared' ? <div className="exact-form">
            <label htmlFor="prepared-review">Prepared review plan ID</label>
            <input id="prepared-review" value={preparedId} disabled={busy} maxLength={36} spellCheck={false}
              onChange={(event) => { invalidate(); setPreparedId(event.target.value); }} placeholder="Review plan UUID" />
            <p className="field-hint">Open the exact sequence prepared for a template review using the same reviewer account. Check its signing order, repetitions and transitions before recording your decision.</p>
          </div> : <div className="exact-form">
            <label htmlFor="exact-text">Exact source text</label>
            <textarea id="exact-text" rows={6} maxLength={2048} disabled={busy} value={text} onChange={(event) => { invalidate(); setText(event.target.value); }} placeholder="Enter an exact registered expression" />
            <label htmlFor="profile-id">Avatar profile ID</label>
            <input id="profile-id" value={profileId} disabled={busy || locked} onChange={(event) => { invalidate(); setProfileId(event.target.value); }} placeholder="Avatar profile UUID" spellCheck={false} />
            <p className="field-hint">Exact lookup checks the registered expression and its eligibility. Unavailable content returns its review or availability state.</p>
          </div>}
        </section>

        <section className="viewer-panel panel" aria-labelledby="viewer-heading">
          <div className="panel-heading"><h2 id="viewer-heading">Avatar preview</h2><span className={`player-state state-${snapshot.state.toLowerCase()}`} aria-live="polite">{humanize(snapshot.state)}</span></div>
          <div className="viewer-stage">
            <AvatarViewer key={session} ref={viewer} token={token} onState={onState} />
            {!locked && <div className="stage-empty"><span className="stage-symbol" aria-hidden="true">↗</span><strong>Ready when your content is</strong><p>Prepare a sequence to load the avatar.</p></div>}
            <span className="stage-label">ISL · CONTENT PREVIEW</span>
          </div>
          <div className="caption-area" aria-live="polite" aria-atomic="true"><span className="eyebrow">{manifest?.purpose === 'EXACT_CONTENT' ? 'SOURCE TEXT' : 'REVIEW SEQUENCE'}</span><p>{caption}</p></div>
          <div className="playback-status">
            <div><span>{statusLabels[snapshot.state] ?? humanize(snapshot.state)}</span><span>{snapshot.total > 0 ? `${snapshot.completed} / ${snapshot.total} clips` : 'Full-sequence readiness'}</span></div>
            <progress max="100" value={progress} aria-label="Completed clips" />
            {selectedItem && snapshot.state === 'PLAYING' && <p className="current-clip">Now playing: {selectedItem.semantic_key}</p>}
          </div>
          <div className="playback-controls"><button className="primary" onClick={play} disabled={pending || !['READY', 'COMPLETE'].includes(snapshot.state)}>{snapshot.state === 'COMPLETE' ? 'Replay sequence' : 'Play sequence'}</button>
            <button className="secondary" onClick={() => { invalidate(); setCaption('Playback stopped. Prepare the sequence again to continue.'); }} disabled={!['PLAYING', 'PRELOADING', 'TRANSITIONING'].includes(snapshot.state)}>Stop</button>
            <button className="subtle reset-button" onClick={resetSession}>Reset session</button></div>
        </section>

        <section className="sequence-panel panel" aria-labelledby="sequence-heading">
          <div className="panel-heading"><h2 id="sequence-heading">{mode === 'review' ? 'Review sequence' : mode === 'prepared' ? 'Prepared review' : 'Exact lookup'}</h2><span className="counter">{mode === 'review' ? `${queue.length} / 64` : 'English'}</span></div>
          {mode === 'review' ? <>
            <div className="avatar-selection"><label htmlFor="avatar-selection">Persistent avatar source</label>
              <select id="avatar-selection" disabled={!motions.length || locked || busy} value={avatarId} onChange={(event) => { invalidate(); setAvatarId(event.target.value); setProfileId(motions.find((motion) => motion.motion_version_id === event.target.value).avatar_profile_id); }}>
                {!motions.length && <option value="">Load library first</option>}
                {motions.map((motion) => <option key={motion.motion_version_id} value={motion.motion_version_id}>{motion.gloss} · v{motion.version_no}</option>)}
              </select><p className="field-hint">{locked ? 'Avatar is fixed for this session. Reset to change it.' : 'The avatar stays in the scene while compatible clips change.'}</p></div>
            <ol className="queue-list" aria-label="Ordered review sequence">
              {!queue.length && <li className="empty-state"><strong>Build your review sequence</strong><p>Add motions from the library. Add a motion again to review repetition.</p></li>}
              {queue.map((motion, index) => <li key={motion.occurrence} className={`queue-item ${snapshot.state === 'PLAYING' && snapshot.index === index ? 'is-current' : ''}`}>
                <span className="queue-index">{String(index + 1).padStart(2, '0')}</span><div className="queue-text"><strong>{motion.gloss}</strong><span>v{motion.version_no} · {seconds(motion.duration_seconds)}</span></div>
                <div className="queue-actions"><button disabled={busy || index === 0} aria-label={`Move item ${index + 1} ${motion.gloss} up`} onClick={() => move(index, -1)}>↑</button>
                  <button disabled={busy || index === queue.length - 1} aria-label={`Move item ${index + 1} ${motion.gloss} down`} onClick={() => move(index, 1)}>↓</button>
                  <button disabled={busy} aria-label={`Remove item ${index + 1} ${motion.gloss}`} onClick={() => editQueue(queue.filter((_, item) => item !== index))}>×</button></div>
              </li>)}
            </ol>
          </> : mode === 'prepared' ? <div className="exact-explanation"><h3>Inspect the prepared construction</h3><p>The review plan preserves the template's exact concept order and interruption groups. Its ID links your review evidence to that construction and those motion versions.</p><p>Reset the session before opening a plan for a different avatar.</p></div>
            : <div className="exact-explanation"><h3>Retrieve a complete expression</h3><p>The backend returns a version-pinned plan only when the exact content is eligible for the selected avatar.</p><p>The original source text remains visible if signing is unavailable.</p></div>}
          <div className="prepare-area">
            {mode === 'review' && <div className="sequence-summary"><span>Sequence duration</span><strong>{seconds(queue.reduce((sum, item) => sum + item.duration_seconds, 0))}</strong></div>}
            <button className="primary prepare-button" onClick={prepare} disabled={prepareDisabled}>{pending === 'prepare' || snapshot.state === 'PRELOADING' ? 'Preparing all clips…' : mode === 'review' ? 'Prepare sequence' : mode === 'prepared' ? 'Open review plan' : 'Find exact content'}</button>
            <p className="field-hint">Review playback stays private. Prepare and publish operational messages in the Announcements workspace.</p>
          </div>
        </section>
      </div>

      {(error || snapshot.error) && <div className="notice error-notice" role="alert"><strong>Unable to continue</strong><p>{error || snapshot.error.message}</p></div>}
      {result && result.status !== 'READY' && <div className="notice review-notice" role="status"><strong>{humanize(result.status)}</strong>
        <p>{result.reasons?.map(humanize).join(' · ') || 'This content does not currently have an available playback plan.'}</p></div>}
      {manifest && <div className="manifest-details"><span>Plan {manifest.manifest_id}</span><span>{manifest.items.length} occurrences · expires {new Date(manifest.valid_until).toLocaleTimeString()}</span></div>}
      <footer className="workspace-footer"><span>Signora AI</span><span>Persistent avatar · Version-pinned playback · Indian Sign Language</span></footer>
    </main>
  </>;
}
