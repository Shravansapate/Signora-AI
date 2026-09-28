/** Basic setup uses numbered platforms; advanced definitions retain exact identifiers. */
export function numberedStationDefinition(name, count, existing = {}, replacePlatforms = true) {
  const total = Number(count), stationName = name.trim();
  if (!stationName || stationName.length > 160) throw new Error('Enter a station name (1–160 characters).');
  if (!String(count).trim() || !Number.isInteger(total) || total < 1 || total > 256) {
    throw new Error('Enter a whole platform count from 1 to 256.');
  }
  return { ...existing, name: stationName,
    platforms: !replacePlatforms && existing.platforms?.length ? existing.platforms
      : Array.from({ length: total }, (_, index) => String(index + 1)) };
}

export function suggestedStationId(name) {
  return name.trim().toUpperCase().replace(/[^A-Z0-9]+/gu, '_').replace(/^_|_$/gu, '').slice(0, 32);
}
