"""Точка входа: python -m app  (так же запускает ярлык установщика через pythonw)."""
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORTS = range(8765, 8785)  # если 8765 занят чужой программой, берём следующий свободный


def _setup_env() -> None:
    # консоль/редирект на Windows бывает в cp1252: без этого print с кириллицей падает
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    # ffmpeg/ffprobe, которые кладёт установщик в <папка установки>\bin
    bindir = ROOT / "bin"
    if bindir.is_dir():
        os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")
    # pythonw: нет консоли, stdout/stderr == None -> пишем в лог
    if sys.stdout is None or sys.stderr is None:
        logdir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "MontageSorter"
        logdir.mkdir(parents=True, exist_ok=True)
        f = open(logdir / "log.txt", "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f


def _is_ours(port: int) -> bool:
    """На порту уже работает именно Montage Sorter?"""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/state", timeout=1.5) as r:
            return "phase" in json.load(r)
    except Exception:
        return False


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _pick_port() -> tuple[int, bool]:
    """(порт, уже_запущено_у_нас)"""
    for p in PORTS:
        if _is_ours(p):
            return p, True
        if _port_free(p):
            return p, False
    raise SystemExit("Нет свободного порта в диапазоне 8765-8784")


def main() -> None:
    _setup_env()
    port, running = _pick_port()
    url = f"http://127.0.0.1:{port}"
    open_browser = "--no-browser" not in sys.argv
    if running:  # второй запуск ярлыка — просто открыть страницу
        if open_browser:
            webbrowser.open(url)
        return
    if "--autoexit" in sys.argv:  # закрыл вкладку — сервер сам завершается
        os.environ["MS_AUTOEXIT"] = "1"
    import uvicorn

    print(f"Montage Sorter: {url}  (Ctrl+C — выход)")
    if open_browser:
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True).start()
    uvicorn.run("app.server:app", host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
