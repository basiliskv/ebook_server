import os, zipfile, io, hashlib, json, tempfile, subprocess, logging
import re
import time
from functools import lru_cache
from urllib.parse import unquote

from PIL import Image
import fitz  # PyMuPDF
try:
    import rarfile
except Exception:
    rarfile = None

LOGGER = logging.getLogger(__name__)
EAGLE_CACHE_TTL_SECONDS = float(os.environ.get("EAGLE_CACHE_TTL_SECONDS", "8"))
EAGLE_PAGE_NEIGHBOR_SCAN_LIMIT = int(os.environ.get("EAGLE_PAGE_NEIGHBOR_SCAN_LIMIT", "900"))
EAGLE_PAGE_WINDOW_LIMIT = int(os.environ.get("EAGLE_PAGE_WINDOW_LIMIT", "360"))
EAGLE_FOLDER_SUMMARY_SCAN_LIMIT = int(os.environ.get("EAGLE_FOLDER_SUMMARY_SCAN_LIMIT", "5000"))
EAGLE_ITEM_SNAPSHOT_CACHE = {}
EAGLE_ITEM_INDEX_CACHE = {}
EAGLE_ITEM_INDEX_VERSION = 1

# ======================
# 設定
# ======================
CONFIG_FILE = os.environ.get("EBOOK_SERVER_CONFIG", "local_config.json")
DEFAULT_CONFIG = {
    "defaultProfile": "default",
    "defaultLibrary": "sample",
    "cacheRoot": ".cache",
    "profileOptions": {
        "default": {
            "queryPlusAsSpace": False,
        },
    },
    "profiles": {
        "default": {
            "sample": "./library",
        },
    },
}


def load_local_config():
    if not os.path.exists(CONFIG_FILE):
        return DEFAULT_CONFIG

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Could not read config %s: %s", CONFIG_FILE, exc)
        return DEFAULT_CONFIG

    profiles = config.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        LOGGER.warning("Config %s has no profiles; using defaults", CONFIG_FILE)
        return DEFAULT_CONFIG

    return config


LOCAL_CONFIG = load_local_config()
LIBRARY_PROFILES = {
    profile: dict(libraries)
    for profile, libraries in LOCAL_CONFIG.get("profiles", {}).items()
    if isinstance(libraries, dict) and libraries
}
PROFILE_OPTIONS = LOCAL_CONFIG.get("profileOptions", {})
if not LIBRARY_PROFILES:
    LIBRARY_PROFILES = dict(DEFAULT_CONFIG["profiles"])

ACTIVE_PROFILE = LOCAL_CONFIG.get("defaultProfile") or next(iter(LIBRARY_PROFILES))
LIBRARIES = dict(LIBRARY_PROFILES.get(ACTIVE_PROFILE) or next(iter(LIBRARY_PROFILES.values())))
DEFAULT_LIBRARY = LOCAL_CONFIG.get("defaultLibrary") if LOCAL_CONFIG.get("defaultLibrary") in LIBRARIES else next(iter(LIBRARIES))
BOOK_DIR = LIBRARIES[DEFAULT_LIBRARY]
CACHE_ROOT = LOCAL_CONFIG.get("cacheRoot") or ".cache"
THUMB_DIR = "thumb_cache"
PAGE_CACHE_DIR = "page_cache"
PDF_PAGE_CACHE_DIR = "pdf_page_cache"
VIDEO_CACHE_DIR = "video_cache"
EAGLE_INDEX_DIR = "eagle_index"
META_FILE = ".cache_meta.json"


def configure_cache_paths():
    global CACHE_ROOT, THUMB_DIR, PAGE_CACHE_DIR, PDF_PAGE_CACHE_DIR, VIDEO_CACHE_DIR, EAGLE_INDEX_DIR, META_FILE

    CACHE_ROOT = (
        os.environ.get("EBOOK_CACHE_ROOT")
        or get_profile_option("cacheRoot", default=None)
        or LOCAL_CONFIG.get("cacheRoot")
        or ".cache"
    )
    THUMB_DIR = os.path.join(CACHE_ROOT, "thumb_cache")
    PAGE_CACHE_DIR = os.path.join(CACHE_ROOT, "page_cache")
    PDF_PAGE_CACHE_DIR = os.path.join(CACHE_ROOT, "pdf_page_cache")
    VIDEO_CACHE_DIR = os.path.join(CACHE_ROOT, "video_cache")
    EAGLE_INDEX_DIR = os.path.join(CACHE_ROOT, "eagle_index")
    META_FILE = os.path.join(CACHE_ROOT, ".cache_meta.json")

    os.makedirs(CACHE_ROOT, exist_ok=True)
    os.makedirs(THUMB_DIR, exist_ok=True)
    os.makedirs(PAGE_CACHE_DIR, exist_ok=True)
    os.makedirs(PDF_PAGE_CACHE_DIR, exist_ok=True)
    os.makedirs(VIDEO_CACHE_DIR, exist_ok=True)
    os.makedirs(EAGLE_INDEX_DIR, exist_ok=True)


def configure_library_profile(profile=None):
    global ACTIVE_PROFILE, DEFAULT_LIBRARY, BOOK_DIR

    selected = profile or os.environ.get("EBOOK_SERVER_PROFILE") or ACTIVE_PROFILE
    if selected not in LIBRARY_PROFILES:
        selected = next(iter(LIBRARY_PROFILES))

    ACTIVE_PROFILE = selected
    LIBRARIES.clear()
    LIBRARIES.update(LIBRARY_PROFILES[selected])
    env_default = os.environ.get("EBOOK_DEFAULT_LIBRARY")
    if env_default in LIBRARIES:
        DEFAULT_LIBRARY = env_default
    elif LOCAL_CONFIG.get("defaultLibrary") in LIBRARIES:
        DEFAULT_LIBRARY = LOCAL_CONFIG["defaultLibrary"]
    else:
        DEFAULT_LIBRARY = next(iter(LIBRARIES))
    BOOK_DIR = LIBRARIES[DEFAULT_LIBRARY]
    configure_cache_paths()


def get_active_profile():
    return ACTIVE_PROFILE


def get_profile_option(name, default=None, profile=None):
    selected = profile or ACTIVE_PROFILE
    options = PROFILE_OPTIONS.get(selected, {})
    if not isinstance(options, dict):
        return default
    return options.get(name, default)


def query_plus_as_space(profile=None):
    selected = (profile or ACTIVE_PROFILE or "").lower()
    configured = get_profile_option("queryPlusAsSpace", default=None, profile=profile)
    if configured is not None:
        return bool(configured)
    return selected in {"mac", "darwin"}


configure_library_profile()
ARCHIVE_EXTS = (".zip", ".rar")
VIDEO_EXTS = (".mp4", ".webm", ".mov", ".m4v")
STANDALONE_MEDIA_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif") + VIDEO_EXTS
UPLOAD_EXTS = ARCHIVE_EXTS + (".pdf",) + STANDALONE_MEDIA_EXTS

# ======================
# キャッシュ管理
# ======================
PAGE_LIST_CACHE = {}

if os.path.exists(META_FILE):
    try:
        with open(META_FILE, "r", encoding="utf-8") as f:
            CACHE_META = json.load(f)
    except (json.JSONDecodeError, OSError):
        CACHE_META = {}
else:
    CACHE_META = {}


def save_meta():
    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(CACHE_META, f, indent=2)


def resolve_library(library):
    return library if library in LIBRARIES else DEFAULT_LIBRARY


def is_eagle_library(library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    return LIBRARIES[library].lower().endswith(".library")


def cache_book_key(book, library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    return f"{library}::{book}"


def invalidate_cache(book, library=DEFAULT_LIBRARY):
    key_name = cache_book_key(book, library)
    PAGE_LIST_CACHE.pop(key_name, None)
    key = hashlib.md5(key_name.encode()).hexdigest()

    for d in (PAGE_CACHE_DIR, PDF_PAGE_CACHE_DIR, THUMB_DIR, VIDEO_CACHE_DIR):
        for f in os.listdir(d):
            if f.startswith(key):
                try:
                    os.remove(os.path.join(d, f))
                except:
                    pass

    CACHE_META.pop(key_name, None)
    save_meta()


# ======================
# utils
# ======================
def resolve_existing_relpath(rel, library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    rel = os.path.normpath(unquote(rel))
    base_dir = LIBRARIES[library]

    path = os.path.join(base_dir, rel)
    if os.path.exists(path):
        return rel

    alt_rel = rel.replace("+", " ")
    alt_path = os.path.join(base_dir, alt_rel)
    if alt_rel != rel and os.path.exists(alt_path):
        return alt_rel

    return rel


def fullpath(rel, library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    return os.path.join(LIBRARIES[library], resolve_existing_relpath(rel, library))


def library_base_dir(library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    return LIBRARIES[library]


def safe_library_relpath(rel):
    rel = (rel or "").replace("\\", "/").strip("/")
    normalized = os.path.normpath(unquote(rel)).replace("\\", "/")
    if normalized in (".", ""):
        return ""
    if normalized.startswith("../") or normalized == ".." or os.path.isabs(normalized):
        raise ValueError("Invalid path")
    return normalized


def library_path_for_write(rel, library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    base_dir = os.path.abspath(LIBRARIES[library])
    safe_rel = safe_library_relpath(rel)
    target = os.path.abspath(os.path.join(base_dir, safe_rel))
    if target != base_dir and not target.startswith(base_dir + os.sep):
        raise ValueError("Invalid path")
    return target


def list_books(library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    if is_eagle_library(library):
        return list_eagle_items(library)

    base_dir = LIBRARIES[library]
    out = []
    for root, _, files in os.walk(base_dir):
        for f in files:
            if f.lower().endswith(UPLOAD_EXTS):
                rel = os.path.relpath(os.path.join(root, f), base_dir)
                out.append(rel.replace("\\", "/"))
    return sorted(out)


def _natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def eagle_updated_at(meta):
    for key in ("modificationTime", "lastModified", "mtime", "btime"):
        value = meta.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return value / 1000 if value > 10_000_000_000 else value
    return 0


def eagle_sort_time(meta):
    value = meta.get("modificationTime") if isinstance(meta, dict) else None
    if isinstance(value, (int, float)) and value > 0:
        return value / 1000 if value > 10_000_000_000 else value
    return eagle_updated_at(meta if isinstance(meta, dict) else {})


def get_next_book_suggestion(book, library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    books = list_books(library)
    if book not in books:
        return None

    cur_dir = os.path.dirname(book)
    same_dir = [b for b in books if os.path.dirname(b) == cur_dir and b != book]
    if same_dir:
        ordered = sorted(same_dir + [book], key=_natural_key)
        pos = ordered.index(book)
        if pos + 1 < len(ordered):
            return ordered[pos + 1]

    pos = books.index(book)
    if pos + 1 < len(books):
        return books[pos + 1]
    return None


def is_archive(book):
    return book.lower().endswith(ARCHIVE_EXTS)


def is_standalone_media(book):
    return book.lower().endswith(STANDALONE_MEDIA_EXTS)


def is_upload_file(name):
    return name.lower().endswith(UPLOAD_EXTS)


def is_video_ext(path):
    return os.path.splitext(path.lower())[1] in VIDEO_EXTS


def has_video(book, library=DEFAULT_LIBRARY):
    if is_eagle_library(library):
        item = get_eagle_item(book, library)
        return bool(item and is_video_ext(item["media_name"]))

    if is_video_ext(book):
        return True

    if not is_archive(book):
        return False

    key_name = cache_book_key(book, library)
    path = fullpath(book, library)
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        return False

    meta = CACHE_META.get(key_name, {})
    if meta.get("mtime") != mtime:
        meta = {}

    if "has_video" in meta:
        return bool(meta["has_video"])

    try:
        with open_archive(path) as arc:
            names = archive_namelist(arc)
        has_vid = any(n.lower().endswith(".mp4") for n in names)
    except Exception:
        has_vid = False

    CACHE_META[key_name] = {"mtime": mtime, "has_video": has_vid}
    save_meta()
    return has_vid


def is_valid_image(p):
    p = p.lower()
    return not (p.startswith("__macosx/") or "/._" in p) and p.endswith(
        (".jpg", ".jpeg", ".png", ".webp")
    )


def is_valid_media(p):
    p = p.lower()
    return not (p.startswith("__macosx/") or "/._" in p) and p.endswith(
        STANDALONE_MEDIA_EXTS
    )


def media_mimetype(p):
    ext = os.path.splitext(p.lower())[1]
    if ext == ".gif":
        return "image/gif"
    if ext == ".mp4":
        return "video/mp4"
    if ext == ".webm":
        return "video/webm"
    if ext == ".mov":
        return "video/quicktime"
    if ext == ".m4v":
        return "video/x-m4v"
    if ext == ".webp":
        return "image/webp"
    if ext == ".png":
        return "image/png"
    return "image/jpeg"


def eagle_images_dir(library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    return os.path.join(LIBRARIES[library], "images")


def _file_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def eagle_library_metadata(library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    meta_path = os.path.join(LIBRARIES[library], "metadata.json")
    return _eagle_library_metadata_cached(meta_path, _file_mtime(meta_path))


@lru_cache(maxsize=16)
def _eagle_library_metadata_cached(meta_path, _mtime):
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Skipping unreadable Eagle library metadata %s: %s", meta_path, exc)
        return {}


def eagle_folder_map(library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    meta_path = os.path.join(LIBRARIES[library], "metadata.json")
    return _eagle_folder_map_cached(meta_path, _file_mtime(meta_path))


@lru_cache(maxsize=16)
def _eagle_folder_map_cached(meta_path, _mtime):
    folders = _eagle_library_metadata_cached(meta_path, _mtime).get("folders", [])
    out = {}

    def walk(nodes, parents):
        for node in nodes:
            name = node.get("name") or node.get("id") or "(unnamed)"
            path_parts = parents + [name]
            out[node.get("id")] = {
                "id": node.get("id"),
                "name": name,
                "path": "/".join(path_parts),
            }
            walk(node.get("children", []), path_parts)

    walk(folders, [])
    return out


def eagle_folder_summaries(library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    return eagle_item_index(library).get("folder_summaries", [])


@lru_cache(maxsize=16)
def _eagle_folder_structure_summaries_cached(library_meta_path, _mtime):
    folders = _eagle_library_metadata_cached(library_meta_path, _mtime).get("folders", [])
    rows = []

    def walk(nodes, parents):
        sorted_nodes = sorted(
            [node for node in nodes if isinstance(node, dict)],
            key=lambda node: (
                -(eagle_updated_at(node) or 0),
                str(node.get("name") or node.get("id") or "").lower(),
            ),
        )
        for node in sorted_nodes:
            name = node.get("name") or node.get("id") or "(unnamed)"
            path_parts = parents + [name]
            children = node.get("children", [])
            if not isinstance(children, list):
                children = []
            rows.append(
                {
                    "group": "/".join(path_parts),
                    "title": name,
                    "count": 0,
                    "depth": max(len(path_parts) - 1, 0),
                    "hasChildren": bool(children),
                }
            )
            walk(children, path_parts)

    if isinstance(folders, list):
        walk(folders, [])
    return rows


@lru_cache(maxsize=16)
def _eagle_folder_summaries_cached(images_dir, library_meta_path, _signature, library):
    folder_lookup = _eagle_folder_map_cached(library_meta_path, _file_mtime(library_meta_path))
    counts = {}
    newest = {}
    children = {}

    def folder_parts(path):
        return [part.strip() for part in str(path).replace("／", "/").split("/") if part.strip()]

    snapshot = eagle_items_snapshot(library)
    for item in snapshot.get("items", {}).values():
        meta = item.get("meta", {})
        if not isinstance(meta, dict):
            meta = {}
        if meta.get("isDeleted") is True:
            continue

        folder_ids = item.get("folder_ids") or meta.get("folders", [])
        if not isinstance(folder_ids, list):
            folder_ids = []
        folder_paths = [folder_lookup[fid]["path"] for fid in folder_ids if fid in folder_lookup]
        group = folder_paths[0] if folder_paths else "(root)"
        updated_at = eagle_updated_at(meta)

        if group == "(root)":
            counts["(root)"] = counts.get("(root)", 0) + 1
            newest["(root)"] = max(newest.get("(root)", 0), updated_at)
            continue

        parts = folder_parts(group)
        if not parts:
            counts["(root)"] = counts.get("(root)", 0) + 1
            newest["(root)"] = max(newest.get("(root)", 0), updated_at)
            continue

        group_path = "/".join(parts)
        counts[group_path] = counts.get(group_path, 0) + 1
        newest[group_path] = max(newest.get(group_path, 0), updated_at)

        current = []
        for part in parts:
            parent = "/".join(current) if current else None
            current.append(part)
            path = "/".join(current)
            children.setdefault(parent, set()).add(path)
            newest[path] = max(newest.get(path, 0), updated_at)

    def folder_title(path):
        parts = folder_parts(path)
        return parts[-1] if parts else path

    rows = []
    if counts.get("(root)", 0) > 0:
        rows.append({"group": "(root)", "title": "未分類", "count": counts["(root)"], "depth": 0, "hasChildren": False})

    def append_rows(parent, depth):
        child_paths = sorted(
            children.get(parent, set()),
            key=lambda path: (-newest.get(path, 0), folder_title(path).lower()),
        )
        for path in child_paths:
            rows.append(
                {
                    "group": path,
                    "title": folder_title(path),
                    "count": counts.get(path, 0),
                    "depth": depth,
                    "hasChildren": bool(children.get(path)),
                }
            )
            append_rows(path, depth + 1)

    append_rows(None, 0)
    return rows


def eagle_item_dir(book, library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    book = os.path.normpath(unquote(book))
    if book.lower().endswith(".info"):
        item_id = os.path.basename(book)[:-5]
    else:
        item_id = os.path.basename(book)
    return os.path.join(eagle_images_dir(library), item_id + ".info")


def _find_eagle_media_name(item_dir, meta):
    name = meta.get("name") or "item"
    ext = str(meta.get("ext") or "").lstrip(".")
    if ext:
        candidate = f"{name}.{ext}"
        if os.path.exists(os.path.join(item_dir, candidate)):
            return candidate

    for fname in os.listdir(item_dir):
        lower = fname.lower()
        if lower.startswith("."):
            continue
        if lower == "metadata.json":
            continue
        if lower.endswith(".json"):
            continue
        if "_thumbnail" in lower:
            continue
        if not lower.endswith(STANDALONE_MEDIA_EXTS):
            continue
        return fname
    return None


def _find_eagle_thumb_name(item_dir):
    for fname in os.listdir(item_dir):
        lower = fname.lower()
        if "_thumbnail" in lower and lower.endswith((".png", ".jpg", ".jpeg", ".webp")):
            return fname
    return None


def get_eagle_item(book, library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    if not is_eagle_library(library):
        return None

    item_id = os.path.basename(os.path.normpath(unquote(book)))
    if item_id.lower().endswith(".info"):
        item_id = item_id[:-5]
    item_dir = os.path.join(eagle_images_dir(library), item_id + ".info")
    meta_path = os.path.join(item_dir, "metadata.json")
    return _get_eagle_item_cached(item_dir, meta_path, _file_mtime(meta_path), library)


def invalidate_eagle_item_cache():
    _get_eagle_item_cached.cache_clear()
    _list_eagle_items_cached.cache_clear()
    invalidate_eagle_index()


@lru_cache(maxsize=8192)
def _get_eagle_item_cached(item_dir, meta_path, _mtime, library):
    if _mtime is None:
        return None

    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Skipping unreadable Eagle item metadata %s: %s", meta_path, exc)
        return None

    media_name = _find_eagle_media_name(item_dir, meta)
    thumb_name = _find_eagle_thumb_name(item_dir)
    item_id = os.path.basename(item_dir)[:-5]
    folder_lookup = eagle_folder_map(library)
    folder_ids = meta.get("folders", [])
    if not isinstance(folder_ids, list):
        folder_ids = []
    folders = [folder_lookup[fid]["path"] for fid in folder_ids if fid in folder_lookup]
    return {
        "id": item_id,
        "title": media_name or meta.get("name") or item_id,
        "media_name": media_name,
        "media_path": os.path.join(item_dir, media_name) if media_name else None,
        "thumb_path": os.path.join(item_dir, thumb_name) if thumb_name else None,
        "folder_ids": folder_ids,
        "folders": folders,
        "meta": meta,
    }


def eagle_index_signature(library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    library_path = LIBRARIES[library]
    images_dir = eagle_images_dir(library)
    metadata_path = os.path.join(library_path, "metadata.json")
    return {
        "libraryPath": os.path.abspath(library_path),
        "imagesMtime": _file_mtime(images_dir),
        "libraryMtime": _file_mtime(metadata_path),
    }


def eagle_index_cache_path(library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    library_path = os.path.abspath(LIBRARIES[library])
    key = hashlib.md5(library_path.encode("utf-8")).hexdigest()
    return os.path.join(EAGLE_INDEX_DIR, key + ".json")


def _compact_eagle_meta(meta):
    if not isinstance(meta, dict):
        return {}
    keys = (
        "name",
        "ext",
        "width",
        "height",
        "size",
        "isDeleted",
        "star",
        "tags",
        "url",
        "annotation",
        "modificationTime",
        "lastModified",
        "mtime",
        "btime",
    )
    return {key: meta[key] for key in keys if key in meta}


def _fast_eagle_media_name(item_dir, meta):
    name = meta.get("name")
    ext = str(meta.get("ext") or "").lstrip(".")

    if isinstance(name, str) and name.lower().endswith(STANDALONE_MEDIA_EXTS):
        if os.path.exists(os.path.join(item_dir, name)):
            return name

    if isinstance(name, str) and ext:
        candidate = f"{name}.{ext}"
        if os.path.exists(os.path.join(item_dir, candidate)):
            return candidate

    try:
        return _find_eagle_media_name(item_dir, meta)
    except OSError:
        return None


def _build_eagle_folder_summary_rows(items, folder_lookup):
    counts = {}
    newest = {}
    children = {}

    def folder_parts(path):
        return [part.strip() for part in str(path).replace("／", "/").split("/") if part.strip()]

    for item in items.values():
        meta = item.get("meta", {})
        if isinstance(meta, dict) and meta.get("isDeleted") is True:
            continue

        folders = item.get("folders") or []
        group = folders[0] if folders else "(root)"
        updated_at = item.get("sort_time") or eagle_updated_at(meta if isinstance(meta, dict) else {})

        if group == "(root)":
            counts["(root)"] = counts.get("(root)", 0) + 1
            newest["(root)"] = max(newest.get("(root)", 0), updated_at)
            continue

        parts = folder_parts(group)
        if not parts:
            counts["(root)"] = counts.get("(root)", 0) + 1
            newest["(root)"] = max(newest.get("(root)", 0), updated_at)
            continue

        group_path = "/".join(parts)
        counts[group_path] = counts.get(group_path, 0) + 1
        newest[group_path] = max(newest.get(group_path, 0), updated_at)

        current = []
        for part in parts:
            parent = "/".join(current) if current else None
            current.append(part)
            path = "/".join(current)
            children.setdefault(parent, set()).add(path)
            newest[path] = max(newest.get(path, 0), updated_at)

    def folder_title(path):
        parts = folder_parts(path)
        return parts[-1] if parts else path

    rows = []
    if counts.get("(root)", 0) > 0:
        rows.append({"group": "(root)", "title": "未分類", "count": counts["(root)"], "depth": 0, "hasChildren": False})

    def append_rows(parent, depth):
        child_paths = sorted(
            children.get(parent, set()),
            key=lambda path: (-newest.get(path, 0), folder_title(path).lower()),
        )
        for path in child_paths:
            rows.append(
                {
                    "group": path,
                    "title": folder_title(path),
                    "count": counts.get(path, 0),
                    "depth": depth,
                    "hasChildren": bool(children.get(path)),
                }
            )
            append_rows(path, depth + 1)

    append_rows(None, 0)
    return rows


def build_eagle_item_index(library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    images_dir = eagle_images_dir(library)
    folder_lookup = eagle_folder_map(library)
    items = {}

    try:
        entries = list(os.scandir(images_dir))
    except OSError as exc:
        LOGGER.warning("Skipping unreadable Eagle images directory %s: %s", images_dir, exc)
        return {"items": {}, "ordered_ids": [], "active_ids": [], "deleted_ids": [], "folder_summaries": []}

    for entry in entries:
        if not entry.name.lower().endswith(".info"):
            continue

        item_id = entry.name[:-5]
        meta_path = os.path.join(entry.path, "metadata.json")
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                raw_meta = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Skipping unreadable Eagle item metadata %s: %s", meta_path, exc)
            continue

        meta = _compact_eagle_meta(raw_meta)
        media_name = _fast_eagle_media_name(entry.path, meta)
        if not media_name:
            continue

        folder_ids = meta.get("folders", raw_meta.get("folders", []))
        if not isinstance(folder_ids, list):
            folder_ids = []
        folders = [folder_lookup[fid]["path"] for fid in folder_ids if fid in folder_lookup]
        sort_time = eagle_sort_time(meta)
        items[item_id] = {
            "id": item_id,
            "title": media_name or meta.get("name") or item_id,
            "media_name": media_name,
            "folder_ids": folder_ids,
            "folders": folders,
            "meta": meta,
            "sort_time": sort_time,
        }

    ordered_ids = sorted(
        items.keys(),
        key=lambda item_id: (
            -items[item_id].get("sort_time", 0),
            _natural_key(item_id),
        ),
    )
    active_ids = [item_id for item_id in ordered_ids if items[item_id].get("meta", {}).get("isDeleted") is not True]
    deleted_ids = [item_id for item_id in ordered_ids if items[item_id].get("meta", {}).get("isDeleted") is True]
    return {
        "items": items,
        "ordered_ids": ordered_ids,
        "active_ids": active_ids,
        "deleted_ids": deleted_ids,
        "folder_summaries": _build_eagle_folder_summary_rows(items, folder_lookup),
    }


def _read_eagle_item_index_from_disk(path, signature):
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    if payload.get("version") != EAGLE_ITEM_INDEX_VERSION:
        return None
    if payload.get("signature") != signature:
        return None
    index = payload.get("index")
    return index if isinstance(index, dict) else None


def _write_eagle_item_index_to_disk(path, signature, index):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "version": EAGLE_ITEM_INDEX_VERSION,
                    "signature": signature,
                    "storedAt": time.time(),
                    "index": index,
                },
                f,
                ensure_ascii=False,
                separators=(",", ":"),
            )
    except OSError as exc:
        LOGGER.warning("Could not write Eagle index cache %s: %s", path, exc)


def eagle_item_index(library=DEFAULT_LIBRARY, refresh=False):
    library = resolve_library(library)
    library_path = LIBRARIES[library]
    signature = eagle_index_signature(library)
    cache_key = os.path.abspath(library_path)

    cached = EAGLE_ITEM_INDEX_CACHE.get(cache_key)
    if not refresh and cached and cached.get("signature") == signature:
        return cached["index"]

    cache_path = eagle_index_cache_path(library)
    if not refresh:
        disk_index = _read_eagle_item_index_from_disk(cache_path, signature)
        if disk_index is not None:
            EAGLE_ITEM_INDEX_CACHE[cache_key] = {"signature": signature, "index": disk_index}
            return disk_index

    index = build_eagle_item_index(library)
    EAGLE_ITEM_INDEX_CACHE[cache_key] = {"signature": signature, "index": index}
    _write_eagle_item_index_to_disk(cache_path, signature, index)
    return index


def eagle_index_item(book, library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    item_id = os.path.basename(os.path.normpath(unquote(book)))
    if item_id.lower().endswith(".info"):
        item_id = item_id[:-5]
    return eagle_item_index(library).get("items", {}).get(item_id)


def invalidate_eagle_index(library=None):
    if library is None:
        EAGLE_ITEM_INDEX_CACHE.clear()
        return

    library = resolve_library(library)
    cache_key = os.path.abspath(LIBRARIES[library])
    EAGLE_ITEM_INDEX_CACHE.pop(cache_key, None)
    try:
        os.remove(eagle_index_cache_path(library))
    except OSError:
        pass


def list_eagle_items(library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    return eagle_item_index(library)["ordered_ids"]


def list_eagle_item_ids_fast(library=DEFAULT_LIBRARY, deleted=None):
    library = resolve_library(library)
    index = eagle_item_index(library)
    if deleted == "only":
        return index.get("deleted_ids", [])
    if deleted == "exclude":
        return index.get("active_ids", [])
    return index.get("ordered_ids", [])


def eagle_items_snapshot(library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    library_path = LIBRARIES[library]
    images_dir = eagle_images_dir(library)
    metadata_path = os.path.join(library_path, "metadata.json")
    now = time.monotonic()
    signature = (
        _file_mtime(images_dir),
        _file_mtime(metadata_path),
    )

    cached = EAGLE_ITEM_SNAPSHOT_CACHE.get(library_path)
    if cached and cached["signature"] == signature and now < cached["expires_at"]:
        return cached["snapshot"]

    snapshot = build_eagle_items_snapshot(library, images_dir)
    EAGLE_ITEM_SNAPSHOT_CACHE[library_path] = {
        "signature": signature,
        "expires_at": now + EAGLE_CACHE_TTL_SECONDS,
        "snapshot": snapshot,
    }
    return snapshot


def invalidate_eagle_item_snapshot(library=DEFAULT_LIBRARY):
    library = resolve_library(library)
    EAGLE_ITEM_SNAPSHOT_CACHE.pop(LIBRARIES[library], None)
    invalidate_eagle_index(library)


def build_eagle_items_snapshot(library, images_dir):
    folder_lookup = eagle_folder_map(library)
    items = {}
    ordered_ids = []

    try:
        entries = os.listdir(images_dir)
    except OSError as exc:
        LOGGER.warning("Skipping unreadable Eagle images directory %s: %s", images_dir, exc)
        return {"items": items, "ordered_ids": ordered_ids}

    for entry in entries:
        if not entry.lower().endswith(".info"):
            continue

        item_dir = os.path.join(images_dir, entry)
        meta_path = os.path.join(item_dir, "metadata.json")
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Skipping unreadable Eagle item metadata %s: %s", meta_path, exc)
            continue

        try:
            media_name = _find_eagle_media_name(item_dir, meta)
            if not media_name:
                continue
            thumb_name = _find_eagle_thumb_name(item_dir)
        except OSError as exc:
            LOGGER.warning("Skipping unreadable Eagle item directory %s: %s", item_dir, exc)
            continue

        item_id = entry[:-5]
        folder_ids = meta.get("folders", [])
        if not isinstance(folder_ids, list):
            folder_ids = []
        folders = [folder_lookup[fid]["path"] for fid in folder_ids if fid in folder_lookup]
        sort_time = eagle_sort_time(meta)
        items[item_id] = {
            "id": item_id,
            "title": media_name or meta.get("name") or item_id,
            "media_name": media_name,
            "media_path": os.path.join(item_dir, media_name),
            "thumb_path": os.path.join(item_dir, thumb_name) if thumb_name else None,
            "folder_ids": folder_ids,
            "folders": folders,
            "meta": meta,
            "sort_time": sort_time,
        }
        ordered_ids.append(item_id)

    ordered_ids.sort(
        key=lambda item_id: (
            -items[item_id].get("sort_time", 0),
            _natural_key(item_id),
        )
    )
    return {"items": items, "ordered_ids": ordered_ids}


@lru_cache(maxsize=16)
def _list_eagle_items_cached(images_dir, _fingerprint, library):
    out = []
    try:
        entries = os.listdir(images_dir)
    except OSError as exc:
        LOGGER.warning("Skipping unreadable Eagle images directory %s: %s", images_dir, exc)
        return out

    for entry in entries:
        if not entry.lower().endswith(".info"):
            continue
        item_id = entry[:-5]
        item = get_eagle_item(item_id, library)
        if item and item["media_name"]:
            out.append(item_id)
    return sorted(out, key=_natural_key)


def eagle_images_fingerprint(images_dir):
    try:
        entries = os.listdir(images_dir)
        root_mtime = os.path.getmtime(images_dir)
    except OSError:
        return None

    metadata_state = []
    for entry in entries:
        if not entry.lower().endswith(".info"):
            continue
        meta_path = os.path.join(images_dir, entry, "metadata.json")
        try:
            metadata_state.append((entry, os.path.getmtime(meta_path), os.path.getsize(meta_path)))
        except OSError:
            metadata_state.append((entry, None, None))
    return (root_mtime, tuple(sorted(metadata_state)))


def eagle_page_token(item):
    return f"{item['id']}/{item['media_name']}"


def parse_eagle_page_token(page):
    item_id, sep, media_name = str(page).partition("/")
    if not sep:
        return None, page
    return item_id, media_name


def get_book_kind(book, library=DEFAULT_LIBRARY):
    if is_eagle_library(library):
        item = get_eagle_item(book, library)
        if item and item["media_name"]:
            return "eagle"
    if is_archive(book):
        return "archive"
    if book.lower().endswith(".pdf"):
        return "pdf"
    if is_standalone_media(book):
        return "media"
    return None


def get_book_title(book, library=DEFAULT_LIBRARY):
    if is_eagle_library(library):
        item = get_eagle_item(book, library)
        if item:
            return item["title"]
    return os.path.basename(book)


def get_book_groups(book, library=DEFAULT_LIBRARY):
    if is_eagle_library(library):
        item = get_eagle_item(book, library)
        if item and item["folders"]:
            return item["folders"]
    return [os.path.dirname(book) or "(root)"]


def get_book_dimensions(book, library=DEFAULT_LIBRARY):
    if is_eagle_library(library):
        item = get_eagle_item(book, library)
        meta = item.get("meta", {}) if item else {}
        width = meta.get("width")
        height = meta.get("height")
        if isinstance(width, (int, float)) and isinstance(height, (int, float)) and width > 0 and height > 0:
            return int(width), int(height)
    return None, None


def get_book_metadata(book, library=DEFAULT_LIBRARY):
    if not is_eagle_library(library):
        return {}

    item = get_eagle_item(book, library)
    meta = item.get("meta", {}) if item else {}
    if not isinstance(meta, dict):
        return {}

    tags = meta.get("tags")
    if not isinstance(tags, list):
        tags = []
    tags = [tag for tag in tags if isinstance(tag, str) and tag.strip()]

    source_url = meta.get("url")
    if not isinstance(source_url, str) or not source_url.strip():
        source_url = None

    annotation = meta.get("annotation")
    if not isinstance(annotation, str) or not annotation.strip():
        annotation = None

    size = meta.get("size")
    if not isinstance(size, (int, float)) or size < 0:
        size = None

    return {
        "tags": tags,
        "sourceUrl": source_url,
        "annotation": annotation,
        "fileSize": int(size) if size is not None else None,
    }


def get_eagle_pages(book, library=DEFAULT_LIBRARY):
    item = get_eagle_item(book, library)
    if not item or not item["media_name"]:
        raise FileNotFoundError(book)

    selected_folder_ids = item.get("folder_ids") or []
    selected_folder = selected_folder_ids[0] if selected_folder_ids else None
    ordered_ids = list_eagle_item_ids_fast(library, deleted="exclude")
    try:
        selected_pos = ordered_ids.index(item["id"])
    except ValueError:
        return [eagle_page_token(item)]

    half_scan = max(EAGLE_PAGE_NEIGHBOR_SCAN_LIMIT // 2, EAGLE_PAGE_WINDOW_LIMIT)
    scan_start = max(0, selected_pos - half_scan)
    scan_end = min(len(ordered_ids), selected_pos + half_scan + 1)
    sibling_items = []
    for item_id in ordered_ids[scan_start:scan_end]:
        sibling = get_eagle_item(item_id, library)
        if not sibling or not sibling["media_name"]:
            continue

        folder_ids = sibling.get("folder_ids") or []
        sibling_folder = folder_ids[0] if folder_ids else None
        if sibling_folder == selected_folder:
            sibling_items.append(sibling)

    if not sibling_items:
        sibling_items = [item]

    selected_index = next(
        (index for index, sibling in enumerate(sibling_items) if sibling["id"] == item["id"]),
        0,
    )
    ordered_items = sibling_items[selected_index:] + sibling_items[:selected_index]
    return [eagle_page_token(sibling) for sibling in ordered_items[:EAGLE_PAGE_WINDOW_LIMIT]]

def _thumb_image_from_media(raw, path_hint):
    ext = os.path.splitext(path_hint.lower())[1]
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        try:
            return Image.open(io.BytesIO(raw))
        except Exception:
            return None

    if ext == ".mp4":
        try:
            import imageio.v3 as iio

            frame = iio.imread(io.BytesIO(raw), index=0)
            return Image.fromarray(frame)
        except Exception:
            pass

        try:
            import cv2
            import tempfile

            tmp_path = None
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as f:
                f.write(raw)
                tmp_path = f.name

            cap = cv2.VideoCapture(tmp_path)
            ok, frame = cap.read()
            cap.release()
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

            if ok:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                return Image.fromarray(frame)
        except Exception:
            return None

    return None


def open_archive(path):
    lower = path.lower()
    if lower.endswith(".zip"):
        return zipfile.ZipFile(path)
    if lower.endswith(".rar"):
        if rarfile is None:
            raise RuntimeError("rarfile package is required for .rar support")
        return rarfile.RarFile(path)
    raise RuntimeError("Unsupported archive format")


def archive_namelist(arc):
    if hasattr(arc, "namelist"):
        return arc.namelist()
    if hasattr(arc, "infolist"):
        return [i.filename for i in arc.infolist()]
    return []


def resolve_archive_member_name(arc, name):
    names = archive_namelist(arc)
    if name in names:
        return name

    alt_name = name.replace("+", " ")
    if alt_name != name and alt_name in names:
        return alt_name

    return name


def read_archive_member(arc, name):
    if hasattr(arc, "read"):
        return arc.read(resolve_archive_member_name(arc, name))
    raise RuntimeError("Archive object does not support read()")


def thumb_path(book, library=DEFAULT_LIBRARY):
    key_name = cache_book_key(book, library)
    return os.path.join(THUMB_DIR, hashlib.md5(key_name.encode()).hexdigest() + ".jpg")


def page_cache_path(book, page, library=DEFAULT_LIBRARY):
    key_name = cache_book_key(book, library)
    h = hashlib.md5((key_name + "::" + page).encode()).hexdigest()
    return os.path.join(PAGE_CACHE_DIR, h + ".jpg")


def video_cache_path(book, page, library=DEFAULT_LIBRARY):
    key_name = cache_book_key(book, library)
    h = hashlib.md5((key_name + "::video-audio-v2::" + page).encode()).hexdigest()
    return os.path.join(VIDEO_CACHE_DIR, h + ".mp4")


def pdf_page_cache_path(book, page, library=DEFAULT_LIBRARY):
    key_name = cache_book_key(book, library)
    h = hashlib.md5((key_name + "::pdf::" + str(page)).encode()).hexdigest()
    return os.path.join(PDF_PAGE_CACHE_DIR, h + ".jpg")


def _transcode_video_to_mp4(input_path, output_path):
    import imageio_ffmpeg

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    vf = "scale='if(gt(iw,ih),min(960,iw),-2)':'if(gt(iw,ih),-2,min(960,ih))'"
    cmd = [
        ffmpeg_exe,
        "-y",
        "-i",
        input_path,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "30",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        output_path,
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"ffmpeg failed: {input_path}")

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError(f"Empty video cache: {output_path}")


def ensure_video_cache_from_path(source_path, cache_path):
    if os.path.exists(cache_path):
        return cache_path

    tmp_path = cache_path + ".tmp.mp4"
    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except Exception:
            pass
    _transcode_video_to_mp4(source_path, tmp_path)
    os.replace(tmp_path, cache_path)
    return cache_path


def ensure_video_cache_from_bytes(raw, suffix, cache_path):
    if os.path.exists(cache_path):
        return cache_path

    input_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
            f.write(raw)
            input_path = f.name
        return ensure_video_cache_from_path(input_path, cache_path)
    finally:
        if input_path and os.path.exists(input_path):
            try:
                os.remove(input_path)
            except Exception:
                pass


def ensure_image_cache_from_path(source_path, cache_path, max_size=(3000, 3000)):
    if os.path.exists(cache_path):
        return cache_path

    img = Image.open(source_path).convert("RGB")
    img.thumbnail(max_size, Image.LANCZOS)
    img.save(cache_path, "JPEG", quality=85, optimize=True, progressive=True)
    return cache_path


# ======================
# ZIP / PDF ページ一覧
# ======================
def get_zip_pages(book, library=DEFAULT_LIBRARY):
    key_name = cache_book_key(book, library)
    path = fullpath(book, library)
    mtime = os.path.getmtime(path)

    if key_name in CACHE_META and CACHE_META[key_name]["mtime"] != mtime:
        invalidate_cache(book, library)

    if key_name in PAGE_LIST_CACHE:
        return PAGE_LIST_CACHE[key_name]

    with open_archive(path) as arc:
        pages = sorted(
            [p for p in archive_namelist(arc) if is_valid_media(p)],
            key=_natural_key,
        )

    PAGE_LIST_CACHE[key_name] = pages
    CACHE_META[key_name] = {"mtime": mtime}
    save_meta()
    return pages


def get_pdf_pages(book, library=DEFAULT_LIBRARY):
    doc = fitz.open(fullpath(book, library))
    pages = list(range(len(doc)))
    doc.close()
    return pages


# ======================
# サムネ生成
# ======================
def make_zip_thumb(book, library=DEFAULT_LIBRARY):
    with open_archive(fullpath(book, library)) as arc:
        for p in archive_namelist(arc):
            if is_valid_media(p):
                img = _thumb_image_from_media(read_archive_member(arc, p), p)
                if img is None:
                    continue
                img.thumbnail((300, 300))
                img.convert("RGB").save(thumb_path(book, library), "JPEG", quality=85)
                return


def make_pdf_thumb(book, library=DEFAULT_LIBRARY):
    doc = fitz.open(fullpath(book, library))
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2))
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    img.thumbnail((300, 300))
    img.save(thumb_path(book, library), "JPEG", quality=85)
    doc.close()
