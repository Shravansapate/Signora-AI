'use client';

import { useEffect, useRef, useState } from 'react';
import { SectionTitle, StatusBadge } from './Icon.jsx';
import AvatarViewer from './AvatarViewer.jsx';
import { Feedback, Field, JsonDetails, Pager, readable, useWorkspace } from './workspace.jsx';
import { arrivalExample } from '../services/template-example.mjs';

export default function TemplateManager({ token, admin }) {
  const work = useWorkspace(token), viewer = useRef(null), activePreview = useRef(null);
  const [items, setItems] = useState([]), [offset, setOffset] = useState(0), [selected, setSelected] = useState(null);
  const [stations, setStations] = useState([]), [station, setStation] = useState(''), [values, setValues] = useState({}), [date, setDate] = useState('');
  const [preview, setPreview] = useState(null), [snapshot, setSnapshot] = useState({ state: 'IDLE' }), [rendered, setRendered] = useState([]), [playerSession, setPlayerSession] = useState(0);
  const [reason, setReason] = useState(''), [evidence, setEvidence] = useState(''), [confirmed, setConfirmed] = useState(false), [decision, setDecision] = useState('APPROVED');
  const [key, setKey] = useState(''), [definition, setDefinition] = useState(''), [expected, setExpected] = useState(0);
  const busy = !!work.busy || ['PLAYING','PRELOADING'].includes(snapshot.state);
  const list = (page = 0) => work.run('Loading construction coverage', async api => {
    setItems((await api(`/api/v1/review/templates?offset=${page}`)).items); setOffset(page);
    const caps = await api('/api/v1/input/capabilities'); setStations(caps.stations); setStation(value => value || caps.stations[0]?.id || '');
  });
  useEffect(() => { list(); }, []);
  function clearPreview() { activePreview.current = null; viewer.current?.cancel(); setPreview(null); setConfirmed(false); }
  async function load(api, id) {
    const data = await api(`/api/v1/review/templates/${id}`);
    clearPreview(); setSelected(data); setValues({ ...data.definition.fixed_slots }); setRendered([]);
    setKey(data.template_key); setExpected(data.version_no); setDefinition(JSON.stringify(data.definition, null, 2));
  }
  function select(id) { setPlayerSession(value => value+1); work.run('Loading template dependencies', api => load(api, id)); }
  function prepare() {
    clearPreview();
    work.run('Preparing complete construction example', async api => {
      const result = await api(`/api/v1/review/templates/${selected.id}/prepare`, { body: {
        station_id: station, example: { intent: selected.definition.intent, temporal_state: selected.definition.temporal_state,
          polarity: selected.definition.polarity, slots: values }, ...(date ? { service_date: date } : {}),
      } });
      if (result.status !== 'READY') throw new Error((result.reasons || [result.status]).join('; '));
      activePreview.current = result.manifest; setPreview(result.manifest);
      if (!await viewer.current.prepare(result.manifest)) throw new Error('Construction preview preparation failed.');
    });
  }
  function playback(state) {
    setSnapshot(state);
    if (state.state === 'COMPLETE' && activePreview.current) {
      const plan = activePreview.current;
      setRendered(previous => previous.some(row => row.id === plan.manifest_id) ? previous : [...previous, { id: plan.manifest_id, caption: plan.caption_text }].slice(-16));
    }
  }
  function review() {
    work.run('Recording construction review', async api => {
      await api(`/api/v1/review/templates/${selected.id}`, { body: { expected_revision: selected.revision,
        definition_hash: selected.definition_hash, decision, preview_manifest_ids: rendered.map(row => row.id), rendered_review_confirmed: true, evidence, reason } });
      await load(api, selected.id);
      setItems((await api(`/api/v1/review/templates?offset=${offset}`)).items);
    }, 'Construction review recorded. Activation is a separate administrator action.');
  }
  function activate(enabled) {
    work.run('Updating construction availability', async api => {
      await api(`/api/v1/admin/templates/${selected.id}/activation`, { body: { expected_revision: selected.revision, enabled, reason } });
      await load(api, selected.id); setItems((await api(`/api/v1/review/templates?offset=${offset}`)).items);
    }, 'Construction availability updated.');
  }
  function loadArrivalExample() {
    work.run('Building a draft from your library', async api => {
      const concepts = [];
      for (let page = 0; ; page += 100) {
        const result = await api(`/api/v1/review/signs?limit=100&offset=${page}`);
        concepts.push(...result.items);
        if (result.items.length < 100) break;
      }
      const train = concepts.find(item => item.canonical_text.toLowerCase() === 'train');
      if (!train) throw new Error('The library needs a TRAIN concept.');
      const detail = await api(`/api/v1/review/signs/${train.id}`);
      const version = detail.versions.find(row => row.id === detail.library_selection?.motion_version_id || row.id === detail.concept.active_motion_version_id)
        ?? detail.versions.find(row => !row.deleted_at && !row.revoked_at);
      if (!version) throw new Error('TRAIN needs an available GLB version.');
      const example = arrivalExample(concepts, version.avatar_profile_id);
      clearPreview(); setSelected(null); setRendered([]); setExpected(0);
      setKey(`arrival-example-${Date.now()}`); setDefinition(JSON.stringify(example, null, 2));
      setReason('Stage an arrival example for learning template coverage');
    }, 'Draft loaded. Inspect the JSON, then click Stage template version. This does not approve or publish it.');
  }
  return <section className="panel management-section"><div className="panel-heading"><SectionTitle icon="file" title="Reviewed construction coverage" /><button disabled={busy} onClick={() => list(offset)}>Refresh templates</button></div>
    <p><strong>Library</strong> stores signs and GLB versions. A <strong>template</strong> is an ordered announcement recipe with variable slots. <strong>Coverage</strong> checks the required signs, allowed slot values, avatar compatibility and exact reviewed version bindings.</p>
    <details><summary>Try a template and understand coverage</summary>
      <p>The arrival example supports train 1201 or 1202 and platform 1 or 2: “Train 1201 is arriving at platform 2.” It references TRAIN, PLATFORM, NOW RIGHT NOW, ARRIVE and digits 0, 1, 2 from your actual library.</p>
      <p>Load the example below, stage it, then inspect Exact dependency bindings. Try train 1202 / platform 1. Train 9999 or platform 3 is outside this example's explicit values. A missing or unapproved dependency prevents strict construction preview; development announcement playback remains a separate path.</p>
      <p>Replacing a sign can preserve vocabulary coverage but invalidate the template's exact-version review. Refresh the template and inspect “selected version requires construction review.” Staging or loading an example never approves ISL or publishes an announcement.</p>
      {admin && <button disabled={busy} onClick={loadArrivalExample}>Load arrival example from library</button>}
    </details><Feedback {...work} />
    <div className="table-scroll"><table><thead><tr><th>Template</th><th>Meaning</th><th>Review / availability</th><th>Action</th></tr></thead><tbody>
      {!items.length && <tr><td colSpan={4}><div className="empty-state"><strong>No templates loaded</strong>Load an arrival example below or stage a construction definition.</div></td></tr>}
      {items.map(row => <tr key={row.id}><td>{row.template_key} v{row.version_no}</td><td>{readable(row.definition.intent)} · {readable(row.definition.temporal_state)} · {row.definition.polarity}</td>
        <td>{row.status} · {row.enabled ? 'Enabled' : 'Disabled'}</td><td><button disabled={busy} onClick={() => select(row.id)}>Review template</button></td></tr>)}
    </tbody></table></div><Pager offset={offset} count={items.length} onChange={list} busy={busy} />
    {selected && <section className="panel management-section"><h2>{selected.template_key} v{selected.version_no}</h2><p className="identifier">Definition SHA-256: {selected.definition_hash}</p>
      <JsonDetails label="Recipe, fixed values and safe semantic boundaries" value={selected.definition} />
      <h3>Exact dependency bindings</h3><ul>{selected.dependencies.map(row => <li key={row.id}>{row.gloss} · {row.eligible ? 'Catalog eligible' : 'Unavailable'}
        {selected.bindings[row.id]?.motion_version_id === row.active_motion_version_id && row.active_motion_version_id ? ' · selected version matches review' : ' · selected version requires construction review'}</li>)}</ul>
      <div className="management-grid"><div>
        <label className="form-field">Example station<select aria-label="Example station" value={station} disabled={busy} onChange={e => { clearPreview(); setStation(e.target.value); }}><option value="">Select station</option>{stations.map(row => <option key={row.id} value={row.id}>{row.definition.name}</option>)}</select></label>
        {Object.keys(selected.definition.slot_types).map(slot => <Field key={slot} label={`Example ${readable(slot).toLowerCase()}`} value={values[slot] || ''}
          disabled={busy || slot in selected.definition.fixed_slots} onChange={value => { clearPreview(); setValues(previous => ({ ...previous, [slot]: value })); }} />)}
        <Field label="Example service date" value={date} type="date" disabled={busy} onChange={value => { clearPreview(); setDate(value); }} />
        <button disabled={busy || !station || !Object.keys(selected.definition.slot_types).every(slot => values[slot]?.trim())} onClick={prepare}>Prepare construction example</button>
        <p className="field-hint">Review enough examples to cover every dependency in the recipe. The server verifies exact coverage and version bindings.</p>
      </div><div><div className="viewer-stage"><AvatarViewer key={playerSession} token={token} ref={viewer} onState={playback} /></div><p role="status">Construction playback: {snapshot.state}</p>
        <div className="actions"><button disabled={busy || !['READY','COMPLETE'].includes(snapshot.state)} onClick={() => viewer.current.start()}>Play construction review</button><button disabled={!['PLAYING','PRELOADING'].includes(snapshot.state)} onClick={clearPreview}>Stop construction review</button></div>
        {preview && <p>{preview.caption_text}</p>}{snapshot.error && <p role="alert">{snapshot.error.message}</p>}
      </div></div>
      <h3>Rendered examples retained for this review</h3><ul>{rendered.map(row => <li key={row.id}>{row.caption} <button disabled={busy} onClick={() => { setRendered(list => list.filter(item => item.id !== row.id)); setConfirmed(false); }}>Remove example</button></li>)}</ul>
      <Field label="Template review evidence" value={evidence} onChange={value => { setEvidence(value); setConfirmed(false); }} multiline rows={3} maxLength={4000} disabled={busy} />
      <Field label="Template change reason" value={reason} onChange={value => { setReason(value); setConfirmed(false); }} maxLength={4000} disabled={busy} />
      <label>Construction decision<select aria-label="Construction decision" value={decision} disabled={busy} onChange={e => { setDecision(e.target.value); setConfirmed(false); }}><option>APPROVED</option><option>REJECTED</option></select></label>
      <label className="confirmation"><input type="checkbox" checked={confirmed} disabled={busy} onChange={e => setConfirmed(e.target.checked)} />I reviewed the complete rendered examples, exact values, transitions and safe boundaries.</label>
      <div className="actions"><button disabled={busy || !confirmed || !reason.trim() || !evidence.trim() || !rendered.length} onClick={review}>Record construction review</button>
        {admin && <><button disabled={busy || !confirmed || !reason.trim() || selected.enabled} onClick={() => activate(true)}>Enable template</button>
          <button disabled={busy || !confirmed || !reason.trim() || !selected.enabled} onClick={() => activate(false)}>Disable template</button></>}
      </div>
    </section>}
    {admin && <section className="panel management-section"><SectionTitle icon="file" title="Stage a construction definition" /><p>Supply a complete definition from the approved content workflow, or edit a selected retained version. Staging does not approve or enable it.</p>
      <Field label="Template key" value={key} disabled={busy} onChange={value => { setKey(value); setExpected(0); }} maxLength={80} />
      <Field label="Expected latest version (0 for a new key)" value={expected} type="number" min={0} disabled={busy} onChange={value => setExpected(Number(value))} />
      <Field label="Construction definition JSON" value={definition} onChange={setDefinition} multiline rows={12} maxLength={30000} disabled={busy} />
      {!selected && <Field label="Template change reason" value={reason} onChange={setReason} disabled={busy} maxLength={4000} />}
      <button disabled={busy || !key || !definition.trim() || !reason.trim()} onClick={() => work.run('Staging construction version', async api => {
        let parsed; try { parsed = JSON.parse(definition); } catch { throw new Error('The construction definition is not valid JSON.'); }
        const result = await api('/api/v1/admin/templates', { body: { template_key: key, expected_version: expected, definition: parsed, reason } });
        await load(api, result.id); setItems((await api('/api/v1/review/templates')).items); setOffset(0);
      }, 'Construction staged for exact rendered review.')}>Stage template version</button>
    </section>}
  </section>;
}
