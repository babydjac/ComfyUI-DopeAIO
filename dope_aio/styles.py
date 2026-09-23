"""Prompt styles for the Grok prompt builder.

Each style = one researched system prompt (dope_aio/prompts/*.md, editable) plus a
header builder that turns the node's settings into the control lines that system
prompt expects. A shared DELIVERY block is appended so every style returns the same
{"positive", "negative"} JSON.
"""

import math
import os

PROMPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

DETAIL_CHOICES = ["standard", "minimal", "concise", "detailed"]

DELIVERY = """

==================================================
DELIVERY (software contract — overrides any output-format wording above)
==================================================
Your reply is parsed by software as a JSON object with exactly two string fields:
- "positive": the complete final prompt, exactly as the output rules above describe it (keep its own line breaks if the format uses them; no labels, no code fences, no commentary).
- "negative": the negative prompt only when the rules above call for one for this request; otherwise "". Never include a ---NEGATIVE--- marker in either field.
Extra request lines you may see:
- NOTES: additional instructions from the user. Follow them unless they break the rules above.
- VARIATION: an integer. Produce a clearly different interpretation (angle, framing, lighting, pose, layout, beats) while keeping every must-keep element.
- REFERENCE IMAGE ATTACHED: look at the attached image and ground the prompt in what is actually visible in it.
"""


def _aspect(width, height):
    g = math.gcd(int(width), int(height)) or 1
    w, h = int(width) // g, int(height) // g
    if w > 40 or h > 40:  # ugly ratio -> decimal form
        return f"{width}x{height}"
    return f"{w}:{h}"


def _h3_aspect(width, height):
    r = width / max(1, height)
    options = {"16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1.0, "4:3": 4 / 3, "3:4": 3 / 4, "21:9": 21 / 9}
    return min(options, key=lambda k: abs(options[k] - r))


def _triggers(ctx, sep):
    t = [x for x in (ctx.get("lora_triggers") or []) if x]
    return sep.join(t)


# ---------------------------------------------------------------- headers
def _krea_header(ctx, target):
    creativity = {"minimal": "raw", "concise": "low", "standard": "medium", "detailed": "high"}[ctx["detail"]]
    tags = [f"[target: {target}]", f"[creativity: {creativity}]", f"[aspect: {_aspect(ctx['width'], ctx['height'])}]"]
    trig = _triggers(ctx, "; ")
    if trig:
        tags.append(f"[lora_triggers: {trig}]")
    return " ".join(tags) + "\n" + ctx["idea"]


def _flux_header(ctx, target):
    detail = {"minimal": "concise", "concise": "concise", "standard": "standard", "detailed": "detailed"}[ctx["detail"]]
    lines = [f"TARGET: {target}", "FORMAT: prose", f"DETAIL: {detail}", f"SIZE: {ctx['width']}x{ctx['height']}"]
    trig = _triggers(ctx, ", ")
    if trig:
        lines.append(f"TRIGGERS: {trig}")
    lines.append(f"IDEA: {ctx['idea']}")
    return "\n".join(lines)


def _zimage_header(ctx, variant):
    lines = [f"VARIANT: {variant}", f"WIDTH: {ctx['width']}", f"HEIGHT: {ctx['height']}", "LANGUAGE: auto",
             f"NEGATIVE: {'on' if ctx['want_negative'] and variant == 'base' else 'off'}"]
    trig = _triggers(ctx, ", ")
    if trig:
        lines.append(f"LORA_TRIGGERS: {trig}")
    lines.append(f"IDEA: {ctx['idea']}")
    return "\n".join(lines)


def _h3_header(ctx, fmt):
    frames = max(5, int(ctx.get("length") or 124))
    while frames % 17 != 5:  # H3 frame grid (17k+5 @ 24fps)
        frames += 1
    duration = min(15.0, max(4.0, frames / 24.0))
    mode = "I2VA" if ctx.get("has_image") else "T2VA"
    idea = ctx["idea"]
    trig = _triggers(ctx, ", ")
    if trig:
        idea += f"\n(Include these LoRA trigger words verbatim: {trig})"
    lines = [f"MODE: {mode}", f"DURATION: {duration:.2f}", f"ASPECT: {_h3_aspect(ctx['width'], ctx['height'])}",
             f"FORMAT: {fmt}", f"IDEA: {idea}"]
    return "\n".join(lines)


# ---------------------------------------------------------------- registry
STYLES = [
    {"key": "krea2_turbo", "label": "Krea 2 (Turbo)", "file": "krea2.md", "uses_negative": False,
     "header": lambda c: _krea_header(c, "krea2-turbo")},
    {"key": "krea2_raw", "label": "Krea 2 (Raw)", "file": "krea2.md", "uses_negative": True,
     "header": lambda c: _krea_header(c, "krea2-raw")},
    {"key": "h3_ir", "label": "MiniMax H3 (video+audio)", "file": "minimax_h3.md", "uses_negative": False,
     "header": lambda c: _h3_header(c, "IR")},
    {"key": "h3_brief", "label": "MiniMax H3 (director brief)", "file": "minimax_h3.md", "uses_negative": False,
     "header": lambda c: _h3_header(c, "BRIEF")},
    {"key": "flux2", "label": "Flux.2", "file": "flux.md", "uses_negative": False,
     "header": lambda c: _flux_header(c, "flux2")},
    {"key": "flux2_klein", "label": "Flux.2 Klein", "file": "flux.md", "uses_negative": False,
     "header": lambda c: _flux_header(c, "flux2_klein")},
    {"key": "flux1", "label": "Flux.1 (dev/schnell)", "file": "flux.md", "uses_negative": False,
     "header": lambda c: _flux_header(c, "flux1")},
    {"key": "flux1_krea", "label": "Flux.1 Krea [dev]", "file": "flux.md", "uses_negative": False,
     "header": lambda c: _flux_header(c, "flux1_krea")},
    {"key": "zimage_turbo", "label": "Z-Image Turbo", "file": "zimage.md", "uses_negative": False,
     "header": lambda c: _zimage_header(c, "turbo")},
    {"key": "zimage_base", "label": "Z-Image Base", "file": "zimage.md", "uses_negative": True,
     "header": lambda c: _zimage_header(c, "base")},
]

STYLE_LABELS = [s["label"] for s in STYLES]
_BY_LABEL = {s["label"]: s for s in STYLES}
_BY_KEY = {s["key"]: s for s in STYLES}
_prompt_cache = {}


def _load_system(filename):
    path = os.path.join(PROMPT_DIR, filename)
    mtime = os.path.getmtime(path)
    cached = _prompt_cache.get(filename)
    if cached and cached[0] == mtime:
        return cached[1]
    with open(path, "r", encoding="utf-8") as f:
        text = f.read().strip() + DELIVERY
    _prompt_cache[filename] = (mtime, text)
    return text


def get_style(name):
    st = _BY_LABEL.get(name) or _BY_KEY.get(name)
    if st is None:
        low = (name or "").lower()
        st = next((s for s in STYLES if low and (low in s["label"].lower() or low in s["key"])), STYLES[0])
    return dict(st, system=_load_system(st["file"]))


def build_user_message(style, idea, want_negative=True, extra="", has_image=False, lora_triggers=None, seed=None,
                       width=1024, height=1024, length=124, detail="standard"):
    ctx = {
        "idea": idea or "(no text idea given — build the prompt from the attached reference image)",
        "want_negative": bool(want_negative),
        "has_image": bool(has_image),
        "lora_triggers": lora_triggers or [],
        "width": int(width), "height": int(height), "length": int(length),
        "detail": detail if detail in DETAIL_CHOICES else "standard",
    }
    msg = style["header"](ctx)
    if extra and extra.strip():
        msg += f"\nNOTES: {extra.strip()}"
    if seed:
        msg += f"\nVARIATION: {int(seed)}"
    if has_image:
        msg += "\nREFERENCE IMAGE ATTACHED"
    return msg
