'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { apiRequest } from '../services/api.mjs';
import { usePathname } from 'next/navigation';
import Icon from './Icon.jsx';

export function useWorkspace(token) {
  const [busy, setBusy] = useState(''), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const active = useRef(null);
  useEffect(() => () => { active.current?.abort(); active.current = null; }, [token]);
  const run = useCallback(async (label, task, success = '') => {
    if (active.current) return;
    const controller = new AbortController(); active.current = controller;
    setBusy(label); setError(''); setNotice('');
    const call = async (path, options = {}) => {
      const value = await apiRequest(path, { ...options, token, signal: controller.signal });
      controller.signal.throwIfAborted(); return value;
    };
    try {
      const value = await task(call);
      if (!controller.signal.aborted) setNotice(success);
      return value;
    } catch (cause) {
      if (!controller.signal.aborted) setError(`${cause.message}${cause.requestId ? ` Request: ${cause.requestId}` : ''}`);
    } finally {
      if (active.current === controller) { active.current = null; setBusy(''); }
    }
  }, [token]);
  return { run, busy, error, notice };
}

export function Feedback({ busy, error, notice }) {
  return <>{busy && <p role="status">{busy}…</p>}{error && <p role="alert" className="notice error-notice">{error}</p>}
    {notice && <p role="status" className="notice success-notice">{notice}</p>}</>;
}
export function Field({ label, value, onChange, multiline = false, ...props }) {
  const Input = multiline ? 'textarea' : 'input';
  return <label className="form-field">{label}<Input aria-label={label} value={value} onChange={e => onChange(e.target.value)} {...props} /></label>;
}
export function JsonDetails({ label, value }) {
  return <details><summary>{label}</summary><pre className="json-details">{JSON.stringify(value, null, 2)}</pre></details>;
}
export function Pager({ offset, count, onChange, busy, size = 50 }) {
  return <div className="actions"><button disabled={busy || !offset} onClick={() => onChange(Math.max(0, offset-size))}>Previous page</button>
    <span>Page {Math.floor(offset/size)+1}</span><button disabled={busy || count < size} onClick={() => onChange(offset+size)}>Next page</button></div>;
}
export function Navigation({ identity }) {
  const pathname = usePathname();
  const links = [['/announcements', 'Announcements', 'megaphone'], ['/admin', 'Library & operations', 'book'], ['/', 'Content preview', 'monitor'], ['/display', 'Live display', 'play']];
  return <header className="topbar"><a className="brand" href="/" aria-label="Signora home">Signora<span className="brand-ai">AI</span></a><nav aria-label="Workspaces">
    {links.map(([href, label, icon]) => <a key={href} href={href} aria-current={pathname === href ? 'page' : undefined}><Icon name={icon} />{label}</a>)}
  </nav>{identity && <div className="profile-summary"><span className="profile-initials" aria-hidden="true">{identity.subject.split(/[-_ ]/u).map(word => word[0]).join('').slice(0, 2).toUpperCase()}</span><div><strong>{identity.subject}</strong><small>{identity.roles.join(', ')}</small></div></div>}</header>;
}

export const readable = value => String(value ?? '').replaceAll('_', ' ');
export const time = value => value ? new Date(value).toLocaleString() : 'Never';
