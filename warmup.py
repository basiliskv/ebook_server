import argparse
import os, io, zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image

from backend import (
    LIBRARIES,
    LIBRARY_PROFILES,
    configure_library_profile,
    list_books,
    is_eagle_library,
    is_archive,
    is_video_ext,
    get_zip_pages,
    get_eagle_item,
    fullpath,
    open_archive,
    read_archive_member,
    thumb_path,
    page_cache_path,
    video_cache_path,
    ensure_image_cache_from_path,
    ensure_video_cache_from_path,
    ensure_video_cache_from_bytes,
    save_meta,
)

MAX_WORKERS = min(4, os.cpu_count() or 2)


def is_deleted_eagle_book(book, library):
    item = get_eagle_item(book, library)
    meta = item.get("meta") if isinstance(item, dict) else None
    return isinstance(meta, dict) and meta.get("isDeleted") is True


def convert_rar_to_zip(book, library):
    if not book.lower().endswith(".rar"):
        return book

    src_path = fullpath(book, library)
    zip_book = book[:-4] + ".zip"
    dst_path = fullpath(zip_book, library)

    if os.path.exists(dst_path):
        print("[convert] skip (zip exists):", zip_book)
        try:
            os.remove(src_path)
            print("[convert] removed duplicate rar:", book)
        except Exception:
            pass
        return zip_book

    print("[convert] rar -> zip:", f"{library}:{book}")
    try:
        with open_archive(src_path) as arc, zipfile.ZipFile(
            dst_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as zf:
            for name in arc.namelist():
                if not name or name.endswith("/"):
                    continue
                zf.writestr(name, read_archive_member(arc, name))
        os.remove(src_path)
        print("[convert] done:", f"{library}:{zip_book}")
        return zip_book
    except Exception as e:
        print("[convert] error:", f"{library}:{book}", e)
        if os.path.exists(dst_path):
            try:
                os.remove(dst_path)
            except Exception:
                pass
        return book

def warmup_book(book, library):
    print("[warmup]", f"{library}:{book}")

    if is_eagle_library(library):
        item = get_eagle_item(book, library)
        meta = item.get("meta") if isinstance(item, dict) else None
        if isinstance(meta, dict) and meta.get("isDeleted") is True:
            return
        if not item or not item["media_path"] or not item["media_name"]:
            return
        try:
            if is_video_ext(item["media_name"]):
                cache = video_cache_path(item["id"], item["media_name"], library)
                if os.path.exists(cache):
                    return
                ensure_video_cache_from_path(item["media_path"], cache)
                return

            ext = os.path.splitext(item["media_name"].lower())[1]
            if ext == ".gif":
                return

            if ext in (".webp", ".png"):
                img = Image.open(item["media_path"])
                if getattr(img, "is_animated", False):
                    return

            cache = page_cache_path(item["id"], item["media_name"], library)
            if os.path.exists(cache):
                return
            ensure_image_cache_from_path(item["media_path"], cache)
        except Exception as e:
            print(" eagle cache error:", item["media_name"], e)
        return

    pages = get_zip_pages(book, library)

    # 表紙
    try:
        if not os.path.exists(thumb_path(book, library)):
            with open_archive(fullpath(book, library)) as arc:
                for p in pages:
                    raw = read_archive_member(arc, p)
                    img = Image.open(io.BytesIO(raw))
                    img.thumbnail((300,300))
                    img.convert("RGB").save(thumb_path(book, library), "JPEG", quality=85)
                    break
    except Exception as e:
        print(" thumb error:", e)

    # ページ
    with open_archive(fullpath(book, library)) as arc:
        for p in pages:
            try:
                raw = read_archive_member(arc, p)
                if is_video_ext(p):
                    cache = video_cache_path(book, p, library)
                    if os.path.exists(cache):
                        continue
                    ensure_video_cache_from_bytes(raw, os.path.splitext(p)[1], cache)
                    continue

                cache = page_cache_path(book, p, library)
                if os.path.exists(cache):
                    continue
                img = Image.open(io.BytesIO(raw)).convert("RGB")
                img.thumbnail((3000,3000), Image.LANCZOS)
                img.save(cache, "JPEG",
                         quality=85,
                         optimize=True,
                         progressive=True)
            except Exception as e:
                print(" page error:", p, e)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=sorted(LIBRARY_PROFILES.keys()))
    parser.add_argument("--mac", action="store_true", help="Mac用のライブラリ設定で起動します")
    args = parser.parse_args()

    configure_library_profile("mac" if args.mac else args.profile)

    books = []
    for library in sorted(LIBRARIES.keys()):
        library_books = list_books(library)
        if is_eagle_library(library):
            library_books = [b for b in library_books if not is_deleted_eagle_book(b, library)]
            books.extend((library, b) for b in library_books)
            continue

        library_books = [b for b in library_books if is_archive(b)]
        library_books = [convert_rar_to_zip(b, library) for b in library_books]
        books.extend((library, b) for b in library_books)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(warmup_book, b, lib) for lib, b in books]
        for f in as_completed(futures):
            pass

    save_meta()
    print("[warmup] done")

if __name__ == "__main__":
    main()
