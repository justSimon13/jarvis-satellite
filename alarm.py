"""
JARVIS Satellite Alarm — läuft lokal auf dem Satellite-Device.
Unabhängig vom Server: klingelt auch wenn die WebSocket-Verbindung weg ist.
Wecker werden in alarm_state.json persistiert und beim Start wiederhergestellt.
"""
import datetime
import json
import os
import threading

import numpy as np

_PCM_RATE = 24000
_active: dict[str, dict] = {}  # alarm_id → entry
_ringing: set[str] = set()     # alarm_ids die gerade beepen
_lock = threading.Lock()

_STATE_FILE = os.path.join(os.path.dirname(__file__), "alarm_state.json")


def _save_state() -> None:
    data = {}
    with _lock:
        for aid, entry in _active.items():
            data[aid] = {
                "label": entry["label"],
                "fire_ts": entry["fire_ts"],
                "snooze_minutes": entry["snooze_minutes"],
                "max_snooze": entry["max_snooze"],
                "snooze_count": entry["snooze_count"],
                "device": entry["device"],
                "song": entry["song"],
            }
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"[alarm] State speichern fehlgeschlagen: {e}", flush=True)


def _load_state() -> None:
    if not os.path.exists(_STATE_FILE):
        return
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return

    now = datetime.datetime.now().timestamp()
    restored = 0
    for aid, entry in data.items():
        fire_ts = entry.get("fire_ts", 0)
        if fire_ts <= now:
            # Zeit bereits vorbei — auf nächsten Tag verschieben
            fire_dt = datetime.datetime.fromtimestamp(fire_ts)
            fire_dt += datetime.timedelta(days=1)
            fire_ts = fire_dt.timestamp()
        seconds = fire_ts - now
        _schedule_timer(
            alarm_id=aid,
            fire_ts=fire_ts,
            seconds=seconds,
            label=entry["label"],
            snooze_minutes=entry.get("snooze_minutes", 9),
            max_snooze=entry.get("max_snooze", 2),
            snooze_count=entry.get("snooze_count", 0),
            audio_output_device=entry.get("device"),
            song=entry.get("song"),
        )
        restored += 1
    if restored:
        print(f"[alarm] {restored} Wecker aus alarm_state.json wiederhergestellt.", flush=True)


def _schedule_timer(alarm_id, fire_ts, seconds, label, snooze_minutes, max_snooze,
                    snooze_count=0, audio_output_device=None, song=None):
    stop = threading.Event()
    timer = threading.Timer(seconds, _fire, args=[alarm_id])
    timer.daemon = True
    with _lock:
        _active[alarm_id] = {
            "label": label,
            "fire_ts": fire_ts,
            "snooze_minutes": snooze_minutes,
            "max_snooze": max_snooze,
            "snooze_count": snooze_count,
            "device": audio_output_device,
            "song": song,
            "stop": stop,
            "timer": timer,
        }
    timer.start()


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
    fire_ts = target_dt.timestamp()

    _schedule_timer(alarm_id, fire_ts, seconds, label, snooze_minutes, max_snooze,
                    audio_output_device=audio_output_device, song=song)
    _save_state()
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
            _save_state()
            return False

        entry["stop"] = threading.Event()
        fire_ts = (datetime.datetime.now() + datetime.timedelta(minutes=minutes)).timestamp()
        entry["fire_ts"] = fire_ts

    print(f"[alarm] Snooze {minutes} min ({count}/{max_s})", flush=True)
    _save_state()
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
    _save_state()
    return dismissed


# Beim Import: gespeicherte Wecker wiederherstellen
_load_state()
