# WisprFlow SDK

Unofficial Python SDK for Wispr Flow.

This project reverse-engineers the Wispr Flow desktop client and exposes its transcription and command APIs through a clean Python interface. Send audio files directly from Python, stream live audio, customize transcription behavior, and receive structured results without interacting with the desktop application's UI.

## Features

* One-shot audio transcription
* Real-time streaming transcription
* Command mode
* Context-aware transcription
* Custom dictionaries and replacements
* Automatic audio conversion via FFmpeg
* Live partial result callbacks
* No browser automation
* No Flask server required
* Pure Python interface
* Single-file implementation
* No generated protobuf classes required

---

## Quick Start

```python
from wisprflow_sdk import WisprClient

client = WisprClient()

result = client.transcribe("audio.wav")

print(result.final)
```

---

## Design Goals

The goal of this project is to provide programmatic access to Wispr Flow while preserving the behavior of the official client.

The SDK does **not**:

* Bypass subscriptions
* Bypass usage limits
* Bypass quotas
* Bypass account restrictions
* Bypass authentication
* Unlock premium features

All requests are performed using your own authenticated Wispr Flow account and remain subject to the same limits and policies enforced by Wispr.

If your account cannot perform an action through the official client, this SDK cannot perform it either.

---

> [!WARNING]
> This project is provided **as-is** with no warranties or guarantees.
>
> This SDK interacts with Wispr Flow using reverse-engineered client behavior and implementation details that may change at any time.
>
> By using this project, you accept full responsibility for any consequences, including service interruptions, account restrictions, account suspension, or account termination.
>
> The authors of this project are not responsible for any loss of access, data, service availability, or account status resulting from the use of this software.
>
> Use at your own risk.

---

> [!IMPORTANT]
> This SDK is **not a replacement for the Wispr Flow desktop application**.
>
> It does not:
>
> * Capture microphone audio automatically
> * Inject text into applications
> * Simulate keyboard input
> * Replace the Wispr desktop experience
> * Provide background dictation
>
> The goal of this project is to provide a programmatic Python interface to Wispr Flow's backend services.
>
> Typical use cases include:
>
> * Transcribing audio files from Python
> * Streaming audio from a custom microphone source
> * Building automation workflows
> * Processing recordings in scripts and applications
> * Accessing Wispr transcription functionality from code
>
> You should continue using the official Wispr Flow desktop application for normal day-to-day dictation and text insertion workflows.

---

> [!NOTE]
> The SDK intentionally does not implement its own authentication flow.
>
> Instead, it reuses the authentication session created by the official Wispr Flow desktop application.
>
> To keep the project simple and avoid maintaining a separate login implementation, users should periodically open and use the official Wispr Flow desktop application so it can refresh its own authentication tokens (and run a dictation).
>
> If authentication tokens expire, the SDK may stop working until Wispr Flow is launched again and refreshes the session.
>
> As a general recommendation, open and use the desktop application occasionally (for example, once every week or two) to ensure authentication remains current.

---

## Project Structure

The core SDK is intentionally self-contained.

```text
wisprflow_sdk.py
```

The entire implementation lives in a single Python file.

This makes it easy to:

* Audit
* Learn from
* Modify
* Embed into existing projects
* Debug protocol changes

No local servers, browser automation, Electron integration, or multi-module dependency chains are required.

---

## Installation

### Requirements

* Python 3.9+
* FFmpeg available on PATH
* Installed and logged-in Wispr Flow desktop application

Install dependencies:

```bash
pip install grpcio requests
```

---

## Setup

### 1. Install Wispr Flow

Install and log into the official Wispr Flow desktop application.

The SDK reuses your existing Wispr session and does not implement its own authentication flow.

### 2. Extract Runtime Configuration

Run:

```powershell
patch_wispr.ps1
```

Wispr Flow stores some runtime connection information internally and does not expose it through a public API.

The patch modifies the locally installed Wispr Flow application so that the runtime configuration used by the client is exported to:

```text
%LOCALAPPDATA%\WisprFlow\wispr_runtime.json
```

The patch:

1. Creates a backup of the original application.
2. Extracts the Electron application bundle.
3. Injects a small runtime hook.
4. Repackages the application.

After running the patch, perform a transcription in Wispr Flow once to generate the file.

The patch **does not**:

* Create login sessions
* Generate authentication tokens
* Create accounts
* Bypass authentication

Authentication continues to be handled entirely by the official Wispr Flow desktop application.

**You will have to rerun the powershell script everytime the Wispr Flow app updates.**

### 3. Verify Required Files

The SDK expects the following files to exist:

```text
%APPDATA%\Wispr Flow\session.json
%LOCALAPPDATA%\WisprFlow\wispr_runtime.json
```

`session.json` is automatically created by Wispr Flow after login.

`wispr_runtime.json` is generated by the patch.

---


## Basic Transcription

```python
from wisprflow_sdk import WisprClient

client = WisprClient()

result = client.transcribe(
    "meeting.m4a",
    languages=["en"],
    style="FORMAL",
    cleanup="MEDIUM"
)

print(result.final)
```

---

## Command Mode

```python
cmd = client.command(
    "command.wav",
    selected_text="i am going to work tomorrow"
)

print(cmd.action)
print(cmd.result)
```

---

## Live Streaming

```python
with client.live_session(languages=["en"]) as sess:
    for chunk in pcm_audio_source():
        sess.send(chunk)

print(sess.result.final)
```

---

## Example File

The repository includes:

```text
wispr_demo.py
```

which demonstrates virtually every public feature of the SDK.

Covered examples include:

* Authentication checks
* Basic transcription
* Advanced transcription options
* Context injection
* Command mode
* Live streaming
* Partial callbacks
* Custom dictionaries
* Replacements
* Snippets
* Cleanup testing
* Language testing
* Runtime overrides
* Configuration management

For most users, reading `wispr_demo.py` is the fastest way to learn the SDK.

---

## Configuration

The SDK supports:

* Custom vocabulary
* Replacements
* Snippets
* Language preferences
* Cleanup levels
* Style preferences
* Signature settings

Configuration is stored in:

```text
wispr_config.json
```

and can be managed programmatically:

```python
client.config.add_word("OpenAI")
client.config.add_replacement("dont", "don't")
client.config.save()
```

---

## Technical Documentation

For contributors and anyone interested in the internals, see:

```text
TECHNICAL_DETAILS.md
```

This document explains:

* Authentication flow
* Runtime configuration discovery
* Patch implementation
* gRPC protocol structure
* Audio processing pipeline
* Context injection
* Live streaming architecture
* Configuration management
* Security considerations

It serves as a complete technical reference for the project.

---

## Security

Never commit:

```text
session.json
wispr_runtime.json
wispr_config.json
```

These files may contain:

* Authentication tokens
* API keys
* Account information
* Personal preferences
* Custom vocabulary
* Custom replacements

Recommended `.gitignore`:

```gitignore
session.json
wispr_runtime.json
wispr_config.json
*.wav
```

---

## Limitations

This SDK depends on implementation details extracted from the Wispr Flow desktop application.

Future Wispr updates may change:

* Authentication storage
* Runtime configuration
* API endpoints
* gRPC message formats
* Backend infrastructure

As a result, future Wispr releases may require corresponding SDK updates.

---

## Disclaimer

This is an unofficial community project and is not affiliated with, endorsed by, or supported by Wispr Flow.

This project was created for educational and interoperability purposes. Use at your own risk.

All trademarks and product names belong to their respective owners.

---

## License

MIT License
