from flask import Flask, send_file, render_template, abort, url_for, redirect, request, jsonify
import argparse
import backend
import os, io, json, logging, sys, tempfile, threading, time
from collections import deque
from contextlib import contextmanager
from urllib.parse import unquote, unquote_plus, quote
from urllib.request import Request, urlopen

from backend import (
    query_plus_as_space,
    list_books,
    resolve_library,
    LIBRARIES,
    LIBRARY_PROFILES,
    configure_library_profile,
    get_active_profile,
    is_eagle_library,
    get_next_book_suggestion,
    has_video,
    get_book_kind,
    get_book_title,
    get_book_groups,
    get_book_dimensions,
    get_book_metadata,
    get_eagle_item,
    get_eagle_pages,
    eagle_page_token,
    eagle_folder_map,
    eagle_folder_summaries,
    parse_eagle_page_token,
    thumb_path,
    make_zip_thumb,
    make_pdf_thumb,
    get_zip_pages,
    get_pdf_pages,
    is_video_ext,
    fullpath,
    page_cache_path,
    video_cache_path,
    pdf_page_cache_path,
    media_mimetype,
    ensure_image_cache_from_path,
    ensure_video_cache_from_path,
    ensure_video_cache_from_bytes,
    is_archive,
    is_upload_file,
    invalidate_eagle_item_snapshot,
    invalidate_eagle_item_cache,
    list_eagle_item_ids_fast,
    library_path_for_write,
    library_base_dir,
    safe_library_relpath,
    open_archive,
    read_archive_member,
)

app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)
EAGLE_API_BASE = os.environ.get("EAGLE_API_BASE", "http://localhost:41595")
EAGLE_RESTORE_DELAY_SECONDS = float(os.environ.get("EAGLE_RESTORE_DELAY_SECONDS", "12"))
EAGLE_IMPORT_TEMP_TTL_SECONDS = float(os.environ.get("EAGLE_IMPORT_TEMP_TTL_SECONDS", "600"))


class ServerLogBuffer:
    def __init__(self, limit=800):
        self._entries = deque(maxlen=limit)
        self._lock = threading.Lock()
        self._next_id = 1

    def append(self, stream, message):
        text = str(message).rstrip("\n")
        if not text:
            return
        with self._lock:
            entry = {
                "id": self._next_id,
                "time": time.time(),
                "stream": stream,
                "message": text,
            }
            self._next_id += 1
            self._entries.append(entry)

    def entries(self, since=None, limit=300):
        with self._lock:
            entries = list(self._entries)
        if since is not None:
            entries = [entry for entry in entries if entry["id"] > since]
        return entries[-limit:]


SERVER_LOGS = ServerLogBuffer()


class TeeStream:
    def __init__(self, wrapped, stream_name):
        self.wrapped = wrapped
        self.stream_name = stream_name
        self._buffer = ""

    def write(self, text):
        self.wrapped.write(text)
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            SERVER_LOGS.append(self.stream_name, line)

    def flush(self):
        if self._buffer:
            SERVER_LOGS.append(self.stream_name, self._buffer)
            self._buffer = ""
        self.wrapped.flush()

    def isatty(self):
        return self.wrapped.isatty()

    def fileno(self):
        return self.wrapped.fileno()

    def __getattr__(self, name):
        return getattr(self.wrapped, name)


class BufferLogHandler(logging.Handler):
    def emit(self, record):
        try:
            SERVER_LOGS.append(record.levelname.lower(), self.format(record))
        except Exception:
            pass


def install_log_capture():
    if not isinstance(sys.stdout, TeeStream):
        sys.stdout = TeeStream(sys.stdout, "stdout")
    if not isinstance(sys.stderr, TeeStream):
        sys.stderr = TeeStream(sys.stderr, "stderr")

    root_logger = logging.getLogger()
    if not any(isinstance(handler, BufferLogHandler) for handler in root_logger.handlers):
        handler = BufferLogHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
        root_logger.addHandler(handler)


install_log_capture()


def raw_query_param(name):
    prefix = f"{name}=".encode("utf-8")
    for chunk in request.query_string.split(b"&"):
        if chunk.startswith(prefix):
            value = chunk[len(prefix):].decode("utf-8", errors="strict")
            return unquote_plus(value) if query_plus_as_space() else unquote(value)
    return None


def book_updated_at(book, library):
    if is_eagle_library(library):
        item = get_eagle_item(book, library)
        meta = item.get("meta", {}) if item else {}
        return eagle_updated_at(meta)

    try:
        return os.path.getmtime(fullpath(book, library))
    except OSError:
        return 0


def eagle_updated_at(meta):
    for key in ("lastModified", "mtime", "modificationTime", "btime"):
        value = meta.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return value / 1000 if value > 10_000_000_000 else value
    return 0


def eagle_metadata_payload(meta):
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

    star = meta.get("star")
    if not isinstance(star, (int, float)):
        star = 0

    return {
        "tags": tags,
        "sourceUrl": source_url,
        "annotation": annotation,
        "fileSize": int(size) if size is not None else None,
        "isDeleted": bool(meta.get("isDeleted") is True),
        "star": min(max(int(star), 0), 5),
    }


def bounded_int_arg(name, default=None, minimum=0, maximum=None):
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


# ======================
# 本棚
# ======================
@app.route("/")
def index():
    library = resolve_library(request.args.get("library"))
    view = "recent" if request.args.get("view") == "recent" else "all"
    books = list_books(library)
    title_map = {b: get_book_title(b, library) for b in books}
    groups = {}
    for b in books:
        for group in get_book_groups(b, library):
            groups.setdefault(group, []).append(b)

    group_names = sorted([g for g in groups.keys() if g != "(root)"])
    if "(root)" in groups:
        group_names = ["(root)"] + group_names

    grouped = [{"name": g, "books": groups[g]} for g in group_names]
    video_map = {b: has_video(b, library) for b in books}
    return render_template(
        "index.html",
        groups=grouped,
        video_map=video_map,
        title_map=title_map,
        library=library,
        libraries=sorted(LIBRARIES.keys()),
        view=view,
    )


@app.route("/api/libraries")
def api_libraries():
    return jsonify(
        {
            "defaultLibrary": resolve_library(request.args.get("library")),
            "libraries": sorted(LIBRARIES.keys()),
            "libraryKinds": library_kinds(),
            "uploadLibraries": upload_libraries(),
            "profile": get_active_profile(),
        }
    )


@app.route("/api/logs")
def api_logs():
    try:
        limit = min(max(int(request.args.get("limit", 300)), 1), 800)
    except ValueError:
        limit = 300

    since = request.args.get("since")
    try:
        since_id = int(since) if since is not None else None
    except ValueError:
        since_id = None

    return jsonify({"logs": SERVER_LOGS.entries(since=since_id, limit=limit)})


def safe_upload_filename(filename):
    name = os.path.basename((filename or "").replace("\\", "/")).strip()
    name = "".join(ch for ch in name if ch >= " " and ch not in '<>:"|?*')
    if not name or name in (".", ".."):
        raise ValueError("Invalid filename")
    return name


def upload_libraries():
    libraries = [library for library in LIBRARIES.keys() if not is_eagle_library(library)]
    if eagle_api_available():
        libraries.extend(library for library in LIBRARIES.keys() if is_eagle_library(library))
    return sorted(libraries)


def library_kinds():
    return {
        library: "eagle" if is_eagle_library(library) else "folder"
        for library in sorted(LIBRARIES.keys())
    }


def eagle_api_request(path, method="GET", payload=None, timeout=5):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request_obj = Request(
        EAGLE_API_BASE.rstrip("/") + path,
        data=data,
        headers=headers,
        method=method,
    )
    with urlopen(request_obj, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8"))


def eagle_api_available():
    try:
        result = eagle_api_request("/api/application/info", timeout=1.5)
        return result.get("status") == "success"
    except Exception:
        return False


def normalized_library_path(path):
    return os.path.normcase(os.path.normpath(path)) if path else None


def eagle_active_library_path():
    result = eagle_api_request("/api/library/info")
    if result.get("status") != "success":
        return None

    data = result.get("data") or {}
    for key in ("libraryPath", "path", "library"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            nested = value.get("libraryPath") or value.get("path")
            if isinstance(nested, str) and nested:
                return nested
    return None


def eagle_return_library_path():
    env_path = os.environ.get("EAGLE_RETURN_LIBRARY_PATH")
    if env_path:
        return env_path

    default_library = backend.DEFAULT_LIBRARY
    if default_library in LIBRARIES and is_eagle_library(default_library):
        return LIBRARIES[default_library]

    for library in sorted(LIBRARIES.keys()):
        if is_eagle_library(library):
            return LIBRARIES[library]
    return None


def switch_eagle_library_path(path):
    result = eagle_api_request(
        "/api/library/switch",
        method="POST",
        payload={"libraryPath": path},
        timeout=10,
    )
    if result.get("status") != "success":
        raise RuntimeError("Could not switch Eagle library")


def delayed_eagle_restore(path, delay):
    def worker():
        time.sleep(delay)
        try:
            switch_eagle_library_path(path)
        except Exception as exc:
            app.logger.warning("Could not restore Eagle library %s: %s", path, exc)

    threading.Thread(target=worker, daemon=True).start()


def delayed_remove_file(path, delay):
    def worker():
        time.sleep(delay)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            app.logger.warning("Could not remove temporary import file %s: %s", path, exc)

    threading.Thread(target=worker, daemon=True).start()


def ensure_eagle_library_active(library):
    if not eagle_api_available():
        raise RuntimeError("Eagle API is not available. Please start Eagle on the server machine.")
    switch_eagle_library_path(LIBRARIES[library])


@contextmanager
def eagle_library_session(library, restore_delay=0):
    if not eagle_api_available():
        raise RuntimeError("Eagle API is not available. Please start Eagle on the server machine.")

    target_path = LIBRARIES[library]
    restore_path = None
    try:
        restore_path = eagle_active_library_path()
    except Exception as exc:
        app.logger.warning("Could not read active Eagle library before switching: %s", exc)
    if not restore_path:
        restore_path = eagle_return_library_path()

    switch_eagle_library_path(target_path)
    try:
        yield
    finally:
        if restore_path and normalized_library_path(restore_path) != normalized_library_path(target_path):
            if restore_delay > 0:
                delayed_eagle_restore(restore_path, restore_delay)
            else:
                try:
                    switch_eagle_library_path(restore_path)
                except Exception as exc:
                    app.logger.warning("Could not restore Eagle library %s: %s", restore_path, exc)


def flatten_eagle_api_folders(nodes, parents=None):
    parents = parents or []
    out = {}
    for node in nodes or []:
        folder_id = node.get("id")
        name = node.get("name") or folder_id or "(unnamed)"
        path_parts = parents + [name]
        if folder_id:
            out[folder_id] = {
                "id": folder_id,
                "name": name,
                "path": "/".join(path_parts),
            }
        out.update(flatten_eagle_api_folders(node.get("children", []), path_parts))
    return out


def eagle_api_folder_map(library):
    ensure_eagle_library_active(library)
    result = eagle_api_request("/api/folder/list")
    if result.get("status") != "success":
        raise RuntimeError("Eagle folder list failed")
    return flatten_eagle_api_folders(result.get("data", []))


def eagle_folder_id_for_path(library, directory):
    directory = safe_library_relpath(directory)
    if not directory:
        return None
    folder_lookup = eagle_api_folder_map(library) if eagle_api_available() else eagle_folder_map(library)
    for folder_id, folder in folder_lookup.items():
        if folder.get("path") == directory:
            return folder_id
    raise ValueError("Unknown Eagle folder")


def safe_directory_name(name):
    cleaned = (name or "").strip().replace("\\", "").replace("/", "")
    cleaned = "".join(ch for ch in cleaned if ch >= " " and ch not in '<>:"|?*')
    if not cleaned or cleaned in (".", ".."):
        raise ValueError("Invalid folder name")
    return cleaned


def create_eagle_folder(library, parent, name):
    parent = safe_library_relpath(parent)
    name = safe_directory_name(name)
    with eagle_library_session(library):
        parent_id = eagle_folder_id_for_path(library, parent) if parent else None
        payload = {"folderName": name}
        if parent_id:
            payload["parent"] = parent_id
        result = eagle_api_request("/api/folder/create", method="POST", payload=payload)
        if result.get("status") != "success":
            raise RuntimeError("Eagle folder create failed")
        return "/".join([part for part in (parent, name) if part])


def save_eagle_upload(storage, filename, library, directory):
    if not filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".webm", ".mov", ".m4v")):
        raise ValueError("Eagle upload supports image and video files only")

    temp_path = None
    try:
        with eagle_library_session(library, restore_delay=EAGLE_RESTORE_DELAY_SECONDS):
            folder_id = eagle_folder_id_for_path(library, directory)
            suffix = os.path.splitext(filename)[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
                storage.save(temp_file)
                temp_path = temp_file.name

            payload = {
                "path": temp_path,
                "name": os.path.splitext(filename)[0],
            }
            if folder_id:
                payload["folderId"] = folder_id
            result = eagle_api_request("/api/item/addFromPath", method="POST", payload=payload, timeout=60)
            if result.get("status") != "success":
                raise RuntimeError("Eagle import failed")
            return {
                "path": filename,
                "title": filename,
                "size": os.path.getsize(temp_path),
            }
    finally:
        if temp_path and os.path.exists(temp_path):
            delayed_remove_file(temp_path, EAGLE_IMPORT_TEMP_TTL_SECONDS)


def update_eagle_item_star(library, book, star):
    if not is_eagle_library(library):
        raise ValueError("Library is not an Eagle library")

    star = min(max(int(star), 0), 5)
    item = get_eagle_item(book, library)
    if not item:
        raise FileNotFoundError(book)

    api_error = None
    try:
        with eagle_library_session(library):
            result = eagle_api_request(
                "/api/item/update",
                method="POST",
                payload={"id": item["id"], "star": star},
                timeout=10,
            )
            if result.get("status") == "success":
                invalidate_eagle_item_cache()
                invalidate_eagle_item_snapshot(library)
                return
            api_error = RuntimeError("Eagle item update failed")
    except RuntimeError as exc:
        api_error = exc

    meta_path = os.path.join(eagle_item_dir(book, library), "metadata.json")
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        if not isinstance(meta, dict):
            raise ValueError("Invalid Eagle metadata")
        meta["star"] = star
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        if api_error:
            raise RuntimeError(f"{api_error}; metadata fallback failed: {exc}") from exc
        raise

    invalidate_eagle_item_cache()
    invalidate_eagle_item_snapshot(library)
    updated = get_eagle_item(book, library)
    if not updated:
        raise FileNotFoundError(book)
    return updated


@app.route("/api/directories")
def api_directories():
    library = resolve_library(request.args.get("library"))
    if is_eagle_library(library):
        try:
            with eagle_library_session(library):
                folder_lookup = eagle_api_folder_map(library)
        except RuntimeError as exc:
            return jsonify({"error": str(exc), "library": library, "directories": []}), 400
        directories = [""] + sorted(
            [folder["path"] for folder in folder_lookup.values() if folder.get("path")],
            key=lambda value: (value.count("/"), value.lower()),
        )
        return jsonify({"library": library, "directories": directories})

    base_dir = library_base_dir(library)
    directories = [""]
    for root, dirs, _ in os.walk(base_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        rel = os.path.relpath(root, base_dir)
        if rel != ".":
            directories.append(rel.replace("\\", "/"))

    return jsonify(
        {
            "library": library,
            "directories": sorted(directories, key=lambda value: (value.count("/"), value.lower())),
        }
    )


@app.route("/api/directories/create", methods=["POST"])
def api_create_directory():
    library = resolve_library(request.form.get("library"))
    parent = request.form.get("parent") or ""
    name = request.form.get("name") or ""

    try:
        if is_eagle_library(library):
            created = create_eagle_folder(library, parent, name)
        else:
            folder_name = safe_directory_name(name)
            parent = safe_library_relpath(parent)
            created = "/".join([part for part in (parent, folder_name) if part])
            os.makedirs(library_path_for_write(created, library), exist_ok=False)
    except FileExistsError:
        return jsonify({"error": "Folder already exists"}), 400
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        return jsonify({"error": str(exc)}), 400

    app.logger.info("Created folder %s in library %s", created or "(root)", library)
    return jsonify({"library": library, "directory": created})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    library = resolve_library(request.form.get("library"))

    try:
        directory = safe_library_relpath(request.form.get("directory") or "")
        target_dir = None if is_eagle_library(library) else library_path_for_write(directory, library)
    except ValueError:
        return jsonify({"error": "Invalid destination directory."}), 400

    files = request.files.getlist("files")
    if not files:
        file = request.files.get("file")
        files = [file] if file else []
    if not files:
        return jsonify({"error": "No files were uploaded."}), 400

    if target_dir:
        os.makedirs(target_dir, exist_ok=True)
    uploaded = []
    for storage in files:
        try:
            filename = safe_upload_filename(storage.filename)
        except ValueError:
            return jsonify({"error": "Invalid filename."}), 400
        if not is_upload_file(filename):
            return jsonify({"error": f"Unsupported file type: {filename}"}), 400

        if is_eagle_library(library):
            try:
                uploaded_item = save_eagle_upload(storage, filename, library, directory)
            except (ValueError, RuntimeError) as exc:
                return jsonify({"error": str(exc)}), 400
            invalidate_eagle_item_snapshot(library)
            uploaded.append(uploaded_item)
            app.logger.info("Uploaded %s to Eagle library %s", uploaded_item["title"], library)
            continue

        target_path = library_path_for_write(os.path.join(directory, filename), library)
        storage.save(target_path)
        rel = os.path.relpath(target_path, library_base_dir(library)).replace("\\", "/")
        if is_archive(rel) or rel.lower().endswith(".pdf"):
            invalidate_target = rel
            try:
                from backend import invalidate_cache
                invalidate_cache(invalidate_target, library)
            except Exception:
                pass
        uploaded.append(
            {
                "path": rel,
                "title": os.path.basename(rel),
                "size": os.path.getsize(target_path),
            }
        )
        app.logger.info("Uploaded %s to library %s", rel, library)

    return jsonify({"library": library, "uploaded": uploaded})


@app.route("/api/books")
def api_books():
    library = resolve_library(request.args.get("library"))
    requested_limit = bounded_int_arg("limit", default=None, minimum=1, maximum=1000)
    offset = bounded_int_arg("offset", default=0, minimum=0)
    deleted_filter = request.args.get("deleted")
    include_summaries = str(request.args.get("summaries", "1")).lower() not in ("0", "false", "no")
    if is_eagle_library(library) and requested_limit is not None:
        all_books = list_eagle_item_ids_fast(library)
    else:
        all_books = list_books(library)

    if is_eagle_library(library) and deleted_filter in ("only", "exclude"):
        def matches_deleted_filter(book):
            item = get_eagle_item(book, library)
            meta = item.get("meta") if isinstance(item, dict) else None
            is_deleted = isinstance(meta, dict) and meta.get("isDeleted") is True
            return is_deleted if deleted_filter == "only" else not is_deleted

        all_books = [book for book in all_books if matches_deleted_filter(book)]

    total = len(all_books)
    if requested_limit is None:
        books = all_books
        next_offset = None
        has_more = False
    else:
        books = all_books[offset:offset + requested_limit]
        next_offset = offset + len(books)
        has_more = next_offset < total

    def book_payload(book):
        if is_eagle_library(library):
            item = get_eagle_item(book, library)
            if not item or not item.get("media_name"):
                return None
            meta = item.get("meta", {}) if item else {}
            if not isinstance(meta, dict):
                meta = {}
            width = meta.get("width")
            height = meta.get("height")
            if not (isinstance(width, (int, float)) and width > 0):
                width = None
            if not (isinstance(height, (int, float)) and height > 0):
                height = None
            media_name = item.get("media_name") if item else None
            folders = item.get("folders", []) if item else []
            payload = {
                "path": book,
                "title": item.get("title") if item else book,
                "group": folders[0] if folders else "(root)",
                "kind": "eagle",
                "hasVideo": bool(media_name and is_video_ext(media_name)),
                "coverUrl": url_for("cover", book=book, library=library),
                "updatedAt": eagle_updated_at(meta),
                "width": int(width) if width is not None else None,
                "height": int(height) if height is not None else None,
            }
            payload.update(eagle_metadata_payload(meta if isinstance(meta, dict) else {}))
            return payload

        width, height = get_book_dimensions(book, library)
        payload = {
            "path": book,
            "title": get_book_title(book, library),
            "group": get_book_groups(book, library)[0],
            "kind": get_book_kind(book, library),
            "hasVideo": has_video(book, library),
            "coverUrl": url_for("cover", book=book, library=library),
            "updatedAt": book_updated_at(book, library),
            "width": width,
            "height": height,
        }
        payload.update(get_book_metadata(book, library))
        return payload

    book_payloads = [payload for payload in (book_payload(book) for book in books) if payload]

    return jsonify(
        {
            "library": library,
            "libraries": sorted(LIBRARIES.keys()),
            "libraryKinds": library_kinds(),
            "uploadLibraries": upload_libraries(),
            "folderSummaries": eagle_folder_summaries(library) if include_summaries and is_eagle_library(library) else None,
            "offset": offset,
            "limit": requested_limit,
            "total": total,
            "hasMore": has_more,
            "nextOffset": next_offset if has_more else None,
            "books": book_payloads,
        }
    )


@app.route("/api/eagle/item/star", methods=["POST"])
def api_eagle_item_star():
    payload = request.get_json(silent=True) or request.form
    library = resolve_library(payload.get("library") or request.args.get("library"))
    book = payload.get("book") or request.args.get("book")
    if not book:
        abort(400)

    try:
        star = min(max(int(payload.get("star", 0)), 0), 5)
        update_eagle_item_star(library, book, star)
        return jsonify(
            {
                "library": library,
                "book": book,
                "star": star,
            }
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError:
        abort(404)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503


@app.route("/api/book")
def api_book():
    book = raw_query_param("book") or request.args.get("book")
    library = resolve_library(request.args.get("library"))
    if not book:
        abort(400)

    try:
        kind = get_book_kind(book, library)
        if kind == "archive":
            pages = get_zip_pages(book, library)
        elif kind == "pdf":
            pages = [str(p) for p in get_pdf_pages(book, library)]
        elif kind == "eagle":
            if request.args.get("fast") == "1":
                item = get_eagle_item(book, library)
                if not item or not item["media_name"]:
                    abort(404)
                pages = [eagle_page_token(item)]
            else:
                pages = get_eagle_pages(book, library)
        elif kind == "media":
            pages = [book]
        else:
            abort(404)
    except (FileNotFoundError, KeyError):
        abort(404)

    next_book = None if kind == "eagle" else get_next_book_suggestion(book, library)
    return jsonify(
        {
            "book": book,
            "title": get_book_title(book, library),
            "kind": kind,
            "library": library,
            "pages": pages,
            "pageUrls": [
                url_for(
                    "pdf_page" if kind == "pdf" else ("eagle_page" if kind == "eagle" else "zip_page"),
                    book=book,
                    page=p,
                    library=library,
                )
                if kind != "media"
                else url_for("media_page", book=book, library=library)
                for p in pages
            ],
            "nextBook": next_book,
        }
    )


@app.route("/open/<path:book>")
def open_book(book):
    library = resolve_library(request.args.get("library"))
    return redirect(url_for("view_book", book=book, library=library))


# ======================
# 表紙（キャッシュ対応）
# ======================
@app.route("/cover/<path:book>")
def cover(book):
    library = resolve_library(request.args.get("library"))

    if is_eagle_library(library):
        item = get_eagle_item(book, library)
        if not item:
            abort(404)
        if item["thumb_path"] and os.path.exists(item["thumb_path"]):
            return send_file(item["thumb_path"], mimetype=media_mimetype(item["thumb_path"]))
        if item["media_path"] and os.path.exists(item["media_path"]):
            return send_file(item["media_path"], mimetype=media_mimetype(item["media_path"]))
        abort(404)

    kind = get_book_kind(book, library)
    if kind == "media":
        path = fullpath(book, library)
        if not os.path.exists(path):
            abort(404)
        if is_video_ext(path):
            abort(404)
        return send_file(path, mimetype=media_mimetype(path))

    if not os.path.exists(thumb_path(book, library)):
        try:
            make_pdf_thumb(book, library) if book.lower().endswith(".pdf") else make_zip_thumb(book, library)
        except:
            pass

    if os.path.exists(thumb_path(book, library)):
        resp = send_file(thumb_path(book, library), mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

    abort(404)


# ======================
# ビュー
# ======================
@app.route("/view/<path:book>")
def view_book(book):
    library = resolve_library(request.args.get("library"))
    next_book = get_next_book_suggestion(book, library)
    kind = get_book_kind(book, library)
    if kind == "archive":
        pages = get_zip_pages(book, library)
    elif kind == "pdf":
        pages = get_pdf_pages(book, library)
    elif kind == "eagle":
        pages = get_eagle_pages(book, library)
    elif kind == "media":
        pages = [book]
    else:
        abort(404)

    return render_template(
        "view.html",
        book=book,
        title=get_book_title(book, library),
        pages=pages,
        kind=kind,
        library=library,
        next_book=next_book,
    )


# ======================
# ページ取得
# ======================
@app.route("/media_page")
def media_page():
    book = raw_query_param("book") or request.args.get("book")
    library = resolve_library(request.args.get("library"))
    use_original = request.args.get("original") == "1"
    if not book:
        abort(400)

    path = fullpath(book, library)
    if not os.path.exists(path):
        abort(404)

    if is_video_ext(path):
        if use_original:
            return send_file(path, mimetype=media_mimetype(path), download_name=os.path.basename(path))
        cache = video_cache_path(book, os.path.basename(path), library)
        try:
            ensure_video_cache_from_path(path, cache)
        except RuntimeError:
            abort(404)
        return send_file(
            cache,
            mimetype="video/mp4",
            download_name=os.path.splitext(os.path.basename(path))[0] + ".mp4",
        )

    ext = os.path.splitext(path.lower())[1]
    if ext == ".gif":
        return send_file(path, mimetype=media_mimetype(path), download_name=os.path.basename(path))
    if ext in (".webp", ".png"):
        try:
            from PIL import Image
            img = Image.open(path)
            if getattr(img, "is_animated", False):
                return send_file(path, mimetype=media_mimetype(path), download_name=os.path.basename(path))
        except Exception:
            pass

    cache = page_cache_path(book, os.path.basename(path), library)
    try:
        ensure_image_cache_from_path(path, cache)
    except Exception:
        abort(404)
    return send_file(cache, mimetype="image/jpeg")


@app.route("/zip_page")
def zip_page():
    book = raw_query_param("book") or request.args.get("book")
    page = raw_query_param("page") or request.args.get("page")
    library = resolve_library(request.args.get("library"))
    use_original = request.args.get("original") == "1"
    if not book or not page:
        abort(400)

    page = unquote(page)
    ext = os.path.splitext(page.lower())[1]

    try:
        if is_video_ext(page):
            with open_archive(fullpath(book, library)) as arc:
                raw = read_archive_member(arc, page)
            if use_original:
                return send_file(
                    io.BytesIO(raw),
                    mimetype=media_mimetype(page),
                    download_name=os.path.basename(page),
                )
            cache = video_cache_path(book, page, library)
            ensure_video_cache_from_bytes(raw, ext, cache)
            return send_file(
                cache,
                mimetype="video/mp4",
                download_name=os.path.splitext(os.path.basename(page))[0] + ".mp4",
            )

        if ext == ".gif":
            with open_archive(fullpath(book, library)) as arc:
                raw = read_archive_member(arc, page)
            return send_file(
                io.BytesIO(raw),
                mimetype=media_mimetype(page),
                download_name=os.path.basename(page),
            )

        if ext in (".webp", ".png"):
            from PIL import Image
            with open_archive(fullpath(book, library)) as arc:
                raw = read_archive_member(arc, page)
            try:
                img = Image.open(io.BytesIO(raw))
                if getattr(img, "is_animated", False):
                    return send_file(
                        io.BytesIO(raw),
                        mimetype=media_mimetype(page),
                        download_name=os.path.basename(page),
                    )
            except Exception:
                pass

        cache = page_cache_path(book, page, library)
        if os.path.exists(cache):
            return send_file(cache, mimetype="image/jpeg")

        from PIL import Image

        with open_archive(fullpath(book, library)) as arc:
            raw = read_archive_member(arc, page)

        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((3000, 3000), Image.LANCZOS)
        img.save(cache, "JPEG", quality=85, optimize=True, progressive=True)
        return send_file(cache, mimetype="image/jpeg")
    except (FileNotFoundError, RuntimeError):
        abort(404)


@app.route("/eagle_page")
def eagle_page():
    book = raw_query_param("book") or request.args.get("book")
    page = raw_query_param("page") or request.args.get("page")
    library = resolve_library(request.args.get("library"))
    use_original = request.args.get("original") == "1"
    if not book or not page:
        abort(400)

    page_item_id, page_media_name = parse_eagle_page_token(page)
    item = get_eagle_item(page_item_id or book, library)
    if not item or not item["media_path"]:
        abort(404)
    if page_media_name != item["media_name"]:
        abort(404)

    if is_video_ext(item["media_name"]):
        if use_original:
            return send_file(
                item["media_path"],
                mimetype=media_mimetype(item["media_path"]),
                download_name=item["media_name"],
            )
        cache = video_cache_path(item["id"], item["media_name"], library)
        try:
            ensure_video_cache_from_path(item["media_path"], cache)
        except RuntimeError:
            abort(404)
        return send_file(
            cache,
            mimetype="video/mp4",
            download_name=os.path.splitext(item["media_name"])[0] + ".mp4",
        )

    ext = os.path.splitext(item["media_name"].lower())[1]
    if ext == ".gif":
        return send_file(
            item["media_path"],
            mimetype=media_mimetype(item["media_path"]),
            download_name=item["media_name"],
        )

    if ext in (".webp", ".png"):
        from PIL import Image
        try:
            img = Image.open(item["media_path"])
            if getattr(img, "is_animated", False):
                return send_file(
                    item["media_path"],
                    mimetype=media_mimetype(item["media_path"]),
                    download_name=item["media_name"],
                )
        except Exception:
            pass

    cache = page_cache_path(item["id"], item["media_name"], library)
    try:
        ensure_image_cache_from_path(item["media_path"], cache)
    except Exception:
        abort(404)
    return send_file(
        cache,
        mimetype="image/jpeg",
        download_name=os.path.splitext(item["media_name"])[0] + ".jpg",
    )


@app.route("/pdf_page")
def pdf_page():
    book = raw_query_param("book") or request.args.get("book")
    page = request.args.get("page")
    library = resolve_library(request.args.get("library"))
    if book is None or page is None:
        abort(400)

    page = int(page)
    cache = pdf_page_cache_path(book, page, library)
    if os.path.exists(cache):
        return send_file(cache, mimetype="image/jpeg")

    import fitz  # PyMuPDF
    from PIL import Image

    try:
        doc = fitz.open(fullpath(book, library))
        pix = doc[page].get_pixmap(matrix=fitz.Matrix(2, 2))
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        img.thumbnail((3000, 3000), Image.LANCZOS)
        img.save(cache, "JPEG", quality=85, optimize=True, progressive=True)
        doc.close()
        return send_file(cache, mimetype="image/jpeg")
    except FileNotFoundError:
        abort(404)


@app.template_filter("q")
def q(s):
    return quote(s, safe="")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=sorted(LIBRARY_PROFILES.keys()))
    parser.add_argument("--mac", action="store_true", help="Mac用のライブラリ設定で起動します")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    configure_library_profile("mac" if args.mac else args.profile)
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
