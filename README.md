# WisprFlow SDK

Unofficial Python SDK for Wispr Flow.

Reverse-engineers the Wispr Flow desktop client and exposes its transcription and command APIs through a clean Python interface. Send audio files directly from Python, stream live audio, customize transcription behavior, and receive structured results — no UI interaction required.

[![PyPI version](https://badge.fury.io/py/wisprflow-sdk.svg)](https://pypi.org/project/wisprflow-sdk/)
## Features

* One-shot audio transcription
* Real-time streaming transcription
* Command mode
* Context-aware transcription (cursor position, screen content, app info)
* Custom dictionaries, replacements, and snippets
* Automatic audio conversion via FFmpeg
* Live partial result callbacks
* CLI included
* No browser automation, no Flask server, no generated protobuf classes

---

## Design Goals

Provide programmatic access to Wispr Flow while preserving the behavior of the official client.

The SDK does **not**:

* Bypass subscriptions, usage limits, quotas, or authentication
* Unlock premium features

All requests are performed using your own authenticated Wispr Flow account, subject to the same limits enforced by Wispr. If your account can't do something in the official client, this SDK can't either.

---

> [!WARNING]
> This project is provided **as-is** with no warranties or guarantees.
>
> This SDK interacts with Wispr Flow using reverse-engineered client behavior and implementation details that may change at any time.
>
> By using this project, you accept full responsibility for any consequences, including service interruptions, account restrictions, account suspension, or account termination.
>
> Use at your own risk.

---

> [!IMPORTANT]
> This SDK is **not a replacement for the Wispr Flow desktop application**. It does not capture microphone audio, inject text into applications, or simulate keyboard input.
>
> Typical use cases: transcribing audio files from Python, streaming audio from a custom source, building automation workflows, processing recordings in scripts.
>
> Continue using the official desktop app for day-to-day dictation.

---

> [!NOTE]
> The SDK does not implement its own authentication. It reuses the session created by the official Wispr Flow desktop application.
>
> Open and use the desktop app occasionally (once every week or two) to keep authentication tokens fresh. If tokens expire, the SDK will stop working until Wispr Flow is launched again.

---

## Installation

**Requirements:** Python 3.9+, FFmpeg on PATH, Wispr Flow desktop app installed and logged in.

```bash
pip install wisprflow-sdk
```

---

## Setup

### 1. Install Wispr Flow

Install and log into the official Wispr Flow desktop application.

### 2. Run the patch

```bash
wisprflow-patch
```

Wispr Flow stores runtime connection info internally and doesn't expose it publicly. The patch modifies your local install to export it to `%LOCALAPPDATA%\WisprFlow\wispr_runtime.json` on the next dictation.

The command explains what it will do and asks for confirmation before touching anything. It creates a backup automatically.

**Re-run after every Wispr Flow update.**

> The patch does not create login sessions, generate tokens, or bypass authentication. It only exposes config that's already inside the app.

### 3. Populate the runtime config

Open Wispr Flow and do one dictation. This generates `wispr_runtime.json`.

### 4. Verify

```python
from wisprflow_sdk import WisprClient

client = WisprClient()
print(client.auth_status())
# {"ok": True, "status": "valid", "expires_utc": "...", "seconds_remaining": 3600}
```

The SDK expects these two files to exist:

```
%APPDATA%\Wispr Flow\session.json       ← created by Wispr login
%LOCALAPPDATA%\WisprFlow\wispr_runtime.json  ← created by the patch
```

---

## Quick Start

```python
from wisprflow_sdk import WisprClient

client = WisprClient()

result = client.transcribe("audio.wav")
print(result.final)
```

---

## Transcription

```python
result = client.transcribe(
    "meeting.m4a",
    languages=["en"],        # see Languages below
    style="FORMAL",          # FORMAL | CASUAL | VERY_CASUAL | EXCITED
    app_type="email",        # personal | work | email | other
    cleanup="MEDIUM",        # NONE | LIGHT | MEDIUM | HIGH
)

print(result.final)          # use this 99% of the time
print(result.raw)            # raw ASR before any formatting
print(result.formatted)      # after Wispr's server-side formatting
print(result.post_processing)# replacements/snippets that fired
```


### Languages

Pass any language code Wispr Flow supports.

```python
# Hindi-English code-switching
result = client.transcribe(AUDIO, languages=["en", "hien"])

# Hindi only
result = client.transcribe(AUDIO, languages=["hi"])

# Japanese only
result = client.transcribe(AUDIO, languages=["jp"])

# British English
result = client.transcribe(AUDIO, languages=["engb"])
```

`hien` always auto-adds `en` alongside it. `None` falls back to `wispr_config.json`.

### Context injection

Pass cursor position and screen context to improve accuracy:

```python
result = client.transcribe(
    "audio.wav",
    before_text="Dear John,",
    after_text="Regards",
    selected_text="old text",
    content_text="visible screen text",
    app_name="Chrome",
    url="https://mail.google.com",
)
```

---

## Command Mode

Transcribes a spoken command and applies it to `selected_text` via Wispr's command routing API.

```python
cmd = client.command(
    "command.wav", # verbal instructions what to do with the text, e.g: make it formal
    selected_text="i am going to work tomorrow"
)

print(cmd.action)   # e.g. "rewrite"
print(cmd.result)   # the transformed text — use this
```

---

## Live Streaming

Feed raw 16kHz mono PCM16 bytes in real time.

```python
with client.live_session(languages=["en"]) as sess:
    for chunk in pcm_audio_source():
        sess.send(chunk)  # raw PCM16 bytes at 16kHz

print(sess.result.final)
```

Limits: 300 seconds, 25 MB per session.

---

## CLI

```bash
# Basic transcription
wisprflow audio.wav

# With options
wisprflow audio.wav --style FORMAL --cleanup MEDIUM --languages en hien

# With context
wisprflow audio.wav --before "Dear John," --after "Regards"

# Test matrices
wisprflow audio.wav --matrix-cleanup # detailed explaination in the example file
wisprflow audio.wav --matrix-language

# Debug output
wisprflow audio.wav --verbose
```

---

## Configuration

Persistent config is stored in `wispr_config.json` and managed via `client.config`:

```python
# Custom vocabulary
client.config.add_word("OpenAI")
client.config.add_word("Dube", starred=True)

# Replacements (applied after every transcription)
client.config.add_replacement("dont", "don't")

# Snippets (spoken phrase → short form)
client.config.add_snippet("as soon as possible", "ASAP")

# Style defaults per context
client.config.set_style("work", "FORMAL")
client.config.set_cleanup("MEDIUM")

client.config.save()
```

Config path can be overridden:
```python
client = WisprClient(config_path="/path/to/config.json")
# or set env var: WISPRFLOW_TEST_CFG=/path/to/config.json
```

---

## Project Structure

```
wisprflow_sdk/
├── __init__.py        ← public exports
├── _core.py           ← entire SDK implementation
└── _installer.py      ← wisprflow-patch entry point
```

The implementation is intentionally self-contained in `_core.py`. No local servers, browser automation, or multi-module dependency chains.

---

## Demo File

`wisprflow_example.py` in the repository covers every public feature with annotated code: all `transcribe()` parameters, every `TranscriptResult` field, command mode, live streaming, config management, test matrices, and per-call overrides. Start there.

---

## Security


```gitignore
session.json
wispr_runtime.json
wispr_config.json
```

These files may contain authentication tokens, API keys, and personal vocabulary.

---

## Limitations

* Patch script is Windows-only
* Depends on implementation details from the Wispr desktop client — future Wispr updates may change authentication storage, runtime config format, gRPC message structure, or API endpoints
* The `session.json` key name is Supabase project-specific and may change if Wispr rotates their backend

---

## Technical Documentation

See `TECHNICAL_DETAILS.md` for the full internals: authentication flow, runtime config discovery, patch implementation, gRPC protocol, audio pipeline, and security considerations.

---

## Disclaimer

Unofficial community project. Not affiliated with, endorsed by, or supported by Wispr Flow. Created for educational and interoperability purposes.

All trademarks and product names belong to their respective owners.

---

## License

MIT License