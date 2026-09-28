/** Local decorative line icons keep existing accessible control names intact. */
const paths = {
  book: 'M12 5C8 2 4 3 2 4v16c3-2 7-2 10 0 3-2 7-2 10 0V4c-2-1-6-2-10 1Zm0 0v15',
  monitor: 'M3 3h18v13H3zM12 16v5m-5 0h10',
  file: 'M5 2h9l5 5v15H5zM14 2v6h5M8 12h8m-8 4h6',
  upload: 'M7 17H5a4 4 0 0 1-1-8 7 7 0 0 1 13-3 5 5 0 0 1 2 11h-2M12 22V10m-4 4 4-4 4 4',
  database: 'M20 5c0 4-16 4-16 0s16-4 16 0ZM4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0',
  audit: 'M3 11a9 9 0 1 1 2 7M3 4v7h7m2-5v7l4 2',
  megaphone: 'M3 9v6h5l11 5V4L8 9H3Zm5 6 2 6H6l-2-6M22 9v6',
  lock: 'M5 10h14v12H5zM8 10V6a4 4 0 0 1 8 0v4m-4 5v3',
  link: 'm9 15 6-6M8 17l-2 2a4 4 0 0 1-5-5l5-5a4 4 0 0 1 5 0m2 6a4 4 0 0 0 5 0l5-5a4 4 0 0 0-5-5l-2 2',
  search: 'M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0Zm-2 5 7 7',
  stack: 'm12 2 10 5-10 5L2 7l10-5ZM2 12l10 5 10-5M2 17l10 5 10-5',
  info: 'M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0ZM12 11v6m0-11v1',
  keyboard: 'M2 5h20v14H2zM5 9h1m3 0h1m3 0h1m3 0h2M5 12h1m3 0h1m3 0h1m3 0h2M6 16h12',
  list: 'M9 5h13M9 12h13M9 19h13M2 5h1m-1 7h1m-1 7h1',
  mic: 'M9 4a3 3 0 0 1 6 0v8a3 3 0 0 1-6 0V4ZM5 10v2a7 7 0 0 0 14 0v-2m-7 9v3m-4 0h8',
  user: 'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM4 22v-3a8 8 0 0 1 16 0v3Z',
  play: 'm7 3 14 9-14 9V3Z', stop: 'M5 5h14v14H5z',
  arrow: 'M3 12h18m-6-6 6 6-6 6',
  location: 'M19 9c0 6-7 13-7 13S5 15 5 9a7 7 0 0 1 14 0ZM15 9a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z',
  refresh: 'M3 11a9 9 0 0 1 16-5l2 3M21 3v6h-6M21 13a9 9 0 0 1-16 5l-2-3M3 21v-6h6',
  trash: 'M3 6h18M9 6V2h6v4M5 6l1 16h12l1-16M10 10v8m4-8v8',
};
export default function Icon({ name, className = '' }) {
  return <svg className={`icon ${className}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d={paths[name] || paths.info} /></svg>;
}
export function SectionTitle({ icon, title, description }) {
  return <div className="section-title"><span className="icon-badge"><Icon name={icon} /></span><div><h2>{title}</h2>{description && <p>{description}</p>}</div></div>;
}
export function StatusBadge({ value }) {
  const tone = /^(READY|COMPLETE|CONNECTED|APPROVED|ACTIVE|PLAYING)$/u.test(value) ? 'positive' : /ERROR|FAIL|REJECT/u.test(value) ? 'negative' : /PENDING|REVIEW|QUEUED|LOADING/u.test(value) ? 'pending' : 'neutral';
  return <span className={`status-badge ${tone}`}><span aria-hidden="true" />{String(value).replaceAll('_', ' ')}</span>;
}
