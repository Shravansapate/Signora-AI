'use client';

import { useEffect, useRef, useState } from 'react';
import AvatarViewer from './AvatarViewer.jsx';
import { LiveDisplay } from '../services/live-display.mjs';
import { Navigation } from './workspace.jsx';
import Icon, { SectionTitle, StatusBadge } from './Icon.jsx';

export default function DisplayConsole() {
  const [token, setToken] = useState('');
  const [device, setDevice] = useState('');
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState({ state: 'DISCONNECTED' });
  const [playback, setPlayback] = useState({ state: 'IDLE' });
  const viewer = useRef(null), session = useRef(null);
  useEffect(() => () => session.current?.close(), []);
  const connect = event => {
    event.preventDefault();
    try {
      session.current = new LiveDisplay({ displayId: device, token, player: viewer.current,
        onStatus: update => setStatus(previous => ({ ...previous, ...update })) });
      session.current.connect(); setConnected(true);
    } catch (error) { setStatus({ state: 'ERROR', detail: error.message }); }
  };
  return <><a className="skip-link" href="#live-workspace">Skip to live display</a><Navigation /><main id="live-workspace" className="workspace live-display">
    <div className="page-heading"><div><p className="eyebrow">STATION DISPLAY</p><h1>Live railway announcements</h1><p className="subtitle">Connect a display to receive live announcements from your station.</p></div></div>
    <div className="display-connect-grid"><section className="panel management-section">
      <SectionTitle icon="monitor" title={connected ? 'Display session' : 'Connect display'} description="Use the registered display ID and its station-scoped access token." />
      {!connected ? <form onSubmit={connect}>
        <div className="display-fields"><label className="form-field">Display ID<input value={device} placeholder="Enter display ID" onChange={event => setDevice(event.target.value)} required /></label>
        <label className="form-field">Display access token<input type="password" placeholder="Enter display access token" value={token} onChange={event => setToken(event.target.value)} autoComplete="off" required /></label></div>
        <button className="primary" type="submit"><Icon name="link" />Connect display</button>
      </form> : <><p className="identifier">Display: {device}</p><button onClick={() => { session.current?.close(); session.current = null; setConnected(false); setToken(''); setStatus({ state: 'DISCONNECTED' }); }}><Icon name="link" />Disconnect display</button></>}
    </section><section className="panel display-status" aria-label="Display status"><div role="status"><h2>Connection status</h2><StatusBadge value={status.state} /><h2>Signing status</h2><StatusBadge value={playback.state} /><span className="sr-only">Connection: {status.state} · Signing: {playback.state}</span></div><p><Icon name="info" />{connected ? 'Incoming announcements play automatically on this display.' : 'Enter your display details to start receiving station announcements.'}</p></section></div>
    {status.detail && <p role="alert">{status.detail}</p>}
    {playback.error?.message && <p role="alert">{playback.error.message}</p>}
    <section className="panel live-output"><SectionTitle icon="monitor" title="Live display output" description="The connected display shows the announcement caption and its signing sequence." />
      <div className="live-stage"><AvatarViewer ref={viewer} token={token} onState={snapshot => { setPlayback(snapshot); session.current?.onPlayback(snapshot); }} />
        {!status.caption && <div className="stage-empty"><div className="railway-illustration" aria-hidden="true" /><strong>No current announcement</strong><p>Live announcements will appear here when connected.</p></div>}
      </div>
      <p className="caption-area" aria-live="assertive">{status.caption || 'No current announcement.'}</p>
      {status.state === 'CONNECTED' && !status.caption && <p className="display-waiting">Waiting for this station's next announcement. Enter text or record voice on the <a href="/announcements" target="_blank" rel="noreferrer">Announcements page</a> and select Play announcement.</p>}
    </section>
  </main></>;
}
