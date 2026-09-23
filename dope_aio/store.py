"""Private on-disk storage (API keys, hash cache, Civitai cache, thumbnails).

Lives in ComfyUI's "system user" directory (``user/__dope_aio``), which ComfyUI
never serves over HTTP, so saved API keys can't be read back through /userdata.
"""

import json
import os
import tempfile
import threading
import time

import folder_paths

_lock = threading.RLock()


def data_dir(*parts):
    try:
        base = folder_paths.get_system_user_directory("dope_aio")
    except Exception:
        base = os.path.join(folder_paths.get_user_directory(), "__dope_aio")
    path = os.path.join(base, *parts)
    os.makedirs(path, exist_ok=True)
    return path


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, private=False):
    """Atomic write via a unique temp file (safe with concurrent readers, threads and
    several ComfyUI processes sharing one user dir); retries briefly on Windows locks."""
    with _lock:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            if private:
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
            for attempt in range(5):
                try:
                    os.replace(tmp, path)
                    return
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def _config_path():
    return os.path.join(data_dir(), "config.json")


def get_config():
    return read_json(_config_path(), {}) or {}


def set_config_value(key, value):
    with _lock:
        cfg = get_config()
        if value in (None, ""):
            cfg.pop(key, None)
        else:
            cfg[key] = value
        write_json(_config_path(), cfg, private=True)


def _key_from_file(name):
    """Plain-text key file next to the pack (e.g. ComfyUI-DopeAIO/xai_api_key.txt)."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def get_secret(kind):
    """kind = 'xai' | 'civitai'. Returns (key, source)."""
    envs = {"xai": ("XAI_API_KEY", "GROK_API_KEY"), "civitai": ("CIVITAI_API_KEY", "CIVITAI_TOKEN")}[kind]
    for env in envs:
        val = os.environ.get(env, "").strip()
        if val:
            return val, f"env:{env}"
    val = (get_config().get(f"{kind}_api_key") or "").strip()
    if val:
        return val, "saved"
    val = _key_from_file(f"{kind}_api_key.txt")
    if val:
        return val, "file"
    return "", ""


def env_override(kind):
    """Name of the env var that currently wins over a saved key, if any."""
    envs = {"xai": ("XAI_API_KEY", "GROK_API_KEY"), "civitai": ("CIVITAI_API_KEY", "CIVITAI_TOKEN")}[kind]
    return next((e for e in envs if os.environ.get(e, "").strip()), "")


def redact(key):
    if not key:
        return ""
    return f"{key[:4]}…{key[-4:]}" if len(key) > 12 else "…"
