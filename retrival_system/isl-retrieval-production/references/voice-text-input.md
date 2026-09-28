# Voice and typed-text input contract

Voice and typing are two input adapters for the same announcement compiler. Do not create separate retrieval logic for voice.

## Input paths

Supported operator-facing inputs:

1. Typed free text.
2. Structured operator form.
3. Push-to-talk voice input.
4. Authorized structured feed when available.

Recommended V1 voice flow:

microphone
-> audio capture
-> bounded audio preprocessing
-> optional voice activity detection
-> ASR
-> final transcript
-> transcript/critical-slot validation
-> common AnnouncementInput
-> normalization
-> intent/slot parsing
-> meaning compiler
-> existing template/retrieval pipeline.

Use push-to-talk for the first production pilot. Continuous always-listening publication is out of scope until independently evaluated.

## Common input envelope

Converge all adapters into one internal input contract, for example:

- input_type: TEXT | VOICE | STRUCTURED | FEED
- text/transcript
- source_text_language
- station/source identity when applicable
- source event/revision when applicable
- ASR metadata only for voice
- operator/device identity as required by current auth model

Downstream meaning/retrieval code should not branch on whether the text came from a keyboard or microphone except for provenance and review policy.

## Voice processing

Keep ASR behind an interface so the implementation can use the best working engine in the repository/environment. Faster-Whisper/Whisper is a practical baseline, not a mandatory dependency if an equivalent working engine already exists.

Normalize audio consistently before ASR. Prefer a bounded server format such as mono PCM/WAV at a model-compatible sample rate. Use FFmpeg or the existing audio utility. Optional VAD/noise processing must not fabricate speech or silently remove meaningful words.

Only finalized ASR text enters the announcement compiler. Partial streaming hypotheses may be shown in the UI but must not publish or trigger signing.

## Safety and confirmation

ASR confidence is not operational truth. Parse and validate critical values after transcription:

- train identifier
- platform identifier
- old/new platform direction
- clock time versus duration
- cancellation/arrival/departure status
- source/destination
- polarity/negation

If the transcript or parsed value is ambiguous, out of configured station range, or conflicts with structured context, return NEEDS_CONFIRMATION/NEEDS_REVIEW rather than guessing.

For V1 voice UX:

record
-> transcribe
-> show transcript and extracted critical fields
-> allow retry/correction
-> preview
-> publish.

Do not automatically publish an uncertain voice announcement.

## Language scope

Start with the text languages actually supported by deterministic parsing/templates and tests. A multilingual ASR or embedding model does not by itself enable Hindi/Marathi operational publication. Add each language through reviewed normalization, slot parsing, aliases, templates, and held-out tests.

## API shape

Adapt existing routes rather than forcing names. A practical split is:

- POST /api/v1/translate for normalized text/common input
- POST /api/v1/voice/transcribe for uploaded audio -> transcript + ASR metadata
- POST /api/v1/voice/prepare or reuse /translate with the finalized transcript
- WebSocket streaming ASR only if later required

Keep publication separate from transcription. The publish endpoint revalidates the prepared meaning/manifests under the normal announcement rules.

## Verification

Test at least:

- typed text and voice transcript produce the same meaning record for the same announcement
- train/platform numbers are preserved exactly
- leading/repeated digits are preserved
- arrival/departure and cancellation polarity are not flipped
- low-confidence/ambiguous critical values require confirmation
- partial ASR output never publishes
- ASR failure leaves typed/structured input paths working
- noisy/empty audio fails clearly
- unauthorized audio upload/publish is rejected
