'use client';
import { useEffect, useState } from 'react';
import { apiRequest } from '../services/api.mjs';
import { SectionTitle, StatusBadge } from './Icon.jsx';

export default function ControlRoom({ token, station, platforms, onRouting }) {
  const [data, setData] = useState({ items: [], history: [] });
  const [selected, setSelected] = useState([]), [audience, setAudience] = useState('SELECTED');
  const [emergency, setEmergency] = useState(false), [error, setError] = useState('');
  const [reason, setReason] = useState('Operator announcement assignment');
  const [pending, setPending] = useState(false), [notice, setNotice] = useState('');
  const [retry, setRetry] = useState(null);
  useEffect(() => {
    let active = true, timer;
    const controller = new AbortController();
    async function refresh() {
      try {
        const result = await apiRequest(`/api/v1/control-room/displays?station_id=${encodeURIComponent(station)}`, { token, signal: controller.signal });
        if (active) { setData(result); setError(''); }
      } catch (cause) { if (active) setError(`Status unavailable: ${cause.message}`); }
      finally { if (active) timer = setTimeout(refresh, 1000); }
    }
    refresh();
    return () => { active = false; clearTimeout(timer); controller.abort(); };
  }, [token, station]);
  const enabled = data.items.filter(d => d.enabled);
  const targets = enabled.filter(d => audience === 'ALL' || selected.includes(d.id));
  useEffect(() => {
    onRouting({ audience, display_ids: audience === 'SELECTED' ? targets.map(d => d.id) : [],
      expected_routes: Object.fromEntries(targets.map(d => [d.id, d.route_revision])), emergency, reason,
      ready: targets.length > 0 && !!reason.trim() && !error });
  }, [data, selected, audience, emergency, reason, error, onRouting]);
  async function stop() {
    const body = retry ?? { request_id: crypto.randomUUID(), station_id: station,
      display_ids: targets.map(d => d.id), expected_routes: Object.fromEntries(targets.map(d => [d.id, d.route_revision])), reason };
    setRetry(body); setPending(true); setNotice('');
    try {
      await apiRequest('/api/v1/control-room/stop', { token, body });
      setRetry(null); setNotice('Stop recorded. Displays stop at the next safe animation boundary.');
    } catch (cause) { setNotice(cause.message); }
    finally { setPending(false); }
  }
  async function platform(device, value) {
    setPending(true);
    try {
      await apiRequest(`/api/v1/control-room/displays/${device.id}/platform`, { token,
        body: { platform: value || null, expected_revision: device.revision, reason } });
      setNotice('Platform assignment updated. Announcement text is unchanged.');
    } catch (cause) { setNotice(cause.message); }
    finally { setPending(false); }
  }
  return <section className="panel management-section control-room" aria-label="Control room">
    <SectionTitle icon="monitor" title="Control room" description="Assign independent announcements or broadcast to every enabled display at this station. Status refreshes every second." />
    {error && <p role="alert">{error} Display statuses below may be stale.</p>}
    <div className="actions">
      <label><input type="radio" name="audience" checked={audience === 'SELECTED'} onChange={() => setAudience('SELECTED')} />Selected displays</label>
      <label><input type="radio" name="audience" checked={audience === 'ALL'} onChange={() => setAudience('ALL')} />Broadcast to all displays</label>
      <label><input type="checkbox" checked={emergency} onChange={e => setEmergency(e.target.checked)} />Emergency priority</label>
    </div>
    <label>Dispatch reason<input value={reason} maxLength={4000} onChange={e => setReason(e.target.value)} /></label>
    <p>{targets.length} display(s) targeted. Enter text or record voice below, then select Play announcement. Sending replaces their current assignments; other displays are unaffected. Offline displays receive the latest unexpired assignment on reconnect.</p>
    <div className="table-scroll"><table><thead><tr><th>Select</th><th>Display / platform</th><th>Connection</th><th>Assigned announcement</th><th>Playback</th></tr></thead><tbody>
      {data.items.map(d => <tr key={d.id} data-display-id={d.id}>
        <td><input type="checkbox" aria-label={`Select ${d.name}`} disabled={!d.enabled || pending} checked={audience === 'ALL' ? d.enabled : selected.includes(d.id)} onChange={e => { setAudience('SELECTED'); setSelected(current => e.target.checked ? [...new Set([...current, d.id])] : current.filter(id => id !== d.id)); }} /></td>
        <td><strong>{d.name}</strong><select aria-label={`Platform for ${d.name}`} value={d.platform || ''} disabled={pending || !reason.trim()} onChange={e => platform(d, e.target.value)}><option value="">Station / common area</option>{platforms.map(p => <option key={p}>{p}</option>)}</select></td>
        <td><StatusBadge value={!d.enabled ? 'DISABLED' : d.online ? 'ONLINE' : 'OFFLINE'} /><small>{d.last_seen ? new Date(d.last_seen).toLocaleTimeString() : 'Never connected'}</small></td>
        <td>{d.caption || (d.playback === 'LEGACY' ? 'No individual assignment yet' : 'No current announcement')}</td>
        <td><StatusBadge value={d.playback} />{d.error_code && <p>{d.error_code}</p>}</td>
      </tr>)}
      {!data.items.length && <tr><td colSpan={5}>No displays registered. Add their identities in Library &amp; operations → Displays.</td></tr>}
    </tbody></table></div>
    <div className="actions"><button className="danger" disabled={pending || !targets.length || !reason.trim() || !!error} onClick={stop}>{retry ? 'Retry same stop command' : 'Stop selected displays'}</button>{retry && <button onClick={() => setRetry(null)}>Discard stop retry</button>}</div>
    {notice && <p role="status">{notice}</p>}
    <details><summary>Display assignment history ({data.history.length} recent records)</summary><ol className="assignment-history">{data.history.map(h => <li key={h.id}><strong>{h.name}: {h.action}</strong> — {h.caption || 'Stopped'}<br /><small>{new Date(h.created_at).toLocaleString()} · {h.actor} · {h.reason}</small></li>)}</ol></details>
  </section>;
}
