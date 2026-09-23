"""ComfyUI-DopeAIO — all-in-one loader + Grok prompt builder + Civitai LoRA thumbnails."""

from .dope_aio.nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
from .dope_aio import routes  # noqa: F401  (registers /dope_aio/* HTTP routes)

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
