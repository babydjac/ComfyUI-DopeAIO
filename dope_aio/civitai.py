"""Civitai info + thumbnails for local LoRA files.

Lookup order (cheapest first):
  1. local sidecars other tools already wrote (rgthree, Lora-Manager, Civitai Helper)
  2. our own cache (user/__dope_aio/civitai/<sha256>.json)
  3. Civitai GET /api/v1/model-versions/by-hash/<hash>   (AutoV3 from the safetensors
     header first — zero hashing — then full SHA256, computed once and cached)
Thumbnails: local preview file > embedded cover > Civitai image (downloaded once, cached).
"""

import base64
import hashlib
import json
import logging
import os
import re
import struct
import threading
import time
from urllib.parse import urlparse, urlunparse

import requests

import folder_paths

from . import store

log = logging.getLogger("DopeAIO")

API = "https://civitai.com/api/v1"
USER_AGENT = "ComfyUI-DopeAIO/1.0 (+https://github.com/comfyanonymous/ComfyUI)"
NSFW_LEVELS = {"PG": 1, "PG13": 2, "R": 4, "X": 8, "XXX": 16}
EDGE_WIDTHS = (96, 320, 450, 512, 800, 1200, 1600)
NEGATIVE_TTL = 7 * 24 * 3600
IMAGE_EXTS = ("png", "jpg", "jpeg", "webp", "gif", "avif")
VIDEO_EXTS = ("mp4", "webm")
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

_file_locks = {}
_file_locks_guard = threading.Lock()


def _lock_for(key):
    with _file_locks_guard:
        return _file_locks.setdefault(key, threading.Lock())


def lora_path(name):
    """Resolve a lora name ONLY inside the configured lora folders (no path traversal)."""
    if not name or not isinstance(name, str):
        return None
    return folder_paths.get_full_path("loras", name)


# ------------------------------------------------------------------ hashing
def _sha256_file(path):
    with open(path, "rb", buffering=0) as f:
        if hasattr(hashlib, "file_digest"):
            return hashlib.file_digest(f, "sha256").hexdigest()
        h = hashlib.sha256()
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
        return h.hexdigest()


def _hash_cache_path():
    return os.path.join(store.data_dir(), "hashes.json")


def _cached_sha(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    e = (store.read_json(_hash_cache_path(), {}) or {}).get(os.path.realpath(path))
    if e and e.get("size") == st.st_size and e.get("mtime_ns") == st.st_mtime_ns:
        return e.get("sha256")
    return None


def _save_sha(path, sha, st):
    with _file_locks_guard:
        data = store.read_json(_hash_cache_path(), {}) or {}
        data[os.path.realpath(path)] = {"sha256": sha, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
        store.write_json(_hash_cache_path(), data)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def sidecar_sha256(path):
    base = os.path.splitext(path)[0]
    d = _read_json(path + ".rgthree-info.json")
    if isinstance(d, dict) and isinstance(d.get("sha256"), str) and len(d["sha256"]) == 64:
        return d["sha256"].lower()
    d = _read_json(base + ".metadata.json")
    if isinstance(d, dict) and isinstance(d.get("sha256"), str) and len(d["sha256"]) == 64:
        return d["sha256"].lower()
    try:
        with open(base + ".sha256", "r", encoding="utf-8") as f:
            s = f.read().strip().lower()
            if len(s) == 64:
                return s
    except OSError:
        pass
    return None


def get_sha256(path, compute=True):
    sha = _cached_sha(path) or sidecar_sha256(path)
    if sha or not compute:
        return sha
    with _lock_for(("sha", os.path.realpath(path))):
        sha = _cached_sha(path)
        if sha:
            return sha
        before = os.stat(path)
        t = time.time()
        sha = _sha256_file(path)
        after = os.stat(path)
        if (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns):
            _save_sha(path, sha, after)
        log.info("DopeAIO: hashed %s in %.1fs", os.path.basename(path), time.time() - t)
        return sha


def _safetensors_meta(path):
    if not path.endswith(".safetensors"):
        return {}
    try:
        with open(path, "rb") as f:
            (n,) = struct.unpack("<Q", f.read(8))
            if n > 64 * 1024 * 1024:
                return {}
            return json.loads(f.read(n)).get("__metadata__") or {}
    except (OSError, ValueError, struct.error):
        return {}


def embedded_autov3(path):
    h = _safetensors_meta(path).get("sshs_model_hash") or _safetensors_meta(path).get("modelspec.hash_sha256")
    if isinstance(h, str):
        h = h.strip().lower()
        if h.startswith("0x"):
            h = h[2:]
        if len(h) >= 12 and re.fullmatch(r"[0-9a-f]+", h) and h[:12] != EMPTY_SHA256[:12]:
            return h[:12]
    return None


def embedded_cover(path):
    meta = _safetensors_meta(path)
    try:
        covers = json.loads(meta.get("ssmd_cover_images") or "[]")
        if covers and covers[0]:
            return base64.b64decode(covers[0])
    except (ValueError, TypeError):
        pass
    thumb = meta.get("modelspec.thumbnail")
    if isinstance(thumb, str) and thumb.startswith("data:") and "," in thumb:
        try:
            return base64.b64decode(thumb.split(",", 1)[1])
        except ValueError:
            pass
    return None


# ------------------------------------------------------------------ sidecars / previews
def find_local_preview(path):
    base = os.path.splitext(path)[0]
    cands = [f"{base}{sfx}.{ext}" for ext in IMAGE_EXTS for sfx in (".preview", "")]
    for c in cands:
        if os.path.isfile(c):
            return c
    try:
        folder, stem = os.path.dirname(path), os.path.basename(base).lower()
        lowered = {e.lower(): e for e in os.listdir(folder) if e.lower().startswith(stem)}
        for c in cands:
            hit = lowered.get(os.path.basename(c).lower())
            if hit:
                return os.path.join(folder, hit)
    except OSError:
        pass
    return None


def sidecar_civitai(path):
    base = os.path.splitext(path)[0]
    d = _read_json(path + ".rgthree-info.json")
    raw = ((d or {}).get("raw") or {}).get("civitai") if isinstance(d, dict) else None
    if isinstance(raw, dict) and raw.get("modelId"):
        return raw
    d = _read_json(base + ".metadata.json")
    if isinstance(d, dict) and isinstance(d.get("civitai"), dict) and d["civitai"].get("modelId"):
        return d["civitai"]
    d = _read_json(base + ".civitai.info")
    if isinstance(d, dict) and d.get("modelId"):
        return d
    return None


# ------------------------------------------------------------------ civitai api
def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    key, _ = store.get_secret("civitai")
    if key:
        s.headers["Authorization"] = f"Bearer {key}"
    return s


def by_hash(h):
    """Returns (status, version_json|None). Retries 429/5xx."""
    url = f"{API}/model-versions/by-hash/{h.upper()}"
    with _session() as s:
        for attempt in range(4):
            try:
                r = s.get(url, timeout=20)
                if r.status_code == 200:
                    return 200, r.json()
                if r.status_code == 404:
                    return 404, None
                if r.status_code not in (429, 500, 502, 503, 504):
                    return r.status_code, None
            except (requests.RequestException, ValueError) as e:
                log.debug("DopeAIO civitai: %s", e)
            time.sleep(min(20, 2 ** attempt))
    return 0, None


def media_url(url, width=320, media_type="image", poster=False):
    """Rewrite an image.civitai.com URL to a resized edge variant."""
    p = urlparse(url or "")
    if not (p.hostname or "").endswith("image.civitai.com"):
        return url
    segs = p.path.split("/")
    if len(segs) < 4:
        return url
    name = segs[-1]
    head = segs[:-2] if "=" in segs[-2] else segs[:-1]
    stem = name.rsplit(".", 1)[0]
    params = []
    if media_type == "video":
        if poster:
            params.append("anim=false")
        params.append("transcode=true")
        name = stem + (".jpeg" if poster else ".mp4")
    if width:
        w = next((s for s in EDGE_WIDTHS if s >= width), EDGE_WIDTHS[-1])
        params += [f"width={w}", "optimized=true"]
    else:
        params.append("original=true")
    return urlunparse(p._replace(path="/".join(head + [",".join(params), name])))


def summarize(v, sha=None):
    model = v.get("model") or {}
    images = []
    for i in v.get("images") or []:
        if not i.get("url"):
            continue
        typ = i.get("type") or "image"
        images.append({
            "type": typ,
            "nsfwLevel": int(i.get("nsfwLevel") or 0),
            "width": i.get("width"), "height": i.get("height"),
            "thumb": media_url(i["url"], 320, typ, poster=True),
            "full": media_url(i["url"], 800, typ, poster=False),
        })
    words = []
    for w in v.get("trainedWords") or []:
        w = (w.get("word") if isinstance(w, dict) else w) or ""
        if w.strip():
            words.append(w.strip())
    return {
        "modelId": v.get("modelId"), "versionId": v.get("id"),
        "modelName": model.get("name") or v.get("modelName"), "versionName": v.get("name"),
        "type": model.get("type"), "baseModel": v.get("baseModel"),
        "trainedWords": words,
        "url": f"https://civitai.com/models/{v.get('modelId')}?modelVersionId={v.get('id')}" if v.get("modelId") else None,
        "sha256": sha,
        "images": images,
    }


def _info_cache_path(key):
    return os.path.join(store.data_dir("civitai"), f"{key}.json")


def _by_name_path(path):
    k = hashlib.sha1(os.path.realpath(path).encode("utf-8")).hexdigest()
    return os.path.join(store.data_dir("civitai", "by_file"), f"{k}.json")


def get_info(name, fetch=True, refresh=False):
    """Info dict for one lora. fetch=False never hashes or touches the network."""
    path = lora_path(name)
    if not path:
        raise FileNotFoundError(name)
    st = os.stat(path)
    out = {"file": name, "local_preview": bool(find_local_preview(path)), "civitai": None, "status": "unknown"}

    # fast path: per-file pointer written after the last successful lookup
    ptr = store.read_json(_by_name_path(path)) if not refresh else None
    if ptr and ptr.get("size") == st.st_size and ptr.get("mtime_ns") == st.st_mtime_ns:
        cached = store.read_json(_info_cache_path(ptr["key"]))
        if cached and (cached.get("status") == 200 or time.time() - cached.get("at", 0) < NEGATIVE_TTL):
            out["civitai"], out["status"] = cached.get("data"), ("found" if cached.get("status") == 200 else "not_found")
            return out

    if not refresh:
        side = sidecar_civitai(path)
        if side:
            out["civitai"], out["status"] = summarize(side, sidecar_sha256(path)), "found"
            return out

    if not fetch:
        return out

    with _lock_for(("info", os.path.realpath(path))):
        keys = []
        v3 = embedded_autov3(path)
        if v3:
            keys.append(v3)
        sha = get_sha256(path, compute=False)
        if sha:
            keys.insert(0, sha)
        tried = set()
        result = None
        for attempt in range(2):
            for k in keys:
                if k in tried:
                    continue
                tried.add(k)
                cached = None if refresh else store.read_json(_info_cache_path(k))
                if cached and (cached.get("status") == 200 or time.time() - cached.get("at", 0) < NEGATIVE_TTL):
                    status, data = cached.get("status"), cached.get("data")
                else:
                    status, v = by_hash(k)
                    if status not in (200, 404):
                        out["status"] = f"error_{status}"
                        return out
                    data = summarize(v, sha) if v else None
                    store.write_json(_info_cache_path(k), {"status": status, "at": time.time(), "data": data})
                if status == 200:
                    result = (k, data)
                    break
            if result or sha:
                break
            sha = get_sha256(path, compute=True)  # AutoV3 missed / absent -> full hash, once
            keys = [sha]
        key = result[0] if result else (sha or (keys[0] if keys else None))
        if key:
            store.write_json(_by_name_path(path), {"key": key, "size": st.st_size, "mtime_ns": st.st_mtime_ns})
        if result:
            out["civitai"], out["status"] = result[1], "found"
        else:
            out["status"] = "not_found"
        return out


def trigger_words(name):
    """Cached trained words for a lora (never hits the network)."""
    try:
        info = get_info(name, fetch=False)
    except (FileNotFoundError, OSError):
        return []
    return (info.get("civitai") or {}).get("trainedWords") or []


# ------------------------------------------------------------------ thumbnails
def _pick(images, max_level):
    usable = [i for i in images or [] if i.get("thumb")]
    for img in usable:
        if 0 < img.get("nsfwLevel", 0) <= max_level:
            return img, False
    if usable:
        return min(usable, key=lambda i: i.get("nsfwLevel") or 99), True
    return None, False


def get_thumb(name, fetch=True, max_level=NSFW_LEVELS["PG13"]):
    """Returns (kind, payload, blurred). kind: 'file' (path) | 'bytes' (raw image) | None."""
    path = lora_path(name)
    if not path:
        return None, None, False
    local = find_local_preview(path)
    if local:
        return "file", local, False
    cover = embedded_cover(path)
    if cover:
        return "bytes", cover, False
    info = get_info(name, fetch=fetch)
    img, blurred = _pick((info.get("civitai") or {}).get("images"), max_level)
    if not img:
        return None, None, False
    url = img["thumb"]
    host = (urlparse(url).hostname or "").lower()
    if urlparse(url).scheme != "https" or not (host == "image.civitai.com" or host.endswith(".civitai.com")):
        return None, None, False  # never fetch arbitrary URLs from sidecar files
    fname = hashlib.sha1(url.encode("utf-8")).hexdigest() + ".img"
    cache = os.path.join(store.data_dir("thumbs"), fname)
    if os.path.isfile(cache):
        return "file", cache, blurred
    if not fetch:
        return None, None, False
    with _lock_for(("thumb", url)):
        if not os.path.isfile(cache):
            try:
                r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
                r.raise_for_status()
                tmp = cache + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(r.content)
                os.replace(tmp, cache)
            except requests.RequestException as e:
                log.warning("DopeAIO: thumbnail download failed for %s: %s", name, e)
                return None, None, False
    return "file", cache, blurred
