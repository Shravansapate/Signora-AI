"""Versioned railway meaning and reviewed construction contracts."""

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from app.lifecycle_schema import ReviewText
from app.retrieval.playback_schema import Digest, PlaybackPlan, WireModel

StationId = Annotated[str, Field(pattern=r"^[A-Z0-9_-]{1,32}$")]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=64)]
Intent = Literal[
    "TRAIN_ARRIVAL", "TRAIN_DEPARTURE", "TRAIN_DELAY", "TRAIN_CANCELLATION", "PLATFORM_CHANGE"
]
Temporal = Literal[
    "ARRIVING_NOW",
    "ALREADY_ARRIVED",
    "SCHEDULED_ARRIVAL",
    "DEPARTING_NOW",
    "ALREADY_DEPARTED",
    "SCHEDULED_DEPARTURE",
    "DELAYED",
    "CANCELLED",
    "CURRENT_CHANGE",
]
Polarity = Literal["POSITIVE", "NEGATIVE"]
SlotName = Literal[
    "train_identifier",
    "platform_identifier",
    "old_platform",
    "new_platform",
    "clock_time",
    "delay_duration",
    "source",
    "destination",
    "train_name",
]
SlotType = Literal["TRAIN_IDENTIFIER", "PLATFORM_IDENTIFIER", "CLOCK_TIME", "DURATION", "NAME"]


class NamedEntity(WireModel):
    id: Identifier
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
    aliases: list[Annotated[str, StringConstraints(min_length=1, max_length=160)]] = Field(
        default_factory=list, max_length=32
    )


class DevelopmentGlossPolicy(WireModel):
    """Configurable domain construction, explicitly not a linguistic approval."""

    rule_id: str = "railway-domain-draft-1"
    order: list[
        Literal[
            "entity", "source", "destination", "location", "time", "polarity", "event", "duration"
        ]
    ] = Field(
        default_factory=lambda: [
            "entity",
            "source",
            "destination",
            "location",
            "time",
            "polarity",
            "event",
            "duration",
        ]
    )
    lexical_forms: dict[str, str] = Field(default_factory=lambda: {"NOW": "NOW RIGHT NOW"})

    @model_validator(mode="after")
    def complete_order(self):
        if len(self.order) != 8 or len(set(self.order)) != 8:
            raise ValueError("Every semantic group must appear exactly once")
        if any(not key.strip() or not value.strip() for key, value in self.lexical_forms.items()):
            raise ValueError("Gloss lexical mappings must be nonempty")
        return self


class StationDefinition(WireModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
    platforms: list[Annotated[str, Field(pattern=r"^[0-9]{1,6}[A-Z]{0,2}$")]] = Field(
        min_length=1, max_length=256
    )
    train_min_digits: int = Field(default=1, ge=1, le=12)
    train_max_digits: int = Field(default=12, ge=1, le=12)
    train_suffixes: list[Annotated[str, Field(pattern=r"^[A-Z]{1,2}$")]] = Field(
        default_factory=list, max_length=32
    )
    places: list[NamedEntity] = Field(default_factory=list, max_length=256)
    trains: list[NamedEntity] = Field(default_factory=list, max_length=256)
    timezone: Literal["Asia/Kolkata"] = "Asia/Kolkata"
    development_gloss: DevelopmentGlossPolicy = Field(default_factory=DevelopmentGlossPolicy)

    @model_validator(mode="after")
    def unique_values(self):
        if len(set(self.platforms)) != len(self.platforms):
            raise ValueError("Platform identifiers must be unique")
        if self.train_min_digits > self.train_max_digits:
            raise ValueError("Invalid train identifier length range")
        for entities in (self.places, self.trains):
            names = [" ".join(n.casefold().split()) for e in entities for n in [e.name, *e.aliases]]
            if len(set(e.id for e in entities)) != len(entities) or len(set(names)) != len(names):
                raise ValueError("Entity IDs and names/aliases must be unambiguous")
        return self


class StationWrite(WireModel):
    expected_revision: int = Field(ge=0)
    definition: StationDefinition
    reason: ReviewText


class Slot(WireModel):
    kind: SlotType
    raw: str = Field(max_length=256)
    value: str = Field(min_length=1, max_length=256)
    source_start: int | None = Field(default=None, ge=0)
    source_end: int | None = Field(default=None, ge=0)
    valid: bool = True


class Issue(WireModel):
    code: str
    detail: str
    slot: SlotName | None = None


class Meaning(WireModel):
    schema_version: Literal[1] = 1
    parser_version: Literal["railway-en-1"] = "railway-en-1"
    station_id: StationId
    source_text_language: Literal["en"] = "en"
    intent: Intent
    temporal_state: Temporal
    polarity: Polarity
    priority: Literal["P1", "P2"]
    slots: dict[SlotName, Slot]


class DevelopmentMeaning(Meaning):
    parser_version: Literal["railway-en-domain-2"] = "railway-en-domain-2"
    temporal_state: Temporal | Literal["ARRIVAL_UNSPECIFIED", "DEPARTURE_UNSPECIFIED"]
    relations: dict[str, str] = Field(default_factory=dict)
    event_form: str | None = None
    time_modifier: str | None = None


class StructuredMeaning(WireModel):
    intent: Intent
    temporal_state: Temporal
    polarity: Polarity = "POSITIVE"
    slots: dict[SlotName, Annotated[str, StringConstraints(min_length=1, max_length=256)]]


class AnnouncementInput(WireModel):
    request_id: UUID
    station_id: StationId
    input_type: Literal["TEXT", "VOICE", "STRUCTURED"]
    source_text_language: Literal["en"] = "en"
    text: str | None = Field(default=None, min_length=1, max_length=2048)
    structured: StructuredMeaning | None = None
    service_date: date | None = None
    valid_until: AwareDatetime | None = None
    transcript_id: UUID | None = None
    transcript_confirmed: bool = False
    demo_mode: bool = False

    @model_validator(mode="after")
    def adapter_contract(self):
        if self.input_type == "STRUCTURED":
            if self.structured is None or self.text is not None:
                raise ValueError("Structured input requires fields and generates its own caption")
        elif self.text is None or not self.text.strip() or self.structured is not None:
            raise ValueError("Text/voice input requires nonempty text")
        if self.input_type == "VOICE":
            if self.transcript_id is None:
                raise ValueError("Voice input requires a server-issued finalized transcript")
        elif self.transcript_id is not None or self.transcript_confirmed:
            raise ValueError("Only voice input can carry a transcript confirmation")
        return self


class RealizationPolicy(WireModel):
    mode: Literal["EXACT_VALUES", "IDENTIFIER_CHARACTERS", "ISL_FINGERSPELL"]
    units: dict[Annotated[str, StringConstraints(min_length=1, max_length=256)], list[UUID]] = (
        Field(min_length=1, max_length=128)
    )
    spellings: dict[Identifier, Annotated[str, Field(pattern=r"^[A-Z]{1,40}$")]] = Field(
        default_factory=dict, max_length=128
    )

    @model_validator(mode="after")
    def bounded_units(self):
        if any(not 1 <= len(v) <= 16 for v in self.units.values()):
            raise ValueError("Each realization needs 1-16 explicit concepts")
        if self.mode == "IDENTIFIER_CHARACTERS" and (
            not set("0123456789").issubset(self.units)
            or any(
                len(k) != 1 or k not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" for k in self.units
            )
        ):
            raise ValueError("Identifier policy requires ten digits and explicit optional suffixes")
        if self.mode == "ISL_FINGERSPELL" and set(self.units) != set("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
            raise ValueError("ISL fingerspelling requires an explicitly reviewed Latin alphabet")
        if (self.mode == "ISL_FINGERSPELL") != bool(self.spellings):
            raise ValueError("Only fingerspelling requires explicit reviewed entity spellings")
        return self


class ConceptStep(WireModel):
    kind: Literal["CONCEPT"]
    concept_id: UUID
    # Context signs (for example PLATFORM before its number) do not consume the
    # variable slot. Whole-recipe validation still requires every meaning once.
    covers: list[str] = Field(max_length=12)
    group: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")]


class SlotStep(WireModel):
    kind: Literal["SLOT"]
    slot: SlotName
    policy: RealizationPolicy
    group: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")]


class TemplateDefinition(WireModel):
    retrieval_stage: Literal["TEMPLATE", "EXACT", "PHRASE", "WORD"] = "TEMPLATE"
    source_text_language: Literal["en"] = "en"
    intent: Intent
    temporal_state: Temporal
    polarity: Polarity
    slot_types: dict[SlotName, SlotType]
    fixed_slots: dict[SlotName, str] = Field(default_factory=dict)
    avatar_profile_id: UUID
    recipe: list[Annotated[ConceptStep | SlotStep, Field(discriminator="kind")]] = Field(
        min_length=1, max_length=32
    )
    transition_policy: Literal["FULL_CLIP_CUT"] = "FULL_CLIP_CUT"
    safe_after_groups: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def coverage(self):
        if self.retrieval_stage == "EXACT" and (
            len(self.recipe) != 1
            or not isinstance(self.recipe[0], ConceptStep)
            or set(self.fixed_slots) != set(self.slot_types)
        ):
            raise ValueError(
                "Exact sentence constructions require one complete literal-bound concept"
            )
        required = {"intent", "polarity", "temporal_state", *self.slot_types}
        covered, groups, closed = [], [], set()
        for step in self.recipe:
            if groups and step.group != groups[-1]:
                closed.add(groups[-1])
            if step.group in closed:
                raise ValueError("A semantic group must be contiguous")
            groups.append(step.group)
            if isinstance(step, ConceptStep):
                covered.extend(step.covers)
                if any(a in self.slot_types and a not in self.fixed_slots for a in step.covers):
                    raise ValueError("Baked content must declare its exact literal slot values")
            else:
                covered.append(step.slot)
                kind = self.slot_types.get(step.slot)
                if step.slot in self.fixed_slots:
                    raise ValueError("Fixed values cannot be replaced by a variable slot")
                if step.policy.mode == "IDENTIFIER_CHARACTERS" and kind not in {
                    "TRAIN_IDENTIFIER",
                    "PLATFORM_IDENTIFIER",
                }:
                    raise ValueError(
                        "Identifier spelling cannot realize quantities, clocks or names"
                    )
                if step.policy.mode == "ISL_FINGERSPELL" and kind != "NAME":
                    raise ValueError("Fingerspelling is restricted to reviewed name slots")
        if set(covered) != required or len(covered) != len(required):
            raise ValueError("The recipe must cover every meaning element exactly once")
        if not set(self.fixed_slots).issubset(self.slot_types):
            raise ValueError("Unknown fixed slot")
        if not set(self.safe_after_groups).issubset(groups):
            raise ValueError("Safe boundaries must name complete semantic groups")
        return self


class TemplateCreate(WireModel):
    template_key: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")]
    expected_version: int = Field(ge=0)
    definition: TemplateDefinition
    reason: ReviewText


class TemplateReview(WireModel):
    expected_revision: int = Field(ge=1)
    definition_hash: Digest
    decision: Literal["APPROVED", "REJECTED"]
    preview_manifest_ids: list[UUID] = Field(min_length=1, max_length=16)
    rendered_review_confirmed: Literal[True]
    evidence: ReviewText
    reason: ReviewText


class TemplateActivation(WireModel):
    expected_revision: int = Field(ge=1)
    enabled: bool
    reason: ReviewText


class TemplatePreviewRequest(WireModel):
    station_id: StationId
    example: StructuredMeaning
    service_date: date | None = None


class TemplateContext(WireModel):
    template_version_id: UUID
    template_revision: int = Field(ge=1)
    definition_hash: Digest
    review_id: UUID
    input_id: UUID
    station_id: StationId
    station_revision: int = Field(ge=1)
    meaning: Meaning
    meaning_hash: Digest


class AnnouncementPlan(PlaybackPlan):
    schema_version: Literal[3] = 3
    purpose: Literal["ANNOUNCEMENT_PREVIEW"] = "ANNOUNCEMENT_PREVIEW"
    announcement: TemplateContext


class TranslateResult(WireModel):
    input_id: UUID
    status: Literal[
        "READY", "NEEDS_CONFIRMATION", "NEEDS_REVIEW", "UNSUPPORTED", "ASSET_UNAVAILABLE"
    ]
    original_text: str
    normalized_text: str
    meaning: DevelopmentMeaning | Meaning | None = None
    meaning_hash: Digest | None = None
    issues: list[Issue] = Field(default_factory=list)
    # Demo mode returns a private CONTENT_REVIEW plan, never a publishable
    # announcement construction.
    manifest: AnnouncementPlan | PlaybackPlan | None = None
    demo_mode: bool = False
    retrieval: dict | None = None
