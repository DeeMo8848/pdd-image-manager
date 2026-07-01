"""
Image Manager 2 - FastAPI backend
重构版：重点优化缩略图缓存、流畅预览、稳定 API
"""

import os
import json
import shutil
import sqlite3
import hashlib
import mimetypes
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager
from collections import OrderedDict

from fastapi import FastAPI, Form, HTTPException, Request, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ============================================================
# 配置与常量
# ============================================================
BASE_DIR = Path(__file__).parent
SETTINGS_FILE = BASE_DIR / "settings.json"
DB_FILE = BASE_DIR / "manager.db"
THUMB_CACHE_DIR = BASE_DIR / "thumb_cache"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
TRASH_FOLDER_NAME = "__trash__"

THUMB_CACHE_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif', '.tiff', '.tif', '.heic'}
THUMB_SIZE = 240  # 缩略图尺寸（较大保证清晰，文件依然小）
THUMB_QUALITY = 72

# LRU 内存缓存：最近访问的缩略图路径（避免重复磁盘检查）
_THUMB_LRU: "OrderedDict[str, Path]" = OrderedDict()
_LRU_MAX = 512


# ============================================================
# 数据库
# ============================================================
def get_db():
    conn = sqlite3.connect(str(DB_FILE), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        color TEXT DEFAULT '#7c3aed',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS album_tags (
        album_path TEXT NOT NULL,
        tag_id INTEGER NOT NULL,
        PRIMARY KEY (album_path, tag_id),
        FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_album_tags_tag ON album_tags(tag_id);
    CREATE INDEX IF NOT EXISTS idx_album_tags_path ON album_tags(album_path);
    """)
    conn.close()


# ============================================================
# 设置
# ============================================================
def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"organize_dir": "", "albums_dir": "", "trash_dir": ""}


def save_settings(s: dict):
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


def _gdir(role: str):
    s = load_settings()
    p = s.get(role + "_dir", "").strip()
    return Path(p) if p and Path(p).exists() else None


def _organize():
    return _gdir("organize")


def _albums():
    return _gdir("albums")


def _trash():
    s = load_settings()
    p = s.get("trash_dir", "").strip()
    if p and Path(p).exists():
        return Path(p)
    org = _gdir("organize")
    if org:
        t = org / TRASH_FOLDER_NAME
        t.mkdir(exist_ok=True)
        return t
    return None


def is_image(p: Path) -> bool:
    return p.suffix.lower() in IMAGE_EXTS


def _count_dir_fast(dir_path: Path):
    """用 os.scandir 快速统计目录下的图片数和是否有子目录，避免 stat 调用"""
    fc = 0
    has = False
    try:
        for entry in os.scandir(dir_path):
            if entry.is_dir():
                if entry.name != TRASH_FOLDER_NAME:
                    has = True
            elif entry.is_file():
                # 用 os.path.splitext 比 Path.suffix 更快
                ext = os.path.splitext(entry.name)[1].lower()
                if ext in IMAGE_EXTS:
                    fc += 1
    except OSError:
        pass
    return fc, has


def safe_move(src: Path, dst_dir: Path) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dst.exists():
        stem, suffix = src.stem, src.suffix
        i = 1
        while dst.exists():
            dst = dst_dir / f"{stem}_{i}{suffix}"
            i += 1
    shutil.move(str(src), str(dst))
    return dst


# ============================================================
# 缩略图生成与缓存
# ============================================================
def _thumb_key_for(rel_path: str) -> str:
    """为相对路径生成稳定的 md5 key"""
    return hashlib.md5(rel_path.encode("utf-8")).hexdigest()


def make_thumb(src: Path, key: str) -> Path | None:
    """
    生成缩略图并缓存到磁盘。
    - 优先返回已有缓存
    - GIF 取首帧
    - 失败返回 None
    """
    cache = THUMB_CACHE_DIR / f"{key}.jpg"

    # LRU 命中
    if key in _THUMB_LRU:
        p = _THUMB_LRU.pop(key)
        if p.exists():
            _THUMB_LRU[key] = p
            return p
        else:
            del _THUMB_LRU[key]

    if cache.exists():
        _THUMB_LRU[key] = cache
        if len(_THUMB_LRU) > _LRU_MAX:
            _THUMB_LRU.popitem(last=False)
        return cache

    try:
        from PIL import Image
        img = Image.open(str(src))
        if getattr(img, "is_animated", False):
            img.seek(0)
        img = img.convert("RGB")
        img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
        img.save(str(cache), "JPEG", quality=THUMB_QUALITY, optimize=True)
    except Exception:
        return None

    _THUMB_LRU[key] = cache
    if len(_THUMB_LRU) > _LRU_MAX:
        _THUMB_LRU.popitem(last=False)
    return cache


# ============================================================
# 操作日志（撤回用）
# ============================================================
_action_log: list[dict] = []
_ACTION_LOG_MAX = 50


def log_action(action: str, src: str, dst: str):
    _action_log.append({
        "action": action,
        "src": src,
        "dst": dst,
        "ts": datetime.now().isoformat()
    })
    if len(_action_log) > _ACTION_LOG_MAX:
        _action_log.pop(0)


# ============================================================
# FastAPI
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="Image Manager 2", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ============================================================
# 设置 API
# ============================================================
@app.get("/api/settings")
async def api_get_settings():
    s = load_settings()
    trash_dir = s.get("trash_dir", "")
    if not trash_dir:
        org = s.get("organize_dir", "")
        if org:
            trash_dir = str(Path(org) / TRASH_FOLDER_NAME)
    return {
        "organize_dir": s.get("organize_dir", ""),
        "albums_dir": s.get("albums_dir", ""),
        "trash_dir": s.get("trash_dir", ""),
        "trash_effective": trash_dir
    }


@app.post("/api/settings/pick_folder")
async def api_pick_folder(role: str = Form(...), path: str = Form(...)):
    p = Path(path)
    if not p.exists() or not p.is_dir():
        raise HTTPException(400, f"目录不存在: {path}")
    s = load_settings()
    if role in ("organize", "albums", "trash"):
        s[role + "_dir"] = str(p.resolve())
        save_settings(s)
    return {"ok": True, "settings": s}


@app.post("/api/open_folder")
async def api_open_folder(request: Request):
    import subprocess
    data = await request.json()
    path = data.get("path", "").strip()
    if not path:
        raise HTTPException(400, "path required")
    p = Path(path)
    if not p.exists():
        raise HTTPException(400, f"目录不存在: {path}")
    subprocess.Popen(["explorer", str(p.resolve())])
    return {"ok": True}


# ============================================================
# 文件系统浏览
# ============================================================
@app.get("/api/fs/list")
async def fs_list(path: str = "C:\\"):
    p = Path(path)
    if not p.exists():
        return {"error": f"路径不存在: {path}", "parent": None, "dirs": []}
    if not p.is_dir():
        return {"error": "不是目录", "parent": None, "dirs": []}
    dirs = []
    try:
        for e in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if e.is_dir():
                try:
                    dirs.append({"name": e.name, "path": str(e)})
                except Exception:
                    pass
    except PermissionError:
        return {"error": "权限不足", "parent": str(p.parent), "dirs": []}
    return {"parent": str(p.parent), "dirs": dirs, "current": str(p)}


# ============================================================
# 标签 API
# ============================================================
@app.get("/api/tags")
async def api_list_tags():
    conn = get_db()
    rows = conn.execute("SELECT id,name,color,created_at FROM tags ORDER BY id").fetchall()
    conn.close()
    return {"tags": [dict(r) for r in rows]}


@app.post("/api/tags")
async def api_create_tag(request: Request):
    data = await request.json()
    name = data.get("name", "").strip()
    color = data.get("color", "#7c3aed")
    if not name:
        raise HTTPException(400, "name required")
    conn = get_db()
    try:
        conn.execute("INSERT INTO tags (name,color) VALUES (?,?)", (name, color))
        conn.close()
        return {"ok": True}
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, "标签已存在")


@app.put("/api/tags/{tag_id}")
async def api_update_tag(tag_id: int, request: Request):
    data = await request.json()
    name = data.get("name", "").strip()
    color = data.get("color")
    conn = get_db()
    if name:
        conn.execute("UPDATE tags SET name=? WHERE id=?", (name, tag_id))
    if color:
        conn.execute("UPDATE tags SET color=? WHERE id=?", (color, tag_id))
    conn.close()
    return {"ok": True}


@app.delete("/api/tags/{tag_id}")
async def api_delete_tag(tag_id: int):
    conn = get_db()
    conn.execute("DELETE FROM tags WHERE id=?", (tag_id,))
    conn.execute("DELETE FROM album_tags WHERE tag_id=?", (tag_id,))
    conn.close()
    return {"ok": True}


# ============================================================
# 相册 API
# ============================================================
@app.get("/api/albums")
async def api_list_albums(tag_id: int = None, sort: str = "name"):
    ad = _albums()
    if not ad:
        return {"albums": [], "error": "albums_dir not set"}
    conn = get_db()
    tagged = None
    if tag_id is not None:
        rows = conn.execute(
            "SELECT album_path FROM album_tags WHERE tag_id=?", (tag_id,)
        ).fetchall()
        tagged = {r["album_path"] for r in rows}

    albums = []
    try:
        for entry in ad.iterdir():
            if not entry.is_dir() or entry.name == TRASH_FOLDER_NAME:
                continue
            rel = str(entry.relative_to(ad)).replace("\\", "/")
            if tagged is not None and rel not in tagged:
                continue
            items = list(entry.iterdir())
            covers = [f for f in items if f.is_file() and is_image(f)]
            has_children = any(f.is_dir() and f.name != TRASH_FOLDER_NAME for f in items)
            cover = ""
            is_gif = False
            if covers:
                first = covers[0]
                is_gif = first.suffix.lower() == '.gif'
                cover_rel = f"{rel}/{first.name}"
                cover = f"/api/thumb?key={_thumb_key_for(cover_rel)}&src={cover_rel}"
            # 获取目录修改时间
            try:
                mtime = entry.stat().st_mtime
            except Exception:
                mtime = 0
            albums.append({
                "name": entry.name,
                "path": rel,
                "cover": cover,
                "file_count": len(covers),
                "has_children": has_children,
                "is_gif": is_gif,
                "mtime": mtime,
                "mtime_iso": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else ""
            })
    except Exception as e:
        conn.close()
        return {"albums": [], "error": str(e)}

    # 排序
    if sort == "time":
        albums.sort(key=lambda a: a["mtime"], reverse=True)
    elif sort == "count":
        albums.sort(key=lambda a: a["file_count"], reverse=True)
    else:
        albums.sort(key=lambda a: a["name"].lower())

    # 批量获取 tags
    if albums:
        paths = tuple(a["path"] for a in albums)
        ph = ",".join("?" * len(paths))
        tr = conn.execute(
            f"SELECT at.album_path,t.id,t.name,t.color FROM album_tags at "
            f"JOIN tags t ON at.tag_id=t.id WHERE at.album_path IN ({ph})",
            paths
        ).fetchall()
        tm = {}
        for r in tr:
            tm.setdefault(r["album_path"], []).append({
                "id": r["id"], "name": r["name"], "color": r["color"]
            })
        for a in albums:
            a["tags"] = tm.get(a["path"], [])
    conn.close()
    return {"albums": albums}


@app.get("/api/albums/tree")
async def api_album_tree():
    """返回顶层相册目录树结构（延迟加载）"""
    ad = _albums()
    if not ad:
        return {"tree": [], "error": "albums_dir not set"}
    tree = []
    try:
        for e in sorted(ad.iterdir(), key=lambda x: x.name.lower()):
            if not e.is_dir() or e.name == TRASH_FOLDER_NAME:
                continue
            fc, has = _count_dir_fast(e)
            tree.append({
                "name": e.name,
                "path": e.name,
                "file_count": fc,
                "has_children": has,
                "children": []
            })
    except Exception:
        pass
    return {"tree": tree}


@app.get("/api/albums/tree_expand")
async def api_expand_tree_node(path: str = ""):
    """展开指定节点的子目录"""
    ad = _albums()
    if not ad:
        return {"children": [], "error": "albums_dir not set"}
    target = ad / path if path else ad
    if not target.exists() or not target.is_dir():
        return {"children": []}
    children = []
    try:
        for e in sorted(target.iterdir(), key=lambda x: x.name.lower()):
            if not e.is_dir() or e.name == TRASH_FOLDER_NAME:
                continue
            cr = f"{path}/{e.name}" if path else e.name
            fc, has = _count_dir_fast(e)
            children.append({
                "name": e.name,
                "path": cr,
                "file_count": fc,
                "has_children": has,
                "children": []
            })
    except Exception:
        pass
    return {"children": children}


@app.get("/api/albums/all_paths")
async def api_all_album_paths():
    """递归获取所有相册路径（用于移动选择器）"""
    ad = _albums()
    if not ad:
        return {"paths": []}
    paths = []

    def walk(d: Path, prefix: str):
        try:
            for e in sorted(d.iterdir(), key=lambda x: x.name.lower()):
                if e.is_dir() and e.name != TRASH_FOLDER_NAME:
                    rel = f"{prefix}/{e.name}" if prefix else e.name
                    paths.append(rel)
                    walk(e, rel)
        except Exception:
            pass

    walk(ad, "")
    return {"paths": paths}


@app.get("/api/albums/tags")
async def api_get_album_tags(path: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT t.id,t.name,t.color FROM album_tags at "
        "JOIN tags t ON at.tag_id=t.id WHERE at.album_path=?",
        (path,)
    ).fetchall()
    conn.close()
    return {"tags": [dict(r) for r in rows]}


@app.post("/api/albums/tags")
async def api_set_album_tags(request: Request):
    data = await request.json()
    path = data.get("path", "").strip()
    tag_ids = data.get("tag_ids", [])
    if not path:
        raise HTTPException(400, "path required")
    conn = get_db()
    conn.execute("DELETE FROM album_tags WHERE album_path=?", (path,))
    for tid in tag_ids:
        conn.execute(
            "INSERT OR IGNORE INTO album_tags (album_path,tag_id) VALUES (?,?)",
            (path, tid)
        )
    conn.close()
    return {"ok": True}


@app.post("/api/albums/create")
async def api_create_album(request: Request):
    data = await request.json()
    name = data.get("name", "").strip()
    parent = data.get("parent", "").strip()
    if not name:
        raise HTTPException(400, "name required")
    ad = _albums()
    if not ad:
        raise HTTPException(400, "albums_dir not set")
    np = (ad / parent / name) if parent else (ad / name)
    if np.exists():
        raise HTTPException(400, "相册已存在")
    np.mkdir(parents=True)
    return {"ok": True, "path": str(np.relative_to(ad)).replace("\\", "/")}


@app.post("/api/albums/delete")
async def api_delete_album(request: Request):
    data = await request.json()
    album_path = data.get("album_path", "").strip()
    if not album_path:
        raise HTTPException(400, "album_path required")
    ad = _albums()
    if not ad:
        raise HTTPException(400, "albums_dir not set")
    target = ad / album_path
    if not target.exists():
        raise HTTPException(404, "Album not found")
    trash = _trash()
    if trash:
        td = trash / target.name
        i = 1
        while td.exists():
            td = trash / f"{target.name}_{i}"
            i += 1
        shutil.move(str(target), str(td))
    else:
        shutil.rmtree(str(target))
    conn = get_db()
    conn.execute("DELETE FROM album_tags WHERE album_path=?", (album_path,))
    conn.close()
    return {"ok": True}


@app.get("/api/albums/images")
async def api_list_album_images(path: str = "", limit: int = 1000, offset: int = 0):
    ad = _albums()
    if not ad:
        return {"images": [], "error": "albums_dir not set", "children": [], "total": 0}
    target = (ad / path) if path else ad
    if not target.exists() or not target.is_dir():
        raise HTTPException(404, "Album not found")

    children = []
    images = []
    try:
        for c in sorted(target.iterdir(), key=lambda x: x.name.lower()):
            if c.is_dir() and c.name != TRASH_FOLDER_NAME:
                cr = str(c.relative_to(ad)).replace("\\", "/")
                items = list(c.iterdir())
                cnt = len([x for x in items if x.is_file() and is_image(x)])
                cov = ""
                is_gif = False
                cs = [x for x in items if x.is_file() and is_image(x)]
                if cs:
                    first = cs[0]
                    is_gif = first.suffix.lower() == '.gif'
                    cov_rel = f"{cr}/{first.name}"
                    cov = f"/api/thumb?key={_thumb_key_for(cov_rel)}&src={cov_rel}"
                children.append({
                    "name": c.name,
                    "path": cr,
                    "count": cnt,
                    "cover": cov,
                    "is_gif": is_gif
                })
    except Exception:
        pass

    try:
        for f in sorted(target.iterdir(), key=lambda x: x.name.lower()):
            if f.is_file() and is_image(f):
                r = str(f.relative_to(ad)).replace("\\", "/")
                is_gif = f.suffix.lower() == '.gif'
                thk = _thumb_key_for(r)
                images.append({
                    "name": f.name,
                    "path": r,
                    "url": f"/api/raw?path={r}",
                    "thumb": f"/api/thumb?key={thk}&src={r}",
                    "is_gif": is_gif
                })
    except Exception:
        pass

    total = len(images)
    return {
        "images": images[offset:offset + limit],
        "total": total,
        "children": children
    }


@app.post("/api/albums/delete_images")
async def api_delete_images(request: Request):
    data = await request.json()
    ap = data.get("album_path", "").strip()
    names = data.get("image_names", [])
    if not ap or not names:
        raise HTTPException(400, "album_path + image_names required")
    ad = _albums()
    if not ad:
        raise HTTPException(400, "albums_dir not set")
    album_dir = ad / ap
    if not album_dir.exists():
        raise HTTPException(404, "Album not found")
    trash = _trash()
    deleted = 0
    for n in names:
        t = album_dir / n
        if t.exists() and t.is_file():
            try:
                if trash:
                    dst = safe_move(t, trash)
                    log_action("delete", str(t), str(dst))
                else:
                    os.remove(str(t))
                deleted += 1
            except Exception:
                pass
    return {"ok": True, "deleted": deleted}


@app.post("/api/albums/single_action")
async def api_single_image_action(request: Request):
    """单图片操作：删除/移动到指定相册，供大图查看器使用"""
    data = await request.json()
    action = data.get("action", "").strip()  # "delete" | "move"
    image_path = data.get("image_path", "").strip()
    target_album = data.get("target_album", "").strip()
    if not action or not image_path:
        raise HTTPException(400, "action + image_path required")
    if action == "move" and not target_album:
        raise HTTPException(400, "target_album required for move")
    ad = _albums()
    if not ad:
        raise HTTPException(400, "albums_dir not set")
    src = ad / image_path
    if not src.exists() or not src.is_file():
        raise HTTPException(404, "Image not found")
    if action == "delete":
        trash = _trash()
        if trash:
            dst = safe_move(src, trash)
            log_action("delete", str(src), str(dst))
        else:
            os.remove(str(src))
        return {"ok": True, "action": "delete"}
    elif action == "move":
        dst_dir = ad / target_album
        if not dst_dir.exists():
            dst_dir.mkdir(parents=True, exist_ok=True)
        dst = safe_move(src, dst_dir)
        log_action("move", str(src), str(dst))
        return {"ok": True, "action": "move", "new_path": str(dst.relative_to(ad)).replace("\\", "/")}
    raise HTTPException(400, f"Unknown action: {action}")


@app.post("/api/albums/move_images")
async def api_move_images(request: Request):
    data = await request.json()
    ap = data.get("album_path", "").strip()
    names = data.get("image_names", [])
    target = data.get("target_album", "").strip()
    if not ap or not names or not target:
        raise HTTPException(400, "required")
    ad = _albums()
    if not ad:
        raise HTTPException(400, "albums_dir not set")
    src_dir = ad / ap
    dst_dir = ad / target
    if not src_dir.exists():
        raise HTTPException(404, "Source album not found")
    if not dst_dir.exists():
        dst_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for n in names:
        s = src_dir / n
        if s.exists() and s.is_file():
            try:
                dst = safe_move(s, dst_dir)
                log_action("move", str(s), str(dst))
                moved += 1
            except Exception:
                pass
    return {"ok": True, "moved": moved}


# ============================================================
# 图库整理 API
# ============================================================
@app.get("/api/organize/pending")
async def api_list_pending():
    org = _organize()
    if not org:
        return {"images": [], "error": "organize_dir not set", "count": 0}
    imgs = []
    try:
        for f in sorted(org.iterdir(), key=lambda x: x.name.lower()):
            if f.is_file() and is_image(f):
                thk = _thumb_key_for(f"org/{f.name}")
                imgs.append({
                    "name": f.name,
                    "url": f"/api/raw_organize?name={f.name}",
                    "thumb": f"/api/thumb?key={thk}&src_org={f.name}",
                    "is_gif": f.suffix.lower() == '.gif'
                })
    except Exception as e:
        return {"images": [], "error": str(e), "count": 0}
    return {"images": imgs, "count": len(imgs)}


@app.post("/api/organize/move")
async def api_move_to_album(request: Request):
    data = await request.json()
    iname = data.get("image_name", "").strip()
    apath = data.get("album_path", "").strip()
    if not iname or not apath:
        raise HTTPException(400, "required")
    org = _organize()
    ad = _albums()
    if not org or not ad:
        raise HTTPException(400, "dirs not set")
    src = org / iname
    if not src.exists():
        raise HTTPException(404, "Not found")
    dst_dir = ad / apath
    if not dst_dir.exists():
        dst_dir.mkdir(parents=True, exist_ok=True)
    try:
        dst = safe_move(src, dst_dir)
        log_action("move", str(src), str(dst))
        return {"ok": True, "new_path": str(dst.relative_to(ad)).replace("\\", "/")}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/organize/trash")
async def api_move_to_trash(request: Request):
    data = await request.json()
    iname = data.get("image_name", "").strip()
    if not iname:
        raise HTTPException(400, "required")
    org = _organize()
    trash = _trash()
    if not org or not trash:
        raise HTTPException(400, "dirs not set")
    src = org / iname
    if not src.exists():
        raise HTTPException(404, "Not found")
    try:
        dst = safe_move(src, trash)
        log_action("trash", str(src), str(dst))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/organize/undo")
async def api_undo_last():
    if not _action_log:
        return {"ok": False, "msg": "无可撤销操作"}
    last = _action_log.pop()
    src = Path(last["src"])
    dst = Path(last["dst"])
    if not dst.exists():
        return {"ok": False, "msg": "文件已不存在"}
    try:
        if not src.parent.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
        # 如果源位置已有同名文件（不太可能但保险），重命名
        final = src
        if src.exists():
            stem, suffix = src.stem, src.suffix
            i = 1
            while src.exists():
                src = src.parent / f"{stem}_{i}{suffix}"
                i += 1
        shutil.move(str(dst), str(src))
        return {"ok": True, "undone": last["action"], "restored_to": str(src)}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


@app.get("/api/organize/undo_count")
async def api_undo_count():
    return {"count": len(_action_log)}


# ============================================================
# 垃圾桶 API
# ============================================================
@app.get("/api/trash")
async def api_list_trash():
    trash = _trash()
    if not trash:
        return {"images": [], "folders": [], "error": "no trash dir", "count": 0}
    images = []
    folders = []
    try:
        for entry in sorted(trash.iterdir(), key=lambda x: x.name.lower()):
            if entry.is_file() and is_image(entry):
                thk = _thumb_key_for(f"trash_img/{entry.name}")
                images.append({
                    "name": entry.name,
                    "url": f"/api/raw_trash?name={entry.name}",
                    "thumb": f"/api/thumb?key={thk}&src_trash={entry.name}",
                    "is_gif": entry.suffix.lower() == '.gif',
                    "is_folder": False
                })
            elif entry.is_dir():
                fc = sum(1 for _ in entry.rglob('*') if _.is_file() and is_image(_))
                folders.append({
                    "name": entry.name,
                    "path": entry.name,
                    "file_count": fc,
                    "is_folder": True
                })
    except Exception:
        pass
    return {"images": images, "folders": folders, "count": len(images) + len(folders), "trash_path": str(trash)}


@app.post("/api/trash/restore")
async def api_restore_from_trash(request: Request):
    data = await request.json()
    iname = data.get("image_name", "").strip()
    name = data.get("name", iname).strip()
    is_folder = data.get("is_folder", False)
    if not name:
        raise HTTPException(400, "required")
    trash = _trash()
    org = _organize()
    if not trash or not org:
        raise HTTPException(400, "dirs not set")
    src = trash / name
    if not src.exists():
        raise HTTPException(404, "Not found")
    try:
        if is_folder and src.is_dir():
            dst_dir = org / name
            i = 1
            while dst_dir.exists():
                dst_dir = org / f"{name}_{i}"
                i += 1
            shutil.move(str(src), str(dst_dir))
        else:
            dst = safe_move(src, org)
            log_action("restore", str(src), str(dst))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/trash/delete")
async def api_delete_permanently(request: Request):
    data = await request.json()
    iname = data.get("image_name", "").strip()
    name = data.get("name", iname).strip()
    is_folder = data.get("is_folder", False)
    if not name:
        raise HTTPException(400, "required")
    trash = _trash()
    if not trash:
        raise HTTPException(400, "no trash dir")
    src = trash / name
    if not src.exists():
        raise HTTPException(404, "Not found")
    try:
        if is_folder and src.is_dir():
            shutil.rmtree(str(src))
        else:
            os.remove(str(src))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/trash/empty")
async def api_empty_trash():
    trash = _trash()
    if not trash:
        raise HTTPException(400, "no trash dir")
    count = 0
    for f in trash.iterdir():
        try:
            if f.is_dir():
                shutil.rmtree(str(f))
                count += 1
            else:
                f.unlink()
                count += 1
        except Exception:
            pass
    return {"ok": True, "deleted": count}


# ============================================================
# 缩略图缓存 API
# ============================================================
@app.get("/api/thumb")
async def api_thumb(key: str, src: str = "", src_org: str = "", src_trash: str = "", path: str = ""):
    """获取缩略图，自动生成并缓存"""
    cache = THUMB_CACHE_DIR / f"{key}.jpg"

    # 快速路径：缓存命中
    if cache.exists():
        return FileResponse(str(cache), media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})

    # 确定源文件
    source = None
    if src_org:
        org = _organize()
        if org:
            source = org / src_org
    elif src_trash:
        trash = _trash()
        if trash:
            source = trash / src_trash
    elif src:
        ad = _albums()
        if ad:
            source = ad / src
    elif path:
        ad = _albums()
        if ad:
            source = ad / path

    if source and source.is_file():
        p = make_thumb(source, key)
        if p:
            return FileResponse(str(p), media_type="image/jpeg",
                                headers={"Cache-Control": "public, max-age=86400"})
    raise HTTPException(404)


@app.get("/api/thumb/stats")
async def api_thumb_stats():
    total_size = 0
    count = 0
    try:
        for f in THUMB_CACHE_DIR.iterdir():
            if f.is_file():
                try:
                    total_size += f.stat().st_size
                    count += 1
                except Exception:
                    pass
    except Exception:
        pass

    def fmt(n):
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        if n < 1024 * 1024 * 1024:
            return f"{n / 1024 / 1024:.1f} MB"
        return f"{n / 1024 / 1024 / 1024:.2f} GB"

    return {
        "count": count,
        "size_bytes": total_size,
        "size_formatted": fmt(total_size),
        "cache_dir": str(THUMB_CACHE_DIR)
    }


@app.post("/api/thumb/clear")
async def api_clear_thumb_cache():
    deleted = 0
    size_freed = 0
    _THUMB_LRU.clear()
    for f in THUMB_CACHE_DIR.iterdir():
        if f.is_file():
            try:
                size_freed += f.stat().st_size
                f.unlink()
                deleted += 1
            except Exception:
                pass
    return {"ok": True, "deleted": deleted, "size_freed": size_freed}


# ============================================================
# 原图访问
# ============================================================
@app.get("/api/raw")
async def api_raw(path: str):
    ad = _albums()
    if not ad:
        raise HTTPException(400, "albums_dir not set")
    target = (ad / path).resolve()
    ad_resolved = ad.resolve()
    if not str(target).startswith(str(ad_resolved)):
        raise HTTPException(403, "Forbidden")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "Not found")
    mime, _ = mimetypes.guess_type(str(target))
    return FileResponse(str(target), media_type=mime or "application/octet-stream",
                        headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/raw_organize")
async def api_raw_organize(name: str):
    org = _organize()
    if not org:
        raise HTTPException(400, "organize_dir not set")
    target = (org / name).resolve()
    org_resolved = org.resolve()
    if not str(target).startswith(str(org_resolved)):
        raise HTTPException(403, "Forbidden")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "Not found")
    mime, _ = mimetypes.guess_type(str(target))
    return FileResponse(str(target), media_type=mime or "application/octet-stream",
                        headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/raw_trash")
async def api_raw_trash(name: str):
    trash = _trash()
    if not trash:
        raise HTTPException(400, "no trash dir")
    target = (trash / name).resolve()
    trash_resolved = trash.resolve()
    if not str(target).startswith(str(trash_resolved)):
        raise HTTPException(403, "Forbidden")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "Not found")
    mime, _ = mimetypes.guess_type(str(target))
    return FileResponse(str(target), media_type=mime or "application/octet-stream",
                        headers={"Cache-Control": "public, max-age=3600"})


# ============================================================
# 首页
# ============================================================
@app.get("/", response_class=HTMLResponse)
async def index():
    hp = TEMPLATES_DIR / "index.html"
    if not hp.exists():
        raise HTTPException(404, "index.html not found")
    return HTMLResponse(hp.read_text(encoding="utf-8"))


# ============================================================
# 启动
# ============================================================
if __name__ == "__main__":
    port = 8901
    print(f"Image Manager 2 - http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
