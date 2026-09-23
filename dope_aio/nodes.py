"""DOPE All-In-One loader node.

Model (diffusion model | checkpoint) + CLIP (single | dual | checkpoint) + VAE (+ audio VAE)
+ unlimited LoRA stack + positive/negative encode + model-aware empty latent
+ Grok prompt builder. Everything heavy is cached inside the node instance so changing
the prompt re-encodes text without reloading models from disk.
"""

import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

import comfy.model_management
import comfy.sd
import comfy.utils
import folder_paths
import nodes as comfy_nodes

from . import civitai, grok, store
from .latent import build_empty_latent, get_latent_format
from .styles import DETAIL_CHOICES, STYLE_LABELS

log = logging.getLogger("DopeAIO")

NONE = "None"
BAKED_VAE = "(checkpoint VAE)"
_GROK_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dope-grok")

RESOLUTIONS = [
    "custom",
    "1024x1024 (1:1)", "1152x896 (9:7)", "896x1152 (7:9)", "1216x832 (3:2)", "832x1216 (2:3)",
    "1344x768 (16:9)", "768x1344 (9:16)", "1536x640 (21:9)", "640x1536 (9:21)",
    "1248x832 (3:2 Krea2)", "1360x768 (16:9 Krea2)", "1120x896 (5:4)", "896x1120 (4:5)",
    "1440x1440 (2MP 1:1)", "1920x1088 (2MP 16:9)", "1088x1920 (2MP 9:16)", "2048x2048 (4MP 1:1)",
    "832x480 (480p 16:9)", "1280x704 (720p 16:9)",
]

# CFG-distilled models that never use a negative prompt
_NO_NEGATIVE_FORMATS = {"MiniMaxH3AV"}


def _options(node_cls, name, fallback):
    """Pull live option lists from core nodes so new CLIP types etc. show up automatically."""
    try:
        types = node_cls.INPUT_TYPES()
        for section in ("required", "optional"):
            if name in types.get(section, {}):
                return list(types[section][name][0])
    except Exception:
        pass
    return list(fallback)


def _vae_list():
    try:
        return comfy_nodes.VAELoader.vae_list(comfy_nodes.VAELoader)
    except Exception:
        return folder_paths.get_filename_list("vae")


def _mtime(folder, name):
    try:
        return os.path.getmtime(folder_paths.get_full_path_or_raise(folder, name))
    except Exception:
        return 0


def _require(folder, name, label):
    if name in (None, "", NONE):
        raise ValueError(f"DopeAIO: {label} — nothing selected")
    if folder_paths.get_full_path(folder, name) is None:
        raise FileNotFoundError(f"DopeAIO: {label} '{name}' not found in models/{folder}")


def _enabled_loras(loras):
    stack = loras.get("loras", []) if isinstance(loras, dict) else (loras or [])
    out = []
    for item in stack:
        if isinstance(item, dict) and item.get("on", True) and item.get("lora") and item.get("lora") != NONE:
            out.append(item)
    return out


def _patch_counts(obj):
    patcher = getattr(obj, "patcher", obj)  # CLIP keeps its ModelPatcher in .patcher
    return {k: len(v) for k, v in (getattr(patcher, "patches", None) or {}).items()}


def _grew(before, after):
    return sum(1 for k, n in after.items() if n > before.get(k, 0))


class DopeAIOLoader:
    DESCRIPTION = ("All-in-one loader: diffusion model/checkpoint, single/dual CLIP, VAE, unlimited LoRAs with "
                   "Civitai thumbnails, positive/negative encode, model-aware empty latent and a Grok prompt builder.")
    CATEGORY = "loaders/DopeAIO"
    FUNCTION = "run"
    RETURN_TYPES = ("MODEL", "CLIP", "VAE", "CONDITIONING", "CONDITIONING", "LATENT", "INT", "INT",
                    "STRING", "STRING", "VAE")
    RETURN_NAMES = ("MODEL", "CLIP", "VAE", "positive", "negative", "LATENT", "width", "height",
                    "positive_text", "negative_text", "audio_vae")
    OUTPUT_TOOLTIPS = (
        "Model with all enabled LoRAs applied.", "CLIP with LoRAs applied.", "VAE (or the checkpoint's VAE).",
        "Encoded positive prompt.", "Encoded negative prompt (zeroed when the negative toggle is off).",
        "Empty latent in the loaded model's native format (channels / downscale / video frames).",
        "Actual width (snapped to the model's grid).", "Actual height (snapped to the model's grid).",
        "Final positive prompt text (Grok output when auto-Grok is on).", "Final negative prompt text.",
        "Optional second VAE (e.g. MiniMax H3 audio VAE for VAEDecodeAudio).",
    )

    def __init__(self):
        self._cache = {}

    @classmethod
    def INPUT_TYPES(cls):
        ckpts = [NONE] + folder_paths.get_filename_list("checkpoints")
        unets = [NONE] + folder_paths.get_filename_list("diffusion_models")
        tes = [NONE] + folder_paths.get_filename_list("text_encoders")
        vaes = _vae_list()
        dtypes = _options(comfy_nodes.UNETLoader, "weight_dtype", ["default", "fp8_e4m3fn", "fp8_e4m3fn_fast", "fp8_e5m2"])
        clip_types = _options(comfy_nodes.CLIPLoader, "type", ["stable_diffusion", "sd3", "flux2", "lumina2", "qwen_image"])
        dual_types = _options(comfy_nodes.DualCLIPLoader, "type", ["sdxl", "sd3", "flux", "hidream"])
        return {
            "required": {
                # ---- model
                "model_source": (["diffusion_model", "checkpoint"], {"default": "diffusion_model",
                                 "tooltip": "diffusion_model = UNETLoader file from models/diffusion_models; checkpoint = all-in-one file from models/checkpoints."}),
                "ckpt_name": (ckpts, {"tooltip": "Checkpoint (used when model_source = checkpoint)."}),
                "unet_name": (unets, {"tooltip": "Diffusion model (used when model_source = diffusion_model)."}),
                "weight_dtype": (dtypes, {"tooltip": "Diffusion-model weight dtype (diffusion_model mode only)."}),
                # ---- clip
                "clip_source": (["single", "dual", "checkpoint"], {"default": "single",
                                "tooltip": "single = CLIPLoader, dual = DualCLIPLoader, checkpoint = use the checkpoint's baked text encoder."}),
                "clip_name1": (tes, {"tooltip": "Text encoder (single) / first text encoder (dual)."}),
                "clip_name2": (tes, {"tooltip": "Second text encoder (dual only)."}),
                "clip_type": (clip_types, {"default": "stable_diffusion" if "stable_diffusion" in clip_types else clip_types[0],
                              "tooltip": "Single-CLIP type. Krea 2 = krea2, MiniMax H3 = minimax, Z-Image = lumina2, Flux.2 = flux2, Qwen-Image = qwen_image."}),
                "dual_clip_type": (dual_types, {"default": "flux" if "flux" in dual_types else dual_types[0],
                                   "tooltip": "Dual-CLIP type (flux = clip_l + t5xxl, sdxl = clip_l + clip_g, ...)."}),
                # ---- vae
                "vae_name": ([BAKED_VAE] + vaes, {"default": vaes[0] if vaes else BAKED_VAE,
                             "tooltip": f"VAE file. '{BAKED_VAE}' uses the one inside the checkpoint."}),
                # ---- grok prompt builder
                "grok_style": (STYLE_LABELS, {"default": STYLE_LABELS[0],
                               "tooltip": "Which model's prompting rules Grok follows (use the Krea 2 / MiniMax H3 / Flux / Z-Image buttons)."}),
                "grok_model": (grok.FALLBACK_MODELS, {"default": grok.DEFAULT_MODEL,
                               "tooltip": "xAI model. The list refreshes live from the API once a key is set."}),
                "grok_effort": (grok.EFFORT_CHOICES, {"default": "low",
                                "tooltip": "Reasoning effort. low = fast; 'none' only works on grok-4.3 (mapped to low elsewhere)."}),
                "grok_detail": (DETAIL_CHOICES, {"default": "standard",
                                "tooltip": "How much Grok expands your idea (minimal = polish only ... detailed = strong expansion)."}),
                "grok_idea": ("STRING", {"multiline": True, "default": "", "placeholder": "Rough idea for Grok… (press ✨ Generate, or enable auto)",
                              "tooltip": "Your rough idea. Grok turns it into a full prompt in the selected style."}),
                "grok_instructions": ("STRING", {"multiline": False, "default": "",
                                      "tooltip": "Optional extra instructions for Grok (e.g. 'moody, 35mm film, no people')."}),
                "grok_auto": ("BOOLEAN", {"default": False, "label_on": "auto on queue", "label_off": "manual (✨ button)",
                              "tooltip": "ON: Grok writes the prompt at queue time (cached per idea+seed, shown under the node; your positive box is ignored). OFF: use the ✨ button and edit freely."}),
                "grok_seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFF, "control_after_generate": True,
                              "tooltip": "Variation seed for auto mode. Change it (or set randomize) for a new prompt each run; 0 = no variation hint."}),
                # ---- prompts
                "positive": ("STRING", {"multiline": True, "default": "", "placeholder": "Positive prompt"}),
                "negative_enabled": ("BOOLEAN", {"default": True, "label_on": "negative: encode", "label_off": "negative: zero-out",
                                     "tooltip": "OFF = negative is zeroed conditioning (right for Flux, Z-Image Turbo, Krea 2 Turbo — CFG 1 models). MiniMax H3 is always zeroed."}),
                "negative": ("STRING", {"multiline": True, "default": "", "placeholder": "Negative prompt"}),
                "append_lora_triggers": ("BOOLEAN", {"default": False, "label_on": "add LoRA triggers", "label_off": "no LoRA triggers",
                                         "tooltip": "Adds enabled LoRAs' Civitai trigger words to the prompt (sent to Grok when Grok writes it)."}),
                # ---- latent
                "resolution": (RESOLUTIONS, {"default": "custom", "tooltip": "Preset size (overrides width/height unless 'custom')."}),
                "width": ("INT", {"default": 1024, "min": 64, "max": comfy_nodes.MAX_RESOLUTION, "step": 16}),
                "height": ("INT", {"default": 1024, "min": 64, "max": comfy_nodes.MAX_RESOLUTION, "step": 16}),
                "batch_size": ("INT", {"default": 1, "min": 1, "max": 4096}),
                "length": ("INT", {"default": 124, "min": 1, "max": 3600,
                           "tooltip": "Frames — only used by video models (MiniMax H3 snaps to 17k+5 @24fps: 124 ≈ 5s)."}),
            },
            "optional": {
                "loras": ("DOPE_LORA_STACK", {"default": {"loras": []},
                          "tooltip": "LoRA stack — add as many as you like."}),
                "audio_vae_name": ([NONE] + vaes, {"default": NONE, "advanced": True,
                                   "tooltip": "Second VAE (e.g. minimax_h3_audio_vae for MiniMax H3)."}),
                "clip_device": (["default", "cpu"], {"default": "default", "advanced": True}),
                "grok_image": ("IMAGE", {"tooltip": "Optional reference image Grok looks at (vision) in auto mode."}),
                "grok_nsfw": ("BOOLEAN", {"default": True, "label_on": "🔞 NSFW: explicit", "label_off": "SFW",
                              "tooltip": "ON: Grok writes explicit adult (21+) prompts. OFF: Grok keeps prompts non-explicit."}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    # Naming these inputs here makes ComfyUI skip its combo checks for them, so a workflow
    # from another machine doesn't fail on (e.g.) a missing unet while in checkpoint mode,
    # and the live Grok model list is accepted. The active selections are checked in run()
    # with one precise error instead.
    @classmethod
    def VALIDATE_INPUTS(cls, ckpt_name, unet_name, clip_name1, clip_name2, vae_name, grok_model):
        return True

    # Re-run when cached Civitai trigger words change (they are not part of the inputs).
    @classmethod
    def IS_CHANGED(cls, append_lora_triggers=False, loras=None, **kwargs):
        if not append_lora_triggers:
            return ""
        return json.dumps([civitai.trigger_words(i["lora"]) for i in _enabled_loras(loras)])

    # ------------------------------------------------------------ cached loaders
    def _cached(self, slot, key, loader):
        hit = self._cache.get(slot)
        if hit and hit[0] == key:
            return hit[1]
        self._cache.pop(slot, None)  # free the old object before loading the new one
        value = loader()
        self._cache[slot] = (key, value)
        return value

    def _load_checkpoint(self, ckpt_name):
        # Load every part once: toggling baked CLIP/VAE on and off must not reload the model.
        key = (ckpt_name, _mtime("checkpoints", ckpt_name))
        return self._cached("ckpt", key, lambda: tuple(comfy_nodes.CheckpointLoaderSimple().load_checkpoint(ckpt_name)[:3]))

    def _load_unet(self, unet_name, weight_dtype):
        key = (unet_name, weight_dtype, _mtime("diffusion_models", unet_name))
        return self._cached("unet", key, lambda: comfy_nodes.UNETLoader().load_unet(unet_name, weight_dtype)[0])

    def _load_clip(self, clip_source, n1, n2, clip_type, dual_type, device):
        if clip_source == "dual":
            key = ("dual", n1, n2, dual_type, device, _mtime("text_encoders", n1), _mtime("text_encoders", n2))
            return self._cached("clip", key, lambda: comfy_nodes.DualCLIPLoader().load_clip(n1, n2, dual_type, device)[0])
        key = ("single", n1, clip_type, device, _mtime("text_encoders", n1))
        return self._cached("clip", key, lambda: comfy_nodes.CLIPLoader().load_clip(n1, clip_type, device)[0])

    def _load_vae(self, slot, name):
        key = (name, _mtime("vae", name))
        return self._cached(slot, key, lambda: comfy_nodes.VAELoader().load_vae(name)[0])

    def _lora_sd(self, path):
        cache = self._cache.setdefault("lora_sd", {})
        mt = os.path.getmtime(path)
        hit = cache.get(path)
        if hit and hit[0] == mt:
            return hit[1], hit[2]
        sd, meta = comfy.utils.load_torch_file(path, safe_load=True, return_metadata=True)
        cache[path] = (mt, sd, meta)
        return sd, meta

    def _apply_loras(self, model, clip, loras, base_key):
        """Returns (model, clip, report). report = one entry per enabled row:
        {lora, status: applied|no_match|missing|zero, model_keys, clip_keys}."""
        report, active = [], []
        for item in _enabled_loras(loras):
            name = item["lora"]
            sm = float(item.get("strength", 1.0))
            sc = float(item["strength_clip"]) if item.get("strength_clip") is not None else sm
            path = folder_paths.get_full_path("loras", name)
            if path is None:
                log.warning("DopeAIO: LoRA not found, skipping: %s", name)
                report.append({"lora": name, "status": "missing", "model_keys": 0, "clip_keys": 0})
                continue
            if sm == 0 and sc == 0:
                report.append({"lora": name, "status": "zero", "model_keys": 0, "clip_keys": 0})
                continue
            active.append((name, path, sm, sc, os.path.getmtime(path)))

        used = {a[1] for a in active}
        lora_store = self._cache.setdefault("lora_sd", {})
        for p in list(lora_store):
            if p not in used:
                lora_store.pop(p, None)

        if not active:
            self._cache.pop("lora_applied", None)
            return model, clip, report

        # object identity: a reloaded base model/clip must never reuse old patched copies
        key = (base_key, id(model), id(clip), tuple((a[1], a[2], a[3], a[4]) for a in active))

        def apply():
            m, c, applied = model, clip, []
            for name, path, sm, sc, _ in active:
                sd, meta = self._lora_sd(path)
                mb, cb = _patch_counts(m), (_patch_counts(c) if c is not None else {})
                try:
                    m, c = comfy.sd.load_lora_for_models(m, c, sd, sm, sc if c is not None else 0, lora_metadata=meta)
                except TypeError:  # older ComfyUI without lora_metadata
                    m, c = comfy.sd.load_lora_for_models(m, c, sd, sm, sc if c is not None else 0)
                mk = _grew(mb, _patch_counts(m))
                ck = _grew(cb, _patch_counts(c)) if c is not None else 0
                if mk + ck == 0:
                    log.warning("DopeAIO: LoRA %s matched 0 keys — it was made for a different model type", name)
                applied.append({"lora": name, "status": "applied" if mk + ck else "no_match",
                                "model_keys": mk, "clip_keys": ck, "strength": sm})
            return m, c, applied

        m, c, applied = self._cached("lora_applied", key, apply)
        return m, c, report + applied

    # ------------------------------------------------------------ grok
    def _grok(self, params, image):
        img_key = None
        if image is not None:
            img_key = hashlib.sha1(image[0].detach().cpu().float().numpy().tobytes()).hexdigest()
        key = hashlib.sha256(json.dumps({**params, "img": img_key}, sort_keys=True).encode()).hexdigest()
        mem = self._cache.setdefault("grok", {})
        if key in mem:
            return mem[key]
        disk = os.path.join(store.data_dir("grok_cache"), f"{key}.json")
        res = store.read_json(disk)  # survives restarts: reloading a workflow doesn't re-bill
        if not res:
            fut = _GROK_POOL.submit(grok.generate, image=image, **params)
            while True:  # keep ComfyUI's Cancel button working during the API call
                try:
                    res = fut.result(timeout=0.25)
                    break
                except FutureTimeout:
                    comfy.model_management.throw_exception_if_processing_interrupted()
            store.write_json(disk, res)
        if len(mem) > 32:
            mem.clear()
        mem[key] = res
        return res

    # ------------------------------------------------------------ encode
    @staticmethod
    def _encode(clip, text):
        return comfy_nodes.CLIPTextEncode().encode(clip, text)[0]

    @staticmethod
    def _zero_out(cond):
        return comfy_nodes.ConditioningZeroOut().zero_out(cond)[0]

    # ------------------------------------------------------------ main
    def run(self, model_source, ckpt_name, unet_name, weight_dtype, clip_source, clip_name1, clip_name2,
            clip_type, dual_clip_type, vae_name, grok_style, grok_model, grok_effort, grok_detail, grok_idea,
            grok_instructions, grok_auto, grok_seed, positive, negative_enabled, negative, append_lora_triggers,
            resolution, width, height, batch_size, length, loras=None, audio_vae_name=NONE, clip_device="default",
            grok_image=None, grok_nsfw=True, unique_id=None):

        # ---- resolution
        if resolution and resolution != "custom":
            try:
                w, h = resolution.split(" ")[0].split("x")
                width, height = int(w), int(h)
            except ValueError:
                pass

        # ---- model / clip / vae
        if model_source == "checkpoint" or clip_source == "checkpoint" or vae_name == BAKED_VAE:
            _require("checkpoints", ckpt_name, "ckpt_name")
            ckpt = self._load_checkpoint(ckpt_name)
        else:
            ckpt = None
            self._cache.pop("ckpt", None)

        if model_source == "checkpoint":
            model = ckpt[0]
            self._cache.pop("unet", None)
            base_model_key = ("ckpt", ckpt_name)
        else:
            _require("diffusion_models", unet_name, "unet_name")
            model = self._load_unet(unet_name, weight_dtype)
            base_model_key = ("unet", unet_name, weight_dtype)
        if model is None:
            raise RuntimeError("DopeAIO: the checkpoint has no diffusion model")

        if clip_source == "checkpoint":
            clip = ckpt[1]
            self._cache.pop("clip", None)
            base_clip_key = ("ckpt", ckpt_name)
            if clip is None:
                raise RuntimeError("DopeAIO: this checkpoint has no baked text encoder — use clip_source single/dual")
        else:
            _require("text_encoders", clip_name1, "clip_name1")
            if clip_source == "dual":
                _require("text_encoders", clip_name2, "clip_name2")
            clip = self._load_clip(clip_source, clip_name1, clip_name2, clip_type, dual_clip_type, clip_device)
            base_clip_key = self._cache["clip"][0]

        if vae_name == BAKED_VAE:
            vae = ckpt[2]
            self._cache.pop("vae", None)
            if vae is None:
                raise RuntimeError("DopeAIO: this checkpoint has no baked VAE — pick a VAE file")
        else:
            if vae_name not in _vae_list():
                raise FileNotFoundError(f"DopeAIO: vae_name '{vae_name}' not found in models/vae")
            vae = self._load_vae("vae", vae_name)

        audio_vae = None
        if audio_vae_name and audio_vae_name != NONE:
            audio_vae = self._load_vae("audio_vae", audio_vae_name)
        else:
            self._cache.pop("audio_vae", None)

        # ---- loras
        model, clip, lora_report = self._apply_loras(model, clip, loras, (base_model_key, base_clip_key))
        applied = [r["lora"] for r in lora_report if r["status"] == "applied"]
        triggers = []
        if append_lora_triggers:
            for name in applied:
                try:
                    words = (civitai.get_info(name, fetch=True).get("civitai") or {}).get("trainedWords") or []
                except Exception as e:
                    log.warning("DopeAIO: could not get trigger words for %s: %s", name, e)
                    words = []
                for w in words:
                    if w not in triggers:
                        triggers.append(w)

        # ---- latent (needs the model's latent format)
        latent, latent_info, width, height = build_empty_latent(model, width, height, batch_size, length)
        fmt_name = type(get_latent_format(model)).__name__

        # ---- prompt text
        pos_text, neg_text = positive or "", negative or ""
        grok_note, grok_text = "", ""
        if grok_auto and ((grok_idea or "").strip() or grok_image is not None):
            if not str(grok_model).startswith("grok"):
                log.warning("DopeAIO: grok_model '%s' is not an xAI model (shifted workflow values?) — using %s", grok_model, grok.DEFAULT_MODEL)
                grok_model = grok.DEFAULT_MODEL
            params = dict(idea=grok_idea, style=grok_style, model=grok_model, effort=grok_effort, detail=grok_detail,
                          extra_instructions=grok_instructions, seed=grok_seed or None,
                          want_negative=bool(negative_enabled), lora_triggers=triggers,
                          width=width, height=height, length=length, nsfw=grok_nsfw is not False)
            res = self._grok(params, grok_image)
            pos_text = res["positive"]
            if negative_enabled and res.get("negative"):
                neg_text = res["negative"]
            cost = res.get("cost_usd")
            grok_note = f"Grok {res['model']} · {res['style']}" + (f" · ${cost:.4f}" if cost else "")
            grok_text = pos_text
        if triggers:  # enforced for manual text AND Grok output (LLMs sometimes drop them)
            missing = [t for t in triggers if t.lower() not in pos_text.lower()]
            if missing:
                pos_text = (pos_text.rstrip().rstrip(",") + ", " if pos_text.strip() else "") + ", ".join(missing)

        pos_cond = self._encode(clip, pos_text)
        neg_note = ""
        if fmt_name in _NO_NEGATIVE_FORMATS:
            neg_cond, neg_text, neg_note = self._zero_out(pos_cond), "", " · negative zeroed (CFG-distilled)"
        elif negative_enabled:
            neg_cond = self._encode(clip, neg_text)
        else:
            neg_cond, neg_text = self._zero_out(pos_cond), ""

        n_bad = sum(1 for r in lora_report if r["status"] in ("missing", "no_match"))
        status = (f"{latent_info} · LoRAs applied {len(applied)}/{len(lora_report)}"
                  + (f" ⚠ {n_bad} problem(s)" if n_bad else "") + neg_note + (f" · {grok_note}" if grok_note else ""))
        log.info("DopeAIO: %s", status)
        ui = {  # always the same keys (ComfyUI merges ui dicts across list-mapped calls)
            "dope_status": [status],
            # one entry per enabled LoRA row -> ✓ / ✕ badges in the stack
            "dope_loras": [{"name": r["lora"], "status": r["status"], "applied": r["model_keys"] + r["clip_keys"],
                            "model_keys": r["model_keys"], "clip_keys": r["clip_keys"], "strength": r.get("strength")}
                           for r in lora_report],
            "dope_grok": [grok_text],
        }
        return {"ui": ui, "result": (model, clip, vae, pos_cond, neg_cond, latent, width, height, pos_text, neg_text, audio_vae)}


NODE_CLASS_MAPPINGS = {"DopeAIOLoader": DopeAIOLoader}
NODE_DISPLAY_NAME_MAPPINGS = {"DopeAIOLoader": "🔥 DOPE All-In-One Loader (Grok)"}
