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

# Gesetzt während JARVIS spricht — Record-Loop pausiert dann
_jarvis_speaking = threading.Event()
# Gesetzt wenn JARVIS unterbrochen werden soll
_interrupt_playback = threading.Event()


# ── Audioausgabe ──────────────────────────────────────────────────────────────

def _play_loop(audio_queue: queue.Queue):
    """Spielt eingehende PCM-Chunks vom Server ab. Stoppt bei _interrupt_playback."""
    IDLE_TIMEOUT = 0.4

    while True:
        chunk = audio_queue.get()
        if chunk is None:
            break

        _interrupt_playback.clear()
        _jarvis_speaking.set()
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
            time.sleep(0.3)
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

    while not stop_event.is_set():
        # Warten bis JARVIS fertig ist
        if _jarvis_speaking.is_set():
            _jarvis_speaking.wait()
            stop_event.wait(timeout=0.3)
            in_conversation = True
            silent_turns = 0

        if not in_conversation:
            if not MANUAL_MODE:
                print("[client] Warte auf Wake Word…", flush=True)
                interrupt.clear()
                try:
                    audio.listen_for_wake_word(interrupt=interrupt)
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
    "idle": "● Bereit",
    "listening": "◉ Höre…",
    "thinking": "◈ Denke…",
    "speaking": "◆ Spreche…",
    "tool_running": "◇ Tool läuft…",
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


# ── Empfangs-Loop ─────────────────────────────────────────────────────────────

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
    loop = asyncio.get_event_loop()
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
                loop = asyncio.get_event_loop()

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
