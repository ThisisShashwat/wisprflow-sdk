"""
wispr_demo.py — Every single feature of the Wispr SDK in one script.
Run sections individually by commenting out what you don't need.
"""

from wisprflow_sdk import WisprClient

# ─────────────────────────────────────────────────────────────────────────────
# 0. Setup
# ─────────────────────────────────────────────────────────────────────────────

client = WisprClient(
    config_path=None,   # defaults to wispr_config.json next to wisprflow_sdk.py
                        # or set WISPRFLOW_TEST_CFG env var
                        # or pass explicit path: config_path="C:/path/to/config.json"
    verbose=True,       # prints [wispr] debug lines; set False to silence
)

AUDIO = "test_case_1.wav"   # any format ffmpeg can read: .m4a .mp3 .webm .ogg etc.


# ─────────────────────────────────────────────────────────────────────────────
# 1. Auth check
# ─────────────────────────────────────────────────────────────────────────────

status = client.auth_status()
print(status)
# {"ok": True, "status": "valid", "expires_utc": "2025-01-01 12:00:00 UTC", "seconds_remaining": 3600}
# status["ok"]               → bool
# status["status"]           → "valid" | "near_expiry" | "expired"
# status["expires_utc"]      → human-readable expiry
# status["seconds_remaining"]→ int


# ─────────────────────────────────────────────────────────────────────────────
# 2. Basic transcription — just get the text
# ─────────────────────────────────────────────────────────────────────────────

result = client.transcribe(AUDIO)
print(result.final)          # the text
print(str(result))           # same as result.final


# ─────────────────────────────────────────────────────────────────────────────
# 3. Transcription — every param
# ─────────────────────────────────────────────────────────────────────────────

result = client.transcribe(
    AUDIO,

    # ── language ──────────────────────────────────────────────────────────────
    languages=["en", "hien"],   # en | engb | hi | hien
                                # None → uses wispr_config.json languages field
                                # "hien" always auto-adds "en" alongside it

    # ── style & cleanup ───────────────────────────────────────────────────────
    style="CASUAL",             # FORMAL | CASUAL | VERY_CASUAL | EXCITED | None
    app_type="other",           # personal | work | email | other
                                # determines which style bucket is used
    cleanup="NONE",             # NONE | LIGHT | MEDIUM | HIGH
                                # NONE   → VERBATIM (exact words)
                                # LIGHT  → VERBATIM (removes fillers)
                                # MEDIUM → LIGHT    (clarity edits)
                                # HIGH   → MEDIUM   (brevity rewrite)

    # ── cursor / textbox context ──────────────────────────────────────────────
    before_text="Dear John,",   # text before cursor in active textbox
    after_text="Regards",       # text after cursor
    selected_text="old text",   # currently selected/highlighted text
    textbox_contents="full contents of the textbox",

    # ── screen context ────────────────────────────────────────────────────────
    content_text="hello i am the coolest ever",  # visible text on screen / nearest texts
    content_html="<p>hello</p>",                 # HTML version of screen content

    # ── app context ───────────────────────────────────────────────────────────
    app_name="Chrome",
    bundle_id="com.google.Chrome",
    url="https://mail.google.com",

    # ── dynamic vocabulary (advanced) ─────────────────────────────────────────
    # NOTE: do NOT put personal dict words here — use client.config.add_word() instead.
    # These are for screen-scraped context words only.
    screen_ax=["InboxLabel", "ComposeButton"],   # accessibility tree words
    screen_ocr=["Subject:", "To:"],              # OCR words from screen
    variable_names=["myVariable", "someFunc"],   # for coding contexts
    file_names=["report.pdf", "notes.txt"],

    # ── signature ─────────────────────────────────────────────────────────────
    signature_prefix="UNSPECIFIED",  # UNSPECIFIED | SPOKEN | WRITTEN
    sign_emails=False,
    signature_hyperlink=False,

    # ── misc ──────────────────────────────────────────────────────────────────
    warmup=True,            # hit wisprflow.ai/warmup before streaming (recommended)
    extra_overrides=None,   # raw dict merged into config — last-resort escape hatch
                            # e.g. {"dictionaryPersonal": ["CustomWord"]}

    extra_words= ["Bisleery"],
)


# ─────────────────────────────────────────────────────────────────────────────
# 4. TranscriptResult — every field
# ─────────────────────────────────────────────────────────────────────────────

print(result.final)             # ← use this 99% of the time
                                # = grpc best output → after local replacements/snippets

print(result.raw)               # raw ASR before any Wispr formatting
print(result.formatted)         # after Wispr's server-side formatting pass
print(result.result_html)       # HTML string from gRPC Result message
print(result.result_plaintext)  # plaintext from gRPC Result message
print(result.result_status)     # 1=FORMATTED  2=ERROR  3=RAW_TRANSCRIPT

print(result.grpc_final)        # best text from gRPC before local post-processing
print(result.after_replacements)# after replacementsPersonal + snippetsPersonal applied
                                # (usually same as result.final unless cursor_polish fired)

print(result.post_processing)
# list of dicts for every replacement/snippet rule that fired:
# [{"kind": "replacement", "from": "teh", "to": "the", "count": 1},
#  {"kind": "snippet",     "from": "by the way", "to": "btw", "count": 2}]

print(result.cursor_polish)     # list of cursor-aware edits (reserved for future use)

print(result.warmup)
# {"ok": True, "status_code": 200}
# {"ok": False, "error": "timeout"}

print(result.network)
# {"grpc_timeout_seconds": 90}

print(result.config)
# {"languages": ["en", "hien"], "styleConfig": {"other": "CASUAL", ...}}


# ─────────────────────────────────────────────────────────────────────────────
# 5. Common style/language combos
# ─────────────────────────────────────────────────────────────────────────────

# Formal work email
result = client.transcribe(AUDIO, style="FORMAL", app_type="email", cleanup="MEDIUM")

# Hindi only
result = client.transcribe(AUDIO, languages=["hi"])

# English only, heavy cleanup
result = client.transcribe(AUDIO, languages=["en"], cleanup="HIGH")

# Very casual personal message
result = client.transcribe(AUDIO, style="VERY_CASUAL", app_type="personal")

# Excited (more exclamations)
result = client.transcribe(AUDIO, style="EXCITED")

# No warmup (faster, slightly less reliable cold start)
result = client.transcribe(AUDIO, warmup=False)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Command mode
# ─────────────────────────────────────────────────────────────────────────────
# Speak a command (e.g. "make this formal"), applies it to selected_text.

cmd = client.command(
    AUDIO,
    selected_text="i am going to work tmrw",

    # all the same optional params as transcribe:
    languages=["en", "hien"],
    style="CASUAL",
    app_type="other",
    cleanup=None,
    before_text="",
    after_text="",
    content_text="",
    warmup=True,
    extra_overrides=None,
)

print(cmd.spoken_command)   # what you said, e.g. "make this more formal"
print(cmd.selected_text)    # the text that was passed in
print(cmd.command)          # parsed "Command:" section from Wispr response
print(cmd.action)           # parsed "Action:" section
print(cmd.result)           # ← the edited/transformed text — use this
print(cmd.raw_output)       # full raw string from wisprflow_rest

# the underlying transcription result is also available:
print(cmd.transcript.raw)
print(cmd.transcript.final)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Live / streaming session
# ─────────────────────────────────────────────────────────────────────────────
# Feed raw 16kHz mono PCM16 bytes in real time.
# Text arrives after final commit (current Wispr behavior).

# ── 7a. Context manager (recommended) ────────────────────────────────────────

def my_partial_callback(msg: dict):
    # called for each partial result as it arrives
    # msg = {"type": "partial", "kind": "raw"|"formatted"|"result", "text": "..."}
    print(f"[partial/{msg['kind']}] {msg['text']}")

with client.live_session(
    languages=["en", "hien"],
    style="CASUAL",
    app_type="other",
    cleanup="NONE",
    before_text="",
    after_text="",
    selected_text="",
    content_text="",
    partial_callback=my_partial_callback,
    extra_overrides=None,
) as sess:
    # feed PCM16 bytes — e.g. from a mic, a file read in chunks, etc.
    with open("test_case_1.wav", "rb") as f:
        f.read(44)                          # skip WAV header
        while chunk := f.read(6400):        # ~200ms chunks at 16kHz/16bit
            sess.send(chunk)

# result available after the with-block exits
print(sess.result.final)
print(sess.result.raw)
print(sess.result.post_processing)

# ── 7b. Manual control ────────────────────────────────────────────────────────

sess = client.live_session(languages=["en"])
sess.start()

with open("test_case_1.wav", "rb") as f:
    f.read(44)
    while chunk := f.read(6400):
        sess.send(chunk)

result = sess.finish()      # blocks until transcript arrives
print(result.final)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Config management — client.config
# ─────────────────────────────────────────────────────────────────────────────

# ── dictionary ────────────────────────────────────────────────────────────────
client.config.add_word("Arush")                # adds to dictionaryPersonal
client.config.add_word("Dube", starred=True)   # adds to dictionaryPersonalStarred
client.config.remove_word("Arush")
print(client.config.dictionary)               # ["Dube", ...]

# ── replacements (applied after transcription) ────────────────────────────────
client.config.add_replacement("teh", "the")
client.config.add_replacement("dont", "don't")
client.config.remove_replacement("teh")
print(client.config.replacements)             # {"dont": "don't", ...}

# ── snippets (spoken phrase → short form) ─────────────────────────────────────
client.config.add_snippet("by the way", "btw")
client.config.add_snippet("as soon as possible", "ASAP")
client.config.remove_snippet("by the way")
print(client.config.snippets)                 # {"as soon as possible": "ASAP"}

# ── style defaults ────────────────────────────────────────────────────────────
client.config.set_style("work",     "FORMAL")
client.config.set_style("email",    "FORMAL")
client.config.set_style("personal", "CASUAL")
client.config.set_style("other",    "CASUAL")

# ── default cleanup level ─────────────────────────────────────────────────────
client.config.set_cleanup("MEDIUM")

# ── persist to disk ───────────────────────────────────────────────────────────
client.config.save()        # writes wispr_config.json
client.config.reload()      # re-reads from disk (picks up external changes)

# ── raw access ────────────────────────────────────────────────────────────────
raw = client.config.raw()   # the full dict
print(raw["styleConfig"])
print(raw["dictionaryPersonal"])
print(raw["replacementsPersonal"])
print(raw["snippetsPersonal"])
print(raw["autoCleanupLevel"])
print(raw["editingStrength"])
print(raw["signatureConfig"])
print(raw["languages"])
print(raw["user"])


# ─────────────────────────────────────────────────────────────────────────────
# 9. Test matrices
# ─────────────────────────────────────────────────────────────────────────────

# ── cleanup matrix: all 4 levels on one file ──────────────────────────────────
results = client.run_cleanup_matrix(
    AUDIO,
    languages=["en", "hien"],
    style="CASUAL",
    app_type="other",
    log=print,              # any callable(str), defaults to print
)
# returns dict keyed by level
print(results["NONE"].final)
print(results["LIGHT"].final)
print(results["MEDIUM"].final)
print(results["HIGH"].final)

# ── language matrix: 4 language/dict combos ───────────────────────────────────
results = client.run_language_matrix(
    AUDIO,
    style="CASUAL",
    app_type="other",
    log=print,
)
# keys: "English only + dict", "Catch-All + dict", "Hindi only + dict", "Catch-All without dict"
print(results["English only + dict"].final)
print(results["Catch-All + dict"].final)
print(results["Hindi only + dict"].final)
print(results["Catch-All without dict"].final)

# ── full suite: both matrices back to back ────────────────────────────────────
all_results = client.run_full_test_suite(
    AUDIO,
    languages=["en", "hien"],
    log=print,
)
print(all_results["cleanup"]["HIGH"].final)
print(all_results["language"]["Catch-All + dict"].final)


# ─────────────────────────────────────────────────────────────────────────────
# 10. Per-call overrides without touching config on disk
# ─────────────────────────────────────────────────────────────────────────────
# extra_overrides is merged on top of wispr_config.json for that call only.
# Anything in the config schema can be overridden.

result = client.transcribe(
    AUDIO,
    extra_overrides={
        "dictionaryPersonal": ["TempWord", "AnotherWord"],  # replace dict just for this call
        "replacementsPersonal": {"u": "you"},               # replace rules just for this call
        "signatureConfig": {
            "flowSignaturePrefix": "WRITTEN",
            "flowSignatureHyperlink": True,
            "signEmails": True,
        },
    },
)


# ─────────────────────────────────────────────────────────────────────────────
# 11. Standalone audio conversion utility (no client needed)
# ─────────────────────────────────────────────────────────────────────────────
from wisprflow_sdk import convert_to_wispr_wav, downsample_to_pcm16

# Convert any audio file to 16kHz mono 16-bit PCM WAV
wav_path = convert_to_wispr_wav("input.m4a")                       # saves as input.m4a_wispr.wav
wav_path = convert_to_wispr_wav("input.m4a", "output.wav")         # explicit output path

# Downsample float32 PCM samples (e.g. from PyAudio/sounddevice) to PCM16 bytes
import array
float_samples = array.array("f", [0.0] * 1024)   # your float32 samples
pcm_bytes = downsample_to_pcm16(float_samples, input_rate=44100)   # → 16kHz PCM16 bytes