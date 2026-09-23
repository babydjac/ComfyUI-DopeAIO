"""HTTP routes used by the node's frontend (all under /dope_aio/)."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from aiohttp import web

import folder_paths
from server import PromptServer

from . import civitai, grok, store
from .styles import STYLE_LABELS

log = logging.getLogger("DopeAIO")
routes = PromptServer.instance.routes


# Civitai lookups (hashing, network) get their own small pool so a folder full of LoRAs
# can never starve Grok requests or ComfyUI's default executor.
_CIVITAI_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dope-civitai")


def _run(fn, *args, **kwargs):
    return asyncio.get_running_loop().run_in_executor(None, lambda: fn(*args, **kwargs))


def _run_civ(fn, *args, **kwargs):
    return asyncio.get_running_loop().run_in_executor(_CIVITAI_POOL, lambda: fn(*args, **kwargs))


def _err(e, status=500):
    return web.json_response({"ok": False, "error": str(e)}, status=status)


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _sniff(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    return "application/octet-stream"


# ------------------------------------------------------------------ grok
@routes.get("/dope_aio/grok/models")
async def grok_models(request):
    force = request.query.get("refresh") == "1"
    models, source = await _run(grok.list_models, force)
    key, key_source = store.get_secret("xai")
    return web.json_response({"ok": True, "models": models, "source": source, "has_key": bool(key),
                              "key_source": key_source, "styles": STYLE_LABELS})


@routes.get("/dope_aio/keys")
async def get_keys(request):
    out = {}
    for kind in ("xai", "civitai"):
        key, source = store.get_secret(kind)
        out[kind] = {"set": bool(key), "source": source, "redacted": store.redact(key)}
    return web.json_response({"ok": True, "keys": out})


@routes.post("/dope_aio/keys")
async def set_keys(request):
    try:
        body = await request.json()
    except Exception as e:
        return _err(e, 400)
    kind = body.get("kind")
    if kind not in ("xai", "civitai"):
        return _err("kind must be 'xai' or 'civitai'", 400)
    key = (body.get("key") or "").strip()
    store.set_config_value(f"{kind}_api_key", key)
    if kind == "xai":
        grok._models_cache.update(t=0.0, models=None)
    result = {"ok": True, "set": bool(key)}
    env = store.env_override(kind)
    if key and env:
        result["warning"] = f"{env} is set in ComfyUI's environment and takes priority over the key you just saved."
    if kind == "xai" and key and body.get("test", True):
        try:
            test = await _run(grok.test_key)
            result["test"] = test
            if test.get("blocked"):
                result["test_error"] = "xAI says this key is blocked or disabled (check console.x.ai billing/team status)."
        except Exception as e:
            result["test_error"] = str(e)
    return web.json_response(result)


@routes.post("/dope_aio/grok/generate")
async def grok_generate(request):
    try:
        b = await request.json()
    except Exception as e:
        return _err(e, 400)
    try:
        triggers = []
        if b.get("lora_triggers"):
            for name in b.get("loras") or []:
                triggers += await _run_civ(civitai.trigger_words, name)
        res = await _run(
            grok.generate,
            b.get("idea", ""), b.get("style", STYLE_LABELS[0]),
            model=b.get("model") or grok.DEFAULT_MODEL,
            effort=b.get("effort", "low"),
            want_negative=bool(b.get("negative", True)),
            extra_instructions=b.get("instructions", ""),
            seed=int(b.get("seed") or 0) or None,
            lora_triggers=triggers,
            width=int(b.get("width") or 1024), height=int(b.get("height") or 1024),
            length=int(b.get("length") or 124), detail=b.get("detail", "standard"),
            nsfw=b.get("nsfw", True) is not False,
        )
        return web.json_response({"ok": True, **res})
    except grok.GrokError as e:
        return _err(e, 400 if e.status and e.status < 500 else 502)
    except Exception as e:
        log.exception("DopeAIO grok generate failed")
        return _err(e)


# ------------------------------------------------------------------ loras
@routes.get("/dope_aio/loras")
async def list_loras(request):
    names = folder_paths.get_filename_list("loras")
    if request.query.get("previews") == "1":
        flags = await _run_civ(lambda: {n: bool(civitai.find_local_preview(civitai.lora_path(n) or "")) for n in names})
    else:
        flags = {}
    return web.json_response({"ok": True, "loras": names, "local_previews": flags})


@routes.get("/dope_aio/lora/info")
async def lora_info(request):
    name = request.query.get("file", "")
    fetch = request.query.get("fetch", "1") == "1"
    refresh = request.query.get("refresh") == "1"
    try:
        info = await _run_civ(civitai.get_info, name, fetch, refresh)
        return web.json_response({"ok": True, **info})
    except FileNotFoundError:
        return _err(f"lora not found: {name}", 404)
    except Exception as e:
        log.exception("DopeAIO lora info failed")
        return _err(e)


@routes.get("/dope_aio/lora/thumb")
async def lora_thumb(request):
    name = request.query.get("file", "")
    fetch = request.query.get("fetch", "1") == "1"
    level = civitai.NSFW_LEVELS.get(request.query.get("nsfw", "PG13").upper().replace("-", ""), 2)
    try:
        kind, payload, blurred = await _run_civ(civitai.get_thumb, name, fetch, level)
    except FileNotFoundError:
        kind, payload, blurred = None, None, False
    except Exception as e:
        log.warning("DopeAIO thumb failed for %s: %s", name, e)
        kind, payload, blurred = None, None, False
    headers = {"Cache-Control": "private, max-age=600", "X-Dope-Blurred": "1" if blurred else "0"}
    if kind == "file":
        try:
            data = await _run_civ(_read_bytes, payload)
        except OSError:
            return web.Response(status=404, headers={"Cache-Control": "no-store"})
        return web.Response(body=data, content_type=_sniff(data), headers=headers)
    if kind == "bytes":
        return web.Response(body=payload, content_type=_sniff(payload), headers=headers)
    return web.Response(status=404, headers={"Cache-Control": "no-store"})


@routes.get("/dope_aio/civitai/search")
async def civitai_search(request):
    try:
        data = await _run_civ(
            civitai.search_loras,
            request.query.get("q", ""),
            request.query.getall("base", []),
            request.query.get("sort", "Most Downloaded"),
            request.query.get("nsfw", "1") != "0",
            request.query.get("cursor") or None,
        )
        return web.json_response({"ok": True, **data})
    except Exception as e:
        log.warning("DopeAIO civitai search failed: %s", e)
        return _err(e, 502)


@routes.post("/dope_aio/civitai/download")
async def civitai_download(request):
    try:
        body = await request.json()
    except Exception as e:
        return _err(e, 400)
    try:
        job = await _run_civ(civitai.start_download, body.get("version_id"),
                             body.get("filename") or "lora.safetensors", body.get("sha256"))
        return web.json_response({"ok": True, "job": job})
    except Exception as e:
        return _err(e, 400)


@routes.get("/dope_aio/civitai/download/{job}")
async def civitai_download_status(request):
    job = civitai.download_status(request.match_info["job"])
    if not job:
        return _err("unknown download", 404)
    return web.json_response({"ok": True, **job})
