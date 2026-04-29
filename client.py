"""
J.A.R.V.I.S. Headless Audio Client — für Laptop/PC ohne GUI.
Verbindet via WebSocket mit dem JARVIS-Server (HP EliteDesk).

.env / Umgebungsvariablen:
  JARVIS_SERVER=ws://100.x.x.x:8765   ← Tailscale-IP des Servers
  MANUAL_MODE=false                    ← true = kein Wake Word
  AUDIO_INPUT_DEVICE=                  ← leer = System-Default
  TEXT_ONLY=false                      ← true = kein Mikrofon, nur Tastatur

Start: python3 client.py
"""

import asyncio
import json
import os
import queue
import sys
import threading
import time

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv

load_dotenv()

import protocol as P

JARVIS_SERVER = os.getenv("JARVIS_SERVER", "")
MANUAL_MODE = os.getenv("MANUAL_MODE", "false").lower() == "true"
TEXT_ONLY = os.getenv("TEXT_ONLY", "false").lower() == "true"
AUDIO_INPUT_DEVICE = os.getenv("AUDIO_INPUT_DEVICE")
AUDIO_OUTPUT_DEVICE = int(os.getenv("AUDIO_OUTPUT_DEVICE")) if os.getenv("AUDIO_OUTPUT_DEVICE") else None
CLIENT_NAME = os.getenv("CLIENT_NAME", "")
BT_SPEAKER_MAC = os.getenv("BT_SPEAKER_MAC", "")

# Gesetzt während JARVIS spricht — Record-Loop pausiert dann
_jarvis_speaking = threading.Event()
# Gesetzt wenn JARVIS unterbrochen werden soll
_interrupt_playback = threading.Event()


def _ensure_bt_connected() -> None:
    if not BT_SPEAKER_MAC:
        return
    import subprocess
    try:
        result = subprocess.run(
            ["bluetoothctl", "info", BT_SPEAKER_MAC],
            capture_output=True, text=True, timeout=3
        )
        if "Connected: yes" not in result.stdout:
            print(f"[bt] Box getrennt — verbinde {BT_SPEAKER_MAC}…", flush=True)
            subprocess.run(
                ["bluetoothctl", "connect", BT_SPEAKER_MAC],
                capture_output=True, timeout=5
            )
            time.sleep(2.0)
            print("[bt] Reconnect abgeschlossen.", flush=True)
    except Exception as e:
        print(f"[bt] Fehler: {e}", flush=True)


# ── Audioausgabe ──────────────────────────────────────────────────────────────

def _play_loop(audio_queue: queue.Queue):
    """Spielt eingehende PCM-Chunks vom Server ab. Stoppt bei _interrupt_playback."""
    IDLE_TIMEOUT = 0.4

    while True:
        chunk = audio_queue.get()
        if chunk is None:
            _jarvis_speaking.clear()
            break

        _interrupt_playback.clear()
        _jarvis_speaking.set()
        _ensure_bt_connected()
        try:
            with sd.OutputStream(
                samplerate=P.PCM_SAMPLERATE,
                channels=P.PCM_CHANNELS,
                dtype=P.PCM_DTYPE,
                device=AUDIO_OUTPUT_DEVICE,
            ) as stream:
                stream.write(np.frombuffer(chunk, dtype=np.int16))
                while not _interrupt_playback.is_set():
                    try:
                        chunk = audio_queue.get(timeout=IDLE_TIMEOUT)
                        if chunk is None:
                            return
                        stream.write(np.frombuffer(chunk, dtype=np.int16))
                    except queue.Empty:
                        break
                if _interrupt_playback.is_set():
                    # Queue leeren damit altes Audio nicht nachläuft
                    while not audio_queue.empty():
                        try:
                            audio_queue.get_nowait()
                        except queue.Empty:
                            break
                    print("[client] Wiedergabe unterbrochen.", flush=True)
        finally:
            time.sleep(0.8)
            _jarvis_speaking.clear()


# ── Wake Word während Wiedergabe ──────────────────────────────────────────────

def _interrupt_watcher(stop_event: threading.Event):
    """Läuft parallel — erkennt Wake Word während JARVIS spricht und unterbricht."""
    import audio
    while not stop_event.is_set():
        if not _jarvis_speaking.is_set():
            stop_event.wait(timeout=0.2)
            continue
        try:
            interrupt = threading.Event()
            audio.listen_for_wake_word(interrupt=interrupt)
            if _jarvis_speaking.is_set():
                _interrupt_playback.set()
                print("[client] Wake Word erkannt — unterbreche JARVIS.", flush=True)
        except Exception:
            pass


# ── Aufnahme ──────────────────────────────────────────────────────────────────

def _record_loop(
    ws,
    ws_loop: asyncio.AbstractEventLoop,
    stop_event: threading.Event,
):
    """Wake Word → VAD → WAV-Bytes → WebSocket. Pausiert wenn JARVIS spricht."""
    import audio

    in_conversation = False
    silent_turns = 0
    interrupt = threading.Event()
    last_response_time = time.monotonic()
    prev_speaking = False
    CONVO_TIMEOUT = 40  # Sekunden ohne JARVIS-Antwort → zurück zum Wake Word

    def _speaking_watcher():
        while not stop_event.is_set():
            _jarvis_speaking.wait(timeout=0.1)
            if _jarvis_speaking.is_set():
                interrupt.set()
    threading.Thread(target=_speaking_watcher, daemon=True).start()

    while not stop_event.is_set():
        # Transition JARVIS fertig → Zeitstempel merken
        cur_speaking = _jarvis_speaking.is_set()
        if prev_speaking and not cur_speaking:
            last_response_time = time.monotonic()
        prev_speaking = cur_speaking

        # Conversation-Timeout: kein JARVIS-Response seit CONVO_TIMEOUT → Wake Word
        if in_conversation and not cur_speaking and time.monotonic() - last_response_time > CONVO_TIMEOUT:
            print("[client] Kein Response — zurück zum Wake Word.", flush=True)
            in_conversation = False
            silent_turns = 0

        if cur_speaking:
            stop_event.wait(timeout=0.2)
            in_conversation = True
            silent_turns = 0
            continue

        if not in_conversation:
            if not MANUAL_MODE:
                print("[client] Warte auf Wake Word…", flush=True)
                interrupt.clear()
                try:
                    audio.listen_for_wake_word(interrupt=interrupt)
                    print("[client] Wake Word erkannt!", flush=True)
                    audio.play_beep()
                    last_response_time = time.monotonic()
                except Exception as e:
                    print(f"[client] Wake Word Fehler: {e}", flush=True)
                    if stop_event.is_set():
                        break
                    stop_event.wait(timeout=3.0)
                    continue
                if stop_event.is_set():
                    break
            else:
                print("[client] Bereit (kein Wake Word)…", flush=True)

        if _jarvis_speaking.is_set():
            continue

        interrupt.clear()
        print("[client] Höre zu…", flush=True)
        try:
            wav_path = audio.record_with_vad(interrupt=interrupt)
        except Exception as e:
            print(f"[client] Aufnahme-Fehler: {e}", flush=True)
            wav_path = None

        if not wav_path:
            silent_turns += 1
            if silent_turns >= 2:
                in_conversation = False
                silent_turns = 0
            continue

        silent_turns = 0
        in_conversation = True

        try:
            with open(wav_path, "rb") as f:
                wav_bytes = f.read()
            os.unlink(wav_path)
        except OSError:
            continue

        asyncio.run_coroutine_threadsafe(ws.send(wav_bytes), ws_loop)


# ── Server-Events ausgeben ────────────────────────────────────────────────────

_STATE_LABELS = {
    "idle": "Bereit",
    "listening": "Hoere...",
    "thinking": "Denke...",
    "speaking": "Spreche...",
    "tool_running": "Tool laeuft...",
}


def _handle_event(data: dict):
    t = data.get("type")
    if t == P.STATE:
        label = _STATE_LABELS.get(data.get("state", ""), data.get("state", ""))
        print(f"\r[{label}]          ", end="", flush=True)
    elif t == P.STATUS:
        print(f"\r[client] {data.get('text', '')}          ", end="", flush=True)
    elif t == P.TRANSCRIPT:
        print(f"\n[Du]      {data.get('text', '')}", flush=True)
    elif t == P.RESPONSE_START:
        print("\n[JARVIS]  ", end="", flush=True)
    elif t == P.RESPONSE_CHUNK:
        print(data.get("text", ""), end="", flush=True)
    elif t == P.RESPONSE_DONE:
        print("", flush=True)
    elif t == P.TOOL:
        print(f"\n[Tool]    {data.get('name', '')}", flush=True)
    elif t == P.ERROR:
        print(f"\n[Fehler]  {data.get('message', '')}", flush=True)
    elif t == P.SET_ALARM:
        import alarm as _alarm
        _alarm.schedule(
            alarm_id=data["alarm_id"],
            hour=data["hour"],
            minute=data["minute"],
            label=data.get("label", "Wecker"),
            snooze_minutes=data.get("snooze_minutes", 9),
            max_snooze=data.get("max_snooze", 2),
            audio_output_device=AUDIO_OUTPUT_DEVICE,
            song=data.get("song") or None,
        )
    elif t == P.SNOOZE_ALARM:
        import alarm as _alarm
        _alarm.snooze(data.get("alarm_id"), data.get("minutes", 9))
    elif t == P.CANCEL_ALARM:
        import alarm as _alarm
        _alarm.dismiss(data.get("alarm_id"))
    elif t == P.PLAY_MUSIC:
        import player
        player.play(data["song"], data.get("volume", 70))
    elif t == P.STOP_MUSIC:
        import player
        player.stop()


# ── Empfangs-Loop ─────────────────────────────────────────────────────────────

async def _alarm_event_loop(ws):
    """Leitet alarm.event_queue Events an den Server weiter."""
    import alarm as _alarm
    while True:
        try:
            event = _alarm.event_queue.get_nowait()
            await ws.send(json.dumps(event))
        except queue.Empty:
            await asyncio.sleep(0.5)
        except Exception:
            await asyncio.sleep(1)


async def _recv_loop(ws, audio_queue: queue.Queue):
    async for message in ws:
        if isinstance(message, bytes):
            audio_queue.put(message)
        else:
            _handle_event(json.loads(message))
    audio_queue.put(None)


# ── Stdin → Text-Input (optional) ─────────────────────────────────────────────

async def _stdin_loop(ws):
    """Liest stdin-Zeilen und schickt sie als text_input an den Server."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            text = line.strip()
            if text:
                await ws.send(json.dumps({"type": P.TEXT_INPUT, "text": text}))
        except Exception:
            break


# ── Haupt-Client ──────────────────────────────────────────────────────────────

async def _run():
    import websockets

    if not JARVIS_SERVER:
        print("[client] FEHLER: JARVIS_SERVER ist nicht gesetzt.")
        print("[client] Beispiel in .env:  JARVIS_SERVER=ws://100.x.x.x:8765")
        sys.exit(1)

    print(f"[client] Verbinde mit {JARVIS_SERVER}…", flush=True)

    while True:
        try:
            async with websockets.connect(JARVIS_SERVER, ping_interval=20) as ws:
                print("[client] Verbunden!", flush=True)
                try:
                    import audio as _a
                    _a.beep_ready()
                except Exception:
                    pass
                if CLIENT_NAME:
                    await ws.send(json.dumps({"type": P.CLIENT_HELLO, "name": CLIENT_NAME}))
                try:
                    import alarm as _alarm
                    await ws.send(json.dumps({"type": P.ALARM_SYNC, "alarms": _alarm.get_list()}))
                except Exception:
                    pass
                loop = asyncio.get_running_loop()

                audio_queue: queue.Queue = queue.Queue()
                stop_event = threading.Event()

                # Startup-Greeting abwarten bevor Mic öffnet
                _jarvis_speaking.set()

                threading.Thread(
                    target=_play_loop, args=(audio_queue,), daemon=True
                ).start()
                if not TEXT_ONLY:
                    threading.Thread(
                        target=_record_loop, args=(ws, loop, stop_event), daemon=True
                    ).start()
                    if os.getenv("INTERRUPT_WATCHER", "false").lower() == "true":
                        threading.Thread(
                            target=_interrupt_watcher, args=(stop_event,), daemon=True
                        ).start()
                else:
                    print("[client] TEXT_ONLY-Modus — Eingabe über Tastatur.", flush=True)

                try:
                    await asyncio.gather(
                        _recv_loop(ws, audio_queue),
                        _stdin_loop(ws),
                        _alarm_event_loop(ws),
                    )
                finally:
                    stop_event.set()
                    audio_queue.put(None)

        except KeyboardInterrupt:
            print("\n[client] Beendet.", flush=True)
            return
        except Exception as e:
            print(f"\n[client] Verbindungsfehler: {type(e).__name__}: {e}", flush=True)
            print("[client] Reconnect in 5s…", flush=True)
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(_run())
