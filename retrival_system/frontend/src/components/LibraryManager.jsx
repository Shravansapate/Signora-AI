'use client';

import { useEffect, useRef, useState } from 'react';
import Icon, { SectionTitle } from './Icon.jsx';
import AvatarViewer from './AvatarViewer.jsx';
import { Feedback, Field, JsonDetails, Pager, readable, time, useWorkspace } from './workspace.jsx';

export default function LibraryManager({ token, admin }) {
  const work = useWorkspace(token), viewer = useRef(null);
  const [items, setItems] = useState([]), [query, setQuery] = useState(''), [offset, setOffset] = useState(0);
  const [detail, setDetail] = useState(null), [impact, setImpact] = useState([]), [avatars, setAvatars] = useState([]);
  const [candidateId, setCandidateId] = useState(''), [preview, setPreview] = useState(null), [snapshot, setSnapshot] = useState({ state: 'IDLE' });
  const [playerSession, setPlayerSession] = useState(0), [reason, setReason] = useState(''), [evidence, setEvidence] = useState('');
  const [confirmed, setConfirmed] = useState(false), [linguistic, setLinguistic] = useState('APPROVED'), [composition, setComposition] = useState('PENDING');
  const [meaning, setMeaning] = useState(''), [context, setContext] = useState(''), [domain, setDomain] = useState('');
  const [meaningDecision, setMeaningDecision] = useState('APPROVED');
  const [metadata, setMetadata] = useState(null), [motion, setMotion] = useState(null), [uploadNew, setUploadNew] = useState(true);
  const [cleanup, setCleanup] = useState(null);
  const [metadataEdit, setMetadataEdit] = useState(null), [metadataHistory, setMetadataHistory] = useState(null);
  const [metadataText, setMetadataText] = useState(null), [libraryAlias, setLibraryAlias] = useState('');
  const [deleteVisible, setDeleteVisible] = useState(false), [deleteName, setDeleteName] = useState(''), [deleteReason, setDeleteReason] = useState('');
  const [alias, setAlias] = useState(''), [aliasDecision, setAliasDecision] = useState('APPROVED');
  const [description, setDescription] = useState(''), [sense, setSense] = useState('');
  const concept = detail?.concept, candidate = detail?.versions.find(row => row.id === candidateId);
  const selectedId = detail?.development_mode && detail?.library_selection ? detail.library_selection.motion_version_id : concept?.active_motion_version_id;
  const libraryEnabled = detail?.development_mode && detail?.library_selection ? detail.library_selection.enabled : concept?.enabled;
  const avatar = avatars.find(row => row.id === candidate?.avatar_profile_id);
  const busy = !!work.busy || ['PLAYING', 'PRELOADING'].includes(snapshot.state);
  const hasReason = reason.trim() && confirmed;
  const reviewedPreview = snapshot.state === 'COMPLETE' && preview?.items.some(row => row.motion_version_id === candidateId);
  function clearPreview() { viewer.current?.cancel(); setPreview(null); setConfirmed(false); setSnapshot({ state: 'IDLE' }); }
  const search = (page = 0) => work.run('Searching library', async api => {
    const result = await api(`/api/v1/review/signs?q=${encodeURIComponent(query)}&offset=${page}`);
    setItems(result.items); setOffset(page);
  });
  useEffect(() => { search(); }, []);
  async function load(api, id, chosen) {
    const [data, affected, profiles] = await Promise.all([
      api(`/api/v1/review/signs/${id}`), api(`/api/v1/review/signs/${id}/impact`), api('/api/v1/review/avatars'),
    ]);
    clearPreview(); setDetail(data); setImpact(affected.items); setAvatars(profiles.items);
    setCandidateId(chosen && data.versions.some(row => row.id === chosen) ? chosen : data.versions[0]?.id ?? '');
    setMeaning(data.concept.meaning || ''); setContext(data.concept.context || ''); setDomain(data.concept.domain || '');
    setDescription(data.retrieval_profile?.description || ''); setSense(data.retrieval_profile ? JSON.stringify(data.retrieval_profile.sense, null, 2) : '');
    setDeleteVisible(false); setDeleteName(''); setDeleteReason(''); setMetadataHistory(null); setMetadataEdit(null);
    setMetadataText(null); setLibraryAlias('');
  }
  function select(id) {
    clearPreview(); setPlayerSession(value => value+1); setReason(''); setEvidence(''); setCleanup(null);
    work.run('Loading exact versions and impact', api => load(api, id));
  }
  async function prepare(versionId) {
    clearPreview();
    await work.run('Preparing exact-version preview', async api => {
      if (!avatar?.canonical_motion_version_id) throw new Error('The canonical avatar source is unavailable.');
      const result = await api('/api/v1/review/prepare', { body: { avatar_motion_version_id: avatar.canonical_motion_version_id, motion_version_ids: [versionId] } });
      if (result.status !== 'READY') throw new Error((result.reasons || [result.status]).join('; '));
      setPreview(result.manifest);
      if (!await viewer.current.prepare(result.manifest)) throw new Error('The complete preview could not be prepared.');
    });
  }
  function change(action) {
    const version = candidate, captured = concept;
    const prefix = `/api/v1/admin/signs/${captured.id}`;
    const body = { expected_revision: captured.revision, reason };
    if (['activate', 'rollback'].includes(action)) Object.assign(body, { motion_version_id: version.id, expected_active_motion_version_id: captured.active_motion_version_id });
    const path = ['revoke', 'archive', 'restore'].includes(action) ? `${prefix}/motions/${version.id}/${action}` : `${prefix}/${action}`;
    work.run('Applying version change', async api => {
      const result = await api(path, { body, ...(action === 'delete' ? { method: 'DELETE' } : {}) });
      if (result.cleanup_job_id) setCleanup({ id: result.cleanup_job_id });
      await load(api, captured.id, version.id);
      setItems((await api(`/api/v1/review/signs?q=${encodeURIComponent(query)}&offset=${offset}`)).items);
    }, 'Version change recorded. History and affected templates refreshed.');
  }
  function reviewMotion() {
    work.run('Recording exact-version review', async api => {
      await api(`/api/v1/review/signs/${concept.id}/motions/${candidate.id}`, { body: {
        expected_revision: concept.revision, sha256: candidate.sha256, avatar_profile_id: candidate.avatar_profile_id,
        semantic_revision: concept.semantic_revision, linguistic, composition, composition_scope: 'EXACT_CONTENT',
        preview_manifest_id: preview.manifest_id, rendered_review_confirmed: true, evidence, reason,
      } });
      await load(api, concept.id, candidate.id);
    }, 'Exact-version review recorded. Activation remains a separate admin action.');
  }
  function reviewMeaning() {
    work.run('Recording meaning review', async api => {
      await api(`/api/v1/review/signs/${concept.id}/meaning`, { body: { expected_revision: concept.revision, meaning, context, domain, decision: meaningDecision, evidence, reason } });
      await load(api, concept.id, candidateId);
    }, 'Meaning review recorded. Motion and construction bindings must match the resulting semantic revision.');
  }
  function reviewAvatar(decision) {
    work.run('Recording canonical avatar review', async api => {
      await api(`/api/v1/review/avatars/${avatar.id}`, { body: { expected_revision: avatar.revision, sha256: avatar.source_sha256,
        rig_fingerprint: avatar.rig_fingerprint, decision, evidence, reason } });
      await load(api, concept.id, candidateId);
    }, 'Avatar review recorded.');
  }
  function upload() {
    work.run('Validating uploaded metadata and GLB', async api => {
      if (!metadata || !motion) throw new Error('Choose both the GLB and its matching metadata JSON. A GLB alone does not describe its meaning.');
      if (motion.size > 134217728 || metadata.size > 33554432) throw new Error('Maximum upload: GLB 128 MiB; metadata 32 MiB.');
      const form = new FormData(); form.append('metadata', metadata); form.append('motion', motion);
      if (uploadNew) form.append('new_concept_only', 'true');
      const result = await api(uploadNew ? '/api/v1/admin/motions/stage' : `/api/v1/admin/signs/${concept.id}/motions`, { body: form, timeoutMs: 180000 });
      setPlayerSession(value => value+1); await load(api, result.concept_id, result.motion_version_id);
      setItems((await api(`/api/v1/review/signs?q=${encodeURIComponent(query)}&offset=${offset}`)).items);
    }, 'Upload validated and staged. Existing active content is preserved.');
  }
  function deleteContent() {
    const version = candidate, captured = concept;
    if (deleteName.trim() !== captured.semantic_key || !deleteReason.trim()) return;
    clearPreview();
    work.run('Deleting selected GLB and metadata', async api => {
      const result = await api(`/api/v1/admin/signs/${captured.id}/motions/${version.id}`, { method: 'DELETE', body: {
        expected_revision: captured.revision, confirm_sha256: version.sha256, reason: deleteReason,
        purge_metadata: true, development_reset: detail.development_mode,
      } });
      setCleanup({ id: result.cleanup_job_id, state: 'QUEUED' });
      await load(api, captured.id, version.id); setUploadNew(false);
      setItems((await api(`/api/v1/review/signs?q=${encodeURIComponent(query)}&offset=${offset}`)).items);
      for (let attempt = 0; attempt < 20; attempt++) {
        const job = await api(`/api/v1/admin/cleanup/${result.cleanup_job_id}`); setCleanup(job);
        if (['COMPLETE', 'SHARED', 'FAILED'].includes(job.state)) break;
        await new Promise(resolve => setTimeout(resolve, 500));
      }
    }, 'Version and metadata deleted. Check physical cleanup below, then upload the matching files to add it again.');
  }
  function downloadMetadata() {
    work.run('Loading registered metadata', async api => {
      const result = await api(`/api/v1/review/signs/${concept.id}/motions/${candidate.id}/metadata`);
      setMetadataHistory(result.history);
      const url = URL.createObjectURL(new Blob([JSON.stringify(result.metadata, null, 2)], { type: 'application/json' }));
      const link = document.createElement('a'); link.href = url; link.download = `${concept.semantic_key}-v${candidate.version_no}.metadata.json`;
      link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
  }
  function saveMetadata() {
    work.run('Validating metadata update', async api => {
      let file = metadataEdit;
      if (metadataText !== null) {
        try { JSON.parse(metadataText); } catch { throw new Error('Correct the metadata JSON before saving.'); }
        file = new Blob([metadataText], { type: 'application/json' });
      }
      if (!file || file.size > 33554432) throw new Error('Choose metadata up to 32 MiB.');
      const form = new FormData(); form.append('metadata', file, 'metadata.json'); form.append('expected_revision', concept.revision); form.append('reason', reason);
      await api(`/api/v1/admin/signs/${concept.id}/motions/${candidate.id}/metadata`, { body: form, timeoutMs: 180000 });
      await load(api, concept.id, candidate.id);
      setItems((await api(`/api/v1/review/signs?q=${encodeURIComponent(query)}&offset=${offset}`)).items);
    }, 'Metadata revision saved. Semantic edits require reactivation; previous metadata and GLBs are retained.');
  }
  return <div className="management-section">
    <p><strong>Library</strong> contains available sign concepts and their GLB versions. <strong>Templates</strong> arrange signs into an announcement pattern; <strong>coverage</strong> checks its required signs and version bindings.</p>
    <p>To delete a GLB and its metadata, search for the sign (for example <strong>ISL_A_01</strong>), click its result, then choose <strong>Delete GLB + metadata</strong> next to the version selector.</p>
    <form className="panel actions search-form" onSubmit={e => { e.preventDefault(); search(); }}><Field label="Search library" placeholder="Find a concept by name or keyword" value={query} onChange={setQuery} maxLength={160} />
      <button className="primary" disabled={busy}><Icon name="search" />Search</button></form>
    <Feedback {...work} />
    <div className="management-grid"><aside className="panel library-results" aria-label="Library results"><SectionTitle icon="book" title="Concepts" description="Select a sign to manage its versions." />
      {!items.length && <p>No matching concepts.</p>}{items.map(row => <button key={row.id} className={concept?.id === row.id ? 'selected' : ''} disabled={busy} onClick={() => select(row.id)}>
        <strong><Icon name="file" />{row.gloss}</strong><span>{row.eligible ? 'Production eligible' : row.library_enabled ? 'Library selection enabled' : row.library_enabled === false ? 'Library inactive' : row.meaning_status === 'PENDING' ? 'Meaning review pending' : 'Unavailable'} · {row.level}</span></button>)}
      <Pager offset={offset} count={items.length} onChange={search} busy={busy} />
    </aside><div>
      {concept ? <section className="panel management-section" aria-label="Concept and version details"><div className="panel-heading"><h2>{concept.gloss}</h2>
        <button disabled={busy} onClick={() => work.run('Refreshing concept', api => load(api, concept.id, candidateId))}>Refresh concept</button></div>
        <p>{concept.canonical_text} · {concept.meaning_status} · {libraryEnabled ? 'Enabled' : 'Inactive'} · revision {concept.revision}</p>
        {detail.development_mode && <p>Activation selects the version for development retrieval immediately. It does not grant production or ISL review approval.</p>}
        <p className="identifier">Concept: {concept.id}</p>
        <label className="form-field">Motion version<select aria-label="Motion version" value={candidateId} disabled={busy} onChange={e => { clearPreview(); setCandidateId(e.target.value); setDeleteVisible(false); setDeleteName(''); setMetadataHistory(null); setMetadataEdit(null); setMetadataText(null); }}>
          {detail.versions.map(row => <option key={row.id} value={row.id}>v{row.version_no} · {row.deleted_at ? 'DELETED' : row.lifecycle_status} · {row.id === selectedId ? 'selected for retrieval' : row.revoked_at ? 'revoked' : row.linguistic_review_status}</option>)}
        </select></label>
        {admin && candidate && <>
          <button className="danger-button" disabled={busy || !!candidate.deleted_at} onClick={() => { setDeleteVisible(value => !value); setDeleteName(''); }}><Icon name="trash" />Delete GLB + metadata</button>
          {candidate.deleted_at && <p role="status">This version's content was deleted. Select <strong>New GLB version for: {concept.gloss}</strong> below and upload the matching metadata and GLB again. It will receive a fresh version ID.</p>}
          {deleteVisible && <section className="panel management-section" aria-label="Delete selected content">
            <h3>Delete {concept.gloss} v{candidate.version_no}: GLB + metadata</h3>
            <p>{detail.development_mode ? 'This development action skips the retention wait and invalidates old test previews and development display sequences using this version. No separate archive step is needed.' : `Archive this version first. Production retention (${detail.deletion_retention_days} days) and reference checks apply.`}</p>
            <p>Removes this version's stored GLB, metadata JSON and metadata revisions. Canonical avatars and production references remain protected. Shared GLB bytes stay while another version uses them. Original files on your PC and minimal audit identifiers remain.</p>
            <Field label="Deletion reason" value={deleteReason} onChange={setDeleteReason} disabled={busy} maxLength={4000} />
            <Field label={`Type ${concept.semantic_key} to confirm deletion`} value={deleteName} onChange={setDeleteName} disabled={busy} />
            <div className="actions"><button className="danger-button" disabled={busy || deleteName.trim() !== concept.semantic_key || !deleteReason.trim()} onClick={deleteContent}>Permanently delete selected GLB + metadata</button>
              <button disabled={busy} onClick={() => setDeleteVisible(false)}>Cancel deletion</button></div>
          </section>}
          {cleanup && <p role="status">Managed GLB cleanup: {cleanup.state || 'QUEUED'}. {cleanup.state === 'COMPLETE' ? 'Stored file removed. You can upload it again below.' : cleanup.state === 'SHARED' ? 'Bytes are retained because another version still uses them.' : 'Use Physical cleanup below to refresh or retry.'}</p>}
        </>}
        {candidate && <><p>Technical: {candidate.technical_qc_status} · Linguistic: {candidate.linguistic_review_status} · Composition: {candidate.composition_review_status}</p>
          <p className="identifier">SHA-256: {candidate.sha256}</p><p>{candidate.clip_name} · {candidate.duration_seconds.toFixed(2)} seconds · {(candidate.size_bytes/1048576).toFixed(1)} MB</p>
          <JsonDetails label="Validation findings and binding report" value={candidate.technical_report} /><JsonDetails label="Protected references" value={candidate.references} />
          <div className="actions"><button disabled={busy || !!candidate.revoked_at || !!candidate.deleted_at} onClick={() => prepare(candidate.id)}>Preview selected version</button>
            <button disabled={busy || !selectedId} onClick={() => prepare(selectedId)}>Preview current active version</button>
            <button disabled={busy || !avatar?.canonical_motion_version_id} onClick={() => prepare(avatar.canonical_motion_version_id)}>Preview canonical avatar</button></div>
          <div className="viewer-stage"><AvatarViewer key={playerSession} ref={viewer} token={token} onState={setSnapshot} /></div>
          <p role="status">Review playback: {snapshot.state} · {snapshot.completed || 0} / {snapshot.total || 0}</p>
          <div className="actions"><button disabled={busy || !['READY','COMPLETE'].includes(snapshot.state)} onClick={() => viewer.current.start()}>Play review</button><button disabled={!['PRELOADING','PLAYING'].includes(snapshot.state)} onClick={() => { clearPreview(); }}>Stop review</button></div>
          {snapshot.error && <p role="alert">{snapshot.error.message}</p>}
          {preview && <p>Viewing {preview.items.map(row => `${row.semantic_key} v${row.version_no}`).join(', ')}</p>}
          <h3>Affected templates</h3>{!impact.length ? <p>No template dependencies.</p> : <ul>{impact.map(row => <li key={row.id}>{row.template_key} v{row.version_no} · {row.status} · {row.enabled ? 'enabled' : 'disabled'}
            {row.bindings[concept.id]?.motion_version_id !== candidate.id && ' · selected candidate is not bound by this review'}</li>)}</ul>}
          <p><a href="/admin?tab=templates">Open template review and coverage</a></p>
          <Field label="Change / review reason" value={reason} onChange={value => { setReason(value); setConfirmed(false); }} multiline rows={2} maxLength={4000} disabled={busy} />
          <Field label="Review evidence" value={evidence} onChange={value => { setEvidence(value); setConfirmed(false); }} multiline rows={3} maxLength={4000} disabled={busy} />
          <label className="confirmation"><input type="checkbox" checked={confirmed} disabled={busy} onChange={e => setConfirmed(e.target.checked)} />I checked the selected exact version, intended change, evidence and affected templates.</label>
          <fieldset disabled={busy}><legend>Exact-version motion review</legend>
            <label>Linguistic decision<select aria-label="Linguistic decision" value={linguistic} onChange={e => { setLinguistic(e.target.value); setConfirmed(false); }}><option>APPROVED</option><option>REJECTED</option></select></label>
            <label>Composition for exact content<select aria-label="Composition for exact content" value={composition} onChange={e => { setComposition(e.target.value); setConfirmed(false); }}><option>PENDING</option><option>APPROVED</option><option>REJECTED</option></select></label>
            <button disabled={!hasReason || !evidence.trim() || !reviewedPreview} onClick={reviewMotion}>Record motion review</button>
            <p className="field-hint">Complete the selected version's rendered preview before recording review. Construction approval is separate.</p>
          </fieldset>
          {admin && <fieldset disabled={busy}><legend>Active version and retirement</legend><div className="actions">
            <button disabled={!hasReason || (candidate.id === selectedId && libraryEnabled) || !!candidate.revoked_at || !!candidate.deleted_at} onClick={() => change('activate')}>Activate selected version</button>
            <button disabled={!hasReason || candidate.lifecycle_status !== 'ARCHIVED' || !!candidate.revoked_at || !!candidate.deleted_at} onClick={() => change('rollback')}>Roll back to selected version</button>
            <button disabled={!hasReason || (!libraryEnabled && detail.library_selection)} onClick={() => change('deactivate')}>Deactivate concept</button>
            <button disabled={!hasReason || libraryEnabled || !selectedId} onClick={() => change('reactivate')}>Reactivate concept</button>
            <button disabled={!hasReason || candidate.lifecycle_status === 'ARCHIVED' || !!candidate.revoked_at || !!candidate.deleted_at} onClick={() => change('archive')}>Archive selected version</button>
            <button disabled={!hasReason || candidate.lifecycle_status !== 'ARCHIVED' || !!candidate.revoked_at || !!candidate.deleted_at} onClick={() => change('restore')}>Restore selected version</button>
            <button disabled={!hasReason || !!candidate.revoked_at || !!candidate.deleted_at} onClick={() => change('revoke')}>Revoke selected version</button>
          </div></fieldset>}
          <details><summary>Metadata file and revisions</summary>
            <button disabled={busy || !!candidate.deleted_at} onClick={downloadMetadata}>Download current metadata.json</button>
            {metadataHistory && <JsonDetails label="Metadata revision history" value={metadataHistory} />}
            {admin && <><p>Edit registered metadata here or upload an updated file. Keep motion_code, language, hashes, sizes and animation unchanged. Labels, aliases, meaning and context can change; semantic edits disable retrieval until reactivation.</p>
              <button disabled={busy || !!candidate.deleted_at} onClick={() => work.run('Loading metadata editor', async api => {
                const result = await api(`/api/v1/review/signs/${concept.id}/motions/${candidate.id}/metadata`);
                setMetadataText(JSON.stringify(result.metadata, null, 2)); setMetadataEdit(null); setMetadataHistory(result.history); setConfirmed(false);
              })}>Edit metadata here</button>
              {metadataText !== null && <Field label="Registered metadata JSON" value={metadataText} multiline rows={16} maxLength={33554432} disabled={busy}
                onChange={value => { setMetadataText(value); setConfirmed(false); }} />}
              <label>Updated metadata file<input type="file" accept=".json" disabled={busy || !!candidate.deleted_at} onChange={e => { setMetadataEdit(e.target.files[0] ?? null); setMetadataText(null); setConfirmed(false); }} /></label>
              <button disabled={busy || (metadataText === null && !metadataEdit) || !hasReason || !!candidate.deleted_at} onClick={saveMetadata}>Save metadata revision</button></>}
          </details>
          <details><summary>Canonical avatar approval · {avatar?.status || 'Unavailable'}</summary><p className="identifier">{avatar?.source_sha256}</p>
            <div className="actions">{['APPROVED','RETIRED'].map(decision => <button key={decision} disabled={busy || !hasReason || !evidence.trim() || snapshot.state !== 'COMPLETE' || preview?.items[0]?.motion_version_id !== avatar?.canonical_motion_version_id}
              onClick={() => reviewAvatar(decision)}>{decision === 'APPROVED' ? 'Approve canonical avatar' : 'Retire canonical avatar'}</button>)}</div></details>
        </>}
        <details><summary>Meaning review · semantic revision {concept.semantic_revision}</summary>
          {concept.enabled && <p>Deactivate the concept before changing its meaning.</p>}
          <Field label="Meaning" value={meaning} onChange={setMeaning} multiline rows={3} disabled={busy || concept.enabled} />
          <Field label="Context" value={context} onChange={setContext} multiline rows={2} disabled={busy || concept.enabled} />
          <Field label="Domain" value={domain} onChange={setDomain} disabled={busy || concept.enabled} />
          <label>Meaning decision<select aria-label="Meaning decision" value={meaningDecision} onChange={e => setMeaningDecision(e.target.value)} disabled={busy}><option>APPROVED</option><option>REJECTED</option></select></label>
          <button disabled={busy || concept.enabled || !hasReason || !evidence.trim() || !meaning.trim() || !context.trim() || !domain.trim()} onClick={reviewMeaning}>Record meaning review</button>
        </details>
        <JsonDetails label="Aliases" value={detail.aliases} />
        {admin && <section className="management-section" aria-label="Library vocabulary"><h3>Library vocabulary</h3>
          <p>Add another word or phrase for this concept. New aliases remain pending production review. Removing an alias stops its use in new lookups immediately and preserves audit history.</p>
          <Field label="New library alias" value={libraryAlias} maxLength={2048} disabled={busy} onChange={value => { setLibraryAlias(value); setConfirmed(false); }} />
          <button disabled={busy || !hasReason || !libraryAlias.trim()} onClick={() => work.run('Adding library alias', async api => {
            await api(`/api/v1/admin/signs/${concept.id}/aliases`, { body: { expected_revision: concept.revision, alias: libraryAlias, reason } });
            await load(api, concept.id, candidateId);
          }, 'Library alias added. Production use requires alias review.')}>Add library alias</button>
          <ul>{detail.aliases.filter(row => row.review_status !== 'REJECTED').map(row => <li key={row.id}>{row.alias} ({row.review_status}) <button disabled={busy || !hasReason} onClick={() => work.run('Removing library alias', async api => {
            await api(`/api/v1/admin/signs/${concept.id}/aliases/${row.id}`, { method: 'DELETE', body: { expected_revision: concept.revision, reason } });
            await load(api, concept.id, candidateId);
          }, 'Alias removed from retrieval. Audit history retained.')}>Remove alias {row.alias}</button></li>)}</ul>
        </section>}
        <details><summary>Reviewed aliases and retrieval description</summary>
          <p>Scope: {concept.domain || 'No reviewed domain'} · {concept.context || 'No reviewed context'}. Approximate search remains candidate assistance.</p>
          <Field label="English alias" value={alias} disabled={busy} onChange={value => { setAlias(value); setConfirmed(false); }} maxLength={2048} />
          <label>Alias decision<select aria-label="Alias decision" value={aliasDecision} disabled={busy} onChange={e => { setAliasDecision(e.target.value); setConfirmed(false); }}><option>APPROVED</option><option>REJECTED</option></select></label>
          {detail.retrieval_profile?.status !== 'APPROVED' && <p>Review the retrieval description and sense signature below before reviewing aliases.</p>}
          <button disabled={busy || !hasReason || !evidence.trim() || !alias.trim() || !concept.domain || !concept.context || detail.retrieval_profile?.status !== 'APPROVED'} onClick={() => work.run('Recording alias review', async api => {
            await api(`/api/v1/review/retrieval/concepts/${concept.id}/aliases`, { body: { expected_revision: concept.revision,
              alias, source_language: 'en', domain: concept.domain, context: concept.context, decision: aliasDecision, evidence, reason } });
            await load(api, concept.id, candidateId);
          }, 'Alias review recorded.')}>Record alias review</button>
          <Field label="Curated retrieval description" value={description} onChange={value => { setDescription(value); setConfirmed(false); }} multiline rows={3} disabled={busy} maxLength={1600} />
          <Field label="Sense signature JSON (semantic_class, polarity, temporal_state, literals)" value={sense} onChange={value => { setSense(value); setConfirmed(false); }} multiline rows={5} disabled={busy} />
          <button disabled={busy || !hasReason || !evidence.trim() || !description.trim() || !sense.trim() || !concept.domain || !concept.context} onClick={() => work.run('Reviewing retrieval profile', async api => {
            let parsed; try { parsed = JSON.parse(sense); } catch { throw new Error('The sense signature must be valid JSON.'); }
            await api(`/api/v1/review/retrieval/concepts/${concept.id}`, { body: { expected_revision: concept.revision,
              domain: concept.domain, context: concept.context, sense: parsed, description, decision: 'APPROVED', evidence, reason } });
            await load(api, concept.id, candidateId);
          }, 'Retrieval profile review recorded.')}>Approve retrieval profile</button>
          <button disabled={busy || detail.retrieval_profile?.status !== 'APPROVED'} onClick={() => work.run('Indexing reviewed retrieval description', async api => {
            const result = await api('/api/v1/review/retrieval/index', { body: { concept_ids: [concept.id] }, timeoutMs: 125000 });
            if (result.items?.some(item => item.status === 'PENDING')) throw new Error(result.items.map(item => item.code || item.status).join('; '));
          }, 'Reviewed retrieval indexing request completed.')}>Refresh reviewed embedding</button>
        </details>
        <details><summary>Version review history</summary>{detail.reviews.map(row => <article key={row.id}><strong>{row.entity_type} · {row.actor} · {time(row.created_at)}</strong><p>{row.reason}</p><p>{row.evidence}</p><JsonDetails label="Recorded decision" value={row.decision} /></article>)}</details>
      </section> : <section className="panel management-section"><SectionTitle icon="stack" title="Version management" description="Inspect exact versions, approvals and affected templates." /><div className="empty-state"><Icon name="file" /><strong>Select a concept</strong>Choose a sign from the list to view its GLB versions and metadata.</div></section>}
      {admin && <section className="panel management-section"><SectionTitle icon="upload" title="Add a concept or GLB version" /><p>Supply the matching metadata.json and motion.glb. GLB limit: 128 MiB; metadata limit: 32 MiB. Uploading never overwrites an older version; activate the selected version after validation.</p><label>Upload target<select aria-label="Upload target" disabled={busy} value={uploadNew ? 'new' : 'selected'} onChange={e => setUploadNew(e.target.value === 'new')}><option value="new">Completely new concept</option><option value="selected" disabled={!concept}>New GLB version for: {concept?.gloss}</option></select></label>
        <label className="form-field">Metadata file<input type="file" accept=".json" disabled={busy} onChange={e => setMetadata(e.target.files[0] ?? null)} /></label>
        <label className="form-field">GLB file<input type="file" accept=".glb" disabled={busy} onChange={e => setMotion(e.target.files[0] ?? null)} /></label>
        {motion && !metadata && <p role="status">Metadata is required. Choose the matching metadata JSON containing this motion's identity, meaning, animation and checksum. No files have been uploaded yet.</p>}
        <p>After upload, select and activate the validated version. Production activation also requires the recorded meaning, avatar and exact-version approvals. New requests use committed changes immediately; no server restart is needed.</p>
        <button disabled={busy || !metadata || !motion || (!uploadNew && !concept)} onClick={upload}>Validate and stage upload</button>
      </section>}
      {cleanup && <section className="panel management-section"><h2>Physical cleanup</h2><p>{cleanup.id} · {readable(cleanup.state)}</p><p>{cleanup.error_code}</p><div className="actions">
        <button disabled={busy} onClick={() => work.run('Checking cleanup', async api => setCleanup(await api(`/api/v1/admin/cleanup/${cleanup.id}`)))}>Refresh cleanup</button>
        <button disabled={busy || !hasReason || cleanup.state !== 'FAILED'} onClick={() => work.run('Retrying cleanup', async api => setCleanup(await api(`/api/v1/admin/cleanup/${cleanup.id}/retry`, { body: { expected_attempts: cleanup.attempts, reason } })), 'Cleanup retry queued.')}>Retry cleanup</button>
      </div></section>}
    </div></div>
  </div>;
}
