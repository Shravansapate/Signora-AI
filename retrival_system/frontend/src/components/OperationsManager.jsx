'use client';

import { useEffect, useRef, useState } from 'react';
import { Feedback, Field, JsonDetails, Pager, time, useWorkspace } from './workspace.jsx';
import Icon, { SectionTitle, StatusBadge } from './Icon.jsx';
import { numberedStationDefinition, suggestedStationId } from '../services/station-setup.mjs';
import StationEntities from './StationEntities.jsx';

export function ImportManager({ token }) {
  const work = useWorkspace(token), requestId = useRef(null);
  const [sources, setSources] = useState([]), [source, setSource] = useState(''), [jobs, setJobs] = useState([]), [offset, setOffset] = useState(0);
  const [job, setJob] = useState(null), [itemOffset, setItemOffset] = useState(0), [retry, setRetry] = useState(false);
  const list = (page = 0) => work.run('Loading imports', async api => {
    const configured = await api('/api/v1/admin/import-sources'); setSources(configured.items); setSource(value => value || configured.items[0] || '');
    setJobs((await api(`/api/v1/admin/imports?offset=${page}`)).items); setOffset(page);
  });
  useEffect(() => { list(); }, []);
  const inspect = (id, page = 0) => work.run('Loading import results', async api => {
    setJob(await api(`/api/v1/admin/imports/${id}?offset=${page}`)); setItemOffset(page);
  });
  return <section className="panel management-section"><SectionTitle icon="upload" title="Bulk imports and recovery" description="Import a configured dataset and follow each request through validation and registration." /><Feedback {...work} />
    <label className="form-field">Configured source<select aria-label="Configured source" value={source} disabled={!!work.busy} onChange={e => { setSource(e.target.value); requestId.current = null; }}><option value="">Select source</option>{sources.map(name => <option key={name}>{name}</option>)}</select></label>
    {!sources.length && <p>No import sources configured on the backend.</p>}
    <div className="actions"><button className="primary" disabled={!!work.busy || !source} onClick={() => work.run('Creating resumable import', async api => {
      requestId.current ??= crypto.randomUUID();
      const result = await api('/api/v1/admin/imports', { body: { source_alias: source, request_id: requestId.current } });
      setJob(await api(`/api/v1/admin/imports/${result.job_id}`)); setItemOffset(0);
      setJobs((await api('/api/v1/admin/imports')).items); setOffset(0);
    }, 'Import recorded. The existing backend worker processes queued items.')}><Icon name="upload" />Start / recover import request</button>
      <button disabled={!!work.busy} onClick={() => { requestId.current = null; list(offset); }}>Refresh imports / new request</button></div>
    <p>Registration preserves review states. A queued job needs the configured backend import worker; starting a job does not approve assets.</p>
    <div className="table-scroll"><table><thead><tr><th>Source</th><th>State</th><th>Updated</th><th>Results</th></tr></thead><tbody>{!jobs.length && <tr><td colSpan={4}><div className="empty-state"><Icon name="upload" /><strong>No imports to show</strong>Choose a configured source to start an import.</div></td></tr>}{jobs.map(row => <tr key={row.id}><td>{row.source_alias}</td><td><StatusBadge value={row.state} /></td><td>{time(row.updated_at)}</td><td><button disabled={!!work.busy} onClick={() => inspect(row.id)}>Inspect import</button></td></tr>)}</tbody></table></div>
    <Pager offset={offset} count={jobs.length} onChange={list} busy={!!work.busy} />
    {job && <section className="panel management-section"><h3>Import {job.source_alias} · {job.state}</h3><p className="identifier">{job.job_id}</p>
      <dl className="meaning-fields">{Object.entries(job.counts).map(([label, count]) => <div key={label}><dt>{label.replaceAll('_',' ')}</dt><dd>{count}</dd></div>)}</dl>
      <div className="actions"><button disabled={!!work.busy} onClick={() => inspect(job.job_id, itemOffset)}>Refresh import results</button>
        <button disabled={!!work.busy || job.state === 'PAUSED'} onClick={() => work.run('Pausing import', async api => { await api(`/api/v1/admin/imports/${job.job_id}/pause`, { body: {} }); setJob(await api(`/api/v1/admin/imports/${job.job_id}?offset=${itemOffset}`)); }, 'Import pause recorded.')}>Pause import</button>
        <label><input type="checkbox" checked={retry} disabled={!!work.busy} onChange={e => setRetry(e.target.checked)} />Retry failed items within server retry policy</label>
        <button disabled={!!work.busy} onClick={() => work.run('Resuming import', async api => { await api(`/api/v1/admin/imports/${job.job_id}/resume`, { body: { retry_failed: retry } }); setJob(await api(`/api/v1/admin/imports/${job.job_id}?offset=${itemOffset}`)); }, 'Import resume recorded.')}>Resume import</button>
      </div>
      <div className="table-scroll"><table><thead><tr><th>Source item</th><th>State / attempts</th><th>Finding / recovery</th></tr></thead><tbody>{job.items.map(row => <tr key={row.id}><td>{row.metadata_name}</td><td>{row.state} · {row.attempts}/{row.attempt_limit}</td><td>{row.error_code} {row.error_detail}{row.retryable && ' · Retry permitted'}</td></tr>)}</tbody></table></div>
      <Pager offset={itemOffset} count={job.items.length} size={100} onChange={page => inspect(job.job_id, page)} busy={!!work.busy} />
    </section>}
  </section>;
}

export function StationManager({ token }) {
  const work = useWorkspace(token);
  const [stations, setStations] = useState([]), [id, setId] = useState(''), [revision, setRevision] = useState(0), [definition, setDefinition] = useState('');
  const [reason, setReason] = useState(''), [confirmed, setConfirmed] = useState(false);
  const [name, setName] = useState(''), [count, setCount] = useState(''), [base, setBase] = useState({});
  const [idEdited, setIdEdited] = useState(false), [platformsEdited, setPlatformsEdited] = useState(true), [advanced, setAdvanced] = useState(false);
  const refresh = () => work.run('Loading station configuration', async api => setStations((await api('/api/v1/input/capabilities')).stations));
  useEffect(() => { refresh(); }, []);
  return <section className="panel management-section"><SectionTitle icon="location" title="Station setup" /><p>Start by entering the station name and number of platforms. Each station keeps its own configuration and display registrations. Manage the shared motion library through Library and Imports.</p><Feedback {...work} />
    <div className="actions"><button disabled={!!work.busy} onClick={refresh}><Icon name="refresh" />Refresh stations</button>{stations.map(row => <button key={row.id} disabled={!!work.busy} onClick={() => {
      setId(row.id); setIdEdited(true); setRevision(row.revision); setDefinition(JSON.stringify(row.definition,null,2)); setConfirmed(false);
      setName(row.definition.name); setCount(String(row.definition.platforms.length)); setBase(row.definition); setPlatformsEdited(false); setAdvanced(false);
    }}>{row.definition.name}</button>)}<button disabled={!!work.busy} onClick={() => {
      setId(''); setIdEdited(false); setRevision(0); setDefinition(''); setConfirmed(false); setName(''); setCount(''); setBase({}); setPlatformsEdited(true); setAdvanced(false);
    }}>New station</button></div>
    {!advanced && <><Field label="Station name" placeholder="Enter station name" value={name} disabled={!!work.busy} onChange={value => {
      setName(value); if (!idEdited && !revision) setId(suggestedStationId(value)); setConfirmed(false);
    }} maxLength={160} />
    <Field label="Number of platforms" value={count} type="number" min={1} max={256} step={1} disabled={!!work.busy} onChange={value => { setCount(value); setPlatformsEdited(true); setConfirmed(false); }} />
    <p>{!platformsEdited && base.platforms?.length ? `Current platform identifiers: ${base.platforms.join(', ')}.` : 'Platforms will be numbered from 1 to the count entered.'} For lettered platforms or different numbering, use advanced configuration.</p></>}
    <Field label="Station ID" value={id} disabled={!!work.busy || revision > 0} onChange={value => { setId(value.toUpperCase()); setIdEdited(true); setConfirmed(false); }} maxLength={32} />
    <p>The station ID identifies this installation in announcements and display access. It must be unique and use uppercase letters, digits, underscores or hyphens.</p>
    <label><input type="checkbox" checked={advanced} disabled={!!work.busy} onChange={e => {
      const enabled = e.target.checked;
      work.run('Changing configuration editor', async () => {
        if (enabled) setDefinition(JSON.stringify(numberedStationDefinition(name, count, base, platformsEdited), null, 2));
        else {
          let parsed; try { parsed = JSON.parse(definition); } catch { throw new Error('Correct the JSON before returning to basic setup.'); }
          if (typeof parsed.name !== 'string' || !Array.isArray(parsed.platforms) || !parsed.platforms.length) throw new Error('The definition needs a name and platform identifiers.');
          setBase(parsed); setName(parsed.name); setCount(String(parsed.platforms.length)); setPlatformsEdited(false);
        }
        setAdvanced(enabled); setConfirmed(false);
      });
    }} />Advanced station configuration</label>
    {advanced && <Field label="Station definition JSON" value={definition} disabled={!!work.busy} onChange={value => { setDefinition(value); setConfirmed(false); }} multiline rows={14} maxLength={30000} />}
    {!advanced && ['places', 'trains'].map(kind => <StationEntities key={kind} kind={kind} entries={base[kind] || []} disabled={!!work.busy}
      onChange={entries => { setBase(current => ({ ...current, [kind]: entries })); setConfirmed(false); }} />)}
    <Field label="Station change reason" placeholder="Enter the reason for this change" value={reason} disabled={!!work.busy} onChange={value => { setReason(value); setConfirmed(false); }} maxLength={4000} />
    <label className="confirmation"><input type="checkbox" checked={confirmed} disabled={!!work.busy} onChange={e => setConfirmed(e.target.checked)} />I checked the station definition. Updating it withdraws affected live announcements.</label>
    <button className="primary" disabled={!!work.busy || !confirmed || !id || !(advanced ? definition.trim() : name.trim() && count) || !reason.trim()} onClick={() => work.run('Saving station revision', async api => {
      let parsed;
      if (advanced) { try { parsed = JSON.parse(definition); } catch { throw new Error('Station definition must be valid JSON.'); } }
      else parsed = numberedStationDefinition(name, count, base, platformsEdited);
      const result = await api(`/api/v1/admin/stations/${encodeURIComponent(id)}`, { body: { expected_revision: revision, definition: parsed, reason } });
      const updated = (await api('/api/v1/input/capabilities')).stations;
      const saved = updated.find(row => row.id === id)?.definition ?? parsed;
      setRevision(result.revision); setConfirmed(false); setStations(updated); setBase(saved); setName(saved.name); setCount(String(saved.platforms.length)); setPlatformsEdited(false); setDefinition(JSON.stringify(saved,null,2));
    }, 'Station configuration recorded.')}>Save station configuration</button>
  </section>;
}

export function DisplayManager({ token }) {
  const work = useWorkspace(token);
  const [stations, setStations] = useState([]), [station, setStation] = useState(''), [devices, setDevices] = useState([]), [offset, setOffset] = useState(0);
  const [device, setDevice] = useState(null), [id, setId] = useState(''), [name, setName] = useState(''), [subject, setSubject] = useState(''), [enabled, setEnabled] = useState(true);
  const [confirmed, setConfirmed] = useState(false);
  const [credential, setCredential] = useState(null), [copied, setCopied] = useState(''), [showToken, setShowToken] = useState(false);
  const tokenInput = useRef(null);
  function clearCredential() { setCredential(null); setCopied(''); setShowToken(false); }
  function received(result) {
    const { access_token, ...record } = result;
    setDevice(record); setConfirmed(false);
    if (access_token) { setCredential({ token: access_token, expires: result.token_expires_at, id: result.id }); setCopied(''); setShowToken(false); }
  }
  useEffect(() => { work.run('Loading stations', async api => { const data = await api('/api/v1/input/capabilities'); setStations(data.stations); setStation(data.stations[0]?.id || ''); }); }, []);
  const list = (page = 0) => work.run('Refreshing display status', async api => { setDevices((await api(`/api/v1/operations/displays?station_id=${encodeURIComponent(station)}&offset=${page}`)).items); setOffset(page); });
  return <section className="panel management-section"><SectionTitle icon="monitor" title="Display monitoring and registration" description="Connect station displays with registered identities and monitor their delivery status." /><Feedback {...work} />
    <label className="form-field">Monitor station<select aria-label="Monitor station" value={station} disabled={!!work.busy} onChange={e => { clearCredential(); setStation(e.target.value); setDevices([]); setDevice(null); setId(''); setConfirmed(false); }}><option value="">Select station</option>{stations.map(row => <option key={row.id} value={row.id}>{row.definition.name}</option>)}</select></label>
    <button disabled={!!work.busy || !station} onClick={() => list()}><Icon name="refresh" />Refresh displays</button><p>Snapshot from the last refresh. Check last-seen, freshness, backlog and completed playback together.</p>
    <div className="table-scroll"><table><thead><tr><th>Display</th><th>Connection</th><th>Last seen</th><th>Backlog / playback</th><th>Details</th></tr></thead><tbody>{!devices.length && <tr><td colSpan={5}><div className="empty-state"><Icon name="monitor" /><strong>No displays loaded</strong>Refresh displays for the selected station, or register a display below.</div></td></tr>}{devices.map(row => <tr key={row.id}><td>{row.name}</td><td>{row.lease_current ? 'Fresh lease' : row.enabled ? 'Offline / stale' : 'Disabled'}</td><td>{time(row.last_seen)}</td><td>{row.pending_messages} pending · receive lag {row.receive_lag} · {row.latest_delivery?.state || 'No playback'}</td>
      <td><button disabled={!!work.busy} onClick={() => work.run('Loading device delivery history', async api => { clearCredential(); const data = await api(`/api/v1/admin/displays/${row.id}`); setDevice(data); setId(data.id); setName(data.name); setSubject(data.subject); setEnabled(data.enabled); setConfirmed(false); })}>Inspect display</button></td></tr>)}</tbody></table></div>
    <Pager offset={offset} count={devices.length} onChange={list} busy={!!work.busy} />
    {device && <JsonDetails label="Recorded delivery states and failures" value={device.deliveries} />}
    <section className="panel management-section"><h3>{device ? 'Update registered display' : 'Register display'}</h3>
      <button disabled={!!work.busy} onClick={() => { const nextId = crypto.randomUUID(); clearCredential(); setDevice(null); setId(nextId); setName(''); setSubject(`display_${nextId}`); setEnabled(true); setConfirmed(false); }}>New display identity</button>
      <Field label="Registered display ID" value={id} disabled={!!work.busy || !!device} onChange={setId} />
      <Field label="Display name" value={name} disabled={!!work.busy} onChange={value => { setName(value); setConfirmed(false); }} maxLength={160} />
      <Field label="Display identity" value={subject} disabled={!!work.busy || !!device} onChange={value => { setSubject(value); setConfirmed(false); }} maxLength={160} />
      <label><input type="checkbox" checked={enabled} disabled={!!work.busy} onChange={e => { setEnabled(e.target.checked); setConfirmed(false); }} />Display enabled</label>
      <p>New displays receive an access token immediately. No backend restart is needed. Copy it before leaving this page; if it is lost, inspect the display and generate a new token.</p>
      <label className="confirmation"><input type="checkbox" checked={confirmed} disabled={!!work.busy} onChange={e => setConfirmed(e.target.checked)} />I checked the station and device identity. Updating registration ends the existing display session.</label>
      <button className="primary" disabled={!!work.busy || !confirmed || !station || !id || !subject.trim() || !name.trim()} onClick={() => work.run('Saving display registration', async api => {
        const result = await api(`/api/v1/admin/displays/${encodeURIComponent(id)}`, { body: { station_id: station, subject, name, enabled, expected_revision: device?.revision ?? 0, issue_access_token: !device } });
        received(result); setDevices((await api(`/api/v1/operations/displays?station_id=${encodeURIComponent(station)}`)).items); setOffset(0);
      }, 'Display registration recorded.')}>Save display registration</button>
      {device && <><p>{device.token_expires_at ? `Access token expires ${time(device.token_expires_at)}.` : 'This display uses a separately configured credential. You can replace it with a token here.'}</p>
        <button className="danger" disabled={!!work.busy || !confirmed || !device.enabled} onClick={() => work.run('Replacing display access token', async api => {
          const result = await api(`/api/v1/admin/displays/${encodeURIComponent(id)}/token`, { body: { expected_revision: device.revision, reason: 'Administrator requested a replacement display access token' } });
          received(result);
        }, 'New display token created. Reconnect this display with the new token.')}>Generate new access token</button>
        <p>Replacing a token disconnects this display and invalidates its previous token.</p></>}
      {credential && <section className="notice" aria-label="New display credentials">
        <h3>Display access token ready</h3><p>Use this token with display ID <code>{credential.id}</code> on <a href="/display" target="_blank" rel="noreferrer">Live display</a>. Expires {time(credential.expires)}.</p>
        <label className="form-field">Display access token<input ref={tokenInput} type={showToken ? 'text' : 'password'} readOnly value={credential.token} autoComplete="off" spellCheck={false} /></label>
        <label><input type="checkbox" checked={showToken} onChange={e => setShowToken(e.target.checked)} />Show token</label>
        <button onClick={async () => { try { await navigator.clipboard.writeText(credential.token); setCopied('Token copied.'); } catch { tokenInput.current?.focus(); tokenInput.current?.select(); setCopied('Select the token and copy it manually.'); } }}>Copy access token</button>
        {copied && <p role="status">{copied}</p>}
        <p>This token is shown only after creation or replacement. It is not saved in browser storage.</p>
      </section>}
    </section>
  </section>;
}

export function AuditManager({ token }) {
  const work = useWorkspace(token), [items, setItems] = useState([]), [offset, setOffset] = useState(0);
  const list = (page = 0) => work.run('Loading audit history', async api => { setItems((await api(`/api/v1/admin/audit?limit=50&offset=${page}`)).items); setOffset(page); });
  useEffect(() => { list(); }, []);
  return <section className="panel management-section audit-panel"><div className="panel-heading"><SectionTitle icon="audit" title="Audit history" description="A chronological record of activity in this workspace." /><button disabled={!!work.busy} onClick={() => list(offset)}><Icon name="refresh" />Refresh audit</button></div><Feedback {...work} />
    {!items.length && !work.busy && <div className="empty-state"><Icon name="audit" /><strong>No activity to show</strong>Recorded operations will appear here.</div>}{items.map(row => <article className="audit-entry" key={row.id}><h3><StatusBadge value={row.action} /></h3><p>{row.actor} · {time(row.created_at)}</p><p className="identifier">Entity: {row.entity_id}</p><JsonDetails label="Recorded change" value={row.details} /></article>)}
    <Pager offset={offset} count={items.length} onChange={list} busy={!!work.busy} /></section>;
}
