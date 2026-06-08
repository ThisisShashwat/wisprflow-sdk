# WisprFlow SDK — Developer Reference

This document covers the full API surface, internal behaviour, and parameter reference. For installation, setup, and a feature overview, see `README.md`. For architecture diagrams, gRPC/protobuf internals, and patch implementation details, see `TECHNICAL_DETAILS.md`.


See `wisprflow_example.py` for a comprehensive, fully annotated usage guide covering authentication, transcription, command mode, live streaming, configuration management, test matrices, and advanced per-call overrides.

---

## WisprClient

### Constructor

```python
from wisprflow_sdk import WisprClient

client = WisprClient(
    config_path=None,   # Path to wispr_config.json. Resolved in order:
                        #   1. explicit path passed here
                        #   2. WISPRFLOW_TEST_CFG environment variable
                        #   3. wispr_config.json next to _core.py
    verbose=False,      # Print [wispr] debug lines to stdout
)
```

---

### auth_status()

Decodes the JWT expiry from `session.json` without making a network call.

```python
status = client.auth_status()
```

Returns:

| Key | Type | Description |
|-----|------|-------------|
| `ok` | `bool` | `True` if the token is valid or near expiry |
| `status` | `str` | `"valid"` \| `"near_expiry"` \| `"expired"` |
| `expires_utc` | `str` | Human-readable UTC expiry timestamp |
| `seconds_remaining` | `int` | Seconds until expiry |

`near_expiry` is set when fewer than 3600 seconds remain. Once `status` is `"expired"`, open the Wispr Flow desktop app and do one dictation to refresh.

---

### transcribe()

```python
result = client.transcribe(
    audio_path,                 # str | Path — any FFmpeg-supported format

    # Language
    languages=None,             # list[str] | None
                                # None → reads from wispr_config.json
                                # "hien" always auto-adds "en" alongside it
                                # Supported: "en", "engb", "hi", "hien"

    # Style & cleanup
    style=None,                 # "FORMAL" | "CASUAL" | "VERY_CASUAL" | "EXCITED" | None
    app_type="other",           # "personal" | "work" | "email" | "other"
                                # Selects which styleConfig bucket to use
    cleanup="NONE",             # "NONE" | "LIGHT" | "MEDIUM" | "HIGH"
                                # Maps to gRPC editingStrength:
                                #   NONE   → VERBATIM (exact words)
                                #   LIGHT  → VERBATIM (removes fillers only)
                                #   MEDIUM → LIGHT    (clarity edits)
                                #   HIGH   → MEDIUM   (brevity rewrite)

    # Cursor / textbox context
    before_text=None,           # Text immediately before the cursor
    after_text=None,            # Text immediately after the cursor
    selected_text=None,         # Currently highlighted text
    textbox_contents=None,      # Full contents of the active textbox

    # Screen context
    content_text=None,          # Visible plain text on screen
    content_html=None,          # HTML version of screen content

    # App context
    app_name=None,              # e.g. "Chrome"
    bundle_id=None,             # e.g. "com.google.Chrome"
    url=None,                   # Active URL

    # Dynamic vocabulary (screen-scraped; do NOT use for personal dictionary)
    screen_ax=None,             # list[str] — accessibility tree tokens
    screen_ocr=None,            # list[str] — OCR-extracted tokens
    variable_names=None,        # list[str] — code identifiers
    file_names=None,            # list[str] — open file names
    # Note: all dynamic vocab fields are capped at 50 items each

    # Signature
    signature_prefix="UNSPECIFIED",  # "UNSPECIFIED" | "SPOKEN" | "WRITTEN"
    sign_emails=False,
    signature_hyperlink=False,

    # Misc
    extra_words=None,           # list[str] — ad-hoc vocab for this call only
    warmup=True,                # Hit wisprflow.ai/warmup before streaming
                                # Reduces cold-start latency; safe to disable
    extra_overrides=None,       # dict — merged on top of wispr_config.json
                                # for this call only; does not modify disk config
                                # e.g. {"dictionaryPersonal": ["TempWord"]}
)
```

Returns a `TranscriptResult`.

On transient gRPC errors (`UNAVAILABLE`, `DEADLINE_EXCEEDED`, `RESOURCE_EXHAUSTED`) a single automatic retry fires after 500 ms.

---

### command()

Transcribes a spoken command and applies it to `selected_text` via Wispr's REST command API.

```python
cmd = client.command(
    audio_path,                 # Audio file containing the spoken instruction
    selected_text="",           # Text the command operates on

    # Accepts all the same optional params as transcribe():
    languages=None,
    style=None,
    app_type="other",
    cleanup=None,
    before_text=None,
    after_text=None,
    content_text=None,
    warmup=True,
    extra_overrides=None,
)
```

Returns a `CommandResult`:

| Field | Description |
|-------|-------------|
| `spoken_command` | The transcribed instruction (e.g. `"make this more formal"`) |
| `selected_text` | The text passed in |
| `command` | Parsed `Command:` section from Wispr's response |
| `action` | Parsed `Action:` section (e.g. `"rewrite"`) |
| `result` | The transformed text — **use this** |
| `raw_output` | Full raw string from the REST response |
| `transcript` | The underlying `TranscriptResult` from the audio transcription step |

Text field selection priority for the spoken command: `result_plaintext` → `formatted` → `raw` → `grpc_final` → `final`.

---

### live_session()

Returns a `LiveSession`. Accepts the same style/language/context params as `transcribe()`, plus:

```python
sess = client.live_session(
    languages=None,
    style=None,
    app_type="other",
    cleanup="NONE",
    before_text=None,
    after_text=None,
    selected_text=None,
    content_text=None,
    partial_callback=None,      # callable(dict) — called for each partial result
                                # msg = {
                                #   "type": "partial",
                                #   "kind": "raw" | "formatted" | "result",
                                #   "text": "..."
                                # }
    extra_overrides=None,
)
```

Accepts raw **16 kHz mono PCM16** bytes only. Convert from other formats first using `downsample_to_pcm16()` or `convert_to_wispr_wav()`.

**Limits:** 300 seconds, 25 MB per session.

#### Context manager (recommended)

```python
with client.live_session(languages=["en"]) as sess:
    with open("audio.wav", "rb") as f:
        f.read(44)                       # skip WAV header
        while chunk := f.read(6400):     # ~200 ms at 16 kHz / 16-bit
            sess.send(chunk)

print(sess.result.final)
```

#### Manual control

```python
sess = client.live_session(languages=["en"])
sess.start()

with open("audio.wav", "rb") as f:
    f.read(44)
    while chunk := f.read(6400):
        sess.send(chunk)

result = sess.finish()   # blocks until transcript arrives
print(result.final)
```

---

## TranscriptResult

All fields returned by `transcribe()` and `live_session()`:

| Field | Description |
|-------|-------------|
| `final` | **Use this.** Best text after all post-processing. |
| `raw` | Raw ASR output before any Wispr formatting |
| `formatted` | After Wispr's server-side formatting pass |
| `result_html` | HTML string from the gRPC `Result` message |
| `result_plaintext` | Plain text from the gRPC `Result` message |
| `result_status` | `1` = FORMATTED · `2` = ERROR · `3` = RAW_TRANSCRIPT |
| `grpc_final` | Best text from gRPC before local post-processing |
| `after_replacements` | After `replacementsPersonal` + `snippetsPersonal` applied locally |
| `post_processing` | List of dicts for every replacement/snippet rule that fired |
| `cursor_polish` | Reserved for future cursor-aware edits (currently unused) |
| `warmup` | `{"ok": bool, "status_code": int}` or `{"ok": False, "error": "..."}` |
| `network` | `{"grpc_timeout_seconds": 90}` |
| `config` | `{"languages": [...], "styleConfig": {...}}` used for this call |

`str(result)` returns `result.final`.

#### post_processing format

```python
[
    {"kind": "replacement", "from": "teh",          "to": "the", "count": 1},
    {"kind": "snippet",     "from": "by the way",   "to": "btw", "count": 2},
]
```

---

## WisprConfig (`client.config`)

Manages `wispr_config.json`. All mutations are in-memory until `.save()` is called.

### Dictionary

```python
client.config.add_word("OpenAI")               # adds to dictionaryPersonal
client.config.add_word("Dube", starred=True)   # adds to dictionaryPersonalStarred
client.config.remove_word("OpenAI")

print(client.config.dictionary)               # list[str]
```

### Replacements

Applied after every transcription using whole-word, case-insensitive regex (longest key wins).

```python
client.config.add_replacement("dont", "don't")
client.config.remove_replacement("dont")

print(client.config.replacements)             # {"dont": "don't", ...}
```

### Snippets

```python
client.config.add_snippet("by the way", "btw")
client.config.remove_snippet("by the way")

print(client.config.snippets)                 # {"as soon as possible": "ASAP", ...}
```

### Style defaults

```python
client.config.set_style("work",     "FORMAL")
client.config.set_style("email",    "FORMAL")
client.config.set_style("personal", "CASUAL")
client.config.set_style("other",    "CASUAL")
```

Valid values: `"FORMAL"`, `"CASUAL"`, `"VERY_CASUAL"`, `"EXCITED"`.

### Cleanup default

```python
client.config.set_cleanup("MEDIUM")  # "NONE" | "LIGHT" | "MEDIUM" | "HIGH"
```

### Persistence

```python
client.config.save()     # writes wispr_config.json to disk
client.config.reload()   # re-reads from disk (picks up external edits)
```

### Raw access

```python
raw = client.config.raw()   # returns the full config dict

raw["styleConfig"]            # per-context style defaults
raw["dictionaryPersonal"]     # list[str]
raw["replacementsPersonal"]   # dict[str, str]
raw["snippetsPersonal"]       # dict[str, str]
raw["autoCleanupLevel"]       # "NONE" | "LIGHT" | "MEDIUM" | "HIGH"
raw["editingStrength"]        # raw gRPC enum name
raw["signatureConfig"]        # {"flowSignaturePrefix": ..., ...}
raw["languages"]              # default language list
raw["user"]                   # {"firstName": "", "lastName": "", "email": ""}
```

### Per-call overrides

Pass `extra_overrides` to `transcribe()` or `command()` to merge a temporary config on top of the on-disk config for one call only. `styleConfig` is merged key-by-key; all other keys are replaced outright.

```python
result = client.transcribe(
    AUDIO,
    extra_overrides={
        "dictionaryPersonal": ["TempWord", "AnotherWord"],
        "replacementsPersonal": {"u": "you"},
        "signatureConfig": {
            "flowSignaturePrefix": "WRITTEN",
            "flowSignatureHyperlink": True,
            "signEmails": True,
        },
    },
)
```

---

## Test Matrices

### run_cleanup_matrix()

Runs all four cleanup levels (`NONE`, `LIGHT`, `MEDIUM`, `HIGH`) sequentially and prints a diff against `NONE`.

```python
results = client.run_cleanup_matrix(
    AUDIO,
    languages=["en", "hien"],
    style="CASUAL",
    app_type="other",
    log=print,              # any callable(str)
)

results["NONE"].final
results["LIGHT"].final
results["MEDIUM"].final
results["HIGH"].final
```

### run_language_matrix()

Runs four language/dictionary combinations:

| Key | Languages | Dictionary |
|-----|-----------|------------|
| `"English only + dict"` | `["en"]` | enabled |
| `"Catch-All + dict"` | `["en", "hien"]` | enabled |
| `"Hindi only + dict"` | `["hi"]` | enabled |
| `"Catch-All without dict"` | `["en", "hien"]` | disabled |

```python
results = client.run_language_matrix(AUDIO, style="CASUAL", app_type="other", log=print)
results["Catch-All + dict"].final
```

### run_full_test_suite()

Runs both matrices back-to-back.

```python
all_results = client.run_full_test_suite(AUDIO, languages=["en", "hien"], log=print)

all_results["cleanup"]["HIGH"].final
all_results["language"]["Catch-All + dict"].final
```

All three methods accept a `log` callable (default: `print`) and return `None` for any entry that errors.

---

## Standalone Audio Utilities

These are importable without a `WisprClient` instance.

```python
from wisprflow_sdk import convert_to_wispr_wav, downsample_to_pcm16
```

### convert_to_wispr_wav()

Converts any FFmpeg-supported file to 16 kHz mono 16-bit PCM WAV.

```python
wav_path = convert_to_wispr_wav("input.m4a")                # → input.m4a_wispr.wav
wav_path = convert_to_wispr_wav("input.m4a", "output.wav")  # explicit output path
```

Even `.wav` files are re-encoded to guarantee the correct sample rate and bit depth.

### downsample_to_pcm16()

Converts `float32` PCM samples from any input rate to 16 kHz PCM16 bytes without FFmpeg. Useful when capturing live audio from PyAudio or sounddevice.

```python
import array
float_samples = array.array("f", [0.0] * 1024)   # your float32 samples
pcm_bytes = downsample_to_pcm16(float_samples, input_rate=44100)
# → bytes suitable for LiveSession.send()
```

---

## Enumerations Reference

| Enum | Values |
|------|--------|
| `WRITING_STYLE` | UNSPECIFIED=0, FORMAL=1, CASUAL=2, VERY_CASUAL=3, EXCITED=4 |
| `EDITING_STRENGTH` | UNSPECIFIED=0, VERBATIM=1, LIGHT=2, MEDIUM=3, HEAVY=4 |
| `SIGNATURE_PREFIX` | UNSPECIFIED=0, SPOKEN=1, WRITTEN=2 |
| `LANGUAGE_ENUM` | en=1, engb=2, hi=20, hien=21 |
| `APP_TYPE_ENUM` | other=1, browser=2, personal=3, work=4, email=5, chatbot=6, developer=7 |

---

## Public Exports (`__init__.py`)

```python
from wisprflow_sdk import (
    WisprClient,
    TranscriptResult,
    CommandResult,
    LiveSession,
    convert_to_wispr_wav,
    downsample_to_pcm16,
)
```

---

## Runtime Dependencies

`grpcio` and `requests` only. No generated protobuf stubs, no local servers, no browser automation.
