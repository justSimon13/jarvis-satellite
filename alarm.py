"""
JARVIS Satellite Alarm — läuft lokal auf dem Satellite-Device.
Unabhängig vom Server: klingelt auch wenn die WebSocket-Verbindung weg ist.
"""
import datetime
import threading

import numpy as np

_PCM_RATE = 24000
_active: dict[str, dict] = {}  # alarm_id → entry
_ringing: set[str] = set()     # alarm_ids die gerade beepen
_lock = threading.Lock()


def _beep_pcm() -> np.ndarray:
    sr = _PCM_RATE
    t = np.linspace(0, 0.35, int(sr * 0.35), endpoint=False)
    fade = int(0.02 * sr)
    env = np.ones(len(t))
    env[:fade] = np.linspace(0, 1, fade)
    env[-fade:] = np.linspace(1, 0, fade)
    tone = np.sin(2 * np.pi * 880 * t) * 0.6 + np.sin(2 * np.pi * 1320 * t) * 0.4
    return (tone * env * 0.85 * 32767).astype(np.int16)


def schedule(alarm_id: str, hour: int, minute: int, label: str,
             snooze_minutes: int = 9, max_snooze: int = 2,
             audio_output_device=None, song: str | None = None) -> None:
    now = datetime.datetime.now()
    target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target_dt <= now:
        target_dt += datetime.timedelta(days=1)
    seconds = (target_dt - now).total_seconds()

    stop = threading.Event()
    timer = threading.Timer(seconds, _fire, args=[alarm_id])
    timer.daemon = True

    with _lock:
        _active[alarm_id] = {
            "label": label,
            "snooze_minutes": snooze_minutes,
            "max_snooze": max_snooze,
            "snooze_count": 0,
            "device": audio_output_device,
            "song": song,
            "stop": stop,
            "timer": timer,
        }

    timer.start()
    print(f"[alarm] Wecker gesetzt: {label!r} um {target_dt.strftime('%H:%M')}", flush=True)


def _fire(alarm_id: str) -> None:
    with _lock:
        entry = _active.get(alarm_id)
        if not entry:
            return
        entry["stop"].clear()
        _ringing.add(alarm_id)
    print(f"[alarm] Wecker klingelt: {entry['label']!r}", flush=True)
    if entry.get("song"):
        import player
        player.play(entry["song"], volume=80)
        # Wacht bis stop gesetzt wird (für Snooze/Dismiss)
        threading.Thread(target=_song_watch, args=[alarm_id], daemon=True).start()
    else:
        threading.Thread(target=_beep_loop, args=[alarm_id], daemon=True).start()


def _beep_loop(alarm_id: str) -> None:
    import sounddevice as sd

    with _lock:
        entry = _active.get(alarm_id)
    if not entry:
        return

    arr = _beep_pcm()
    stop = entry["stop"]
    device = entry.get("device")

    while not stop.is_set():
        try:
            sd.play(arr, samplerate=_PCM_RATE, device=device, blocking=True)
        except Exception as e:
            print(f"[alarm] Beep-Fehler: {e}", flush=True)
        stop.wait(timeout=0.9)


def _song_watch(alarm_id: str) -> None:
    """Wartet bis stop gesetzt wird und stoppt dann den Player."""
    with _lock:
        entry = _active.get(alarm_id)
    if not entry:
        return
    entry["stop"].wait()
    import player
    player.stop()


def snooze(alarm_id: str | None = None, minutes: int = 9) -> bool:
    with _lock:
        if alarm_id:
            aid = alarm_id
        else:
            # bevorzuge klingelnden Alarm, sonst nächsten geplanten
            aid = next(iter(_ringing), None) or next(iter(_active), None)
        entry = _active.get(aid) if aid else None
        if not entry:
            return False

        entry["stop"].set()
        _ringing.discard(aid)
        entry["snooze_count"] += 1
        count = entry["snooze_count"]
        max_s = entry["max_snooze"]

        if count > max_s:
            _active.pop(aid, None)
            print("[alarm] Max. Snooze erreicht — Alarm endgültig gestoppt.", flush=True)
            return False

        entry["stop"] = threading.Event()

    print(f"[alarm] Snooze {minutes} min ({count}/{max_s})", flush=True)
    t = threading.Timer(minutes * 60, _fire, args=[aid])
    t.daemon = True
    t.start()
    with _lock:
        if aid in _active:
            _active[aid]["timer"] = t
    return True


def dismiss(alarm_id: str | None = None) -> bool:
    with _lock:
        if alarm_id:
            entries = {alarm_id: _active.pop(alarm_id, None)}
        else:
            entries = dict(_active)
            _active.clear()

    dismissed = False
    for aid, entry in entries.items():
        if entry:
            dismissed = True
            entry["stop"].set()
            t = entry.get("timer")
            if t:
                t.cancel()
            print(f"[alarm] Alarm gestoppt: {entry['label']!r}", flush=True)
    with _lock:
        _ringing.difference_update(entries.keys())
    return dismissed
