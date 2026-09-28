/** An editable draft built from actual catalog IDs, never fabricated motion IDs. */
export function arrivalExample(concepts, avatarProfileId) {
  const normalize = value => value.toLowerCase().replaceAll('_', ' ').replace(/\s+/gu, ' ').trim();
  const find = (...labels) => {
    const matches = concepts.filter(item => labels.some(label =>
      [item.canonical_text, item.gloss].some(value => normalize(value) === label)));
    if (matches.length !== 1) throw new Error(`Need one unambiguous library concept for ${labels[0]}. Found ${matches.length}.`);
    return matches[0].id;
  };
  const train = find('train'), platform = find('platform'), arrive = find('arrive');
  const now = find('now right now'), zero = find('zero', '0', '0 zero'), one = find('1 one', 'one', '1'), two = find('2 two', 'two', '2');
  return {
    retrieval_stage: 'TEMPLATE', source_text_language: 'en', intent: 'TRAIN_ARRIVAL',
    temporal_state: 'ARRIVING_NOW', polarity: 'POSITIVE', avatar_profile_id: avatarProfileId,
    slot_types: { train_identifier: 'TRAIN_IDENTIFIER', platform_identifier: 'PLATFORM_IDENTIFIER' }, fixed_slots: {},
    recipe: [
      { kind: 'CONCEPT', concept_id: train, covers: [], group: 'train' },
      { kind: 'SLOT', slot: 'train_identifier', group: 'train', policy: { mode: 'EXACT_VALUES', units: { '1201': [one, two, zero, one], '1202': [one, two, zero, two] } } },
      { kind: 'CONCEPT', concept_id: platform, covers: [], group: 'location' },
      { kind: 'SLOT', slot: 'platform_identifier', group: 'location', policy: { mode: 'EXACT_VALUES', units: { '1': [one], '2': [two] } } },
      { kind: 'CONCEPT', concept_id: now, covers: ['temporal_state'], group: 'event' },
      { kind: 'CONCEPT', concept_id: arrive, covers: ['intent', 'polarity'], group: 'event' },
    ], transition_policy: 'FULL_CLIP_CUT', safe_after_groups: ['train', 'location', 'event'],
  };
}
