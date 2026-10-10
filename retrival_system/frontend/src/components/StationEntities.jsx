'use client';

import { Field } from './workspace.jsx';

export default function StationEntities({ kind, entries, disabled, onChange }) {
  const singular = kind === 'places' ? 'Place' : 'Train';
  function update(index, field, value) {
    onChange(entries.map((entry, i) => i === index ? { ...entry, [field]: value } : entry));
  }
  return <fieldset disabled={disabled} className="management-section"><legend>{kind === 'places' ? 'Station places' : 'Named trains'}</legend>
    <p>Names and aliases identify announcement entities. Add their verified signs separately in the Library. Changes take effect when you save the station.</p>
    {entries.map((entry, index) => <div className="panel management-section" key={index}>
      <Field label={`${singular} ${index + 1} ID`} value={entry.id} maxLength={64} onChange={value => update(index, 'id', value)} />
      <Field label={`${singular} ${index + 1} name`} value={entry.name} maxLength={160} onChange={value => update(index, 'name', value)} />
      <Field label={`${singular} ${index + 1} aliases (one per line)`} value={(entry.aliases || []).join('\n')} multiline rows={2} maxLength={5151}
        onChange={value => update(index, 'aliases', value.split('\n'))} />
      <button type="button" onClick={() => onChange(entries.filter((_, i) => i !== index))}>Remove {singular.toLowerCase()} {index + 1}</button>
    </div>)}
    <button type="button" disabled={disabled || entries.length >= 256} onClick={() => onChange([...entries, { id: '', name: '', aliases: [] }])}>Add {singular.toLowerCase()}</button>
  </fieldset>;
}
