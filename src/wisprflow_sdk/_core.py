"""
wisprflow_sdk.py — Programmatic Wispr Flow SDK
==========================================
Drop-in replacement for the Flask server + gRPC client stack.
All features preserved; no HTTP server, no UI dependencies.

Quick start
-----------
    from wisprflow_sdk import WisprClient

    client = WisprClient()

    # One-shot transcription (any audio format — ffmpeg converts automatically)
    result = client.transcribe("recording.m4a")
    print(result.final)

    # Rich details
    result = client.transcribe("recording.wav", style="FORMAL", cleanup="MEDIUM")
    print(result.raw, result.formatted, result.final)
    print(result.post_processing)   # replacements / snippets that fired

    # Command mode
    result = client.command("recording.wav", selected_text="I am going to work.")
    print(result.action, result.result)

    # Live / streaming session (feeds raw PCM bytes in real-time)
    with client.live_session() as sess:
        for chunk in my_mic_source():
            sess.send(chunk)
    print(sess.result.final)

    # Test matrices
    client.run_cleanup_matrix("audio.wav", languages=["en", "hien"])
    client.run_language_matrix("audio.wav")

Requirements
------------
    pip install grpcio requests
    ffmpeg must be on PATH for audio conversion

Config
------
    wispr_config.json   — user preferences (autoloaded from script dir or WISPRFLOW_TEST_CFG env var)
    %APPDATA%\\Wispr Flow\\session.json  — auth token (written by Wispr Flow desktop app)
"""

from __future__ import annotations

import base64
import copy
import io
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Generator, Iterator, List, Optional

import requests
import grpc
from grpc import ssl_channel_credentials


# ─────────────────────────────────────────────────────────────────────────────
# Constants & enums
# ─────────────────────────────────────────────────────────────────────────────

WRITING_STYLE = {"UNSPECIFIED": 0, "FORMAL": 1, "CASUAL": 2, "VERY_CASUAL": 3, "EXCITED": 4}
EDITING_STRENGTH = {"UNSPECIFIED": 0, "VERBATIM": 1, "LIGHT": 2, "MEDIUM": 3, "HEAVY": 4}
SIGNATURE_PREFIX = {"UNSPECIFIED": 0, "SPOKEN": 1, "WRITTEN": 2}
LANGUAGE_ENUM = {"en": 1, "engb": 2, "hi": 20, "hien": 21}

# Dashboard "cleanup level" → gRPC editingStrength
CLEANUP_TO_STRENGTH = {
    "NONE": "VERBATIM",
    "LIGHT": "VERBATIM",
    "MEDIUM": "LIGHT",
    "HIGH": "MEDIUM",
    "VERBATIM": "VERBATIM",  # passthrough
}

APP_TYPE_ENUM = {
    "other": 1, "browser": 2, "personal": 3,
    "work": 4, "email": 5, "chatbot": 6, "developer": 7,
}

GRPC_HOST_TEMPLATE = "model-{model_id}.grpc.api.baseten.co"
GRPC_PORT = 443
GRPC_METHOD = "/flow_api.v1.TranscriptionService/TranscribeStream"
GRPC_TIMEOUT = 90

DEFAULT_MODEL_ID = "v31pl413"

DEFAULT_CONFIG: dict = {
    "user": {"firstName": "", "lastName": "", "email": ""},
    "languages": ["en", "hien"],
    "dictionaryPersonal": [],
    "dictionaryPersonalStarred": [],
    "replacementsPersonal": {},
    "snippetsPersonal": {},
    "styleConfig": {"personal": "CASUAL", "work": "FORMAL", "email": "FORMAL", "other": "CASUAL"},
    "autoCleanupLevel": "NONE",
    "editingStrength": "VERBATIM",
    "signatureConfig": {
        "flowSignaturePrefix": "UNSPECIFIED",
        "flowSignatureHyperlink": False,
        "signEmails": False,
    },
}

MAX_LIVE_SECONDS = 300
MAX_LIVE_BYTES = 25 * 1024 * 1024


# ─────────────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TranscriptResult:
    """Returned by WisprClient.transcribe() and live sessions."""
    raw: str = ""
    formatted: str = ""
    result_html: str = ""
    result_plaintext: str = ""
    result_status: int = 0
    grpc_final: str = ""
    after_replacements: str = ""
    final: str = ""
    post_processing: list = field(default_factory=list)
    cursor_polish: list = field(default_factory=list)
    warmup: dict = field(default_factory=dict)
    network: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return self.final


@dataclass
class CommandResult:
    """Returned by WisprClient.command()."""
    transcript: TranscriptResult = field(default_factory=TranscriptResult)
    spoken_command: str = ""
    selected_text: str = ""
    command: str = ""
    action: str = ""
    result: str = ""
    raw_output: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Protobuf encoding (hand-rolled — no protoc required)
# ─────────────────────────────────────────────────────────────────────────────

def _varint(v: int) -> bytes:
    out = []
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)
    return bytes(out)

def _field(fn: int, wt: int, data: bytes) -> bytes:
    return _varint((fn << 3) | wt) + data

def _bytes_field(fn: int, data: bytes) -> bytes:
    return _field(fn, 2, _varint(len(data)) + data)

def _string(fn: int, v: str) -> bytes:
    return _bytes_field(fn, v.encode("utf-8"))

def _varint_field(fn: int, v: int) -> bytes:
    return _field(fn, 0, _varint(v))

def _msg(fn: int, data: bytes) -> bytes:
    return _bytes_field(fn, data)

def _bool_field(fn: int, v: bool) -> bytes:
    return _varint_field(fn, 1 if v else 0)

def _map_entry(fn: int, key: str, value: str) -> bytes:
    return _msg(fn, _string(1, key) + _string(2, value))

def _clean_list(values) -> list:
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    return [str(v).strip() for v in values if str(v).strip()]


# ─────────────────────────────────────────────────────────────────────────────
# Protobuf message builders
# ─────────────────────────────────────────────────────────────────────────────

def _build_version(major=1, minor=5, patch=113) -> bytes:
    return _varint_field(1, major) + _varint_field(2, minor) + _varint_field(3, patch)

def _build_client() -> bytes:
    return _string(1, "Wispr Flow") + _varint_field(2, 2) + _msg(3, _build_version())

def _build_metadata(user_id: str, session_id: str, request_id: str) -> bytes:
    return (
        _string(1, user_id)
        + _string(2, session_id)
        + _string(3, request_id)
        + _varint_field(4, 1)   # AUDIO_ENCODING_WAV
        + _varint_field(5, 1)   # ENVIRONMENT_PRODUCTION
        + _msg(6, _build_client())
    )

def _build_general_style(cfg: dict) -> bytes:
    sc = cfg.get("styleConfig", {})
    data = b""
    for fn, key in ((1, "other"), (2, "personal"), (3, "work"), (4, "email")):
        v = WRITING_STYLE.get(str(sc.get(key, "UNSPECIFIED")).upper(), 0)
        if v:
            data += _varint_field(fn, v)
    return data

def _build_tagging() -> bytes:
    return _bool_field(1, False)

def _build_signature(cfg: dict) -> bytes:
    sig = cfg.get("signatureConfig", {})
    prefix = SIGNATURE_PREFIX.get(str(sig.get("flowSignaturePrefix", "UNSPECIFIED")).upper(), 0)
    data = _varint_field(1, prefix) if prefix else b""
    data += _bool_field(2, bool(sig.get("flowSignatureHyperlink", False)))
    data += _bool_field(3, bool(sig.get("signEmails", False)))
    return data

def _build_style_config(cfg: dict) -> bytes:
    cleanup = str(cfg.get("autoCleanupLevel", "")).upper()
    strength_str = CLEANUP_TO_STRENGTH.get(cleanup, str(cfg.get("editingStrength", "VERBATIM")).upper())
    strength = EDITING_STRENGTH.get(strength_str, 1)
    data = b""
    gs = _build_general_style(cfg)
    if gs:
        data += _msg(1, gs)
    t = _build_tagging()
    if t:
        data += _msg(3, t)
    s = _build_signature(cfg)
    if s:
        data += _msg(4, s)
    if strength:
        data += _varint_field(5, strength)
    return data

def _build_vocabulary(cfg: dict) -> bytes:
    data = b""
    for w in cfg.get("dictionaryPersonal", []):
        data += _string(1, w)
    for w in cfg.get("dictionaryPersonalStarred", []):
        data += _string(3, w)
    return data

def _build_replacements(cfg: dict) -> bytes:
    data = b""
    for k, v in cfg.get("replacementsPersonal", {}).items():
        data += _map_entry(1, k, v)
    for k, v in cfg.get("snippetsPersonal", {}).items():
        data += _map_entry(3, k, v)
    return data

def _build_preferences(cfg: dict, languages: list) -> bytes:
    u = cfg.get("user", {})
    user_msg = b""
    if u.get("firstName"):
        user_msg += _string(1, u["firstName"])
    if u.get("lastName"):
        user_msg += _string(2, u["lastName"])
    if u.get("email"):
        user_msg += _string(3, u["email"])

    data = _msg(1, user_msg)

    lang_values = [LANGUAGE_ENUM[l.strip().lower()] for l in languages if l.strip().lower() in LANGUAGE_ENUM]
    if lang_values:
        data += _bytes_field(2, b"".join(_varint(v) for v in lang_values))

    vocab = _build_vocabulary(cfg)
    if vocab:
        data += _msg(3, vocab)
    repl = _build_replacements(cfg)
    if repl:
        data += _msg(4, repl)
    data += _msg(5, _build_style_config(cfg))
    return data

def _build_app(name="", bundle_id="", url="", app_type=0) -> bytes:
    data = b""
    if name:      data += _string(1, name)
    if bundle_id: data += _string(2, bundle_id)
    if url:       data += _string(3, url)
    if app_type:  data += _varint_field(4, int(app_type))
    return data

def _build_textbox(before="", after="", selected="", contents="") -> bytes:
    data = b""
    if contents:  data += _string(1, contents)
    if before:    data += _string(2, before)
    if selected:  data += _string(3, selected)
    if after:     data += _string(4, after)
    return data

def _build_dynamic_vocab(ax=None, ocr=None, vars_=None, files=None) -> bytes:
    data = b""
    for w in _clean_list(ax)[:50]:    data += _string(1, w)
    for w in _clean_list(ocr)[:50]:   data += _string(2, w)
    for w in _clean_list(vars_)[:50]: data += _string(3, w)
    for w in _clean_list(files)[:50]: data += _string(4, w)
    return data

def _build_context(cfg: dict, ctx: dict) -> bytes:
    """Build the Context gRPC message from a context dict.

    Context dict keys (all optional):
        before_text, after_text, selected_text, content_text, content_html,
        textbox_contents, app_name, bundle_id, url, app_type (int),
        screen_ax (list), screen_ocr (list), variable_names (list),
        file_names (list), session_apps (list of dicts), screenshot (bytes),
        allow_dynamic_vocab (bool)
    """
    data = b""
    app_msg = _build_app(
        name=ctx.get("app_name", ""),
        bundle_id=ctx.get("bundle_id", ""),
        url=ctx.get("url", ""),
        app_type=ctx.get("app_type", 0),
    )
    if app_msg:
        data += _msg(1, app_msg)
    tb = _build_textbox(
        before=ctx.get("before_text", ""),
        after=ctx.get("after_text", ""),
        selected=ctx.get("selected_text", ""),
        contents=ctx.get("textbox_contents", ""),
    )
    if tb:
        data += _msg(2, tb)
    if ctx.get("allow_dynamic_vocab"):
        dv = _build_dynamic_vocab(
            ax=ctx.get("screen_ax"),
            ocr=ctx.get("screen_ocr"),
            vars_=ctx.get("variable_names"),
            files=ctx.get("file_names"),
        )
        if dv:
            data += _msg(3, dv)
    if ctx.get("content_text"):
        data += _string(5, ctx["content_text"])
    if ctx.get("content_html"):
        data += _string(6, ctx["content_html"])
    for app_ctx in ctx.get("session_apps") or []:
        am = _build_app(
            name=app_ctx.get("name", ""),
            bundle_id=app_ctx.get("bundle_id", ""),
            url=app_ctx.get("url", ""),
            app_type=app_ctx.get("type", 0),
        )
        if am:
            data += _msg(7, am)
    sc = ctx.get("screenshot")
    if sc:
        data += _bytes_field(8, sc if isinstance(sc, bytes) else bytes(sc))
    return data

def _build_commit(is_final: bool) -> bytes:
    return _varint_field(4, 1 if is_final else 2)

def _build_init_request(user_id, session_id, request_id, cfg, languages) -> bytes:
    init = _msg(1, _build_metadata(user_id, session_id, request_id)) + _msg(2, _build_preferences(cfg, languages))
    return _msg(1, init)

def _build_context_request(cfg: dict, ctx: dict) -> bytes:
    return _msg(2, _build_context(cfg, ctx))

def _build_audio_request(audio_bytes: bytes, commit: bool) -> bytes:
    audio_file = _bytes_field(1, audio_bytes)
    payload = _msg(2, audio_file)
    data = _msg(3, payload)
    if commit:
        data += _build_commit(True)
    return data

def _build_packet_request(packets, commit: bool) -> bytes:
    if isinstance(packets, (bytes, bytearray)):
        packets = [bytes(packets)]
    packed = b"".join(_bytes_field(1, bytes(p)) for p in packets if p)
    payload = _msg(1, packed)
    data = _msg(3, payload)
    if commit:
        data += _build_commit(True)
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Protobuf response parsing
# ─────────────────────────────────────────────────────────────────────────────

def _decode_varint(data: bytes, pos: int):
    result, shift = 0, 0
    while True:
        if pos >= len(data):
            raise ValueError("Truncated varint")
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
        if shift > 63:
            raise ValueError("Varint too long")
    return result, pos

def _parse_text_msg(data: bytes) -> str:
    pos = 0
    while pos < len(data):
        tag, pos = _decode_varint(data, pos)
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            length, pos = _decode_varint(data, pos)
            val = data[pos:pos+length]; pos += length
            if fn == 1:
                return val.decode("utf-8", errors="replace")
        elif wt == 0:
            _, pos = _decode_varint(data, pos)
    return ""

def _parse_transcription(data: bytes) -> dict:
    pos, out = 0, {}
    while pos < len(data):
        try:
            tag, pos = _decode_varint(data, pos)
        except ValueError:
            break
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            try:
                length, pos = _decode_varint(data, pos)
            except ValueError:
                break
            if pos + length > len(data):
                break
            val = data[pos:pos+length]; pos += length
            if fn == 1: out["html"] = val.decode("utf-8", errors="replace")
            elif fn == 2: out["plaintext"] = val.decode("utf-8", errors="replace")
        elif wt == 0:
            try:
                value, pos = _decode_varint(data, pos)
            except ValueError:
                break
            if fn == 3: out["num_tokens"] = value
        elif wt == 5:
            pos += 4
        else:
            break
    return out

def _parse_response(data: bytes) -> dict:
    pos, out = 0, {}
    while pos < len(data):
        try:
            tag, pos = _decode_varint(data, pos)
        except ValueError:
            break
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            try:
                length, pos = _decode_varint(data, pos)
            except ValueError:
                break
            if pos + length > len(data):
                break
            val = data[pos:pos+length]; pos += length

            if fn == 1:  # Result
                ipos = 0
                while ipos < len(val):
                    try:
                        itag, ipos = _decode_varint(val, ipos)
                    except ValueError:
                        break
                    ifn, iwt = itag >> 3, itag & 7
                    if iwt == 2:
                        try:
                            ilen, ipos = _decode_varint(val, ipos)
                        except ValueError:
                            break
                        if ipos + ilen > len(val):
                            break
                        iv = val[ipos:ipos+ilen]; ipos += ilen
                        if ifn == 1:
                            tx = _parse_transcription(iv)
                            if tx.get("html"):      out["result_html"] = tx["html"]
                            if tx.get("plaintext"): out["result_plaintext"] = tx["plaintext"]
                    elif iwt == 0:
                        try:
                            value, ipos = _decode_varint(val, ipos)
                        except ValueError:
                            break
                        if ifn == 5: out["result_status"] = value
                    else:
                        break

            elif fn == 2:  # State
                spos = 0
                while spos < len(val):
                    try:
                        stag, spos = _decode_varint(val, spos)
                    except ValueError:
                        break
                    sfn, swt = stag >> 3, stag & 7
                    if swt == 2:
                        try:
                            slen, spos = _decode_varint(val, spos)
                        except ValueError:
                            break
                        if spos + slen > len(val):
                            break
                        sv = val[spos:spos+slen]; spos += slen
                        if sfn == 2:
                            t = _parse_text_msg(sv)
                            if t: out["raw_text"] = t
                        elif sfn == 3:
                            t = _parse_text_msg(sv)
                            if t: out["formatted_text"] = t
                    elif swt == 0:
                        try:
                            _, spos = _decode_varint(val, spos)
                        except ValueError:
                            break
                    else:
                        break
        elif wt == 0:
            try:
                _, pos = _decode_varint(data, pos)
            except ValueError:
                break
        else:
            break
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Post-processing
# ─────────────────────────────────────────────────────────────────────────────

def _clean_text(text: str) -> str:
    text = text.lstrip('\ufffd').strip()
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+.*$', '', text).strip()
    return text

def _replace_map(text: str, mapping: dict, kind: str):
    fired = []
    items = sorted(
        ((str(k), str(v)) for k, v in (mapping or {}).items() if str(k).strip()),
        key=lambda x: len(x[0]), reverse=True,
    )
    for key, value in items:
        pattern = re.compile(r'\b' + re.escape(key) + r'\b', flags=re.IGNORECASE)
        text, count = pattern.subn(value, text)
        if count:
            fired.append({"kind": kind, "from": key, "to": value, "count": count})
    return text, fired

def _apply_replacements(text: str, cfg: dict):
    text, r = _replace_map(text, cfg.get("replacementsPersonal", {}), "replacement")
    text, s = _replace_map(text, cfg.get("snippetsPersonal", {}), "snippet")
    return text, r + s

def _phrase_count(text: str, phrase: str) -> int:
    if not text or not phrase:
        return 0
    return len(re.findall(r"\b" + re.escape(str(phrase)) + r"\b", str(text), re.IGNORECASE))

def _merge_post_processing(items: list) -> list:
    seen, merged = set(), []
    for item in items or []:
        key = (item.get("kind", ""), str(item.get("from", "")).lower(), str(item.get("to", "")).lower())
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged

def _infer_post_processing(details: dict, cfg: dict) -> list:
    before_texts = [details.get(k, "") for k in ("raw", "formatted", "result_plaintext", "result_html", "grpc_final")]
    after_text = details.get("after_replacements", "") or details.get("final", "")
    inferred = []
    for kind, mapping in (("replacement", cfg.get("replacementsPersonal", {})), ("snippet", cfg.get("snippetsPersonal", {}))):
        for source, target in (mapping or {}).items():
            source, target = str(source), str(target)
            if not source.strip() or not target.strip():
                continue
            source_before = sum(_phrase_count(t, source) for t in before_texts)
            target_after = _phrase_count(after_text, target)
            if source_before and target_after:
                inferred.append({"kind": kind, "from": source, "to": target, "count": target_after, "source": "inferred"})
    return inferred


# ─────────────────────────────────────────────────────────────────────────────
# Config management
# ─────────────────────────────────────────────────────────────────────────────

class WisprConfig:
    """Manages wispr_config.json with a clean API."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.environ.get("WISPRFLOW_TEST_CFG") or str(
            Path(__file__).parent / "wispr_config.json"
        )
        self._data: dict = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.path):
            with open(self.path) as f:
                return json.load(f)
        return copy.deepcopy(DEFAULT_CONFIG)

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2)

    def reload(self):
        self._data = self._load()

    # ── dictionary ────────────────────────────────────────────────────────────

    @property
    def dictionary(self) -> list:
        return self._data.setdefault("dictionaryPersonal", [])

    def add_word(self, word: str, starred: bool = False):
        key = "dictionaryPersonalStarred" if starred else "dictionaryPersonal"
        lst = self._data.setdefault(key, [])
        if word not in lst:
            lst.append(word)

    def remove_word(self, word: str):
        for key in ("dictionaryPersonal", "dictionaryPersonalStarred"):
            lst = self._data.get(key, [])
            if word in lst:
                lst.remove(word)

    # ── replacements ──────────────────────────────────────────────────────────

    @property
    def replacements(self) -> dict:
        return self._data.setdefault("replacementsPersonal", {})

    def add_replacement(self, key: str, value: str):
        self._data.setdefault("replacementsPersonal", {})[key] = value

    def remove_replacement(self, key: str):
        self._data.get("replacementsPersonal", {}).pop(key, None)

    # ── snippets ──────────────────────────────────────────────────────────────

    @property
    def snippets(self) -> dict:
        return self._data.setdefault("snippetsPersonal", {})

    def add_snippet(self, trigger: str, expansion: str):
        self._data.setdefault("snippetsPersonal", {})[trigger] = expansion

    def remove_snippet(self, trigger: str):
        self._data.get("snippetsPersonal", {}).pop(trigger, None)

    # ── style ─────────────────────────────────────────────────────────────────

    def set_style(self, category: str, style: str):
        """category: personal|work|email|other; style: FORMAL|CASUAL|VERY_CASUAL|EXCITED"""
        self._data.setdefault("styleConfig", {})[category.lower()] = style.upper()

    def set_cleanup(self, level: str):
        """level: NONE|LIGHT|MEDIUM|HIGH"""
        level = level.upper()
        self._data["autoCleanupLevel"] = level
        self._data["editingStrength"] = CLEANUP_TO_STRENGTH.get(level, "VERBATIM")

    # ── raw access ────────────────────────────────────────────────────────────

    def raw(self) -> dict:
        return self._data

    def merge_overrides(self, overrides: Optional[dict]) -> dict:
        merged = copy.deepcopy(self._data)
        if not overrides:
            return merged
        if "editingStrength" in overrides and "autoCleanupLevel" not in overrides:
            merged.pop("autoCleanupLevel", None)
        for key, value in overrides.items():
            if value is None:
                continue
            if key == "styleConfig" and isinstance(value, dict):
                merged.setdefault("styleConfig", {}).update({k: v for k, v in value.items() if v})
            else:
                merged[key] = value
        return merged


# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────

def _get_token() -> str:
    session_path = os.path.expandvars(r"%APPDATA%\Wispr Flow\session.json")
    with open(session_path) as f:
        session = json.load(f)
    auth_data = json.loads(session["sb-dodjkfqhwrzqjwkfnthl-auth-token"])
    return auth_data["access_token"]

def _token_user_id(token: str) -> str:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.b64decode(payload))["sub"]

def _runtime_config() -> dict:
    runtime_path = os.path.expandvars(r"%LOCALAPPDATA%\WisprFlow\wispr_runtime.json")
    cfg = {}
    if os.path.exists(runtime_path):
        with open(runtime_path) as f:
            cfg = json.load(f)
    api_key = cfg.get("basteKey") or cfg.get("apiKey", "")
    model_id = cfg.get("modelId") or DEFAULT_MODEL_ID
    env = cfg.get("environment") or "production"
    host = cfg.get("url") or GRPC_HOST_TEMPLATE.format(model_id=model_id)
    return {
        "host": host,
        "model": f"model-{model_id}",
        "env": env,
        "api_key": f"Api-Key {api_key}",
    }

def _grpc_meta(token: str, rcfg: dict) -> list:
    return [
        ("authorization",         f"Bearer {token}"),
        ("baseten-authorization", rcfg["api_key"]),
        ("baseten-model-id",      rcfg["model"]),
        ("x-baseten-environment", rcfg["env"]),
        ("flow-debug",            "false"),
        ("disable-formatting",    "false"),
        ("content-type",          "application/grpc"),
        ("user-agent",            "grpc-python/1.59.0"),
        ("te",                    "trailers"),
    ]

# ─────────────────────────────────────────────────────────────────────────────
# REST: command routing (inlined from wisprflow_rest — no external dependency)
# ─────────────────────────────────────────────────────────────────────────────

def _rest_command_route(
    token: str,
    command_text: str,
    selected_text: str,
    language: list = ["en"],
    style: str = "CASUAL",
) -> str:
    """Route an already-transcribed command through Wispr's command endpoint."""
    lang_val = language[0] if language else "en"
    payload = {
        "full_text": selected_text,
        "selected_text": selected_text,
        "instruction": command_text,
        "language": lang_val,
        "personalization_style_settings": {
            "other": style,
            "personal": style,
            "work": style,
            "email": style,
            "level": "MEDIUM",
        },
    }
    auth = token if not token.startswith("Bearer ") else token[len("Bearer "):]
    resp = requests.post(
        "https://api.wisprflow.ai/llm/command_mode_route",
        headers={
            "Authorization": auth,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
        json=payload,
    )
    if resp.status_code == 200:
        j = resp.json()
        if "arguments" in j:
            return f"Command: {command_text}\nAction: {j.get('name')}\nResult:\n{j['arguments']}"
        return f"Command: {command_text}\nAction: {j.get('name', 'unknown')}\nResult:\nNo arguments generated."
    return f"Error {resp.status_code}: {resp.text}"


# ─────────────────────────────────────────────────────────────────────────────
# Audio utilities
# ─────────────────────────────────────────────────────────────────────────────

def convert_to_wispr_wav(input_path: str, output_path: Optional[str] = None) -> str:
    """Convert any audio file to 16kHz mono 16-bit PCM WAV via ffmpeg."""
    out = output_path or input_path + "_wispr.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", out],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )
    return out

def _ensure_wav(path: str) -> tuple[str, bool]:
    """Returns (wav_path, needs_cleanup). Converts if not already correct format."""
    if path.lower().endswith(".wav"):
        # Try reading it; if it's already 16kHz/mono/s16 we still re-encode to be safe
        tmp = tempfile.mktemp(suffix="_wispr.wav")
        convert_to_wispr_wav(path, tmp)
        return tmp, True
    tmp = tempfile.mktemp(suffix="_wispr.wav")
    convert_to_wispr_wav(path, tmp)
    return tmp, True

def downsample_to_pcm16(samples, input_rate: int) -> bytes:
    """Downsample float32 PCM to 16kHz 16-bit PCM bytes."""
    target_rate = 16000
    ratio = input_rate / target_rate
    length = max(0, int(len(samples) / ratio))
    out = bytearray(length * 2)
    view = memoryview(out).cast("h")
    offset = 0
    for i in range(length):
        next_offset = min(len(samples), round((i + 1) * ratio))
        chunk = samples[offset:next_offset]
        s = sum(chunk) / len(chunk) if chunk else 0.0
        offset = next_offset
        s = max(-1.0, min(1.0, s))
        view[i] = int(s * 0x7FFF if s >= 0 else s * 0x8000)
    return bytes(out)


# ─────────────────────────────────────────────────────────────────────────────
# Core gRPC transcription
# ─────────────────────────────────────────────────────────────────────────────

def _transient_error(e: Exception) -> bool:
    if isinstance(e, grpc.RpcError):
        try:
            return e.code() in {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.RESOURCE_EXHAUSTED}
        except Exception:
            pass
    text = str(e).lower()
    return any(m in text for m in ("timeout", "timed out", "temporarily unavailable", "unavailable", "deadline"))

def _grpc_stream(
    cfg: dict,
    languages: list,
    audio_bytes: bytes,
    ctx: Optional[dict],
    verbose: bool = False,
) -> TranscriptResult:
    """Low-level: run a single gRPC transcription request, return TranscriptResult."""
    token = _get_token()
    user_id = _token_user_id(token)
    session_id = str(uuid.uuid4())
    request_id = str(uuid.uuid4())
    rcfg = _runtime_config()

    if verbose:
        print(f"[wispr] user={user_id[:8]}... session={session_id[:8]}... host={rcfg['host']}")

    def messages():
        yield _build_init_request(user_id, session_id, request_id, cfg, languages) + _build_commit(False)
        if ctx:
            ctx_bytes = _build_context_request(cfg, ctx)
            if ctx_bytes:
                yield ctx_bytes + _build_commit(False)
        yield _build_audio_request(audio_bytes, commit=True)

    credentials = ssl_channel_credentials()
    channel = grpc.secure_channel(f"{rcfg['host']}:{GRPC_PORT}", credentials)
    call = channel.stream_stream(GRPC_METHOD, request_serializer=lambda x: x, response_deserializer=lambda x: x)

    best = {}
    for rb in call(messages(), metadata=_grpc_meta(token, rcfg), timeout=GRPC_TIMEOUT):
        if not rb or (rb and rb[0] == 0x22):  # skip heartbeats
            continue
        parsed = _parse_response(rb)
        if parsed.get("formatted_text"): best["formatted_text"] = parsed["formatted_text"]
        if parsed.get("raw_text"):       best["raw_text"] = parsed["raw_text"]
        if parsed.get("result_html"):    best["result_html"] = parsed["result_html"]
        if parsed.get("result_plaintext"): best["result_plaintext"] = parsed["result_plaintext"]
        if parsed.get("result_status"):  best["result_status"] = parsed["result_status"]

    grpc_final = _clean_text(
        best.get("result_html") or best.get("result_plaintext")
        or best.get("formatted_text") or best.get("raw_text") or ""
    )
    after_rep, fired = _apply_replacements(grpc_final, cfg)
    fired = _merge_post_processing(fired + _infer_post_processing(
        {"raw": _clean_text(best.get("raw_text", "")),
         "formatted": _clean_text(best.get("formatted_text", "")),
         "result_plaintext": _clean_text(best.get("result_plaintext", "")),
         "result_html": _clean_text(best.get("result_html", "")),
         "grpc_final": grpc_final,
         "after_replacements": after_rep},
        cfg,
    ))

    return TranscriptResult(
        raw=_clean_text(best.get("raw_text", "")),
        formatted=_clean_text(best.get("formatted_text", "")),
        result_html=_clean_text(best.get("result_html", "")),
        result_plaintext=_clean_text(best.get("result_plaintext", "")),
        result_status=best.get("result_status", 0),
        grpc_final=grpc_final,
        after_replacements=after_rep,
        final=after_rep,
        post_processing=fired,
        config={"languages": languages, "styleConfig": cfg.get("styleConfig", {})},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Live / streaming session
# ─────────────────────────────────────────────────────────────────────────────

class LiveSession:
    """
    Streaming upload session. Use as a context manager or call manually.

    Usage (context manager — recommended):
        with client.live_session(languages=["en"]) as sess:
            for chunk in mic_source():
                sess.send(chunk)        # raw PCM16 bytes at 16kHz
        print(sess.result.final)

    Usage (manual):
        sess = client.live_session()
        sess.start()
        sess.send(pcm_bytes)
        sess.finish()
        print(sess.result.final)

    The partial_callback, if supplied, is called on each partial result dict
    as text arrives from Wispr (before final commit).
    """

    def __init__(
        self,
        cfg: dict,
        languages: list,
        ctx: Optional[dict] = None,
        partial_callback: Optional[Callable] = None,
        verbose: bool = False,
    ):
        self._cfg = cfg
        self._languages = languages
        self._ctx = ctx or {}
        self._partial_callback = partial_callback
        self._verbose = verbose

        self._in: queue.Queue = queue.Queue(maxsize=500)
        self._out: queue.Queue = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._active = False
        self._done = False

        self.result: Optional[TranscriptResult] = None
        self.partials: list = []
        self._bytes_sent = 0
        self._chunks_sent = 0

    def start(self):
        if self._active:
            raise RuntimeError("Session already started")
        self._active = True
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def send(self, pcm16_bytes: bytes):
        """Send raw PCM16 16kHz mono bytes."""
        if not self._active:
            raise RuntimeError("Session not started. Call start() first.")
        if self._bytes_sent + len(pcm16_bytes) > MAX_LIVE_BYTES:
            raise RuntimeError("Live session byte limit exceeded")
        self._in.put(pcm16_bytes, timeout=2)
        self._bytes_sent += len(pcm16_bytes)
        self._chunks_sent += 1

    def finish(self) -> TranscriptResult:
        """Signal end of audio and wait for the final result."""
        if not self._active:
            raise RuntimeError("Session not started")
        self._active = False
        self._in.put(None)  # sentinel
        if self._worker:
            self._worker.join(timeout=GRPC_TIMEOUT + 10)
        # drain out queue
        while not self._out.empty():
            msg = self._out.get_nowait()
            self._handle_msg(msg)
        if self.result is None:
            self.result = TranscriptResult()
        return self.result

    def _handle_msg(self, msg: dict):
        kind = msg.get("type")
        if kind == "partial":
            self.partials.append(msg)
            if self._partial_callback:
                self._partial_callback(msg)
        elif kind == "final":
            self.result = TranscriptResult(**{k: msg[k] for k in TranscriptResult.__dataclass_fields__ if k in msg})
        elif kind == "error":
            raise RuntimeError(msg.get("text", "Live transcription error"))

    def _run(self):
        try:
            token = _get_token()
            user_id = _token_user_id(token)
            session_id = str(uuid.uuid4())
            request_id = str(uuid.uuid4())
            rcfg = _runtime_config()
            cfg = self._cfg

            if self._verbose:
                print(f"[wispr/live] connecting host={rcfg['host']}")

            best_raw = best_formatted = best_html = best_plaintext = ""
            best_status = 0

            def request_iter():
                yield _build_init_request(user_id, session_id, request_id, cfg, self._languages) + _build_commit(False)
                if self._ctx:
                    ctx_bytes = _build_context_request(cfg, self._ctx)
                    if ctx_bytes:
                        yield ctx_bytes + _build_commit(False)

                pending = None
                while True:
                    packet = self._in.get()
                    if packet is None:
                        break
                    if pending is not None:
                        yield _build_packet_request(pending, commit=False)
                    pending = packet
                if pending is not None:
                    yield _build_packet_request(pending, commit=True)
                else:
                    yield _build_commit(True)

            channel = grpc.secure_channel(f"{rcfg['host']}:443", ssl_channel_credentials())
            call = channel.stream_stream(GRPC_METHOD, request_serializer=lambda x: x, response_deserializer=lambda x: x)

            for rb in call(request_iter(), metadata=_grpc_meta(token, rcfg), timeout=MAX_LIVE_SECONDS + 60):
                if not rb or (rb and rb[0] == 0x22):
                    continue
                parsed = _parse_response(rb)
                nonlocal_update = {}
                if parsed.get("raw_text"):
                    best_raw = parsed["raw_text"]
                    self._out.put({"type": "partial", "kind": "raw", "text": _clean_text(best_raw)})
                if parsed.get("formatted_text"):
                    best_formatted = parsed["formatted_text"]
                    self._out.put({"type": "partial", "kind": "formatted", "text": _clean_text(best_formatted)})
                if parsed.get("result_html") or parsed.get("result_plaintext"):
                    best_html = parsed.get("result_html", best_html)
                    best_plaintext = parsed.get("result_plaintext", best_plaintext)
                    best_status = parsed.get("result_status", best_status)
                    self._out.put({"type": "partial", "kind": "result", "text": _clean_text(best_html or best_plaintext)})

            grpc_final = _clean_text(best_html or best_plaintext or best_formatted or best_raw)
            after_rep, fired = _apply_replacements(grpc_final, cfg)
            fired = _merge_post_processing(fired)

            self._out.put({
                "type": "final",
                "raw": _clean_text(best_raw),
                "formatted": _clean_text(best_formatted),
                "result_html": _clean_text(best_html),
                "result_plaintext": _clean_text(best_plaintext),
                "result_status": best_status,
                "grpc_final": grpc_final,
                "after_replacements": after_rep,
                "final": after_rep,
                "post_processing": fired,
                "cursor_polish": [],
            })
        except Exception as e:
            import traceback
            if self._verbose:
                print(f"[wispr/live] ERROR: {traceback.format_exc()}")
            self._out.put({"type": "error", "text": str(e)})
        finally:
            self._done = True

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._active:
            self.finish()
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Main client
# ─────────────────────────────────────────────────────────────────────────────

class WisprClient:
    """
    High-level Wispr Flow client.

    Parameters
    ----------
    config_path : str, optional
        Path to wispr_config.json. Defaults to WISPRFLOW_TEST_CFG env var or
        wispr_config.json next to this file.
    verbose : bool
        Print debug info to stdout.
    """

    def __init__(self, config_path: Optional[str] = None, verbose: bool = False):
        self.config = WisprConfig(config_path)
        self.verbose = verbose

    # ── helpers ───────────────────────────────────────────────────────────────

    def _norm_languages(self, languages: Optional[list]) -> list:
        if languages is None:
            languages = self.config.raw().get("languages", ["en"])
        languages = list(languages)
        if "hien" in languages and "en" not in languages:
            languages = ["en"] + languages
        return languages

    def _build_overrides(
        self,
        style: Optional[str],
        app_type: Optional[str],
        cleanup: Optional[str],
        signature_prefix: Optional[str],
        sign_emails: Optional[bool],
        signature_hyperlink: Optional[bool],
        extra: Optional[dict],
        extra_words: Optional[list],
    ) -> dict:
        overrides: dict = {}
        if style:
            style = style.upper()
            cat = (app_type or "other").lower()
            overrides["styleConfig"] = {cat: style}
        if cleanup:
            cleanup = cleanup.upper()
            overrides["autoCleanupLevel"] = cleanup
            overrides["editingStrength"] = CLEANUP_TO_STRENGTH.get(cleanup, "VERBATIM")
        if extra_words:
            overrides["dictionaryPersonal"] = self.config.dictionary + extra_words
        sig = {}
        if signature_prefix is not None: sig["flowSignaturePrefix"] = signature_prefix.upper()
        if sign_emails is not None:      sig["signEmails"] = sign_emails
        if signature_hyperlink is not None: sig["flowSignatureHyperlink"] = signature_hyperlink
        if sig:
            overrides["signatureConfig"] = {**self.config.raw().get("signatureConfig", {}), **sig}
        if extra:
            overrides.update(extra)
        return overrides

    def _build_ctx(
        self,
        before_text: str,
        after_text: str,
        selected_text: str,
        content_text: str,
        content_html: str,
        app_name: str,
        bundle_id: str,
        url: str,
        app_type_str: str,
        screen_ax: Optional[list],
        screen_ocr: Optional[list],
        variable_names: Optional[list],
        file_names: Optional[list],
        textbox_contents: str,
    ) -> Optional[dict]:
        has_ctx = any([before_text, after_text, selected_text, content_text, content_html,
                       app_name, bundle_id, url, screen_ax, screen_ocr, variable_names, file_names])
        if not has_ctx:
            return None
        allow_dv = any([screen_ax, screen_ocr, variable_names, file_names])
        return {
            "before_text": before_text,
            "after_text": after_text,
            "selected_text": selected_text,
            "content_text": content_text,
            "content_html": content_html,
            "textbox_contents": textbox_contents,
            "app_name": app_name,
            "bundle_id": bundle_id,
            "url": url,
            "app_type": APP_TYPE_ENUM.get(app_type_str.lower(), 1),
            "screen_ax": screen_ax or [],
            "screen_ocr": screen_ocr or [],
            "variable_names": variable_names or [],
            "file_names": file_names or [],
            "allow_dynamic_vocab": allow_dv,
        }

    def _warmup(self, token: str) -> dict:
        try:
            r = requests.get(
                "https://api.wisprflow.ai/warmup",
                headers={"Authorization": token,
                         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WisprFlow/1.5.113"},
                verify=False, proxies={"http": None, "https": None}, timeout=5,
            )
            if self.verbose:
                print(f"[wispr] warmup {r.status_code}")
            return {"ok": 200 <= r.status_code < 300, "status_code": r.status_code}
        except Exception as e:
            if self.verbose:
                print(f"[wispr] warmup skipped: {e}")
            return {"ok": False, "error": str(e)[:200]}

    def _run_grpc(self, wav_path: str, cfg: dict, languages: list, ctx: Optional[dict]) -> TranscriptResult:
        """Run transcription with one automatic retry on transient errors."""
        for attempt in (1, 2):
            try:
                with open(wav_path, "rb") as f:
                    audio_bytes = f.read()
                if self.verbose:
                    print(f"[wispr] audio {len(audio_bytes):,} bytes  attempt={attempt}")
                return _grpc_stream(cfg, languages, audio_bytes, ctx, verbose=self.verbose)
            except Exception as e:
                if attempt == 1 and _transient_error(e):
                    if self.verbose:
                        print(f"[wispr] transient error, retrying: {e}")
                    time.sleep(0.5)
                    continue
                raise

    # ── public API ────────────────────────────────────────────────────────────

    def transcribe(
        self,
        audio_path: str,
        *,
        languages: Optional[list] = None,
        style: Optional[str] = None,
        app_type: str = "other",
        cleanup: Optional[str] = None,
        before_text: str = "",
        after_text: str = "",
        selected_text: str = "",
        content_text: str = "",
        content_html: str = "",
        app_name: str = "",
        bundle_id: str = "",
        url: str = "",
        screen_ax: Optional[list] = None,
        screen_ocr: Optional[list] = None,
        variable_names: Optional[list] = None,
        file_names: Optional[list] = None,
        textbox_contents: str = "",
        signature_prefix: Optional[str] = None,
        sign_emails: Optional[bool] = None,
        signature_hyperlink: Optional[bool] = None,
        warmup: bool = True,
        extra_overrides: Optional[dict] = None,
        extra_words: Optional[list] = None,
    ) -> TranscriptResult:
        """
        Transcribe any audio file. Converts to 16kHz WAV automatically.

        Parameters
        ----------
        audio_path    : path to audio file (any format ffmpeg can read)
        languages     : e.g. ["en", "hien"]. Defaults to wispr_config.json.
        style         : FORMAL | CASUAL | VERY_CASUAL | EXCITED
        app_type      : personal | work | email | other (affects style bucket)
        cleanup       : NONE | LIGHT | MEDIUM | HIGH
        before_text   : text before cursor for ASR context
        after_text    : text after cursor
        selected_text : currently selected text
        content_text  : visible screen/page text
        warmup        : hit the warmup endpoint before streaming
        extra_overrides: raw config overrides dict
        extra_words: Appends to custom dictionary only for this call

        Returns
        -------
        TranscriptResult with .final, .raw, .formatted, .post_processing, etc.
        """
        languages = self._norm_languages(languages)
        overrides = self._build_overrides(style, app_type, cleanup, signature_prefix,
                                          sign_emails, signature_hyperlink, extra_overrides, extra_words)
        cfg = self.config.merge_overrides(overrides)
        ctx = self._build_ctx(before_text, after_text, selected_text, content_text, content_html,
                               app_name, bundle_id, url, app_type,
                               screen_ax, screen_ocr, variable_names, file_names, textbox_contents)

        if warmup:
            token = _get_token()
            warmup_info = self._warmup(token)
        else:
            warmup_info = {"attempted": False}

        wav_path, needs_cleanup = _ensure_wav(audio_path)
        try:
            result = self._run_grpc(wav_path, cfg, languages, ctx)
            result.warmup = warmup_info
            return result
        finally:
            if needs_cleanup:
                try:
                    os.remove(wav_path)
                except Exception:
                    pass

    def command(
        self,
        audio_path: str,
        selected_text: str = "",
        *,
        languages: Optional[list] = None,
        style: str = "CASUAL",
        app_type: str = "other",
        cleanup: Optional[str] = None,
        before_text: str = "",
        after_text: str = "",
        content_text: str = "",
        warmup: bool = True,
        extra_overrides: Optional[dict] = None,
    ) -> CommandResult:
        """
        Command Mode: transcribes spoken command, then calls Wispr's REST
        command_mode_route to apply it to selected_text.
        """

        transcript = self.transcribe(
            audio_path,
            languages=languages,
            style=style,
            app_type=app_type,
            cleanup=cleanup,
            before_text=before_text,
            after_text=after_text,
            selected_text=selected_text,
            content_text=content_text,
            warmup=warmup,
            extra_overrides=extra_overrides,
        )

        # Pick cleanest spoken command text
        spoken = ""
        for key in ("result_plaintext", "formatted", "raw", "grpc_final", "final"):
            spoken = _clean_text(getattr(transcript, key, "") or "")
            if spoken:
                break

        if not spoken:
            raise RuntimeError("Command audio produced no transcript.")

        token = _get_token()
        raw_output = _rest_command_route(token, spoken, selected_text, languages or ["en"], style)

        if self.verbose:
            print(f"[wispr/command] spoken={spoken!r}")
            print(f"[wispr/command] output=\n{raw_output}")

        cmd = (re.search(r"Command:\s*([\s\S]*?)(?:\nAction:|$)", raw_output or "") or [None, ""])[1].strip()
        action = (re.search(r"Action:\s*([^\n]*)", raw_output or "") or [None, ""])[1].strip()
        result_text = (re.search(r"Result:\s*([\s\S]*)", raw_output or "") or [None, ""])[1].strip()

        return CommandResult(
            transcript=transcript,
            spoken_command=spoken,
            selected_text=selected_text,
            command=cmd,
            action=action,
            result=result_text,
            raw_output=raw_output,
        )

    def live_session(
        self,
        languages: Optional[list] = None,
        *,
        style: Optional[str] = None,
        app_type: str = "other",
        cleanup: Optional[str] = None,
        before_text: str = "",
        after_text: str = "",
        selected_text: str = "",
        content_text: str = "",
        partial_callback: Optional[Callable] = None,
        extra_overrides: Optional[dict] = None,
        extra_words: Optional[list] = None,
    ) -> LiveSession:
        """
        Create a LiveSession for real-time streaming audio.

        Feed raw PCM16 16kHz mono bytes via sess.send().
        Call sess.finish() (or use as context manager) to get the result.

        partial_callback(msg: dict) is called for each partial result.
        """
        languages = self._norm_languages(languages)
        overrides = self._build_overrides(style, app_type, cleanup, None, None, None, extra_overrides, extra_words)
        cfg = self.config.merge_overrides(overrides)
        ctx = self._build_ctx(before_text, after_text, selected_text, content_text, "",
                               "", "", "", app_type, None, None, None, None, "")
        return LiveSession(cfg, languages, ctx, partial_callback=partial_callback, verbose=self.verbose)

    # ── test matrices ─────────────────────────────────────────────────────────

    def run_cleanup_matrix(
        self,
        audio_path: str,
        languages: Optional[list] = None,
        style: str = "",
        app_type: str = "other",
        log: Optional[Callable] = None,
    ) -> dict:
        """
        Run audio through all four cleanup levels (NONE, LIGHT, MEDIUM, HIGH).
        Returns dict keyed by level with TranscriptResult values.
        """
        log = log or print
        languages = self._norm_languages(languages)
        wav_path, needs_cleanup = _ensure_wav(audio_path)
        results = {}
        try:
            log(f"{'='*60}")
            log(f"CLEANUP MATRIX — {Path(audio_path).name} — lang={languages}")
            log(f"{'='*60}")
            for level in ("NONE", "LIGHT", "MEDIUM", "HIGH"):
                log(f"\n[{level}] → gRPC={CLEANUP_TO_STRENGTH[level]}")
                try:
                    r = self.transcribe(
                        wav_path,
                        languages=languages,
                        style=style or None,
                        app_type=app_type,
                        cleanup=level,
                        warmup=False,
                    )
                    results[level] = r
                    log(f"  RAW:       {r.raw}")
                    log(f"  FORMATTED: {r.formatted}")
                    log(f"  FINAL:     {r.final}")
                    log(f"  FIRED:     {r.post_processing}")
                except Exception as e:
                    log(f"  ERROR: {e}")
                    results[level] = None
                time.sleep(0.5)
        finally:
            if needs_cleanup:
                try:
                    os.remove(wav_path)
                except Exception:
                    pass
        log(f"\n{'='*60}")
        log("DIFF vs NONE:")
        base_final = (results.get("NONE") or TranscriptResult()).final
        for level in ("LIGHT", "MEDIUM", "HIGH"):
            r = results.get(level)
            same = (r.final if r else "") == base_final
            log(f"  [{level:6}] {'IDENTICAL' if same else 'DIFFERENT ✓'}")
        log("Done.")
        return results

    def run_language_matrix(
        self,
        audio_path: str,
        style: str = "",
        app_type: str = "other",
        log: Optional[Callable] = None,
    ) -> dict:
        """
        Run audio through key language/dictionary combinations.
        Returns dict keyed by label string.
        """
        log = log or print
        wav_path, needs_cleanup = _ensure_wav(audio_path)
        cases = [
            ("English only + dict",    ["en"],          False),
            ("Catch-All + dict",        ["en", "hien"],  False),
            ("Hindi only + dict",       ["hi"],           False),
            ("Catch-All without dict",  ["en", "hien"],  True),
        ]
        results = {}
        try:
            log(f"{'='*60}")
            log(f"LANGUAGE / DICTIONARY MATRIX — {Path(audio_path).name}")
            log(f"{'='*60}")
            for label, langs, no_dict in cases:
                log(f"\n[{label}]  dict={'off' if no_dict else 'on'}")
                try:
                    overrides = {"dictionaryPersonal": [], "dictionaryPersonalStarred": []} if no_dict else {}
                    r = self.transcribe(
                        wav_path,
                        languages=langs,
                        style=style or None,
                        app_type=app_type,
                        warmup=False,
                        extra_overrides=overrides or None,
                    )
                    results[label] = r
                    log(f"  RAW:   {r.raw}")
                    log(f"  FINAL: {r.final}")
                except Exception as e:
                    log(f"  ERROR: {e}")
                    results[label] = None
                time.sleep(0.5)
        finally:
            if needs_cleanup:
                try:
                    os.remove(wav_path)
                except Exception:
                    pass
        log("\nDone.")
        return results

    def run_full_test_suite(
        self,
        audio_path: str,
        languages: Optional[list] = None,
        log: Optional[Callable] = None,
    ) -> dict:
        """
        Convenience: runs cleanup matrix + language matrix in sequence.
        Returns {"cleanup": ..., "language": ...}.
        """
        log = log or print
        languages = self._norm_languages(languages)
        return {
            "cleanup": self.run_cleanup_matrix(audio_path, languages=languages, log=log),
            "language": self.run_language_matrix(audio_path, log=log),
        }

    # ── auth utilities ────────────────────────────────────────────────────────

    def auth_status(self) -> dict:
        """Returns auth token validity info (no secrets exposed)."""
        try:
            token = _get_token()
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
            exp = int(data.get("exp", 0))
            remaining = exp - int(time.time())
            return {
                "ok": remaining > 0,
                "status": "expired" if remaining <= 0 else "near_expiry" if remaining < 3600 else "valid",
                "expires_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(exp)) if exp else "",
                "seconds_remaining": remaining,
            }
        except Exception as e:
            return {"ok": False, "status": str(e)[:200]}


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    import argparse

    # Silence SSL warnings from requests
    try:
        import urllib3; urllib3.disable_warnings()
    except Exception:
        pass

    # Fix stdout encoding on Windows
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Wispr Flow SDK CLI")
    parser.add_argument("audio", help="Audio file to transcribe (any format)")
    parser.add_argument("--languages", "-l", nargs="+", default=None, help="Language codes e.g. en hien hi")
    parser.add_argument("--style", choices=["FORMAL", "CASUAL", "VERY_CASUAL", "EXCITED"], default=None)
    parser.add_argument("--cleanup", choices=["NONE", "LIGHT", "MEDIUM", "HIGH"], default=None)
    parser.add_argument("--app-type", default="other")
    parser.add_argument("--before", default="", help="Text before cursor")
    parser.add_argument("--after",  default="", help="Text after cursor")
    parser.add_argument("--context", default="", help="Visible screen text")
    parser.add_argument("--matrix-cleanup", action="store_true", help="Run cleanup matrix")
    parser.add_argument("--matrix-language", action="store_true", help="Run language/dict matrix")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    client = WisprClient(verbose=args.verbose)
    print(f"Auth: {client.auth_status()}")

    if args.matrix_cleanup:
        client.run_cleanup_matrix(args.audio, languages=args.languages, style=args.style or "")
    elif args.matrix_language:
        client.run_language_matrix(args.audio, style=args.style or "")
    else:
        result = client.transcribe(
            args.audio,
            languages=args.languages,
            style=args.style,
            app_type=args.app_type,
            cleanup=args.cleanup,
            before_text=args.before,
            after_text=args.after,
            content_text=args.context,
        )
        print(f"\n{'='*60}")
        print(f"RAW:       {result.raw}")
        print(f"FORMATTED: {result.formatted}")
        print(f"FINAL:     {result.final}")
        if result.post_processing:
            print(f"FIRED:     {result.post_processing}")
        print(f"{'='*60}")


if __name__ == "__main__":
    main()