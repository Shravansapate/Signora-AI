import { SectionTitle } from './Icon.jsx';

const guidance = {
  stations: {
    title: 'About stations',
    description: 'A station represents a physical location with its own platforms, configuration and registered displays. The motion library is shared across stations.',
    fields: [['Station name', 'Use a clear, recognisable name, such as Nagpur.'], ['Number of platforms', 'Basic setup numbers platforms from 1 to the count entered. Use advanced configuration for custom identifiers.'], ['Station ID', 'A unique identifier used by announcements and display access.'], ['Change reason', 'Describe the change so it can be understood in audit history.']],
  },
  displays: {
    title: 'About displays',
    description: 'Each display is registered to a station. Use its display ID and separately configured display credential to connect the Live display page.',
    fields: [['Monitor station', 'Select a station, then refresh to load its registered displays.'], ['Connection and last seen', 'A fresh lease means the display has contacted the server recently.'], ['Backlog / playback', 'Pending messages and playback acknowledgements help you check delivery.'], ['Display identity', 'Use New display identity to create a UUID. The credential subject must match the configured display account.']],
  },
};
export default function WorkspaceGuidance({ section }) {
  const content = guidance[section];
  if (!content) return null;
  return <aside className="workspace-guidance" aria-label="Workspace guidance">
    <section className="panel"><SectionTitle icon="info" title={content.title} /><p>{content.description}</p></section>
    <section className="panel"><SectionTitle icon="book" title="Field guidelines" /><dl>{content.fields.map(([name, text]) => <div key={name}><dt>{name}</dt><dd>{text}</dd></div>)}</dl></section>
  </aside>;
}
