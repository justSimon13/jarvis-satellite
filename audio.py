import ctypes
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import numpy as np
import sounddevice as sd
import scipy.io.wavfile as wav
import config

# ALSA-Fehlermeldungen (C-Level stderr) unterdrücken — harmlose Fallback-Versuche
# _alsa_cb muss auf Modulebene gehalten werden — sonst GC → dangling pointer → SEGV
_alsa_cb = None
try:
    _ALSA_ERROR_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p)
    _alsa = ctypes.cdll.LoadLibrary("libasound.so.2")
    _alsa_cb = _ALSA_ERROR_FUNC(lambda *_: None)
    _alsa.snd_lib_error_set_handler(_alsa_cb)
except Exception:
    pass

def _input_device():
    v = config.AUDIO_INPUT_DEVICE
    return int(v) if v else None


def _input_channels() -> int:
    """Gibt die tatsächlich unterstützte Kanalanzahl des Input-Geräts zurück (min 1, max 2)."""
    try:
        info = sd.query_devices(_input_device(), "input")
        ch = int(info["max_input_channels"])
        print(f"[audio] Input device: {info['name']!r}  max_input_channels={ch}", flush=True)
        return min(2, max(1, ch))
    except Exception as e:
        print(f"[audio] _input_channels fallback (err: {e})", flush=True)
        return 1


_INPUT_BLACKLIST  = ("iphone", "ipad", "teams", "eqmac", "monitor")
_INPUT_PREFER     = ("default", "mikrofon", "microphone", "kopfhörer", "headphone", "macbook", "uacdemo", "pulse")
_OUTPUT_BLACKLIST = ("hdmi", "spdif", "monitor", "null")
_OUTPUT_PREFER    = ("pebble", "speaker", "usb", "lautsprecher", "macbook", "default")


def _rank_input_device(name: str) -> int:
    n = name.lower()
    if any(b in n for b in _INPUT_BLACKLIST):
        return 99
    for i, p in enumerate(_INPUT_PREFER):
        if p in n:
            return i
    return 50


def _rank_output_device(name: str) -> int:
    n = name.lower()
    if any(b in n for b in _OUTPUT_BLACKLIST):
        return 99
    for i, p in enumerate(_OUTPUT_PREFER):
        if p in n:
            return i
    return 50


def _open_input_stream(samplerate: int, blocksize: int, callback) -> sd.InputStream:
    """Öffnet InputStream. Explizit konfiguriertes Gerät zuerst, sonst nach Präferenz sortiert."""
    explicit = _input_device()
    if explicit is not None:
        candidates: list[int] = [explicit]
    else:
        # Alle echten Input-Geräte nach Präferenz sortiert
        devs = [
            (i, info) for i, info in enumerate(sd.query_devices())
            if info.get("max_input_channels", 0) > 0
        ]
        devs.sort(key=lambda x: _rank_input_device(x[1]["name"]))
        candidates = [i for i, _ in devs]

    for device in candidates:
        try:
            dev_info = sd.query_devices(device)
            ch = max(1, min(2, int(dev_info.get("max_input_channels", 1))))
        except Exception:
            dev_info = {}
            ch = 1
        try:
            stream = sd.InputStream(
                samplerate=samplerate,
                channels=ch,
                dtype="float32",
                blocksize=blocksize,
                callback=callback,
                device=device,
            )
            print(f"[audio] InputStream: [{device}] {dev_info.get('name', '?')!r}", flush=True)
            return stream
        except sd.PortAudioError as e:
            print(f"[audio] [{device}] fehlgeschlagen: {e}", flush=True)
    raise sd.PortAudioError("InputStream: kein Input-Gerät funktioniert")


def open_output_stream(samplerate: int, channels: int, dtype: str) -> sd.OutputStream:
    """Öffnet OutputStream. Env-Override zuerst, sonst nach Präferenz sortiert — meidet HDMI."""
    explicit = os.getenv("AUDIO_OUTPUT_DEVICE")
    if explicit:
        candidates: list[int] = [int(explicit)]
    else:
        devs = [
            (i, info) for i, info in enumerate(sd.query_devices())
            if info.get("max_output_channels", 0) > 0
        ]
        devs.sort(key=lambda x: _rank_output_device(x[1]["name"]))
        candidates = [i for i, _ in devs]

    for device in candidates:
        try:
            dev_info = sd.query_devices(device)
            ch = max(1, min(channels, int(dev_info.get("max_output_channels", channels))))
        except Exception:
            dev_info = {}
            ch = channels
        try:
            stream = sd.OutputStream(samplerate=samplerate, channels=ch, dtype=dtype, device=device)
            print(f"[audio] OutputStream: [{device}] {dev_info.get('name', '?')!r}", flush=True)
            return stream
        except sd.PortAudioError as e:
            print(f"[audio] [{device}] output fehlgeschlagen: {e}", flush=True)
    raise sd.PortAudioError("OutputStream: kein Output-Gerät funktioniert")


SAMPLE_RATE = 16000
VAD_BLOCKSIZE = 512
VAD_MAX_SECONDS = 30
VAD_WARMUP_FRAMES = 16  # ~0.5s bei 16kHz/512 — ignoriert Echo-Nachhall nach JARVIS-Antwort

_RMS_SPEECH   = 0.012  # Energie-Schwelle ab der als Sprache gilt
_SPEECH_ONSET = 3      # Aufeinanderfolgende laute Frames um Sprache zu bestätigen
_SILENCE_FRAMES = 47   # ~1.5s Stille bei 16kHz/512 (512/16000*1000 ≈ 32ms/Frame)

_oww_model = None
_oww_lock = threading.Lock()


def _get_oww():
    global _oww_model
    with _oww_lock:
        if _oww_model is None:
            from openwakeword.model import Model
            _oww_model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
    return _oww_model


def play_beep(freq: int = 880, duration: float = 0.12, volume: float = 0.4) -> None:
    """Kurzer Bestätigungston — signalisiert dass JARVIS zugehört hat."""
    try:
        t = np.linspace(0, duration, int(sd.default.samplerate or 24000 * duration), False)
        tone = (np.sin(2 * np.pi * freq * t) * volume * 32767).astype(np.int16)
        sd.play(tone, samplerate=24000, blocking=False)
    except Exception:
        pass


def listen_for_wake_word(interrupt: threading.Event | None = None) -> bool:
    """Blockiert bis 'Hey JARVIS' erkannt oder interrupt gesetzt. Gibt True nur bei echter Erkennung zurück."""
    oww = _get_oww()
    oww.reset()  # State zwischen Sessions leeren

    real_detection = threading.Event()
    stop_stream = threading.Event()
    pcm_q: queue.Queue = queue.Queue(maxsize=20)
    chunk_size = 1280  # 80ms @ 16kHz

    # Callback läuft im PortAudio-Realtime-Thread — nur Daten queuen, KEIN ONNX hier.
    # ONNX RT im Realtime-Thread führt zu unbegrenztem Thread-local-Heap-Wachstum.
    def audio_callback(indata, frames, time_info, status):
        try:
            pcm_q.put_nowait((indata[:, 0] * 32767).astype(np.int16).copy())
        except queue.Full:
            pass

    def inference_worker():
        while not stop_stream.is_set():
            try:
                pcm = pcm_q.get(timeout=0.1)
                scores = oww.predict(pcm)
                if scores.get("hey_jarvis", 0) >= 0.35:
                    real_detection.set()
                    stop_stream.set()
            except queue.Empty:
                pass

    infer_thread = threading.Thread(target=inference_worker, daemon=True)
    infer_thread.start()

    with _open_input_stream(SAMPLE_RATE, chunk_size, audio_callback):
        while not stop_stream.is_set():
            if interrupt and interrupt.is_set():
                stop_stream.set()
                break
            stop_stream.wait(timeout=0.2)

    infer_thread.join(timeout=1.0)
    return real_detection.is_set()


def record_with_vad(interrupt: threading.Event | None = None) -> str:
    """Nimmt auf und stoppt via RMS-VAD bei Redepause. Kein torch nötig."""
    frames = []
    stop_event = threading.Event()
    speech_count = 0
    silence_count = 0
    speaking_started = False
    warmup_frames = 0  # Erste Frames ignorieren — lässt JARVIS-Echo abklingen
    stop_reason = "unknown"
    speech_onset_frame = None
    speech_onset_rms = None

    def audio_callback(indata, frame_count, time_info, status):
        nonlocal speech_count, silence_count, speaking_started, warmup_frames
        nonlocal stop_reason, speech_onset_frame, speech_onset_rms
        chunk = indata[:, 0].copy()
        frames.append(chunk)
        warmup_frames += 1
        if warmup_frames == VAD_WARMUP_FRAMES:
            print(f"[vad] Warmup abgeschlossen (frame {warmup_frames})", flush=True)
        if warmup_frames <= VAD_WARMUP_FRAMES:
            return  # Warmup: keine Spracherkennung in den ersten ~0.5s
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        if rms > _RMS_SPEECH:
            speech_count += 1
            silence_count = 0
            if speech_count >= _SPEECH_ONSET and not speaking_started:
                speaking_started = True
                speech_onset_frame = warmup_frames
                speech_onset_rms = rms
                print(f"[vad] Sprache erkannt — frame={warmup_frames} rms={rms:.4f}", flush=True)
        else:
            silence_count += 1
            speech_count = 0
            if speaking_started and silence_count >= _SILENCE_FRAMES:
                stop_reason = "silence"
                stop_event.set()

    t_start = time.monotonic()
    deadline = t_start + VAD_MAX_SECONDS
    with _open_input_stream(SAMPLE_RATE, VAD_BLOCKSIZE, audio_callback):
        while not stop_event.is_set():
            if interrupt and interrupt.is_set():
                stop_reason = "interrupt"
                stop_event.set()
                break
            if time.monotonic() >= deadline:
                stop_reason = "timeout"
                stop_event.set()
                break
            stop_event.wait(timeout=0.2)

    duration = time.monotonic() - t_start
    if not frames or not speaking_started:
        print(f"[vad] Kein Sprachbeginn — verworfen ({duration:.1f}s)", flush=True)
        return ""
    print(f"[vad] Aufnahme beendet: grund={stop_reason} dauer={duration:.1f}s onset_frame={speech_onset_frame}", flush=True)

    audio = np.concatenate(frames, axis=0)
    audio_int16 = (audio * 32767).astype(np.int16)
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav.write(tmp.name, SAMPLE_RATE, audio_int16)
    return tmp.name


def record_until_enter() -> str:
    """Fallback: Aufnahme manuell per Enter stoppen."""
    frames = []

    def callback(indata, frame_count, time_info, status):
        frames.append(indata.copy())

    stream = sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=callback,
        device=_input_device(),
    )
    stream.start()
    input("")
    stream.stop()
    stream.close()

    if not frames:
        return ""

    audio = np.concatenate(frames, axis=0)
    audio_int16 = (audio * 32767).astype(np.int16)
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav.write(tmp.name, SAMPLE_RATE, audio_int16)
    return tmp.name


def play_mp3(path: str):
    if sys.platform == "darwin":
        subprocess.run(["afplay", path], check=True)
    else:
        subprocess.run(["aplay", path], stderr=subprocess.DEVNULL)


def play_thinking_sound(stop_event: threading.Event):
    """Sanfter pulsierender Ton der loopt bis stop_event gesetzt wird."""
    sample_rate = 24000
    duration = 1.5
    t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
    # Sinus bei 220 Hz, Lautstärke pulsiert langsam
    envelope = 0.3 + 0.2 * np.sin(2 * np.pi * 0.8 * t)
    tone = (envelope * np.sin(2 * np.pi * 220 * t) * 0.15).astype(np.float32)

    with sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32") as stream:
        while not stop_event.is_set():
            stream.write(tone)


def _tone(freq: float, duration: float, volume: float = 0.3):
    """Spielt einen kurzen Ton (non-blocking)."""
    sr = 24000
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    wave = (np.sin(2 * np.pi * freq * t) * volume * 32767).astype(np.int16)
    def _play():
        try:
            with sd.OutputStream(samplerate=sr, channels=1, dtype="int16") as s:
                s.write(wave)
        except Exception:
            pass
    threading.Thread(target=_play, daemon=True).start()


def _beep():
    """Kurzer Ton: Wake Word erkannt."""
    _tone(880, 0.12)


def beep_ready():
    """Zwei aufsteigende Töne: Client verbunden."""
    _tone(660, 0.1)
    import time; time.sleep(0.12)
    _tone(880, 0.15)
