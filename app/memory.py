"""Память ответов: отпечаток файла -> метка, которую выбрал пользователь.
Хранится в %LOCALAPPDATA%\\MontageSorter\\memory.json. Похожие файлы потом размечаются сами."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

THRESH = {"clip": 0.90, "sfx": 0.88, "mood": 0.88}  # косинусная близость, выше — считаем «то же самое»
MAX_ITEMS = 1500
_LOCK = threading.Lock()
_DATA: dict | None = None
_MATS: dict = {}


def _path() -> Path:
    if os.environ.get("MS_MEMORY"):
        return Path(os.environ["MS_MEMORY"])
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "MontageSorter" if base else Path.home() / ".montage_sorter") / "memory.json"


def _load() -> dict:
    global _DATA
    if _DATA is None:
        try:
            _DATA = json.loads(_path().read_text(encoding="utf-8"))
        except Exception:
            _DATA = {"version": 1, "items": []}
    return _DATA


def count() -> int:
    with _LOCK:
        return len(_load()["items"])


def match(space: str, emb) -> dict | None:
    """Лучшая запись той же «области» (clip/sfx/mood) с близостью >= порога, иначе None."""
    if emb is None:
        return None
    try:
        import numpy as np
        with _LOCK:
            if space not in _MATS:
                ents = [e for e in _load()["items"] if e["space"] == space]
                _MATS[space] = (np.asarray([e["emb"] for e in ents], dtype=np.float32) if ents else None, ents)
            mat, ents = _MATS[space]
        if mat is None:
            return None
        v = np.asarray(emb, dtype=np.float32)
        if v.shape[0] != mat.shape[1]:
            return None
        sims = mat @ v
        k = int(sims.argmax())
        if float(sims[k]) >= THRESH.get(space, 0.9):
            return ents[k]
    except Exception:
        return None
    return None


def add(space: str, emb, topic: str, tag: str | None = None) -> None:
    if emb is None:
        return
    with _LOCK:
        data = _load()
        data["items"].append({"space": space, "emb": [round(float(x), 4) for x in emb], "topic": topic, "tag": tag})
        data["items"] = data["items"][-MAX_ITEMS:]
        _MATS.pop(space, None)
        try:
            p = _path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
