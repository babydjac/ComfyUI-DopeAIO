"""xAI Grok client for the prompt builder.

Uses the Responses API (POST /v1/responses — xAI's recommended endpoint) with
structured JSON output, falling back to legacy /v1/chat/completions.
Model list comes live from GET /v1/language-models (cached 1h) with a hardcoded
fallback verified against docs.x.ai on 2026-09-22.
"""

import base64
import hashlib
import io
import json
import logging
import random
import re
import threading
import time

import requests

from . import store
from .styles import STYLE_LABELS, build_user_message, get_style

log = logging.getLogger("DopeAIO")

BASE_URL = "https://api.x.ai/v1"

# Newest / best first. grok-4.7 = flagship (2026-09-21), grok-4.3 = fast & cheap.
FALLBACK_MODELS = [
    "grok-4.7",
    "grok-4.6",
    "grok-4.5",
    "grok-4.3",
    "grok-4.20-0309-reasoning",
    "grok-4.20-0309-non-reasoning",
]
DEFAULT_MODEL = "grok-4.7"

FALLBACK_EFFORTS = {
    "grok-4.7": ["low", "medium", "high", "xhigh"],
    "grok-4.6": ["low", "medium", "high", "xhigh"],
    "grok-4.5": ["low", "medium", "high"],
    "grok-4.3": ["none", "low", "medium", "high", "xhigh"],
    "grok-4.20-0309-reasoning": [],
    "grok-4.20-0309-non-reasoning": [],
}
EFFORT_CHOICES = ["low", "medium", "high", "xhigh", "none", "auto"]

_EXCLUDE = ("imagine", "voice", "multi-agent", "embed", "transcribe", "tts", "stt")

PROMPT_SCHEMA = {
    "type": "object",
    "properties": {
        "positive": {"type": "string"},
        "negative": {"type": "string"},
    },
    "required": ["positive", "negative"],
    "additionalProperties": False,
}


class GrokError(RuntimeError):
    def __init__(self, status, code, message):
        super().__init__(f"Grok API error {status} [{code}]: {message}")
        self.status, self.code, self.message = status, code, message


# ------------------------------------------------------------------ http
def _raise_for_error(r):
    if r.status_code < 400:
        return
    code, msg = "unknown", r.text[:500]
    try:
        j = r.json()
        if isinstance(j.get("error"), dict):
            msg = j["error"].get("message", msg)
            code = str(j["error"].get("code") or j["error"].get("type") or code)
        else:
            msg = j.get("error") or j.get("message") or msg
            code = j.get("code") or code
    except ValueError:
        pass
    raise GrokError(r.status_code, code, msg)


def _request(method, path, api_key, body=None, timeout=300, retries=3):
    """GETs retry on any network error. POSTs (billed generations) are only re-sent when the
    request provably never reached xAI (connect timeout / refused) or on 429/502/503/504 —
    never after a read timeout, which could mean xAI already did (and billed) the work."""
    url = f"{BASE_URL}{path}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    is_post = method.upper() == "POST"
    for attempt in range(retries + 1):
        try:
            r = requests.request(method, url, headers=headers, json=body, timeout=(10, timeout))
        except requests.ConnectTimeout as e:
            if attempt >= retries:
                raise GrokError(0, "network", f"could not connect to api.x.ai: {e}")
            time.sleep(min(20, 2 ** attempt) + random.random())
            continue
        except requests.ReadTimeout as e:
            raise GrokError(0, "timeout", f"xAI did not answer within {timeout}s — try a lower reasoning effort ({e})")
        except requests.ConnectionError as e:
            if is_post or attempt >= retries:
                raise GrokError(0, "network", str(e))
            time.sleep(min(20, 2 ** attempt) + random.random())
            continue
        retryable = r.status_code == 429 or (r.status_code in (502, 503, 504) if is_post else r.status_code >= 500)
        if retryable:
            if attempt >= retries:
                _raise_for_error(r)
            ra = r.headers.get("retry-after", "")
            try:
                delay = min(60.0, float(ra))
            except ValueError:
                delay = min(20, 2 ** attempt) + random.random()
            time.sleep(delay)
            continue
        _raise_for_error(r)
        return r.json()
    raise GrokError(0, "retries-exhausted", url)


def require_key():
    key, _ = store.get_secret("xai")
    if not key:
        raise GrokError(401, "no-key", "No xAI API key set. Click '🔑 Grok API key' on the node, "
                                       "or set the XAI_API_KEY environment variable.")
    return key


# ------------------------------------------------------------------ models
_models_cache = {"t": 0.0, "key": "", "models": None}
_models_lock = threading.Lock()


def _fallback_models():
    return [{"id": m, "efforts": FALLBACK_EFFORTS.get(m, []), "default_effort": None, "vision": True}
            for m in FALLBACK_MODELS]


def list_models(force=False):
    """Returns (models, source). models = [{id, efforts, default_effort, vision}] newest first."""
    key, _ = store.get_secret("xai")
    if not key:
        return _fallback_models(), "fallback (no key)"
    fp = hashlib.sha256(key.encode()).hexdigest()[:12]
    with _models_lock:
        c = _models_cache
        if not force and c["models"] and c["key"] == fp and time.time() - c["t"] < 3600:
            return c["models"], "api (cached)"
    try:
        try:
            rows = _request("GET", "/language-models", key, timeout=20, retries=1).get("models", [])
        except GrokError as e:
            if e.status in (400, 401, 403):
                raise
            rows = _request("GET", "/models", key, timeout=20, retries=1).get("data", [])
        out = []
        for m in rows:
            mid = m.get("id") or ""
            if not mid.startswith("grok") or any(x in mid for x in _EXCLUDE):
                continue
            if "text" not in (m.get("output_modalities") or ["text"]):
                continue
            caps = m.get("capabilities") or {}
            out.append({
                "id": mid,
                "created": m.get("created") or 0,
                "efforts": caps.get("reasoning_effort") or FALLBACK_EFFORTS.get(mid, []),
                "default_effort": caps.get("default_reasoning_effort"),
                "vision": "image" in (m.get("input_modalities") or ["image"]),
            })
        out.sort(key=lambda x: x["created"], reverse=True)
        if not out:
            return _fallback_models(), "fallback (empty list)"
        with _models_lock:
            _models_cache.update(t=time.time(), key=fp, models=out)
        return out, "api"
    except GrokError as e:
        log.warning("DopeAIO: could not list Grok models (%s); using fallback list", e)
        return _fallback_models(), f"fallback ({e.code})"


def _efforts_for(model):
    c = _models_cache.get("models") or []
    for m in c:
        if m["id"] == model:
            return m["efforts"]
    if model in FALLBACK_EFFORTS:
        return FALLBACK_EFFORTS[model]
    return None  # unknown -> don't guess


def resolve_effort(model, effort):
    """Map the UI effort choice onto what this model accepts (None = omit the param)."""
    if effort in (None, "", "auto"):
        return None
    supported = _efforts_for(model)
    if supported is None:
        return None if effort == "none" else effort
    if not supported:
        return None
    if effort in supported:
        return effort
    if effort == "none":
        return "low" if "low" in supported else None
    if effort == "xhigh" and "high" in supported:
        return "high"
    return None


# ------------------------------------------------------------------ images
def image_to_data_url(image, max_side=1536):
    """ComfyUI IMAGE [B,H,W,C] 0..1 -> JPEG data URL (xAI accepts jpg/png, <=20MiB)."""
    import numpy as np
    from PIL import Image
    arr = (image[0].detach().cpu().float().numpy() * 255.0).clip(0, 255).astype(np.uint8)
    img = Image.fromarray(arr).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# ------------------------------------------------------------------ generation
def _split_marker(pos, neg):
    # zimage.md's few-shot format; Grok sometimes puts it inside the JSON positive
    if "---NEGATIVE---" in pos:
        pos, tail = pos.split("---NEGATIVE---", 1)
        neg = neg or tail
    return pos.strip(), neg.strip()


def _parse_pair(text, refusal=""):
    text = (text or "").strip()
    if not text:
        raise GrokError(200, "refused" if refusal else "empty", refusal or "Grok returned an empty response")
    candidates = [text]
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        candidates.append(m.group(0))
    for c in candidates:
        try:
            j = json.loads(c)
        except ValueError:
            continue
        if isinstance(j, dict) and "positive" in j:
            pos, neg = _split_marker(str(j.get("positive") or ""), str(j.get("negative") or ""))
            if not pos:
                raise GrokError(200, "refused", refusal or "Grok declined this idea (empty prompt returned) — rephrase it")
            return pos, neg
    if text.startswith("{"):
        raise GrokError(200, "bad-json", "Grok returned malformed JSON: " + text[:200])
    return _split_marker(text, "")


def _call_responses(key, model, system, user_content, effort, temperature, cache_key, timeout):
    body = {
        "model": model,
        "input": [{"role": "system", "content": system}, {"role": "user", "content": user_content}],
        "store": False,
        "prompt_cache_key": cache_key,
        "text": {"format": {"type": "json_schema", "name": "prompt_pair", "schema": PROMPT_SCHEMA, "strict": True}},
    }
    if effort:
        body["reasoning"] = {"effort": effort}
    if temperature is not None:
        body["temperature"] = temperature
    j = _request("POST", "/responses", key, body, timeout=timeout)
    texts, refusals = [], []
    for item in j.get("output", []):
        if item.get("type") != "message":
            continue
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                texts.append(c.get("text", ""))
            elif c.get("type") == "refusal":
                refusals.append(c.get("refusal") or c.get("text") or "")
    if j.get("status") not in (None, "completed"):
        raise GrokError(200, j.get("status") or "incomplete",
                        f"Grok stopped early: {json.dumps(j.get('incomplete_details'))}")
    return "".join(texts), j.get("usage") or {}, " ".join(r for r in refusals if r)


def _call_chat(key, model, system, user_content, effort, temperature, cache_key, timeout, seed):
    if isinstance(user_content, list):  # convert Responses-style parts to chat parts
        parts = []
        for p in user_content:
            if p["type"] == "input_image":
                parts.append({"type": "image_url", "image_url": {"url": p["image_url"], "detail": p.get("detail", "high")}})
            else:
                parts.append({"type": "text", "text": p["text"]})
        user_content = parts
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_content}],
        "prompt_cache_key": cache_key,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "prompt_pair", "schema": PROMPT_SCHEMA, "strict": True}},
    }
    if effort:
        body["reasoning_effort"] = effort
    if temperature is not None:
        body["temperature"] = temperature
    if seed is not None:
        body["seed"] = int(seed) & 0x7FFFFFFF
    j = _request("POST", "/chat/completions", key, body, timeout=timeout)
    choice = j["choices"][0]
    if choice.get("finish_reason") not in (None, "stop", "end_turn"):
        raise GrokError(200, "incomplete", f"Grok stopped early (finish_reason={choice.get('finish_reason')})")
    msg = choice.get("message") or {}
    return (msg.get("content") or ""), j.get("usage") or {}, (msg.get("refusal") or "")


def generate(idea, style, model=DEFAULT_MODEL, effort="low", want_negative=True, image=None,
             temperature=None, extra_instructions="", seed=None, lora_triggers=None, width=1024, height=1024,
             length=124, detail="standard", timeout=300, nsfw=True):
    """Returns dict(positive, negative, model, style, cost_usd)."""
    key = require_key()
    st = get_style(style)
    idea = (idea or "").strip()
    if not idea and image is None:
        raise GrokError(400, "no-idea", "Type an idea in the Grok idea box first (or connect a reference image).")
    model = (model or DEFAULT_MODEL).strip()
    eff = resolve_effort(model, effort)
    user_text = build_user_message(st, idea, want_negative=want_negative, extra=extra_instructions,
                                   has_image=image is not None, lora_triggers=lora_triggers, seed=seed,
                                   width=width, height=height, length=length, detail=detail, nsfw=nsfw)
    user_content = user_text
    if image is not None:
        data_url = image if isinstance(image, str) else image_to_data_url(image)
        user_content = [{"type": "input_image", "image_url": data_url, "detail": "high"},
                        {"type": "input_text", "text": user_text}]
    cache_key = f"dope-aio-{st['key']}"
    try:
        text, usage, refusal = _call_responses(key, model, st["system"], user_content, eff, temperature, cache_key, timeout)
    except GrokError as e:
        # Only when the Responses *endpoint* itself is unavailable (405, or a 404 that isn't
        # xAI's "model does not exist") fall back to legacy /chat/completions.
        if not (e.status == 405 or (e.status == 404 and "model" not in str(e.message).lower())):
            raise
        log.warning("DopeAIO: /responses unavailable (%s); retrying with /chat/completions", e)
        try:
            text, usage, refusal = _call_chat(key, model, st["system"], user_content, eff, None, cache_key, timeout, seed)
        except GrokError:
            raise e
    positive, negative = _parse_pair(text, refusal)
    if st["key"].startswith("h3"):  # local H3 tokenizer's control token is <|cutoff|>
        positive = positive.replace("<cutoff>", "<|cutoff|>")
    if not want_negative or not st.get("uses_negative", False):
        negative = ""
    ticks = usage.get("cost_in_usd_ticks")
    return {
        "positive": positive,
        "negative": negative,
        "model": model,
        "style": st["label"],
        "cost_usd": (ticks / 1e10) if isinstance(ticks, (int, float)) else None,
    }


def test_key():
    key = require_key()
    j = _request("GET", "/api-key", key, timeout=20, retries=0)
    return {
        "name": j.get("name"),
        "redacted": j.get("redacted_api_key") or store.redact(key),
        "blocked": bool(j.get("api_key_blocked") or j.get("api_key_disabled") or j.get("team_blocked")),
    }

