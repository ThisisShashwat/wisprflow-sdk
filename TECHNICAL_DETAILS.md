# WisprFlow SDK — Technical Documentation

## Overview

WisprFlow SDK is an unofficial Python library that interfaces directly with Wispr Flow's backend transcription infrastructure. Rather than automating the desktop application, the SDK reproduces the exact network requests the official client makes and exposes them through a clean Python API.

The entire implementation lives in a single file, `_core.py`, with no local HTTP servers, no browser automation, and no generated protobuf classes.

---

# Architecture

```text
┌─────────────┐
│ Python App  │
└──────┬──────┘
       │
       ▼
┌─────────────────────────────────────────┐
│             WisprFlow SDK               │
│  WisprClient / LiveSession / WisprConfig│
└──────┬──────────────────────┬───────────┘
       │                      │
       │ Reads                │ Reads
       ▼                      ▼
%APPDATA%\Wispr Flow\    %LOCALAPPDATA%\WisprFlow\
  session.json              wispr_runtime.json
       │                      │
       └──────────┬───────────┘
                  │
                  ▼
       ┌─────────────────────┐
       │   gRPC Backend      │
       │ (Baseten-hosted)    │
       └─────────────────────┘
                  │
       REST (command mode only)
                  │
                  ▼
       https://api.wisprflow.ai
```

---

# Authentication

The SDK does not implement its own login flow. Authentication is delegated entirely to the official Wispr Flow desktop application, whose session is reused.

## session.json

**Location:** `%APPDATA%\Wispr Flow\session.json`

This file is created and maintained by the official desktop app on login. The SDK reads it with:

```python
session_path = os.path.expandvars(r"%APPDATA%\Wispr Flow\session.json")
with open(session_path) as f:
    session = json.load(f)
auth_data = json.loads(session["sb-dodjkfqhwrzqjwkfnthl-auth-token"])
access_token = auth_data["access_token"]
```

The Supabase project key (`sb-dodjkfqhwrzqjwkfnthl-auth-token`) is hardcoded and project-specific — it may change if Wispr rotates their backend.

The access token is a JWT. The SDK decodes its payload (without verification) to extract the `sub` claim as the `user_id`:

```python
payload = token.split(".")[1]
payload += "=" * (-len(payload) % 4)
user_id = json.loads(base64.b64decode(payload))["sub"]
```

The token is attached to all gRPC calls as `Authorization: Bearer <token>` and to all REST calls as `Authorization: <token>`.

## auth_status()

`WisprClient.auth_status()` decodes the JWT expiry (`exp` claim) and returns a dict:

```python
{
    "ok": True,
    "status": "valid",          # "valid" | "near_expiry" | "expired"
    "expires_utc": "2025-01-01 12:00:00 UTC",
    "seconds_remaining": 3600
}
```

`near_expiry` is triggered when fewer than 3600 seconds remain.

---

# Runtime Configuration

The gRPC backend requires four values that are not publicly exposed by Wispr: model ID, environment, host URL, and a Baseten API key. The SDK reads these from `wispr_runtime.json`, which is populated by the patch script.

**Location:** `%LOCALAPPDATA%\WisprFlow\wispr_runtime.json`

```python
def _runtime_config() -> dict:
    cfg = {}
    if os.path.exists(runtime_path):
        with open(runtime_path) as f:
            cfg = json.load(f)
    api_key  = cfg.get("basteKey") or cfg.get("apiKey", "")
    model_id = cfg.get("modelId") or DEFAULT_MODEL_ID   # "v31pl413"
    env      = cfg.get("environment") or "production"
    host     = cfg.get("url") or GRPC_HOST_TEMPLATE.format(model_id=model_id)
    return {
        "host":    host,
        "model":   f"model-{model_id}",
        "env":     env,
        "api_key": f"Api-Key {api_key}",
    }
```

The default model ID `v31pl413` and the host template `model-{model_id}.grpc.api.baseten.co` are hardcoded fallbacks used if the runtime file is absent or missing a field.

---

# Runtime Patch

## What it does

`patch_wispr.ps1` exposes the runtime configuration that already exists inside the installed Wispr app. It does not create sessions, generate tokens, or bypass authentication.

## Steps performed

1. **Locates** the newest `app-x.x.x` directory under `%LOCALAPPDATA%\WisprFlow\`.
2. **Terminates** running Wispr processes (`Wispr Flow`, `WisprFlow`, `WisprHelper`).
3. **Backs up** `app.asar` to `app.asar.backup` in the same directory.
4. **Extracts** the archive using `npx asar extract`.
5. **Patches** the JavaScript: finds the pattern `=this.getDesiredGrpcModelInfo()` in the extracted `.js` files and injects a `fs.writeFileSync(...)` call immediately after it.
6. **Repacks** using `npx asar pack` and removes the extraction directory.
7. If the pattern is not found (e.g. after a Wispr update), the script restores the backup automatically and exits with an error.

## Injected code

```javascript
try {
    require("fs").writeFileSync(
        "<runtime_json_path>",
        JSON.stringify({ modelId: i, environment: s, url: a, apiKey: Ct.Fo })
    )
} catch (_e) {}
```

This fires on the first dictation after patching and writes `wispr_runtime.json`.

## Python entry point

`_installer.py` wraps the script for the `wisprflow-patch` CLI command. It prints a summary, asks for explicit confirmation, then invokes PowerShell:

```python
subprocess.run(
    ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
    check=False,
)
```

The patch must be re-run after every Wispr Flow update.

---

# Required Files

| File | Location | Source |
|------|----------|--------|
| `session.json` | `%APPDATA%\Wispr Flow\` | Written by Wispr login |
| `wispr_runtime.json` | `%LOCALAPPDATA%\WisprFlow\` | Written by patch on first dictation |

Both must be present. `wispr_config.json` (SDK preferences) is optional and created with defaults if absent.

---

# Audio Processing Pipeline

All audio is normalised to 16 kHz mono PCM16 WAV before being sent over gRPC. FFmpeg handles the conversion.

```text
Input file (any FFmpeg-supported format)
    │
    ▼
ffmpeg -ar 16000 -ac 1 -sample_fmt s16
    │
    ▼
Temporary PCM16 WAV (deleted after use)
    │
    ▼
Read into bytes → gRPC upload
    │
    ▼
TranscriptResult
```

Even files already in `.wav` format are re-encoded to guarantee the correct sample rate and bit depth.

For live streaming, the `downsample_to_pcm16(samples, input_rate)` utility converts float32 PCM from any input rate to 16 kHz PCM16 bytes without FFmpeg.

**Supported input formats (via FFmpeg):** wav, mp3, m4a, ogg, webm, aac, flac, and anything else FFmpeg supports.

---

# gRPC Protocol

## Connection

```python
GRPC_HOST_TEMPLATE = "model-{model_id}.grpc.api.baseten.co"
GRPC_PORT          = 443
GRPC_METHOD        = "/flow_api.v1.TranscriptionService/TranscribeStream"
GRPC_TIMEOUT       = 90  # seconds
```

TLS is always enabled (`ssl_channel_credentials()`). The channel is a raw `stream_stream` call with no generated stubs; request serialisation and response deserialisation both use identity functions (`lambda x: x`).

## gRPC metadata headers

Every call sends:

```python
[
    ("authorization",         "Bearer <access_token>"),
    ("baseten-authorization", "Api-Key <baseten_api_key>"),
    ("baseten-model-id",      "model-<model_id>"),
    ("x-baseten-environment", "<environment>"),
    ("flow-debug",            "false"),
    ("disable-formatting",    "false"),
    ("content-type",          "application/grpc"),
    ("user-agent",            "grpc-python/1.59.0"),
    ("te",                    "trailers"),
]
```

## Protobuf — hand-rolled encoding

The SDK constructs protobuf payloads manually using a minimal varint/wire-type encoder. No `.proto` files and no `protoc` dependency are required.

Core primitives:

| Function | Wire type | Purpose |
|----------|-----------|---------|
| `_varint(v)` | — | Encode unsigned integer |
| `_string(fn, v)` | 2 | String field |
| `_varint_field(fn, v)` | 0 | Integer field |
| `_bytes_field(fn, data)` | 2 | Raw bytes field |
| `_bool_field(fn, v)` | 0 | Boolean field |
| `_msg(fn, data)` | 2 | Embedded message field |
| `_map_entry(fn, key, value)` | 2 | Map entry (string→string) |

## Message sequence (one-shot transcription)

```
1. InitRequest   → Metadata (user_id, session_id, request_id, encoding, env, client)
                  + Preferences (user profile, languages, vocabulary, replacements, style)
   + Commit(NON_FINAL)

2. ContextRequest (optional, only if context fields are present)
                  → App info, textbox content, dynamic vocabulary, screen content
   + Commit(NON_FINAL)

3. AudioRequest   → Audio file bytes (wrapped in audio_file → audio_payload → request)
   + Commit(FINAL)
```

Client version reported to the server: `1.5.113`.

## Enumerations

| Enum | Values |
|------|--------|
| `WRITING_STYLE` | UNSPECIFIED=0, FORMAL=1, CASUAL=2, VERY_CASUAL=3, EXCITED=4 |
| `EDITING_STRENGTH` | UNSPECIFIED=0, VERBATIM=1, LIGHT=2, MEDIUM=3, HEAVY=4 |
| `SIGNATURE_PREFIX` | UNSPECIFIED=0, SPOKEN=1, WRITTEN=2 |
| `LANGUAGE_ENUM` | en=1, engb=2, hi=20, hien=21 |
| `APP_TYPE_ENUM` | other=1, browser=2, personal=3, work=4, email=5, chatbot=6, developer=7 |

## Cleanup level mapping

The SDK's user-facing cleanup levels map to gRPC `editingStrength` values as follows:

| Cleanup level | gRPC editingStrength |
|---------------|---------------------|
| NONE | VERBATIM |
| LIGHT | VERBATIM |
| MEDIUM | LIGHT |
| HIGH | MEDIUM |

## Response parsing

Server responses are decoded by a hand-rolled varint parser. The parser walks the binary response looking for two top-level fields:

- Field 1 (`Result`): contains `result_html`, `result_plaintext`, and `result_status`.
- Field 2 (`State`): contains `raw_text` (field 2) and `formatted_text` (field 3).

Multiple response frames arrive during a stream; the parser keeps the most recent non-empty value for each field. Heartbeat frames (first byte `0x22`) are discarded.

`result_status` values: 1 = FORMATTED, 2 = ERROR, 3 = RAW_TRANSCRIPT.

---

# Context Injection

Optional context data improves transcription accuracy by matching surrounding text and screen state. It is encoded into the `Context` gRPC message.

**Textbox context:**

| Field | Description |
|-------|-------------|
| `before_text` | Text immediately before the cursor |
| `after_text` | Text immediately after the cursor |
| `selected_text` | Currently highlighted text |
| `textbox_contents` | Full contents of the active textbox |

**Screen context:**

| Field | Description |
|-------|-------------|
| `content_text` | Visible plain text on screen |
| `content_html` | HTML version of screen content |
| `screenshot` | Raw screenshot bytes (field 8) |

**App context:**

| Field | Description |
|-------|-------------|
| `app_name` | e.g. `"Chrome"` |
| `bundle_id` | e.g. `"com.google.Chrome"` |
| `url` | Active URL |
| `session_apps` | List of additional open apps |

**Dynamic vocabulary** (screen-scraped, capped at 50 items each):

| Field | Description |
|-------|-------------|
| `screen_ax` | Accessibility tree tokens |
| `screen_ocr` | OCR-extracted tokens |
| `variable_names` | Code identifiers |
| `file_names` | Open file names |

Dynamic vocabulary fields are only encoded if at least one is non-empty (`allow_dynamic_vocab` flag).

---

# Transcription Modes

## Standard transcription — `WisprClient.transcribe()`

Converts audio, optionally hits the warmup endpoint, then opens a gRPC stream. On transient errors (`UNAVAILABLE`, `DEADLINE_EXCEEDED`, `RESOURCE_EXHAUSTED`) a single automatic retry fires after 500 ms.

**Warmup:** A GET to `https://api.wisprflow.ai/warmup` using the Wispr user-agent string is sent before opening the gRPC stream. This reduces cold-start latency and can be disabled with `warmup=False`.

## Command mode — `WisprClient.command()`

```text
Audio file
    │
    ▼
transcribe()  →  spoken_command (plain text)
    │
    ▼
POST https://api.wisprflow.ai/llm/command_mode_route
    {
      "full_text": selected_text,
      "selected_text": selected_text,
      "instruction": spoken_command,
      "language": lang,
      "personalization_style_settings": { ... }
    }
    │
    ▼
CommandResult.action + CommandResult.result
```

The cleanest available text field is chosen as the spoken command (priority: `result_plaintext` → `formatted` → `raw` → `grpc_final` → `final`).

The REST response is parsed with regex for `Command:`, `Action:`, and `Result:` sections.

## Live streaming — `LiveSession`

The live session runs the gRPC stream on a background daemon thread. Audio chunks are fed through a bounded `queue.Queue(maxsize=500)`. A sentinel `None` is enqueued on `finish()` to signal end-of-stream.

The request iterator:

1. Sends init + NON_FINAL commit.
2. Sends context (if any) + NON_FINAL commit.
3. Sends each buffered audio packet with NON_FINAL commit, holding back one packet.
4. Sends the final packet with FINAL commit (or just a bare FINAL commit if no audio arrived).

Partial results (raw, formatted, result) are placed on an output queue and dispatched to `partial_callback` (if supplied). The final `TranscriptResult` is assembled from the accumulated best values after the stream closes.

**Limits:** 300 seconds, 25 MB per session.

Usage patterns:

```python
# Context manager (recommended)
with client.live_session(languages=["en"]) as sess:
    for chunk in audio_source():
        sess.send(chunk)
print(sess.result.final)

# Manual control
sess = client.live_session()
sess.start()
sess.send(pcm_bytes)
result = sess.finish()
```

---

# Post-processing

After the gRPC stream closes, the SDK applies local post-processing before returning the final result.

1. **Best-text selection:** `result_html` → `result_plaintext` → `formatted_text` → `raw_text` (first non-empty).
2. **Text cleaning:** strips leading replacement characters (`\ufffd`), control characters, and trailing null bytes.
3. **Replacements and snippets:** `replacementsPersonal` and `snippetsPersonal` from config are applied to the cleaned text using whole-word regex substitution (case-insensitive, longest-key-first). Each fired rule is recorded in `post_processing`.
4. **Deduplication:** `_merge_post_processing()` removes duplicate fired-rule entries.
5. **Inference:** `_infer_post_processing()` cross-references all intermediate text fields against the final text to detect any rules that fired server-side but were not recorded locally.

`TranscriptResult.final` is always the result after local replacements/snippets. `TranscriptResult.grpc_final` is the text before local post-processing.

---

# TranscriptResult Fields

| Field | Description |
|-------|-------------|
| `raw` | Raw ASR output before any Wispr formatting |
| `formatted` | After Wispr's server-side formatting pass |
| `result_html` | HTML string from gRPC `Result` message |
| `result_plaintext` | Plain text from gRPC `Result` message |
| `result_status` | 1=FORMATTED, 2=ERROR, 3=RAW_TRANSCRIPT |
| `grpc_final` | Best text from gRPC before local post-processing |
| `after_replacements` | After `replacementsPersonal` + `snippetsPersonal` |
| `final` | The value to use (same as `after_replacements` unless cursor_polish fires) |
| `post_processing` | List of dicts for each replacement/snippet rule that fired |
| `cursor_polish` | Reserved for cursor-aware edits (currently unused) |
| `warmup` | Warmup endpoint result: `{"ok": bool, "status_code": int}` |
| `network` | `{"grpc_timeout_seconds": 90}` |
| `config` | `{"languages": [...], "styleConfig": {...}}` used for this call |

---

# Configuration System

`WisprConfig` manages `wispr_config.json`. The path is resolved in order: explicit `config_path` argument → `WISPRFLOW_TEST_CFG` environment variable → `wispr_config.json` next to `_core.py`.

Default config structure:

```json
{
    "user": { "firstName": "", "lastName": "", "email": "" },
    "languages": ["en", "hien"],
    "dictionaryPersonal": [],
    "dictionaryPersonalStarred": [],
    "replacementsPersonal": {},
    "snippetsPersonal": {},
    "styleConfig": {
        "personal": "CASUAL",
        "work": "FORMAL",
        "email": "FORMAL",
        "other": "CASUAL"
    },
    "autoCleanupLevel": "NONE",
    "editingStrength": "VERBATIM",
    "signatureConfig": {
        "flowSignaturePrefix": "UNSPECIFIED",
        "flowSignatureHyperlink": false,
        "signEmails": false
    }
}
```

`merge_overrides(overrides)` performs a deep copy of the config and merges per-call overrides on top, without modifying the on-disk config. `styleConfig` is merged key-by-key; all other keys are replaced.

`hien` language always auto-adds `en` alongside it, regardless of how languages are specified.

---

# Test Matrices

Three convenience methods run multiple transcriptions in sequence to compare behaviour:

**`run_cleanup_matrix(audio_path, ...)`** — runs all four cleanup levels (NONE, LIGHT, MEDIUM, HIGH) and prints a diff summary showing which levels produce different output from NONE.

**`run_language_matrix(audio_path, ...)`** — runs four language/dictionary combinations:
- English only + dictionary
- Catch-All (en + hien) + dictionary
- Hindi only + dictionary
- Catch-All without dictionary

**`run_full_test_suite(audio_path, ...)`** — runs both matrices back to back and returns `{"cleanup": ..., "language": ...}`.

All three accept a `log` callable (default: `print`) and return a dict keyed by level/label with `TranscriptResult` values (or `None` on error).

---

# CLI

Installed as the `wisprflow` entry point via `pyproject.toml`.

```text
wisprflow audio.wav
wisprflow audio.wav --style FORMAL --cleanup MEDIUM --languages en hien
wisprflow audio.wav --before "Dear John," --after "Regards"
wisprflow audio.wav --context "visible screen text"
wisprflow audio.wav --matrix-cleanup
wisprflow audio.wav --matrix-language
wisprflow audio.wav --verbose
```

The CLI always prints auth status on startup. `--matrix-cleanup` and `--matrix-language` are mutually exclusive with direct transcription.

---

# Project Structure

```
wisprflow_sdk/
├── __init__.py       ← Public exports: WisprClient, TranscriptResult,
│                        CommandResult, LiveSession
├── _core.py          ← Entire SDK implementation
├── _installer.py     ← wisprflow-patch entry point
└── patch_wispr.ps1   ← PowerShell patch script (bundled with package)
```

The implementation is intentionally self-contained in `_core.py`. Runtime dependencies are `grpcio` and `requests` only.

---

# Security Considerations

Never commit or publish these files:

```gitignore
session.json
wispr_runtime.json
wispr_config.json
*.wav
```

These files may contain access tokens, Baseten API keys, user profile information, and personal vocabulary.

---

# Compatibility

The SDK depends on implementation details extracted from the Wispr desktop client. Future Wispr updates may change any of the following, requiring a corresponding SDK update:

- The Supabase project key in `session.json` (`sb-dodjkfqhwrzqjwkfnthl-auth-token`)
- The JavaScript pattern used by the patch script (`=this.getDesiredGrpcModelInfo()`)
- gRPC message field numbers and structure
- REST API endpoints (`api.wisprflow.ai/...`)
- Model identifiers and Baseten host templates
- Runtime config field names (`basteKey` / `apiKey` fallback already handles one rename)

The patch script must be re-run after every Wispr Flow update.

---

# Disclaimer

Unofficial community project. Not affiliated with, endorsed by, or supported by Wispr Flow. Created for educational and interoperability purposes. All trademarks and product names belong to their respective owners.