'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Icon, { SectionTitle } from './Icon.jsx';
import AvatarViewer from './AvatarViewer';
import { apiRequest } from '../services/api.mjs';
import { PushToTalkRecorder } from '../services/recorder.mjs';
import ControlRoom from './ControlRoom.jsx';
import PublicationPanel from './PublicationPanel.jsx';
import { Navigation } from './workspace.jsx';

const EMPTY = { state: 'IDLE', completed: 0, total: 0 };
const OPTIONS = [
  ['TRAIN_ARRIVAL', 'ARRIVING_NOW', 'Arriving now'], ['TRAIN_ARRIVAL', 'ALREADY_ARRIVED', 'Already arrived'],
  ['TRAIN_ARRIVAL', 'SCHEDULED_ARRIVAL', 'Scheduled arrival'], ['TRAIN_DEPARTURE', 'DEPARTING_NOW', 'Departing now'],
  ['TRAIN_DEPARTURE', 'ALREADY_DEPARTED', 'Already departed'], ['TRAIN_DEPARTURE', 'SCHEDULED_DEPARTURE', 'Scheduled departure'],
  ['TRAIN_DELAY', 'DELAYED', 'Delayed'], ['TRAIN_CANCELLATION', 'CANCELLED', 'Cancelled'], ['PLATFORM_CHANGE', 'CURRENT_CHANGE', 'Platform change'],
];

export default function AnnouncementConsole() {
  const [credential, setCredential] = useState('');
  const [token, setToken] = useState('');
  const [capabilities, setCapabilities] = useState(null);
  const [station, setStation] = useState('');
  const [mode, setMode] = useState('TEXT');
  const [text, setText] = useState('');
  const [date, setDate] = useState('');
  const [fields, setFields] = useState({ event: 'ARRIVING_NOW', polarity: 'POSITIVE', train_identifier: '', platform_identifier: '', old_platform: '', new_platform: '', delay_duration: '', clock_time: '' });
  const [transcript, setTranscript] = useState(null);
  const [confirmed, setConfirmed] = useState(false);
  const [recording, setRecording] = useState(false);
  const [microphoneLevel, setMicrophoneLevel] = useState(0);
  const [pending, setPending] = useState('');
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);
  const [delivery, setDelivery] = useState(null);
  const [routing, setRouting] = useState(null);
  const [publishing, setPublishing] = useState(false);
  const [snapshot, setSnapshot] = useState(EMPTY);
  const [session, setSession] = useState(0);
  const player = useRef(null);
  const recorder = useRef(null);
  const request = useRef(null);
  const generation = useRef(0);
  const feedback = useRef(null);
  const output = useRef(null);
  const stopAction = useRef(null);
  const stoppingRecording = useRef(false);
  const onState = useCallback(value => {
    setSnapshot(value);
    if (value.error) setError(value.error.message);
  }, []);
  const busy = !!pending || publishing || delivery?.state === 'SENDING' || ['PRELOADING'].includes(snapshot.state);
  useEffect(() => {
    if (result?.demo_mode && result.manifest && !error) {
      output.current?.scrollIntoView({ block: 'center', behavior: 'smooth' });
      return;
    }
    if (!result && !error) return;
    feedback.current?.focus({ preventScroll: true });
    feedback.current?.scrollIntoView({ block: 'nearest' });
  }, [result, error]);
  useEffect(() => {
    recorder.current = new PushToTalkRecorder(() => stopAction.current?.(), setMicrophoneLevel);
    return () => { generation.current++; request.current?.abort(); recorder.current?.cancel(); };
  }, []);

  function invalidate() {
    generation.current++; request.current?.abort(); request.current = null;
    player.current?.cancel(); setResult(null); setDelivery(null); setSnapshot(EMPTY); setPending(''); setError('');
  }
  function edit(callback) { invalidate(); setConfirmed(false); callback(); }
  function disconnect() {
    invalidate(); recorder.current?.cancel(); setRecording(false); setTranscript(null); setConfirmed(false);
    setToken(''); setCredential(''); setCapabilities(null); setStation(''); setText(''); setSession(v => v + 1);
  }
  async function connect(event) {
    event.preventDefault(); invalidate();
    const current = generation.current; const controller = new AbortController(); request.current = controller;
    const access = credential.trim(); setPending('Connecting');
    try {
      const data = await apiRequest('/api/v1/input/capabilities', { token: access, signal: controller.signal });
      if (generation.current !== current) return;
      setToken(access); setCredential(''); setCapabilities(data); setStation(data.stations[0]?.id || '');
    } catch (cause) { if (generation.current === current) setError(cause.message); }
    finally { if (generation.current === current) setPending(''); }
  }
  function body() {
    const common = { request_id: crypto.randomUUID(), station_id: station, input_type: mode,
      source_text_language: 'en', ...(date ? { service_date: date } : {}) };
    if (mode !== 'STRUCTURED') return { ...common, text, ...(mode === 'VOICE' ? { transcript_id: transcript.transcript_id, transcript_confirmed: confirmed } : {}) };
    const [intent] = OPTIONS.find(option => option[1] === fields.event);
    const slots = { train_identifier: fields.train_identifier };
    if (intent === 'PLATFORM_CHANGE') { slots.old_platform = fields.old_platform; slots.new_platform = fields.new_platform; }
    else if (intent === 'TRAIN_DELAY') slots.delay_duration = fields.delay_duration;
    else if (intent !== 'TRAIN_CANCELLATION') { slots.platform_identifier = fields.platform_identifier; if (fields.clock_time) slots.clock_time = fields.clock_time; }
    return { ...common, structured: { intent, temporal_state: fields.event, polarity: fields.polarity, slots } };
  }
  async function sendToDisplay(publication, current, signal) {
    setDelivery({ state: 'SENDING', publication });
    try {
      const receipt = await apiRequest('/api/v1/development/announcements', { token, body: publication, signal, timeoutMs: 120000 });
      if (generation.current === current) setDelivery({ state: 'SENT', receipt });
    } catch (cause) {
      if (generation.current === current) setDelivery({ state: 'FAILED', publication, error: cause.message,
        retryable: !(cause.status >= 400 && cause.status < 500 && cause.status !== 408) });
    }
  }
  async function prepare(input) {
    invalidate(); const current = generation.current;
    const controller = new AbortController(); request.current = controller; setPending('Finding motions');
    try {
      const data = await apiRequest('/api/v1/translate', { token, body: input || body(), signal: controller.signal, timeoutMs: 120000 });
      if (generation.current !== current) return;
      setResult(data);
      if (data.status === 'READY' && data.manifest) {
        if (data.demo_mode && routing?.ready && capabilities?.roles?.some(role => ['admin', 'operator'].includes(role))) {
          setPending('Sending to live display');
          await sendToDisplay({ station_id: station, source_event_id: data.input_id,
            source_revision: 1, expected_revision: 0, audience: routing.audience, display_ids: routing.display_ids,
            expected_routes: routing.expected_routes, emergency: routing.emergency, reason: routing.reason,
            preview_manifest_id: data.manifest.manifest_id, preview_manifest_hash: data.manifest.manifest_hash }, current, controller.signal);
          if (generation.current !== current) return;
        }
        setPending('Loading avatar and motions');
        const prepared = await player.current.prepare(data.manifest);
        if (prepared && data.demo_mode && generation.current === current) await player.current.start();
      }
    } catch (cause) { if (generation.current === current) setError(cause.message); }
    finally { if (generation.current === current) setPending(''); }
  }
  async function startRecording() {
    invalidate(); setTranscript(null); setConfirmed(false); setText(''); setPending('Requesting microphone');
    const current = generation.current;
    try { if (await recorder.current.start() && generation.current === current) setRecording(true); }
    catch (cause) { if (generation.current === current) setError(cause.message); }
    finally { if (generation.current === current) setPending(''); }
  }
  async function stopRecording() {
    if (!recording || stoppingRecording.current) return;
    stoppingRecording.current = true;
    setRecording(false); setPending('Transcribing');
    const current = generation.current; const controller = new AbortController(); request.current = controller;
    try {
      const audio = await recorder.current.stop();
      if (generation.current !== current) return;
      const form = new FormData(); form.append('audio', audio, 'announcement.wav');
      const data = await apiRequest('/api/v1/voice/transcribe', { token, body: form, signal: controller.signal, timeoutMs: 125000 });
      if (generation.current !== current) return;
      setTranscript(data); setText(data.text); setConfirmed(false);
      if (capabilities?.demo_mode_enabled && data.text.trim()) {
        await prepare({ request_id: crypto.randomUUID(), station_id: station, input_type: 'VOICE',
          source_text_language: 'en', text: data.text, transcript_id: data.transcript_id });
      }
    } catch (cause) { if (generation.current === current) setError(cause.message); }
    finally { stoppingRecording.current = false; if (generation.current === current) setPending(''); }
  }
  stopAction.current = stopRecording;
  const canPrepare = token && station && !busy && !routing?.blocked && !recording && (mode === 'STRUCTURED' ? fields.train_identifier : text.trim()) && (mode !== 'VOICE' || transcript);
  const stationData = capabilities?.stations.find(row => row.id === station);
  const field = (name, label) => <label key={name}>{label}<input value={fields[name]} disabled={busy || recording} onChange={event => edit(() => setFields({ ...fields, [name]: event.target.value }))} /></label>;

  return <>
    <a className="skip-link" href="#announcement-workspace">Skip to announcement input</a>
    <Navigation />
    <main id="announcement-workspace">
      <div className="page-heading announcement-hero"><div><p className="eyebrow">RAILWAY ANNOUNCEMENTS</p><h1>Prepare an announcement</h1><p className="subtitle">Type an announcement or record your voice to play the available motions.</p></div></div>
      <section className="panel connection">
        {!token ? <form className="connection-form" onSubmit={connect}><label htmlFor="announcement-token">Access token</label><input id="announcement-token" type="password" autoComplete="off" value={credential} onChange={e => setCredential(e.target.value)} /><button className="primary" disabled={busy || !credential.trim()}>Connect</button></form>
          : <div className="connected-row"><span className="session-indicator"><span className="status-dot" />Credentials held in memory for this session</span><button onClick={disconnect}>Disconnect</button></div>}
      </section>
      {token && station && capabilities?.roles?.some(role => ['admin', 'operator'].includes(role)) && <ControlRoom key={`${session}:${station}`} token={token} station={station} platforms={stationData?.definition.platforms || []} onRouting={setRouting} busy={busy || (delivery?.state === 'FAILED' && delivery.retryable)} />}
      {pending && <p role="status">{pending}…</p>}
      <div ref={feedback} tabIndex={-1} aria-label="Announcement preparation result">
        {error && <div role="alert" className="notice error-notice">{error}</div>}
        {result && !result.demo_mode && <section className="notice review-notice" aria-live="polite">
          <h2>{result.status.replaceAll('_', ' ')}</h2>
          {result.status !== 'READY' && <p>No playable sequence was prepared. The avatar stays idle until the complete announcement is ready.</p>}
          {result.meaning && <><p>{result.meaning.intent.replaceAll('_', ' ')} · {result.meaning.temporal_state.replaceAll('_', ' ')} · {result.meaning.polarity}</p><dl className="meaning-fields">{Object.entries(result.meaning.slots).map(([name, value]) => <div key={name}><dt>{name.replaceAll('_', ' ')}</dt><dd>{value.value}{!value.valid && ' — requires correction'}</dd></div>)}</dl></>}
          {result.issues?.map((issue, index) => <p key={index}>{issue.detail}</p>)}
          {result.issues?.some(issue => issue.code === 'UNSUPPORTED_GRAMMAR') && <p>Include the train number and a complete event, such as “is arriving”. Use Structured fields if you are unsure of the supported wording.</p>}
          {result.meaning && result.issues?.some(issue => ['TEMPLATE_UNAVAILABLE', 'MISSING_REALIZATION'].includes(issue.code)) && <p>The announcement meaning was understood, but an administrator/reviewer must configure and approve a construction covering these exact values. Rephrasing alone will not make signing available. You can test individual motions in <a href="/">Content preview</a>.</p>}
        </section>}
        {result?.demo_mode && !result.manifest && <p role="alert">{result.issues?.map(issue => issue.detail).join(' ') || 'No playable animation files are available.'}</p>}
      </div>
      <div className="announcement-grid">
        <section className="panel announcement-form">
          <SectionTitle icon="megaphone" title="Announcement input" description="Configure your announcement, or record a voice message." />
          <label>Station<select value={station} disabled={!token || busy || recording} onChange={e => edit(() => setStation(e.target.value))}><option value="">Select a configured station</option>{capabilities?.stations.map(row => <option key={row.id} value={row.id}>{row.definition.name} ({row.id})</option>)}</select></label>
          {token && !capabilities?.stations.length && <p className="notice review-notice">No station is configured for this account. An administrator must add the station and its platform inventory.</p>}
          <div className="mode-switch" role="group" aria-label="Input method">{['TEXT', 'STRUCTURED', 'VOICE'].map(value => <button key={value} aria-pressed={mode === value} disabled={busy || recording} onClick={() => edit(() => { setMode(value); setTranscript(null); recorder.current?.cancel(); })}><Icon name={value === 'TEXT' ? 'keyboard' : value === 'VOICE' ? 'mic' : 'list'} />{value === 'TEXT' ? 'Type' : value === 'VOICE' ? 'Record voice' : 'Structured fields'}</button>)}</div>
          {mode === 'VOICE' && <div className="voice-controls"><p>English · maximum 60 seconds · local transcription. Wait for the recording status, check the microphone level, and say train numbers digit by digit.</p><button disabled={!token || busy} onClick={recording ? stopRecording : startRecording}>{recording ? 'Stop and transcribe' : 'Start recording'}</button><meter aria-label="Microphone level" min="0" max="1" value={microphoneLevel} /><span role="status">{recording ? 'Microphone recording' : capabilities?.asr_state === 'NOT_CONFIGURED' ? 'Speech recognition is not configured. Typed input remains available.' : ''}</span>{transcript?.metadata?.audio_quality?.warnings?.includes("QUIET_AUDIO") && <p role="status">The recording was quiet. Move closer to the microphone if words are missing.</p>}{transcript?.metadata?.audio_quality?.warnings?.includes("CLIPPED_AUDIO") && <p role="status">The recording was distorted. Lower the microphone level or move slightly farther away.</p>}</div>}
          {mode !== 'STRUCTURED' ? <div><label htmlFor="announcement-text">{mode === 'VOICE' ? 'Final transcript — check and correct' : 'Announcement text'}</label><textarea id="announcement-text" placeholder="Type your announcement here" rows={5} maxLength={2048} value={text} disabled={busy || recording} onChange={e => edit(() => setText(e.target.value))} /></div> : <>
            <label>Event<select value={fields.event} disabled={busy} onChange={e => edit(() => setFields({ ...fields, event: e.target.value }))}>{OPTIONS.map(([, value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
            <label>Polarity<select value={fields.polarity} disabled={busy} onChange={e => edit(() => setFields({ ...fields, polarity: e.target.value }))}><option value="POSITIVE">Affirmed</option><option value="NEGATIVE">Negated (not)</option></select></label>
            {field('train_identifier', 'Train identifier (keep leading zeros)')}
            {fields.event === 'CURRENT_CHANGE' ? <>{field('old_platform', 'Old platform')}{field('new_platform', 'New platform')}</> : fields.event === 'DELAYED' ? field('delay_duration', 'Delay with unit (for example, 10 minutes)') : fields.event !== 'CANCELLED' ? <>{field('platform_identifier', 'Platform')}{field('clock_time', 'Clock time, if provided (for example, 6:30 pm)')}</> : null}
          </>}
          {mode === 'TEXT' && <p className="field-hint">Example: Train 1201 arrives at platform 2.</p>}
          <label>Service date, when the announcement includes a clock time<input type="date" value={date} disabled={busy || recording} onChange={e => edit(() => setDate(e.target.value))} /></label>
          {stationData && <p className="field-hint">Configured platforms: {stationData.definition.platforms.join(', ')} · Asia/Kolkata</p>}
          {!capabilities?.demo_mode_enabled && mode === 'VOICE' && transcript && result?.meaning && <label className="confirmation"><input type="checkbox" checked={confirmed} disabled={busy} onChange={e => { player.current?.cancel(); setConfirmed(e.target.checked); setResult(current => ({ ...current, manifest: null, status: 'NEEDS_CONFIRMATION' })); }} />I checked the transcript, identifiers, platforms, status, negation and times.</label>}
          <button className="primary" disabled={!canPrepare} onClick={() => prepare()}><Icon name="play" />{capabilities?.demo_mode_enabled ? 'Play announcement' : mode === 'VOICE' && !confirmed ? 'Check transcript and fields' : 'Prepare complete preview'}</button>
          <p className="field-hint">{capabilities?.demo_mode_enabled ? 'Available motions play here. Select target displays above to also send this announcement.' : 'Publication and live display delivery are separate from this private preview.'}</p>
          {delivery?.state === 'SENT' && <p role="status">Sent to assigned station displays. <a href="/display" target="_blank" rel="noreferrer">Open live display</a></p>}
          {delivery?.state === 'FAILED' && <div role="alert">Live delivery failed: {delivery.error} {delivery.retryable ? <><button disabled={busy} onClick={() => sendToDisplay(delivery.publication, generation.current)}>Retry live delivery</button><button disabled={busy} onClick={() => setDelivery(null)}>Discard delivery retry</button></> : <p>Review the current targets and prepare the announcement again.</p>}</div>}
        </section>
        <section ref={output} className="panel viewer-panel"><div className="panel-heading"><SectionTitle icon="monitor" title="Avatar output" /><span className="player-state">{snapshot.state}</span></div>
          <div className="announcement-stage"><AvatarViewer key={session} ref={player} token={token} onState={onState} />
            {snapshot.state === 'IDLE' && !result?.manifest && <div className="stage-empty"><span className="avatar-empty-symbol"><Icon name="user" /></span><strong>Your announcement will appear here</strong><p>The avatar will load and play the selected motions.</p></div>}
          </div>
          <div className="caption-area"><p>{result?.original_text || text || 'Your complete announcement will appear here.'}</p></div>
          {result?.retrieval?.translation_status === 'DOMAIN_DRAFT' && <p className="field-hint">Railway gloss uses configurable draft rules; ISL linguistic correctness has not been validated.</p>}
          {result?.retrieval?.translation_status === 'LEXICAL_RECOVERY' && <p className="field-hint">Playing matching words and available letters. This sequence has not been validated as an ISL sentence.</p>}
          {!!result?.retrieval?.missing_gloss?.length && <p className="field-hint">Incomplete signing — missing signs: {result.retrieval.missing_gloss.join(', ')}. Read the complete caption for the intended meaning.</p>}
          {!!result?.retrieval?.skipped?.length && <p className="field-hint">Some motions were unavailable. Playing the available sequence.</p>}
          {result?.retrieval && <details><summary>Motion lookup details</summary><pre style={{ whiteSpace: 'pre-wrap' }}>{JSON.stringify(result.retrieval, null, 2)}</pre></details>}
          <div className="playback-controls"><button className="primary" disabled={!['READY', 'COMPLETE'].includes(snapshot.state) || busy} onClick={async () => { try { await player.current.start(); } catch (cause) { setError(cause.message); } }}><Icon name="play" />Play preview</button><button disabled={!['PLAYING', 'PRELOADING'].includes(snapshot.state)} onClick={() => player.current?.cancel()}>Stop</button><span role="status">{snapshot.completed} / {snapshot.total} clips</span></div>
        </section>
      </div>
      {token && station && !capabilities?.demo_mode_enabled && capabilities?.roles?.some(role => ['admin', 'operator'].includes(role)) && <PublicationPanel key={`${session}:${station}`} token={token} station={station}
        routing={routing} onBusyChange={setPublishing} preview={result?.manifest} previewComplete={snapshot.state === 'COMPLETE'} invalidatePreview={invalidate}
        onCorrect={caption => { edit(() => { setMode('TEXT'); setText(caption); setTranscript(null); }); }} />}
    </main>
  </>;
}
