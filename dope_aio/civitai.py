"""Civitai for local LoRAs: info, thumbnails, browser and downloader.

Lookup order for a local file (cheapest first):
  1. our cache / per-file pointer (user/__dope_aio/civitai)
  2. sidecars other tools already wrote (rgthree, Lora-Manager, Civitai Helper)
  3. Civitai GET /api/v1/model-versions/by-hash/<hash>   (AutoV3 from the safetensors
     header first — zero hashing — then full SHA256, computed once and cached)
Thumbnails: local preview file > embedded cover > Civitai CDN image (downloaded once, cached).
Browser: GET /api/v1/models (cursor paging). Downloader: /api/download/models/<version>,
SHA256-verified while streaming, then pre-registered so its thumbnail/triggers show instantly.
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
from urllib.parse import unquote, urlparse, urlunparse

import requests

import folder_paths

from . import store

log = logging.getLogger("DopeAIO")

API = "https://civitai.com/api/v1"
USER_AGENT = "ComfyUI-DopeAIO/1.0 (+https://github.com/comfyanonymous/ComfyUI)"
NSFW_LEVELS = {"PG": 1, "PG13": 2, "R": 4, "X": 8, "XXX": 16}
EDGE_WIDTHS = (96, 320, 450, 512, 800, 1200, 1600)
NEGATIVE_TTL = 7 * 24 * 3600   # "not on Civitai" is re-checked after a week
TRANSIENT_TTL = 10 * 60        # network / 5xx / 403 failures are retried after 10 minutes
IMAGE_EXTS = ("png", "jpg", "jpeg", "webp", "gif", "avif")
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
LORA_TYPES = ("LORA", "LoCon", "DoRA")
SORTS = ("Most Downloaded", "Highest Rated", "Newest")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HASH_KEY = re.compile(r"^(?:[0-9a-f]{12}|[0-9a-f]{64})$")
# Only ever fetch Civitai's image CDN: fixed scheme+host, no port/userinfo, plain path chars.
_CDN_URL = re.compile(r"^https://image\.civitai\.com/[A-Za-z0-9_\-./=,%~]+$")

_file_locks = {}
_file_locks_guard = threading.Lock()


def _lock_for(key):
    with _file_locks_guard:
        if len(_file_locks) > 4096:
            _file_locks.clear()
        return _file_locks.setdefault(key, threading.Lock())


def is_cdn_url(url):
    return isinstance(url, str) and bool(_CDN_URL.match(url))


def lora_path(name):
    """Resolve a lora name ONLY inside the configured lora folders (no path traversal)."""
    if not name or not isinstance(name, str):
        return None
    return folder_paths.get_full_path("loras", name)


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


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
    if e and e.get("size") == st.st_size and e.get("mtime_ns") == st.st_mtime_ns and _HEX64.match(str(e.get("sha256"))):
        return e["sha256"]
    return None


def _save_sha(path, sha, st):
    if not _HEX64.match(sha or ""):
        return
    with _lock_for("hashes.json"):
        data = store.read_json(_hash_cache_path(), {}) or {}
        data[os.path.realpath(path)] = {"sha256": sha, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
        store.write_json(_hash_cache_path(), data)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _hex64(v):
    v = str(v or "").strip().lower()
    return v if _HEX64.match(v) else None


def sidecar_sha256(path):
    base = os.path.splitext(path)[0]
    d = _read_json(path + ".rgthree-info.json")
    if isinstance(d, dict) and _hex64(d.get("sha256")):
        return _hex64(d.get("sha256"))
    d = _read_json(base + ".metadata.json")
    if isinstance(d, dict) and _hex64(d.get("sha256")):
        return _hex64(d.get("sha256"))
    try:
        with open(base + ".sha256", "r", encoding="utf-8") as f:
            return _hex64(f.read(128).split()[0] if f else "")
    except (OSError, IndexError):
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
            meta = json.loads(f.read(n)).get("__metadata__") or {}
            return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError, struct.error):
        return {}


def embedded_autov3(path):
    meta = _safetensors_meta(path)
    h = meta.get("sshs_model_hash") or meta.get("modelspec.hash_sha256")
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
        if covers and isinstance(covers[0], str) and covers[0]:
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
    raw = ((d.get("raw") or {}).get("civitai") if isinstance(d.get("raw"), dict) else None) if isinstance(d, dict) else None
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
def _session(auth):
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    if auth:
        key, _ = store.get_secret("civitai")
        if key and key.isascii() and " " not in key:
            s.headers["Authorization"] = f"Bearer {key}"
    return s


def by_hash(h):
    """Returns (status, version_json|None). Public endpoint: no auth header (keeps the edge cache).
    Network errors fail fast (status 0); 429/5xx retried with backoff."""
    h = str(h or "").lower()
    if not _HASH_KEY.match(h):
        return 400, None
    url = f"{API}/model-versions/by-hash/{h.upper()}"
    with _session(auth=False) as s:
        for attempt in range(3):
            try:
                r = s.get(url, timeout=(5, 20))
            except (requests.RequestException, ValueError) as e:
                log.debug("DopeAIO civitai: %s", e)
                return 0, None
            if r.status_code == 200:
                try:
                    return 200, r.json()
                except ValueError:
                    return 0, None
            if r.status_code == 404:
                return 404, None
            if r.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                return r.status_code, None
            time.sleep(2 ** attempt)
    return 0, None


def media_url(url, width=320, media_type="image", poster=False):
    """Rewrite an image.civitai.com URL to a resized edge variant (None for anything else)."""
    if not is_cdn_url(url):
        return None
    p = urlparse(url)
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


def _words(v):
    out = []
    for w in v.get("trainedWords") or []:
        w = (w.get("word") if isinstance(w, dict) else w) if isinstance(w, (dict, str)) else ""
        if isinstance(w, str) and w.strip() and w.strip() not in out:
            out.append(w.strip())
    return out


def _images(v, width=320, limit=None):
    images = []
    for i in v.get("images") or []:
        if not isinstance(i, dict) or not is_cdn_url(i.get("url")):
            continue  # sidecar-supplied URLs must also be Civitai CDN URLs
        typ = i.get("type") if i.get("type") in ("image", "video") else "image"
        images.append({
            "type": typ,
            "nsfwLevel": _int(i.get("nsfwLevel")),
            "width": i.get("width"), "height": i.get("height"),
            "thumb": media_url(i["url"], width, typ, poster=True),
            "full": media_url(i["url"], 800, typ, poster=True),
        })
        if limit and len(images) >= limit:
            break
    return images


def summarize(v, sha=None):
    if not isinstance(v, dict):
        return None
    model = v.get("model") if isinstance(v.get("model"), dict) else {}
    mid, vid = _int(v.get("modelId")) or None, _int(v.get("id")) or None
    return {
        "modelId": mid, "versionId": vid,
        "modelName": model.get("name") or v.get("modelName"), "versionName": v.get("name"),
        "type": model.get("type"), "baseModel": v.get("baseModel"),
        "trainedWords": _words(v),
        "url": f"https://civitai.com/models/{mid}?modelVersionId={vid}" if mid else None,
        "sha256": _hex64(sha),
        "images": _images(v),
    }


def _info_cache_path(key):
    if not _HASH_KEY.match(str(key)):
        raise ValueError("bad hash key")
    return os.path.join(store.data_dir("civitai"), f"{key}.json")


def _by_name_path(path):
    k = hashlib.sha1(os.path.realpath(path).encode("utf-8")).hexdigest()
    return os.path.join(store.data_dir("civitai", "by_file"), f"{k}.json")


def _cache_fresh(cached):
    if not isinstance(cached, dict):
        return False
    age = time.time() - (cached.get("at") or 0)
    status = cached.get("status")
    return status == 200 or (status == 404 and age < NEGATIVE_TTL) or age < TRANSIENT_TTL


def _status_name(status):
    return "found" if status == 200 else "not_found" if status == 404 else f"error_{status}"


def _remember(path, key, status, data):
    """Write the info cache for `key` and point the file at it."""
    st = os.stat(path)
    store.write_json(_info_cache_path(key), {"status": status, "at": time.time(), "data": data})
    store.write_json(_by_name_path(path), {"key": key, "size": st.st_size, "mtime_ns": st.st_mtime_ns})


def get_info(name, fetch=True, refresh=False):
    """Info dict for one lora. fetch=False never hashes or touches the network."""
    path = lora_path(name)
    if not path:
        raise FileNotFoundError(name)
    st = os.stat(path)
    out = {"file": name, "local_preview": bool(find_local_preview(path)), "civitai": None, "status": "unknown"}

    # fast path: per-file pointer written after the last lookup
    ptr = store.read_json(_by_name_path(path)) if not refresh else None
    if (isinstance(ptr, dict) and _HASH_KEY.match(str(ptr.get("key")))
            and ptr.get("size") == st.st_size and ptr.get("mtime_ns") == st.st_mtime_ns):
        cached = store.read_json(_info_cache_path(ptr["key"]))
        if _cache_fresh(cached):
            out["civitai"], out["status"] = cached.get("data"), _status_name(cached.get("status"))
            return out

    if not refresh:
        try:
            side = summarize(sidecar_civitai(path), sidecar_sha256(path))
        except Exception as e:  # a malformed sidecar must not break lookups
            log.debug("DopeAIO: bad sidecar for %s: %s", name, e)
            side = None
        if side and side.get("modelId"):
            out["civitai"], out["status"] = side, "found"
            return out

    if not fetch:
        return out

    with _lock_for(("info", os.path.realpath(path))):
        sha = get_sha256(path, compute=False)
        keys = [k for k in (sha, embedded_autov3(path)) if k]
        for rnd in range(2):
            for k in keys:
                cached = None if refresh else store.read_json(_info_cache_path(k))
                if _cache_fresh(cached):
                    status, data = cached.get("status"), cached.get("data")
                else:
                    status, v = by_hash(k)
                    data = summarize(v, sha) if v else None
                if status not in (200, 404):  # transient: remember briefly so we don't hammer
                    _remember(path, k, status, None)
                    out["status"] = _status_name(status)
                    return out
                if status == 200:
                    _remember(path, k, 200, data)
                    out["civitai"], out["status"] = data, "found"
                    return out
                store.write_json(_info_cache_path(k), {"status": 404, "at": time.time(), "data": None})
            if sha:
                break
            sha = get_sha256(path, compute=True)  # AutoV3 missed / absent -> full hash, once
            keys = [sha]
        _remember(path, sha, 404, None)
        out["status"] = "not_found"
        return out


def trigger_words(name):
    """Cached trained words for a lora (never hits the network)."""
    try:
        info = get_info(name, fetch=False)
    except (FileNotFoundError, OSError, ValueError):
        return []
    return (info.get("civitai") or {}).get("trainedWords") or []


# ------------------------------------------------------------------ thumbnails
def _pick(images, max_level):
    usable = [i for i in images or [] if isinstance(i, dict) and is_cdn_url(i.get("thumb"))]
    for img in usable:
        if 0 < img.get("nsfwLevel", 0) <= max_level:
            return img, False
    if usable:
        return min(usable, key=lambda i: i.get("nsfwLevel") or 99), True
    return None, False


# redirect targets: the CDN hands out https://blobs-b2.civitai.com/... (checked hop by hop)
_CIVITAI_HOP = re.compile(r"^https://(?:[a-z0-9-]+\.)*civitai\.com/[A-Za-z0-9_\-./=,%~]+$")


def _fetch_cdn(url, dest, cap=8 * 1024 * 1024):
    """Download a Civitai CDN image to dest: redirects followed manually and only to
    https://*.civitai.com, size cap, image content only."""
    if not is_cdn_url(url):
        return False
    for _ in range(4):
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=(5, 30), stream=True, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            nxt = r.headers.get("location") or ""
            r.close()
            if not _CIVITAI_HOP.match(nxt):
                return False
            url = nxt
            continue
        break
    else:
        return False
    with r:
        if r.status_code != 200 or not (r.headers.get("content-type") or "").startswith("image/"):
            return False
        tmp, n = f"{dest}.{threading.get_ident()}.tmp", 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(64 * 1024):
                n += len(chunk)
                if n > cap:
                    f.close()
                    os.remove(tmp)
                    return False
                f.write(chunk)
        os.replace(tmp, dest)
    return True


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
    cache = os.path.join(store.data_dir("thumbs"), hashlib.sha1(url.encode("utf-8")).hexdigest() + ".img")
    if os.path.isfile(cache):
        return "file", cache, blurred
    if not fetch:
        return None, None, False
    with _lock_for(("thumb", url)):
        if not os.path.isfile(cache):
            try:
                if not _fetch_cdn(url, cache):
                    return None, None, False
            except (requests.RequestException, OSError) as e:
                log.warning("DopeAIO: thumbnail download failed for %s: %s", name, e)
                return None, None, False
    return "file", cache, blurred


# ------------------------------------------------------------------ browser
def search_loras(query="", bases=(), sort="Most Downloaded", nsfw=True, cursor=None, limit=20):
    """Search Civitai LoRAs (LORA/LoCon/DoRA). Civitai pages text searches by cursor only."""
    params = [("types", t) for t in LORA_TYPES]
    params += [("limit", max(1, min(_int(limit, 20), 40))), ("nsfw", "true" if nsfw else "false"),
               ("sort", sort if sort in SORTS else SORTS[0])]
    if (query or "").strip():
        params.append(("query", query.strip()[:200]))
    for b in bases or ():
        if isinstance(b, str) and b.strip():
            params.append(("baseModels", b.strip()[:60]))
    if cursor:
        params.append(("cursor", str(cursor)[:200]))
    with _session(auth=True) as s:
        r = s.get(f"{API}/models", params=params, timeout=(5, 30))
    if r.status_code == 401:
        raise RuntimeError("Civitai rejected the saved API key — fix or clear it under 🔑.")
    if r.status_code >= 400:
        raise RuntimeError(f"Civitai search failed ({r.status_code})")
    data = r.json()

    local_names = {os.path.basename(n).lower(): n for n in folder_paths.get_filename_list("loras")}
    items = []
    for model in data.get("items") or []:
        if not isinstance(model, dict):
            continue
        versions = []
        for ver in (model.get("modelVersions") or [])[:6]:
            if not isinstance(ver, dict):
                continue
            files = []
            for f in ver.get("files") or []:
                fname = (f or {}).get("name") or ""
                if not fname.lower().endswith(".safetensors"):
                    continue
                sha = _hex64(((f.get("hashes") or {}) if isinstance(f.get("hashes"), dict) else {}).get("SHA256"))
                local = local_names.get(fname.lower())
                files.append({"name": fname, "sizeKB": f.get("sizeKB"), "primary": bool(f.get("primary")),
                              "sha256": sha, "local": local})
            if not files:
                continue
            files.sort(key=lambda x: not x["primary"])
            versions.append({
                "id": _int(ver.get("id")), "name": ver.get("name"), "baseModel": ver.get("baseModel"),
                "trainedWords": _words(ver)[:8], "files": files,
                "images": _images(ver, 320, limit=4),
                "downloads": _int((ver.get("stats") or {}).get("downloadCount")),
            })
        if not versions:
            continue
        stats = model.get("stats") if isinstance(model.get("stats"), dict) else {}
        items.append({
            "id": _int(model.get("id")), "name": model.get("name"), "type": model.get("type"),
            "nsfw": bool(model.get("nsfw")),
            "creator": ((model.get("creator") or {}) if isinstance(model.get("creator"), dict) else {}).get("username"),
            "downloads": _int(stats.get("downloadCount")), "rating": stats.get("thumbsUpCount"),
            "url": f"https://civitai.com/models/{_int(model.get('id'))}",
            "versions": versions,
        })
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    return {"items": items, "next_cursor": meta.get("nextCursor")}


# ------------------------------------------------------------------ downloader
_NAME_OK = re.compile(r"[^A-Za-z0-9._ ()\-\[\]]+")
_jobs = {}
_jobs_guard = threading.Lock()


def _safe_filename(name):
    name = os.path.basename(str(name or "").replace("\\", "/"))
    name = _NAME_OK.sub("_", name).strip(" ._")
    if not name.lower().endswith(".safetensors"):
        raise ValueError("only .safetensors files can be downloaded")
    return name[:180]


def _cd_filename(header):
    if not header:
        return ""
    m = re.search(r"filename\*=UTF-8''([^;]+)", header, re.I)
    if m:
        return unquote(m.group(1))
    m = re.search(r'filename="?([^";]+)"?', header, re.I)
    return m.group(1) if m else ""


def _job_set(job, **fields):
    with _jobs_guard:
        cur = _jobs.get(job)
        if cur is not None:
            cur.update(fields)


def download_status(job):
    with _jobs_guard:
        cur = _jobs.get(job)
        return dict(cur) if cur else None


def start_download(version_id, filename, sha256=None):
    version_id = _int(version_id)
    if version_id <= 0:
        raise ValueError("bad version id")
    filename = _safe_filename(filename)
    sha256 = _hex64(sha256)
    with _jobs_guard:
        for j in _jobs.values():  # same file already downloading -> share the job
            if j.get("status") == "running" and j.get("version_id") == version_id:
                return j["job"]
        if sum(1 for j in _jobs.values() if j.get("status") == "running") >= 2:
            raise RuntimeError("two downloads are already running — wait for one to finish")
        job = hashlib.sha1(f"{version_id}:{filename}:{time.time()}".encode()).hexdigest()[:16]
        _jobs[job] = {"job": job, "version_id": version_id, "status": "running", "bytes": 0, "total": 0,
                      "name": filename, "error": ""}
        if len(_jobs) > 24:
            for old in list(_jobs)[:-16]:
                if _jobs[old].get("status") != "running":
                    _jobs.pop(old, None)
    threading.Thread(target=_download_worker, args=(job, version_id, filename, sha256), daemon=True,
                     name=f"dope-dl-{version_id}").start()
    return job


def _register_download(dest, sha, version_id):
    """Pre-fill hash + Civitai info so the new LoRA's thumbnail/triggers appear instantly."""
    try:
        _save_sha(dest, sha, os.stat(dest))
        status, v = by_hash(sha)
        if status == 200:
            _remember(dest, sha, 200, summarize(v, sha))
    except Exception as e:
        log.debug("DopeAIO: could not pre-register %s (version %s): %s", dest, version_id, e)


def _download_worker(job, version_id, filename, expected_sha):
    tmp = None
    try:
        folders = folder_paths.get_folder_paths("loras")
        if not folders:
            raise RuntimeError("no LoRA folder configured")
        dest_dir = os.path.realpath(folders[0])
        os.makedirs(dest_dir, exist_ok=True)
        headers = {"User-Agent": USER_AGENT}
        key, _ = store.get_secret("civitai")
        if key and key.isascii() and " " not in key:
            headers["Authorization"] = f"Bearer {key}"
        url = f"https://civitai.com/api/download/models/{version_id}"
        with requests.get(url, headers=headers, stream=True, timeout=(15, 120), allow_redirects=True) as r:
            if r.status_code in (401, 403):
                raise RuntimeError("This LoRA needs a Civitai login to download — add your Civitai API key under 🔑.")
            if r.status_code >= 400:
                raise RuntimeError(f"Civitai download failed ({r.status_code})")
            ctype = (r.headers.get("content-type") or "").lower()
            if "application/json" in ctype or ctype.startswith("text/"):
                raise RuntimeError((r.text or "Civitai refused the download")[:300])
            try:
                filename = _safe_filename(_cd_filename(r.headers.get("content-disposition")) or filename)
            except ValueError:
                pass
            dest = os.path.join(dest_dir, filename)
            if os.path.isfile(dest):
                have = get_sha256(dest, compute=True)
                if expected_sha is None or have == expected_sha:  # already have this exact file
                    _register_download(dest, have, version_id)
                    _job_set(job, status="done", name=filename, bytes=os.path.getsize(dest),
                             total=os.path.getsize(dest), existed=True)
                    return
                stem = filename[:-len(".safetensors")]
                filename = _safe_filename(f"{stem}_v{version_id}.safetensors")
                dest = os.path.join(dest_dir, filename)
            if os.path.dirname(os.path.realpath(dest)) != dest_dir:
                raise RuntimeError("bad filename")
            total = _int(r.headers.get("content-length"))
            _job_set(job, name=filename, total=total)
            tmp = f"{dest}.{job}.partial"
            h, written, head = hashlib.sha256(), 0, b""
            cap = 8 * 1024 * 1024 * 1024
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if not chunk:
                        continue
                    if len(head) < 9:
                        head += chunk[:9]
                        if len(head) >= 9 and head[8:9] != b"{":
                            raise RuntimeError("download was not a safetensors file")
                    written += len(chunk)
                    if written > cap:
                        raise RuntimeError("file is larger than 8 GB")
                    h.update(chunk)
                    f.write(chunk)
                    _job_set(job, bytes=written)
        if len(head) < 9:
            raise RuntimeError("download was empty")
        sha = h.hexdigest()
        if expected_sha and sha != expected_sha:
            raise RuntimeError("SHA256 mismatch — the download was corrupted, try again")
        os.replace(tmp, dest)
        tmp = None
        getattr(folder_paths, "filename_list_cache", {}).pop("loras", None)
        _register_download(dest, sha, version_id)
        _job_set(job, status="done", name=filename, bytes=written, total=total or written, existed=False)
        log.info("DopeAIO: downloaded LoRA %s (%.1f MB)", filename, written / 1048576)
    except Exception as e:
        log.warning("DopeAIO: Civitai download failed: %s", e)
        _job_set(job, status="error", error=str(e))
    finally:
        if tmp and os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
