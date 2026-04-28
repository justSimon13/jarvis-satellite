"""
mpv-Player für JARVIS Satellite — spielt Songs via YouTube (yt-dlp).
Läuft als Hintergrundprozess, maximal ein Song gleichzeitig.

Voraussetzungen:
  sudo apt install mpv
  pip install yt-dlp
"""
import subprocess
import threading

_proc: subprocess.Popen | None = None
_lock = threading.Lock()


def play(song: str, volume: int = 70) -> None:
    stop()
    with _lock:
        global _proc
        _proc = subprocess.Popen(
            [
                "mpv",
                "--no-video",
                f"--volume={max(0, min(100, volume))}",
                "--really-quiet",
                f"ytdl://ytsearch1:{song}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    print(f"[player] Spiele: {song!r} (vol {volume})", flush=True)


def stop() -> None:
    with _lock:
        global _proc
        if _proc and _proc.poll() is None:
            _proc.terminate()
            _proc = None
    print("[player] Gestoppt.", flush=True)


def is_playing() -> bool:
    with _lock:
        return _proc is not None and _proc.poll() is None
