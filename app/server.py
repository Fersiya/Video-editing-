"""Локальный сервер: сканирование, план, применение, отмена."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import classify as C

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
CACHE_DIR = Path(tempfile.gettempdir()) / "montage_sorter_cache"
LOG_DIRNAME = ".sorter_log"

app = FastAPI(title="Montage Sorter")
CLIP = C.Clip()
AUDIO = C.AudioClap()
LOCK = threading.Lock()

STATE: dict = {
    "phase": "idle",  # idle | scanning | ready | applying | error
    "message": "",
    "found": 0,
    "done": 0,
    "root": None,
    "dest": None,
    "items": [],
    "clip": None,  # None - не использовалась, True/False - результат
    "clip_error": None,
    "audio_ai": None,
    "audio_error": None,
    "error": None,
}


class ScanReq(BaseModel):
    path: str
    content: bool = True
    dups: bool = True
    rename: bool = False  # переименовывать звуки по тегам (нужен ИИ)


class ApplyReq(BaseModel):
    exclude: list[str] = []  # папки (target_dir), которые не трогаем


# ---------------- Сканирование ----------------

def _walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
        for fn in filenames:
            if fn.startswith(".") or fn.lower() in ("thumbs.db", "desktop.ini"):
                continue
            yield Path(dirpath) / fn


def _set(**kw):
    with LOCK:
        STATE.update(kw)


def _target_dir(item: dict) -> str:
    kind = item["kind"]
    base = C.DIRS[kind]
    topic = item.get("topic")
    if kind in ("image", "footage", "sfx", "music") and topic:
        return f"{base}/{topic}"
    return base


def _safe_stem(s: str, n: int = 60) -> str:
    bad = '<>:"/\\|?*'
    s = "".join("_" if ch in bad else ch for ch in s).strip(" .")
    return s[:n] or "audio"


def _analyze_audio(items: list[dict], rename: bool) -> None:
    """Теги для SFX, настроение и BPM для музыки. Меняет items на месте."""
    targets = [it for it in items if it["kind"] in ("sfx", "music")]
    if not targets:
        return
    _set(message="Слушаю звуки…", done=0, found=len(targets))

    def load(it):
        p = Path(it["src"])
        d = C.duration_seconds(p) or 0.0
        if it["kind"] == "sfx":
            return C.decode_audio(p, C.CLAP_SR, 0.0, 10.0), None
        a = C.decode_audio(p, C.CLAP_SR, max(0.0, d * 0.4 - 5.0), 10.0)
        bpm = None
        try:
            y = C.decode_audio(p, 11025, max(0.0, d * 0.3), 25.0)
            bpm = C.estimate_bpm(y, 11025) if y is not None else None
        except Exception:
            pass
        return a, bpm

    with ThreadPoolExecutor(max_workers=min(6, (os.cpu_count() or 4))) as ex:
        loaded = list(ex.map(load, targets))
    for it, (_, bpm) in zip(targets, loaded):
        if bpm:
            it["bpm"] = bpm

    if not AUDIO.load():
        _set(audio_ai=False, audio_error=AUDIO.error)
        return
    _set(message=f"ИИ слушает звуки ({AUDIO.device})…")
    ok = [(it, a) for it, (a, _) in zip(targets, loaded) if a is not None]
    for kind, fn in (("sfx", AUDIO.sfx), ("music", AUDIO.music)):
        group = [(it, a) for it, a in ok if it["kind"] == kind]
        if not group:
            continue
        res = fn([a for _, a in group])
        for (it, _), (cat, tag, conf) in zip(group, res):
            it["topic"] = cat if tag else None
            it["tag"] = tag
            it["conf"] = round(conf, 2)
        _set(done=len(ok))
    _set(audio_ai=True)

    if rename:
        counters: dict[str, int] = defaultdict(int)
        for it in sorted(items, key=lambda x: x["rel"]):
            tag = it.get("tag")
            if it["kind"] not in ("sfx", "music") or not tag:
                continue
            ext = Path(it["name"]).suffix
            if it["kind"] == "sfx":
                counters[tag] += 1
                it["new_name"] = f"{tag}_{counters[tag]:02d}{ext}"
            else:
                bpm = f"_{it['bpm']}bpm" if it.get("bpm") else ""
                it["new_name"] = f"{tag}{bpm}_{_safe_stem(Path(it['name']).stem)}{ext}"


def _scan(req: ScanReq) -> None:
    try:
        root = Path(req.path).expanduser().resolve()
        _set(phase="scanning", message="Ищу файлы…", found=0, done=0, items=[],
             root=str(root), dest=str(root), clip=None, clip_error=None,
             audio_ai=None, audio_error=None, error=None)

        items: list[dict] = []
        for p in _walk(root):
            try:
                size = p.stat().st_size
            except OSError:
                continue
            items.append({"id": len(items), "src": str(p), "name": p.name,
                          "rel": str(p.relative_to(root)), "size": size,
                          "kind": "", "topic": None, "dup_of": None})
            if len(items) % 50 == 0:
                _set(found=len(items))
        _set(found=len(items), message="Определяю типы файлов…")

        # 1) тип
        for i, it in enumerate(items):
            it["kind"] = C.classify_kind(Path(it["src"]))
            if i % 25 == 0:
                _set(done=i)

        # 2) дубликаты (одинаковый размер → полный хэш)
        if req.dups:
            _set(message="Ищу дубликаты…", done=0)
            by_size: dict[int, list[dict]] = defaultdict(list)
            for it in items:
                if it["size"] > 0:
                    by_size[it["size"]].append(it)
            for group in by_size.values():
                if len(group) < 2:
                    continue
                seen: dict[str, dict] = {}
                for it in sorted(group, key=lambda x: x["rel"]):
                    try:
                        h = C.file_hash(Path(it["src"]))
                    except OSError:
                        continue
                    if h in seen:
                        it["dup_of"] = seen[h]["rel"]
                        it["kind"] = "dup"
                    else:
                        seen[h] = it

        # 3) миниатюры + содержимое
        need_thumbs = [it for it in items if it["kind"] in ("image", "footage", "graphics")]
        thumbs: dict[int, Path] = {}
        _set(message="Делаю миниатюры…", done=0)

        def job(it):
            out = CACHE_DIR / f"{it['id']}_{abs(hash(it['src'])) % 10**8}.jpg"
            ok = C.make_thumb(Path(it["src"]), it["kind"], out)
            return it["id"], (out if ok else None)

        with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 4))) as ex:
            for n, (iid, out) in enumerate(ex.map(job, need_thumbs), 1):
                if out:
                    thumbs[iid] = out
                    items[iid]["thumb"] = True
                if n % 10 == 0:
                    _set(done=n, found=len(need_thumbs))

        if req.content:
            targets = [it for it in items if it["kind"] in ("image", "footage") and it["id"] in thumbs]
            if targets:
                _set(message="Загружаю модель CLIP…")
                if CLIP.load():
                    _set(message=f"ИИ смотрит на кадры ({CLIP.device})…", done=0, found=len(targets))
                    B = 64
                    for i in range(0, len(targets), B):
                        chunk = targets[i:i + B]
                        topics = CLIP.classify([thumbs[t["id"]] for t in chunk])
                        for t, topic in zip(chunk, topics):
                            t["topic"] = topic
                        _set(done=min(i + B, len(targets)))
                    _set(clip=True)
                else:
                    _set(clip=False, clip_error=CLIP.error)
            _analyze_audio(items, req.rename)

        for it in items:
            it["target_dir"] = _target_dir(it)
        _set(items=items, phase="ready", message=f"Готово: {len(items)} файлов")
    except Exception as e:  # noqa: BLE001
        _set(phase="error", error=f"{e.__class__.__name__}: {e}")


# ---------------- API ----------------

@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.post("/api/pick")
def pick_folder():
    try:
        if os.name == "nt":  # без tkinter: в встроенном Python его нет
            import subprocess
            import base64
            ps = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
Add-Type -Name W -Namespace N -MemberDefinition '[DllImport("user32.dll")] public static extern void keybd_event(byte b, byte s, int f, int e); [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);'
$f = New-Object System.Windows.Forms.Form
$f.TopMost = $true; $f.ShowInTaskbar = $false; $f.Opacity = 0; $f.StartPosition = 'CenterScreen'
$f.Size = New-Object System.Drawing.Size(1, 1)
$f.Show()
[N.W]::keybd_event(0x12, 0, 0, 0); [N.W]::keybd_event(0x12, 0, 2, 0)   # Alt: разрешает вывести окно на передний план
[void][N.W]::SetForegroundWindow($f.Handle); $f.Activate()
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = 'Папка с материалом'
if ($d.ShowDialog($f) -eq 'OK') { $d.SelectedPath }
$f.Close()
"""
            enc = base64.b64encode(ps.encode("utf-16-le")).decode()
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-EncodedCommand", enc],
                               capture_output=True, creationflags=0x08000000)
            return {"path": r.stdout.decode("utf-8", errors="ignore").strip()}
        import tkinter as tk
        from tkinter import filedialog
        r = tk.Tk()
        r.withdraw()
        r.attributes("-topmost", True)
        d = filedialog.askdirectory(title="Папка с материалом")
        r.destroy()
        return {"path": d or ""}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Окно выбора папки недоступно: {e}")


# --- автозавершение, когда закрыта вкладка (включается флагом --autoexit) ---
_PING = {"last": 0.0, "bye": 0.0}


@app.post("/api/ping")
def ping():
    _PING["last"], _PING["bye"] = time.time(), 0.0
    return {"ok": True}


@app.post("/api/bye")
def bye():
    _PING["bye"] = time.time()
    return {"ok": True}


def _watchdog():
    while True:
        time.sleep(2)
        if STATE["phase"] in ("scanning", "applying"):
            continue
        now = time.time()
        if (_PING["bye"] and now - _PING["bye"] > 10) or (_PING["last"] and now - _PING["last"] > 600):
            os._exit(0)


if os.environ.get("MS_AUTOEXIT"):
    threading.Thread(target=_watchdog, daemon=True).start()


@app.post("/api/scan")
def scan(req: ScanReq):
    p = Path(req.path).expanduser()
    if not p.is_dir():
        raise HTTPException(400, "Такой папки нет")
    if STATE["phase"] in ("scanning", "applying"):
        raise HTTPException(409, "Уже работаю")
    threading.Thread(target=_scan, args=(req,), daemon=True).start()
    return {"ok": True}


def _can_undo() -> bool:
    d = STATE.get("dest")
    return bool(d) and any((Path(d) / LOG_DIRNAME).glob("*.json"))


@app.get("/api/state")
def state():
    with LOCK:
        s = {k: v for k, v in STATE.items() if k != "items"}
        s["count"] = len(STATE["items"])
    s["can_undo"] = _can_undo()
    s["ffmpeg"] = C.HAS_FFMPEG
    return s


@app.get("/api/plan")
def plan():
    with LOCK:
        items = STATE["items"]
        dest = STATE["dest"]
    cards: dict[str, dict] = {}
    out = []
    for it in items:
        td = it["target_dir"]
        cur = str(Path(it["rel"]).parent).replace("\\", "/")
        cur = "" if cur == "." else cur
        moves = cur != td or bool(it.get("new_name") and it["new_name"] != it["name"])
        c = cards.setdefault(td, {"dir": td, "kind": it["kind"], "count": 0, "moves": 0, "size": 0})
        c["count"] += 1
        c["moves"] += 1 if moves else 0
        c["size"] += it["size"]
        out.append({"id": it["id"], "name": it["name"], "rel": it["rel"], "kind": it["kind"],
                    "topic": it.get("topic"), "dir": td, "thumb": bool(it.get("thumb")),
                    "dup_of": it["dup_of"], "new_name": it.get("new_name"), "bpm": it.get("bpm")})
    return {"dest": dest, "cards": sorted(cards.values(), key=lambda c: c["dir"]), "items": out}


@app.get("/api/thumb/{item_id}")
def thumb(item_id: int):
    with LOCK:
        items = STATE["items"]
    if not 0 <= item_id < len(items):
        raise HTTPException(404)
    it = items[item_id]
    for f in CACHE_DIR.glob(f"{item_id}_*.jpg"):
        return FileResponse(f, headers={"Cache-Control": "max-age=3600"})
    raise HTTPException(404)


def _unique(dst: Path) -> Path:
    if not dst.exists():
        return dst
    stem, suf, n = dst.stem, dst.suffix, 1
    while True:
        cand = dst.with_name(f"{stem}_{n}{suf}")
        if not cand.exists():
            return cand
        n += 1


@app.post("/api/apply")
def apply(req: ApplyReq):
    with LOCK:
        if STATE["phase"] != "ready":
            raise HTTPException(409, "Сначала просканируй папку")
        items = list(STATE["items"])
        dest = Path(STATE["dest"])
        STATE["phase"] = "applying"
    moves, errors = [], []
    try:
        for d in list(C.DIRS.values()) + C.EXTRA_DIRS:
            if d.split("/")[0] in {"99_Duplicates", "06_Other"}:
                continue
            (dest / d).mkdir(parents=True, exist_ok=True)
        for it in items:
            if it["target_dir"] in req.exclude:
                continue
            src = Path(it["src"])
            dst = dest / it["target_dir"] / (it.get("new_name") or it["name"])
            if src.resolve() == dst.resolve() or not src.exists():
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst = _unique(dst)
                shutil.move(str(src), str(dst))
                moves.append({"from": str(src), "to": str(dst)})
            except Exception as e:  # noqa: BLE001
                errors.append(f"{it['name']}: {e}")
        # убрать опустевшие исходные папки (структуру проекта не трогаем)
        keep = {d.split("/")[0] for d in list(C.DIRS.values()) + C.EXTRA_DIRS}
        for d in sorted(dest.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                rel = d.relative_to(dest)
                if d.is_dir() and rel.parts[0] not in keep and LOG_DIRNAME not in rel.parts and not any(d.iterdir()):
                    d.rmdir()
            except (OSError, ValueError):
                pass
        if moves:
            logdir = dest / LOG_DIRNAME
            logdir.mkdir(exist_ok=True)
            (logdir / f"{time.strftime('%Y%m%d_%H%M%S')}.json").write_text(
                json.dumps(moves, ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        _set(phase="idle", items=[], message="Применено")
    return {"moved": len(moves), "errors": errors}


@app.post("/api/undo")
def undo():
    dest = STATE.get("dest")
    if not dest:
        raise HTTPException(400, "Нет папки")
    logs = sorted((Path(dest) / LOG_DIRNAME).glob("*.json"))
    if not logs:
        raise HTTPException(400, "Нечего отменять")
    last = logs[-1]
    moves = json.loads(last.read_text(encoding="utf-8"))
    restored, errors = 0, []
    for m in reversed(moves):
        src, dst = Path(m["to"]), Path(m["from"])
        try:
            if not src.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst = _unique(dst)
            shutil.move(str(src), str(dst))
            restored += 1
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
    # убрать пустые папки, которые остались после перемещения
    for d in sorted(Path(dest).rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if d.is_dir() and not any(d.iterdir()) and LOG_DIRNAME not in d.parts:
                d.rmdir()
        except OSError:
            pass
    last.unlink()
    _set(phase="idle", items=[], message="Отменено")
    return {"restored": restored, "errors": errors}
