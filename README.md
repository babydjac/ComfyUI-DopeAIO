# 🔥 DOPE All-In-One Loader (Grok)

One ComfyUI node that replaces the whole loader chain:

| Section | What it does |
|---|---|
| **Model** | `model_source` toggle: **diffusion_model** (UNETLoader, with `weight_dtype`) or **checkpoint** |
| **CLIP** | `clip_source` toggle: **single** (CLIPLoader), **dual** (DualCLIPLoader) or **checkpoint** (baked). Type lists are read live from your ComfyUI, so new types (`krea2`, `minimax`, `flux2`, `lumina2`, …) show up automatically |
| **VAE** | Any VAE file / TAESD / `pixel_space`, or `(checkpoint VAE)`. Optional **audio VAE** output (MiniMax H3) |
| **LoRAs** | Power-LoRA-style stack: add as many as you like, toggle each one, set strength (or split model/CLIP strength), drag to reorder. **Civitai thumbnails**, hover cards with triggers/base model, and a **✓ / ✕ badge per LoRA after every run** showing how many weights it actually patched (✕ 0 = made for a different base model, ✕ missing = file not found) |
| **Civitai browser + downloader** | **+ Add LoRA → Civitai tab**: search Civitai, filter by base model (Krea 2, MiniMax H3, Flux.2, Flux.1, Z-Image, Qwen, Wan, SDXL, Pony, Illustrious, SD 1.5), sort, page through results, pick a version, and **Download**. Downloads are SHA256-verified, saved to `models/loras`, and added to the stack immediately with their thumbnail and trigger words. LoRAs you already have show **✓ Add** instead |
| **Prompts** | Positive + negative CLIP Text Encode. The negative toggle switches between **encode** and **zero-out** (use zero-out for CFG-1 models: Flux, Z-Image Turbo, Krea 2 Turbo, H3) |
| **Latent** | Width × height (or presets) → an empty latent **in the loaded model's native format**: 4ch/8 (SD1.5/SDXL), 16ch/8 (SD3/Flux.1/Z-Image/Krea 2/Qwen), 128ch/16 (Flux.2), a video latent (Wan…), or the audio+video latent for MiniMax H3 (`length` = frames) |
| **Grok prompt builder** | **Prompt model buttons**: Krea 2 · MiniMax H3 · Flux · Z-Image, plus a variant row (Turbo/Raw, video+audio/brief, Flux.2/Klein/Flux.1/Krea, Turbo/Base). The node recolors to match. **🔞 NSFW / SFW toggle** for adult (21+) prompts. Type a rough idea and press **✨ Generate**, or turn on `grok_auto` |

Outputs: `MODEL, CLIP, VAE, positive, negative, LATENT, width, height, positive_text, negative_text, audio_vae`.

## Install

```bash
cd ComfyUI/custom_nodes
cp -r /path/to/ComfyUI-DopeAIO .      # or git clone
pip install -r ComfyUI-DopeAIO/requirements.txt   # just `requests` (already a ComfyUI dependency)
```
Restart ComfyUI. The node is under **loaders → DopeAIO**; search for "DOPE".

## Grok setup

Click **🔑 API keys** on the node, paste your xAI key from [console.x.ai](https://console.x.ai), then press **Save & test**. The key is stored in `ComfyUI/user/__dope_aio/config.json`. ComfyUI never serves that folder over HTTP, so the key never ends up in workflows or PNG metadata. The `XAI_API_KEY` environment variable also works.

- **Models:** the dropdown refreshes live from `GET /v1/language-models`. The fallback list, checked against docs.x.ai on 2026-09-22, is `grok-4.7` (newest flagship, default), `grok-4.6`, `grok-4.5`, `grok-4.3` (fast and cheap, the only one that accepts effort `none`), and `grok-4.20-0309-(non-)reasoning`.
- **Endpoint:** calls go to the xAI **Responses API** with structured JSON output. If that endpoint is unavailable, the node falls back to `/chat/completions`. Each style uses a fixed system prompt plus `prompt_cache_key`, so repeated calls get cached-input pricing.
- **`grok_effort`:** reasoning effort. Each model only receives values it supports (`none` becomes `low` on 4.5–4.7).
- **`grok_detail`:** how far Grok expands your idea, from `minimal` (polish only) to `detailed`.
- **`grok_instructions`:** extra notes for Grok, e.g. "moody, 35mm film".
- **`grok_seed` + `grok_auto`:** auto results are cached per idea/seed (in memory and on disk, so reloading a workflow doesn't pay for the same prompt twice). Change the seed, or set its control to randomize, to get a new variation on every run. In auto mode your positive box is ignored; the status line under the node shows **📋 use Grok prompt**, which copies that prompt into the box and switches to manual.
- **🔞 NSFW / SFW:** NSFW tells Grok to write explicit adult prompts. Everyone depicted is 21+, and minors or real, identifiable people are always refused. SFW keeps prompts non-explicit.
- **`grok_image`** (optional input): Grok looks at a reference image (vision) in auto mode.
- **`append_lora_triggers`:** adds the enabled LoRAs' Civitai trigger words. They are passed to Grok so it can weave them in, or appended to your manual prompt.

### Styles (each is a researched system prompt in `dope_aio/prompts/*.md`, which you can edit)

| Style | Target | Notes |
|---|---|---|
| **Krea 2 (Turbo / Raw)** | Krea 2 open weights (Qwen3-VL-4B encoder, CLIP type `krea2`) | Dense natural-language captions and "avoid the AI look" rules. No negative on Turbo (CFG 1). Raw can get a short negative |
| **MiniMax H3 (video+audio / director brief)** | MiniMax H3 / Hailuo 3.0, a **video + audio** model (CLIP type `minimax`) | Official Context-IR layout: `[Shot N]` timing, camera = type + amplitude + speed, `(S1)` speakers with `<d>[English] …</d>` dialogue, soundscape and music fields. Duration and aspect come from `length`/width/height. No negative prompt (CFG-distilled) |
| **Flux.2 / Flux.2 Klein / Flux.1 / Flux.1 Krea** | BFL FLUX family | Priority word order, prose not tags, **no negation**, lens and lighting specifics, quoted text, hex colors (Flux.2) |
| **Z-Image Turbo / Base** | Tongyi-MAI Z-Image (Qwen3-4B encoder, CLIP type `lumina2`) | Long objective captions (120–300 words) and verbatim quoted text in any language. Negative only on Base (Turbo runs at CFG 1) |

## Civitai thumbnails

Thumbnails come from the first source that has one:
1. A local preview next to the LoRA (`name.preview.png/jpg/webp`, `name.png`, …).
2. A cover image embedded in the safetensors file.
3. Civitai.

For Civitai, the node looks the file up through `GET /api/v1/model-versions/by-hash/<hash>`:
- It tries the AutoV3 hash from the safetensors header first, which needs no hashing.
- If that misses, it computes a full SHA256 once and caches it.
- It also reuses info from rgthree, LoRA-Manager and Civitai-Helper sidecar files.

Results and thumbnails are cached in `user/__dope_aio/`. If Civitai is unreachable, the failure is remembered for 10 minutes, so your UI never stalls. Thumbnails above your chosen rating are blurred; set the ceiling under **Settings → DOPE AIO**. Only `image.civitai.com` URLs are ever downloaded. A Civitai key (🔑 on the node) is optional. Some downloads need it, and it applies your Civitai NSFW browsing settings to searches.

## Recipes

| Model | model | clip | vae | negative | latent |
|---|---|---|---|---|---|
| Flux.2 [dev] | diffusion_model `flux2_dev_*` | single `flux2` (Mistral) | `flux2-vae` | zero-out | auto 128ch/16 |
| Flux.1 dev | diffusion_model | dual `flux` (clip_l + t5xxl) | `ae` | zero-out | auto 16ch |
| Z-Image Turbo | diffusion_model `z_image_turbo_*` | single `lumina2` (`qwen_3_4b`) | `ae` | zero-out (CFG 1) | auto 16ch |
| Krea 2 Turbo | diffusion_model `krea2_turbo_*` | single `krea2` (`qwen3vl_4b`) | `qwen_image_vae` | zero-out (CFG 1, 8 steps) | auto (sizes ×16) |
| MiniMax H3 | diffusion_model `minimax_h3_fl2va_*` | single `minimax` | video VAE + **audio_vae** | zero-out, use BasicGuider | auto AV latent (`length` 124 ≈ 5 s, ×32 sizes) |
| SDXL | checkpoint | checkpoint | (checkpoint VAE) | encode | auto 4ch |

Changing only the prompt re-encodes the text; models and LoRAs stay cached inside the node. They reload only when a file or setting changes.
