"""Определение типа файла, миниатюры, дубликаты и классификация по содержимому (CLIP)."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from PIL import Image

VIDEO = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mts", ".m2ts", ".mxf", ".wmv", ".flv"}
AUDIO = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".aiff", ".aif", ".wma", ".opus"}
IMAGE = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".heic"}
GRAPHICS = {".psd", ".ai", ".eps", ".svg", ".xcf", ".sketch", ".fig"}
PROJECT = {".prproj", ".aep", ".aet", ".drp", ".veg", ".kdenlive", ".fcpxml", ".mogrt", ".ae"}

SFX_MAX_SECONDS = 10.0  # аудио короче этого — SFX, длиннее — музыка

# Папки проекта
DIRS = {
    "footage": "01_Footage",
    "music": "02_Audio/Music",
    "sfx": "02_Audio/SFX",
    "image": "03_Images",
    "graphics": "04_Graphics",
    "project": "05_Project",
    "other": "06_Other",
    "dup": "99_Duplicates",
}
EXTRA_DIRS = ["07_Exports"]

KIND_TITLES = {
    "footage": "Видео",
    "music": "Музыка",
    "sfx": "SFX",
    "image": "Картинки",
    "graphics": "Графика",
    "project": "Проекты",
    "other": "Прочее",
    "dup": "Дубликаты",
}

HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # не мигать консолью на Windows
    return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=flags)


def duration_seconds(path: Path) -> float | None:
    if not HAS_FFPROBE:
        return None
    try:
        r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                  "-of", "json", str(path)])
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return None


def png_has_alpha(path: Path) -> bool:
    try:
        with Image.open(path) as im:
            if im.mode in ("RGBA", "LA"):
                return im.getchannel("A").getextrema()[0] < 250
            return "transparency" in im.info
    except Exception:
        return False


def classify_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in VIDEO:
        return "footage"
    if ext in AUDIO:
        d = duration_seconds(path)
        if d is None:  # нет ffprobe — грубо по размеру
            return "sfx" if path.stat().st_size < 400_000 else "music"
        return "sfx" if d < SFX_MAX_SECONDS else "music"
    if ext in PROJECT:
        return "project"
    if ext in GRAPHICS:
        return "graphics"
    if ext in IMAGE:
        if ext in (".png", ".webp") and png_has_alpha(path):
            return "graphics"  # PNG с прозрачностью — это графика, а не фото
        return "image"
    return "other"


def file_hash(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def make_thumb(src: Path, kind: str, out: Path, size: int = 256) -> bool:
    """Миниатюра JPEG. Для видео — кадр из начала. Для остального — False."""
    if out.exists():
        return True
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        if kind == "footage":
            if not HAS_FFMPEG:
                return False
            dur = duration_seconds(src) or 0
            ts = max(0.0, min(dur * 0.3, 20.0))
            r = _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{ts:.2f}", "-i", str(src),
                      "-frames:v", "1", "-vf", f"scale={size}:-2", "-q:v", "4", str(out)], 60)
            return r.returncode == 0 and out.exists()
        if kind in ("image", "graphics"):
            if src.suffix.lower() in GRAPHICS:
                return False
            with Image.open(src) as im:
                im.thumbnail((size, size))
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                    bg = Image.new("RGB", im.size, (30, 30, 36))
                    bg.paste(im, mask=im.getchannel("A"))
                    im = bg
                else:
                    im = im.convert("RGB")
                im.save(out, "JPEG", quality=80)
            return True
    except Exception:
        return False
    return False


def _feat(x):
    """В разных версиях transformers get_*_features отдаёт тензор или объект с pooler_output."""
    if hasattr(x, "pooler_output") and x.pooler_output is not None:
        return x.pooler_output
    if isinstance(x, (tuple, list)):
        return x[0]
    return x


# ---------- Классификация по содержимому (CLIP) ----------

TOPICS: dict[str, list[str]] = {
    "Люди": ["a photo of a person", "a portrait of a face", "a group of people"],
    "Города и здания": ["a city street", "buildings and architecture", "a photo of a skyscraper"],
    "Природа": ["a nature landscape", "a forest", "mountains and a lake", "a beach and the sea"],
    "Еда и напитки": ["a photo of food", "a drink in a glass", "a meal on a plate"],
    "Животные": ["a photo of an animal", "a cat", "a dog", "a bird"],
    "Транспорт": ["a car on a road", "a motorcycle or a bike", "an airplane or a train"],
    "Игры": ["a video game screenshot", "a 3d game scene"],
    "Экран и текст": ["a screenshot of a computer screen", "a document with text", "a user interface"],
    "Интерьер": ["interior of a room", "a bedroom or an office"],
    "Небо и космос": ["sky and clouds", "a sunset", "space and stars"],
    "Техника": ["electronics and gadgets", "a computer or a phone", "machinery"],
    "Арт и мемы": ["a meme", "a digital illustration", "a cartoon or anime"],
}
UNKNOWN_TOPIC = "Разное"
MIN_CONFIDENCE = 0.28


class Clip:
    """Ленивая обёртка над CLIP. Если torch/transformers нет — available == False."""

    def __init__(self) -> None:
        self.available = False
        self.error: str | None = None
        self.device = "cpu"
        self._model = None

    def load(self) -> bool:
        if self._model is not None:
            return True
        try:
            import torch
            from transformers import CLIPModel, CLIPProcessor
        except Exception as e:  # noqa: BLE001
            self.error = f"torch/transformers не установлены ({e.__class__.__name__})"
            return False
        try:
            name = "openai/clip-vit-base-patch32"
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            self._torch = torch
            self._model = CLIPModel.from_pretrained(name).to(self.device).eval()
            self._proc = CLIPProcessor.from_pretrained(name)
            names = list(TOPICS)
            embs = []
            with torch.no_grad():
                for n in names:
                    inp = self._proc(text=TOPICS[n], return_tensors="pt", padding=True).to(self.device)
                    e = _feat(self._model.get_text_features(**inp))
                    e = e / e.norm(dim=-1, keepdim=True)
                    m = e.mean(dim=0)
                    embs.append(m / m.norm())
            self._text = torch.stack(embs)
            self._names = names
            self.available = True
            return True
        except Exception as e:  # noqa: BLE001
            self.error = f"не удалось загрузить модель: {e}"
            return False

    def classify(self, thumbs: list[Path]) -> list[str]:
        torch = self._torch
        out: list[str] = []
        B = 32
        for i in range(0, len(thumbs), B):
            batch = thumbs[i:i + B]
            imgs, idx = [], []
            for j, p in enumerate(batch):
                try:
                    imgs.append(Image.open(p).convert("RGB"))
                    idx.append(j)
                except Exception:
                    pass
            res = [UNKNOWN_TOPIC] * len(batch)
            if imgs:
                with torch.no_grad():
                    inp = self._proc(images=imgs, return_tensors="pt").to(self.device)
                    f = _feat(self._model.get_image_features(**inp))
                    f = f / f.norm(dim=-1, keepdim=True)
                    probs = (100.0 * f @ self._text.T).softmax(dim=-1)
                    conf, arg = probs.max(dim=-1)
                for k, j in enumerate(idx):
                    if conf[k].item() >= MIN_CONFIDENCE:
                        res[j] = self._names[arg[k].item()]
            out.extend(res)
        return out


# ---------- Звуки: теги, настроение, BPM ----------

# (категория, тег для имени файла, описание для модели)
SFX_TAGS: list[tuple[str, str, str]] = [
    ("Удары и взрывы", "explosion", "an explosion"),
    ("Удары и взрывы", "punch", "a punch or a hit"),
    ("Удары и взрывы", "impact", "a heavy cinematic impact boom"),
    ("Удары и взрывы", "gunshot", "a gunshot"),
    ("Удары и взрывы", "glass_break", "breaking glass"),
    ("Удары и взрывы", "crash", "a loud crash"),
    ("Движение", "whoosh", "a fast whoosh swoosh"),
    ("Движение", "footsteps", "footsteps walking"),
    ("Движение", "rustle", "cloth rustling"),
    ("Двери и предметы", "door_slam", "a door slamming"),
    ("Двери и предметы", "door_creak", "a creaking door"),
    ("Двери и предметы", "knock", "knocking on a door"),
    ("Двери и предметы", "keys", "jingling keys"),
    ("Двери и предметы", "paper", "paper rustling"),
    ("Интерфейс", "click", "a button click"),
    ("Интерфейс", "notification", "a notification chime"),
    ("Интерфейс", "beep", "an electronic beep"),
    ("Интерфейс", "error", "an error buzzer"),
    ("Интерфейс", "success", "a success sound jingle"),
    ("Природа", "rain", "rain"),
    ("Природа", "thunder", "thunder"),
    ("Природа", "wind", "wind blowing"),
    ("Природа", "fire", "a crackling fire"),
    ("Природа", "water", "flowing water"),
    ("Природа", "waves", "ocean waves"),
    ("Природа", "birds", "birds chirping"),
    ("Люди", "applause", "applause clapping"),
    ("Люди", "crowd", "a crowd of people"),
    ("Люди", "laugh", "laughter"),
    ("Люди", "scream", "a scream"),
    ("Люди", "whistle", "whistling"),
    ("Люди", "speech", "a person speaking"),
    ("Животные", "dog", "a dog barking"),
    ("Животные", "cat", "a cat meowing"),
    ("Транспорт", "car_engine", "a car engine"),
    ("Транспорт", "car_horn", "a car horn"),
    ("Транспорт", "siren", "a siren"),
    ("Транспорт", "airplane", "an airplane"),
    ("Транспорт", "train", "a train"),
    ("Техника", "typing", "typing on a keyboard"),
    ("Техника", "camera", "a camera shutter"),
    ("Техника", "phone", "a phone ringing"),
    ("Техника", "machine", "a machine humming"),
    ("Техника", "clock", "a ticking clock"),
    ("Музыкальные", "drum", "a drum hit"),
    ("Музыкальные", "bass_drop", "a bass drop"),
    ("Музыкальные", "riser", "a rising synth riser"),
    ("Музыкальные", "guitar", "a guitar"),
    ("Музыкальные", "piano", "a piano"),
]
MOODS: list[tuple[str, str, str]] = [
    ("Спокойная", "calm", "calm relaxing ambient music"),
    ("Энергичная", "energetic", "energetic upbeat fast music"),
    ("Эпичная", "epic", "epic cinematic orchestral music"),
    ("Грустная", "sad", "sad emotional melancholic music"),
    ("Весёлая", "happy", "happy cheerful playful music"),
    ("Мрачная", "dark", "dark tense suspenseful music"),
    ("Хип-хоп и биты", "beat", "hip hop beat with drums"),
    ("Электроника", "electronic", "electronic dance music"),
    ("Рок", "rock", "rock music with electric guitar"),
    ("Лоу-фай", "lofi", "lofi chill beats"),
]
UNKNOWN_SFX = "Разное"
SFX_MIN_CONF = 0.10
MOOD_MIN_CONF = 0.20
CLAP_SR = 48000


def decode_audio(path: Path, sr: int, start: float = 0.0, dur: float = 10.0):
    """Моно float32 через ffmpeg. None, если не вышло."""
    if not HAS_FFMPEG:
        return None
    try:
        import numpy as np
        r = _run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.2f}", "-i", str(path),
                  "-t", f"{dur:.2f}", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], 60)
        if r.returncode != 0 or len(r.stdout) < 4 * 1000:
            return None
        return np.frombuffer(r.stdout, dtype=np.float32)
    except Exception:
        return None


def estimate_bpm(y, sr: int) -> int | None:
    """Грубая оценка темпа по автокорреляции огибающей. Бывает ошибка в 2 раза."""
    import numpy as np
    hop, frame = 128, 512
    if y is None or len(y) < sr * 6:
        return None
    win = np.lib.stride_tricks.sliding_window_view(y, frame)[::hop]
    env = np.sqrt((win ** 2).mean(axis=1))
    onset = np.maximum(0.0, np.diff(env))
    onset -= onset.mean()
    if not onset.any():
        return None
    n = len(onset)
    f = np.fft.rfft(onset, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    fps = sr / hop
    best, best_score = None, 0.0
    for bpm in np.arange(60.0, 180.5, 0.5):
        lag = fps * 60.0 / bpm
        i = int(round(lag))
        if i + 1 >= n:
            continue
        v = ac[i] * (1 - (lag - i)) + ac[i + 1] * (lag - i)
        w = np.exp(-0.5 * (np.log2(bpm / 115.0) / 0.6) ** 2)  # лёгкое предпочтение 90–140
        if v * w > best_score:
            best, best_score = bpm, v * w
    return int(round(best)) if best else None


class AudioClap:
    """Zero-shot теги для звуков (CLAP). Если нет torch/transformers — available == False."""

    def __init__(self) -> None:
        self.available = False
        self.error: str | None = None
        self.device = "cpu"
        self._model = None

    def load(self) -> bool:
        if self._model is not None:
            return True
        try:
            import torch
            from transformers import ClapModel, ClapProcessor
        except Exception as e:  # noqa: BLE001
            self.error = f"torch/transformers не установлены ({e.__class__.__name__})"
            return False
        try:
            name = "laion/clap-htsat-unfused"
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            self._torch = torch
            self._model = ClapModel.from_pretrained(name).to(self.device).eval()
            self._proc = ClapProcessor.from_pretrained(name)
            self._scale = float(self._model.logit_scale_a.exp()) if hasattr(self._model, "logit_scale_a") else 30.0
            self._sfx_t = self._embed([f"The sound of {p}" for _, _, p in SFX_TAGS])
            self._mood_t = self._embed([p for _, _, p in MOODS])
            self.available = True
            return True
        except Exception as e:  # noqa: BLE001
            self.error = f"не удалось загрузить модель CLAP: {e}"
            return False

    def _embed(self, texts):
        torch = self._torch
        with torch.no_grad():
            inp = self._proc(text=texts, return_tensors="pt", padding=True).to(self.device)
            e = _feat(self._model.get_text_features(**inp))
            return e / e.norm(dim=-1, keepdim=True)

    def _audio(self, arrays):
        torch = self._torch
        arrays = [a for a in arrays]
        try:
            inp = self._proc(audio=arrays, sampling_rate=CLAP_SR, return_tensors="pt")
        except TypeError:  # старые версии: audios=
            inp = self._proc(audios=arrays, sampling_rate=CLAP_SR, return_tensors="pt")
        with torch.no_grad():
            inp = inp.to(self.device)
            f = _feat(self._model.get_audio_features(**inp))
            return f / f.norm(dim=-1, keepdim=True)

    def _rank(self, arrays, text, table, min_conf, unknown):
        torch = self._torch
        out = []
        B = 16
        for i in range(0, len(arrays), B):
            chunk = arrays[i:i + B]
            f = self._audio(chunk)
            probs = (self._scale * f @ text.T).softmax(dim=-1)
            conf, idx = probs.max(dim=-1)
            for c, k in zip(conf.tolist(), idx.tolist()):
                cat, tag, _ = table[k]
                out.append((cat, tag, c) if c >= min_conf else (unknown, None, c))
        return out

    def sfx(self, arrays):
        return self._rank(arrays, self._sfx_t, SFX_TAGS, SFX_MIN_CONF, UNKNOWN_SFX)

    def music(self, arrays):
        return self._rank(arrays, self._mood_t, MOODS, MOOD_MIN_CONF, UNKNOWN_SFX)
