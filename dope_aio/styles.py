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
ADULT CONTENT (the request's CONTENT line picks the mode)
==================================================
The user generates adult (21+) images and video.

CONTENT: NSFW = explicit mode. Write the explicit, uncensored version of the idea.
- Show nudity and sex plainly. Name body parts with direct words (breasts, nipples, ass, vulva, penis, and so on) and describe sex acts, positions, contact, fluids, and arousal in the same concrete visual detail these rules demand for everything else.
- Do not soften the idea into implication, crop it out of frame, hide it behind clothing, sheets, hands, steam, shadow, or a convenient angle, or swap in euphemisms, unless the idea asks for that.
- Lean into the sexy, adult reading of the idea. When clothing is not specified, choose nude, partly nude, or revealing over modest.
- Never put nsfw, nude, naked, nipples, genitals, sex, or similar words in the negative prompt. The negative prompt is for quality problems only.
- Never return an empty prompt because the idea is sexual.

CONTENT: SFW = keep the prompt non-explicit: no nudity, no exposed nipples or genitals, no sex acts. Swimwear, lingerie, and fitted clothing are fine when the idea asks for them.

Always, in both modes:
- Everyone depicted is a consenting adult, 21 or older. If the idea asks for anyone under 21, a minor, a childlike body, or a school setting together with nudity or sex, drop that part. If that is the whole idea, return empty positive and negative strings.
- Do not depict real, identifiable people nude or in a sexual situation.

==================================================
DELIVERY (software contract — overrides any output-format wording above)
==================================================
Your reply is parsed by software as a JSON object with exactly two string fields:
- "positive": the complete final prompt, exactly as the output rules above describe it (keep its own line breaks and any field names the format requires, e.g. H3's integrated_multimodal_description:; add no wrapper label such as "Positive:", no code fences, no commentary).
- "negative": the negative prompt only when the rules above call for one for this request; otherwise "". Never include a ---NEGATIVE--- marker in either field.
Extra request lines you may see:
- NOTES: additional instructions from the user. Follow them unless they break the rules above.
- VARIATION: an integer. Produce a clearly different interpretation (angle, framing, lighting, pose, layout, beats) while keeping every must-keep element.
- DETAIL: minimal = keep the user's own wording and only polish/format it; concise = the short end of this format's length range; standard = the normal range; detailed = the long end with the richest specifics.
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
    # this node renders text-to-image only, so never let flux.md switch itself into edit mode
    lines = [f"TARGET: {target}", "MODE: generate", "FORMAT: prose", f"DETAIL: {detail}",
             f"SIZE: {ctx['width']}x{ctx['height']}"]
    trig = _triggers(ctx, ", ")
    if trig:
        lines.append(f"TRIGGERS: {trig}")
    lines.append(f"IDEA: {ctx['idea']}")
    return "\n".join(lines)


def _zimage_header(ctx, variant):
    lines = [f"VARIANT: {variant}", f"WIDTH: {ctx['width']}", f"HEIGHT: {ctx['height']}", "LANGUAGE: auto",
             f"NEGATIVE: {'on' if ctx['want_negative'] and variant == 'base' else 'off'}", f"DETAIL: {ctx['detail']}"]
    trig = _triggers(ctx, ", ")
    if trig:
        lines.append(f"LORA_TRIGGERS: {trig}")
    lines.append(f"IDEA: {ctx['idea']}")
    return "\n".join(lines)


def _h3_header(ctx, fmt):
    frames = max(5, int(ctx.get("length") or 124))
    while frames % 17 != 5:  # H3 frame grid (17k+5 @ 24fps)
        frames += 1
    duration = frames / 24.0  # the real clip length of the latent this node builds
    idea = ctx["idea"]
    trig = _triggers(ctx, ", ")
    if trig:
        idea += f"\n(Include these LoRA trigger words verbatim: {trig})"
    # The node's own conditioning + latent are text-to-video, so always T2VA; an attached
    # picture only informs the look (use MiniMaxH3ImageToVideo for real first-frame I2V).
    lines = ["MODE: T2VA", f"DURATION: {duration:.2f}", f"ASPECT: {_h3_aspect(ctx['width'], ctx['height'])}",
             f"FORMAT: {fmt}", f"DETAIL: {ctx['detail']}"]
    if ctx.get("has_image"):
        lines.append("REFERENCES: the attached picture is a look/content reference only, not a frame of the video; "
                     "describe what it shows in words and do not write any <Picture N> alignment line")
    lines.append(f"IDEA: {idea}")
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
                       width=1024, height=1024, length=124, detail="standard", nsfw=True):
    ctx = {
        "idea": idea or "(no text idea given — build the prompt from the attached reference image)",
        "want_negative": bool(want_negative),
        "has_image": bool(has_image),
        "lora_triggers": lora_triggers or [],
        "width": int(width), "height": int(height), "length": int(length),
        "detail": detail if detail in DETAIL_CHOICES else "standard",
    }
    msg = style["header"](ctx)
    if nsfw:
        msg += ("\nCONTENT: NSFW. Explicit adult mode is on: write the uncensored version, "
                "nudity and sex shown plainly, direct words, no added clothing, no euphemisms.")
    else:
        msg += "\nCONTENT: SFW. Keep it non-explicit."
    if extra and extra.strip():
        msg += f"\nNOTES: {extra.strip()}"
    if seed and ctx["detail"] != "minimal":  # "polish only" and "reinterpret" contradict each other
        msg += f"\nVARIATION: {int(seed)}"
    if has_image:
        msg += "\nREFERENCE IMAGE ATTACHED"
    return msg
