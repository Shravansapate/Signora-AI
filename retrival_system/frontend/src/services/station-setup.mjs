/** Basic setup uses numbered platforms; advanced definitions retain exact identifiers. */
export function numberedStationDefinition(name, count, existing = {}, replacePlatforms = true) {
  const total = Number(count), stationName = name.trim();
  if (!stationName || stationName.length > 160) throw new Error('Enter a station name (1–160 characters).');
  if (!String(count).trim() || !Number.isInteger(total) || total < 1 || total > 256) {
    throw new Error('Enter a whole platform count from 1 to 256.');
  }
  const entities = {};
  for (const kind of ['places', 'trains']) {
    if (!(kind in existing)) continue;
    entities[kind] = (existing[kind] || []).map(entry => ({ ...entry,
      id: entry.id.trim(), name: entry.name.trim(), ...(entry.aliases ? { aliases: entry.aliases.map(value => value.trim()).filter(Boolean) } : {}) }));
    if (entities[kind].some(entry => !entry.id || !entry.name)) throw new Error(`Every ${kind === 'places' ? 'place' : 'train'} needs an ID and name.`);
  }
  return { ...existing, ...entities, name: stationName,
    platforms: !replacePlatforms && existing.platforms?.length ? existing.platforms
      : Array.from({ length: total }, (_, index) => String(index + 1)) };
}

export function suggestedStationId(name) {
  return name.trim().toUpperCase().replace(/[^A-Z0-9]+/gu, '_').replace(/^_|_$/gu, '').slice(0, 32);
}
