"""Empty-latent builder that matches whatever model is loaded.

Reads the model's own ``latent_format`` (channels / spatial / temporal ratio) so it
produces the same tensor the model-specific "Empty ... Latent" nodes would:

  SD1.5 / SDXL ............ 4ch  /8
  SD3 / Flux.1 / Z-Image .. 16ch /8
  Flux.2 / Ideogram4 ...... 128ch /16
  Qwen-Image / Krea 2 ..... 16ch /8   (Wan21 format, sampler unsqueezes to 5D)
  MiniMax H3 .............. nested video+audio latent (delegates to ComfyUI's own helper)
  video models ............ 5D latent using `length` frames
"""

import logging

import torch

import comfy.model_management

log = logging.getLogger("DopeAIO")

# Image models whose latent_format is a (3D) video format but which are sampled as
# single images -- a plain 4D latent is what the official workflows feed them and
# comfy.sample.fix_empty_latent_channels unsqueezes it for the model.
_IMAGE_MODELS_WITH_VIDEO_FORMAT = {
    "Krea2", "QwenImage", "JoyImage", "Anima", "CosmosT2IPredict2", "HunyuanImage21",
}


def _model_config_name(model):
    try:
        return type(model.model.model_config).__name__
    except Exception:
        return ""


def get_latent_format(model):
    try:
        return model.get_model_object("latent_format")
    except Exception:
        return getattr(getattr(model, "model", None), "latent_format", None)


def snap(value, multiple):
    return max(multiple, (int(value) // multiple) * multiple)


def build_empty_latent(model, width, height, batch_size=1, length=1):
    """Returns (latent_dict, info_str, width, height)."""
    fmt = get_latent_format(model)
    fmt_name = type(fmt).__name__ if fmt is not None else "None"
    cfg_name = _model_config_name(model)
    device = comfy.model_management.intermediate_device()

    if fmt_name == "MiniMaxH3AV":
        from comfy_extras.nodes_minimax_h3 import _empty_av_latent
        width, height = snap(width, 32), snap(height, 32)
        try:
            latent, frames = _empty_av_latent(width, height, length, batch_size)
        except TypeError:  # older signature without batch_size
            latent, frames = _empty_av_latent(width, height, length)
        return latent, f"MiniMax H3 AV latent {width}x{height}, {frames} frames @24fps", width, height

    channels = getattr(fmt, "latent_channels", 4)
    ratio = getattr(fmt, "spacial_downscale_ratio", 8)
    dims = getattr(fmt, "latent_dimensions", 2)
    width, height = snap(width, ratio), snap(height, ratio)

    if dims == 3 and cfg_name not in _IMAGE_MODELS_WITH_VIDEO_FORMAT:
        t_ratio = getattr(fmt, "temporal_downscale_ratio", 1) or 1
        frames = max(1, int(length))
        latent_t = ((frames - 1) // t_ratio) + 1
        samples = torch.zeros([batch_size, channels, latent_t, height // ratio, width // ratio], device=device)
        info = f"{fmt_name} video latent {channels}ch x{latent_t}t @ /{ratio} ({width}x{height}, {frames} frames)"
        return {"samples": samples}, info, width, height

    if dims != 2 and dims != 3:
        raise ValueError(f"DopeAIO: model latent format '{fmt_name}' ({dims}D) is not an image/video model")

    samples = torch.zeros([batch_size, channels, height // ratio, width // ratio], device=device,
                          dtype=comfy.model_management.intermediate_dtype())
    info = f"{fmt_name} latent {channels}ch @ /{ratio} ({width}x{height})"
    return {"samples": samples, "downscale_ratio_spacial": ratio}, info, width, height
