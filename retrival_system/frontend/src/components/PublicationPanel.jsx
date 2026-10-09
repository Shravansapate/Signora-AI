'use client';

import { useEffect, useRef, useState } from 'react';
import { Feedback, Field, Pager, readable, time, useWorkspace } from './workspace.jsx';

export default function PublicationPanel({ token, station, preview, routing, previewComplete, onCorrect, invalidatePreview, onBusyChange }) {
  const work = useWorkspace(token);
  const [messages, setMessages] = useState([]), [devices, setDevices] = useState([]), [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(null), [history, setHistory] = useState(null);
  const [reason, setReason] = useState(''), [confirmed, setConfirmed] = useState(false), [receipt, setReceipt] = useState(null);
  const source = useRef(crypto.randomUUID()), retry = useRef(null), lock = useRef(false);
  const manifestId = preview?.manifest_id;
  const awaitingRetry = !!retry.current;
  const targetsKey = JSON.stringify([routing?.audience, routing?.display_ids, routing?.expected_routes, routing?.emergency]);
  // Targets belong to a new publication. Withdrawal concerns the selected
  // announcement revision and must not lose confirmation when display polling updates.
  useEffect(() => { if (manifestId) setConfirmed(false); }, [targetsKey, manifestId]);
  useEffect(() => { onBusyChange?.(!!work.busy || awaitingRetry); return () => onBusyChange?.(false); }, [work.busy, awaitingRetry, onBusyChange]);
  useEffect(() => { setConfirmed(false); setReceipt(null); retry.current = null; }, [manifestId]);
  const refresh = (page = offset) => work.run('Refreshing station activity', async api => {
    const data = await api(`/api/v1/announcements?station_id=${encodeURIComponent(station)}&offset=${page}`);
    setMessages(data.items); setOffset(page);
    setDevices((await api(`/api/v1/operations/displays?station_id=${encodeURIComponent(station)}`)).items);
  });
  useEffect(() => { if (station) refresh(0); }, [station]); // Parent keys this component by station/session.
  async function choose(item) {
    if (lock.current) return;
    setSelected(item); setConfirmed(false); setReceipt(null); retry.current = null;
    invalidatePreview();
    if (item.caption_text) onCorrect(item.caption_text);
    await work.run('Loading announcement history', async api => setHistory(await api(`/api/v1/announcements/${item.message_id}`)));
  }
  function newAnnouncement() {
    setSelected(null); setHistory(null); setReceipt(null); setConfirmed(false); setReason(''); retry.current = null;
    source.current = crypto.randomUUID(); invalidatePreview();
  }
  async function publish(cancel = false) {
    if (lock.current) return;
    lock.current = true;
    // Keep the exact request after an ambiguous network failure so retries are idempotent.
    const path = selected ? `/api/v1/announcements/${selected.message_id}/revisions` : '/api/v1/announcements';
    const body = retry.current?.body ?? {
      station_id: station, source_event_id: selected?.source_event_id ?? source.current,
      source_revision: (selected?.source_revision ?? 0)+1, expected_revision: selected?.revision ?? 0,
      reason, ...(!cancel && routing?.ready ? { audience: routing.audience, display_ids: routing.display_ids, expected_routes: routing.expected_routes, emergency: routing.emergency } : {}), ...(cancel ? { cancel: true } : { preview_manifest_id: preview.manifest_id, preview_manifest_hash: preview.manifest_hash }),
    };
    retry.current = { path, body };
    try {
      await work.run(cancel ? 'Withdrawing announcement' : 'Publishing announcement', async api => {
        let result;
        try { result = await api(retry.current.path, { body: retry.current.body }); }
        catch (cause) {
          if (cause.status >= 400 && cause.status < 500 && cause.status !== 408) retry.current = null;
          throw cause;
        }
        setReceipt(result); retry.current = null; setConfirmed(false);
        setMessages((await api(`/api/v1/announcements?station_id=${encodeURIComponent(station)}`)).items); setOffset(0);
        setHistory(await api(`/api/v1/announcements/${result.message_id}`));
      }, cancel ? 'Announcement withdrawn from live delivery.' : 'Announcement published. Check display completion below.');
    } finally { lock.current = false; }
  }
  const validPreview = routing?.ready && preview?.purpose === 'ANNOUNCEMENT_PREVIEW' && preview.announcement.station_id === station;
  return <section className="panel management-section" aria-label="Publication and station activity">
    <h2>{selected ? `Correct announcement · revision ${selected.revision}` : 'Publish this announcement'}</h2>
    <p>{selected?.caption_text || (selected ? 'Withdrawn announcement' : '')} <button disabled={!!work.busy} onClick={newAnnouncement}>New announcement</button></p>
    <Field label="Publication reason" value={reason} maxLength={4000} onChange={value => { setReason(value); setConfirmed(false); retry.current = null; }} disabled={!!work.busy} />
    <label className="confirmation"><input type="checkbox" checked={confirmed} disabled={!!work.busy || !!receipt}
      onChange={e => setConfirmed(e.target.checked)} />I checked the current station, complete announcement and intended action.</label>
    <div className="actions"><button className="primary" disabled={!!work.busy || !!receipt || !!retry.current || !reason.trim() || !confirmed || !validPreview || !previewComplete}
      onClick={() => publish(false)}>{selected ? 'Publish correction' : 'Publish announcement'}</button>
      {selected && <button disabled={!!work.busy || !!receipt || !!retry.current || !confirmed || !reason.trim()} onClick={() => publish(true)}>Withdraw announcement</button>}
      {retry.current && !receipt && <button disabled={!!work.busy} onClick={() => publish(!!retry.current.body.cancel)}>Retry same publication request</button>}</div>
    {!previewComplete && <p className="field-hint">Play the complete private preview before publication. Withdrawal removes an event; it does not sign a train cancellation.</p>}
    {receipt && <p role="status">Recorded revision {receipt.revision} · {readable(receipt.state)} · {receipt.message_id}</p>}
    <Feedback {...work} />
    <div className="panel-heading"><h2>Station announcements</h2><button disabled={!!work.busy} onClick={() => refresh()}>Refresh station activity</button></div>
    {!messages.length && <p>No announcements recorded for this station.</p>}
    <div className="table-scroll"><table><thead><tr><th>Announcement</th><th>Revision / state</th><th>Validity</th><th>Action</th></tr></thead><tbody>
      {messages.map(item => <tr key={item.message_id}><td>{item.caption_text || 'Withdrawn announcement'}</td><td>{item.revision} · {item.current ? 'LIVE' : item.state === 'LIVE' ? 'EXPIRED' : item.state}</td>
        <td>{time(item.valid_until)}</td><td><button disabled={!!work.busy || !item.can_revise} onClick={() => choose(item)}>Review / correct</button></td></tr>)}
    </tbody></table></div><Pager offset={offset} count={messages.length} onChange={refresh} busy={!!work.busy} />
    {history && <details open><summary>Revision history</summary><ol>{history.revisions.map(row => <li key={row.revision}>Revision {row.revision}: {row.caption_text || row.state} · {row.actor} · {row.reason} · {time(row.created_at)}</li>)}</ol></details>}
    <h2>Display status</h2><p className="field-hint">Snapshot from the last refresh. An active connection does not establish playback completion.</p>
    {!devices.length && <p>No displays registered for this station.</p>}
    <div className="table-scroll"><table><thead><tr><th>Display</th><th>Connection / last seen</th><th>Pending</th><th>Last playback</th></tr></thead><tbody>
      {devices.map(item => <tr key={item.id}><td>{item.name}</td><td>{item.lease_current ? 'Fresh lease' : item.enabled ? 'Offline / stale' : 'Disabled'} · {time(item.last_seen)}</td>
        <td>{item.pending_messages} · receive lag {item.receive_lag}</td><td>{item.latest_delivery?.state || 'No playback reported'} {item.latest_delivery?.error_code}</td></tr>)}
    </tbody></table></div>
  </section>;
}
