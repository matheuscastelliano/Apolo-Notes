"""Fase 0: mic + loopback ao mesmo tempo, com duas libs de loopback.

    .venv\\Scripts\\python spike\\capture.py                 # 20 s, voz sintetica no meio
    .venv\\Scripts\\python spike\\capture.py --seconds 60 --no-tts   # numa chamada real

Responde:
  - o loopback entrega amostras quando nada esta tocando? (trava, zeros, pula?)
  - abrir o mic do headset Bluetooth (HFP) mata o loopback da saida A2DP?
  - soundcard x PyAudioWPatch: alguma das duas falha ou perde audio?

Os WAVs (16 kHz mono) vao para spike/out/ para voce ouvir.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
import wave
import warnings
from pathlib import Path

import numpy as np
import soxr

OUT = Path(__file__).parent / "out"
SR = 16000
LOOP_SR = 48000
BLOCK_S = 0.1

TTS_TEXT = (
    "Bom dia, pessoal. Vamos começar pela daily. Ontem eu terminei o pull request "
    "da feature de login e subi o deploy no staging. Hoje vou revisar o backlog "
    "com o product owner e ajustar o pipeline de CI. Não tenho nenhum blocker."
)


class Track:
    """Acumula audio de uma fonte e mede o comportamento das entregas."""

    def __init__(self, name: str):
        self.name = name
        self.chunks: list[tuple[float, np.ndarray]] = []  # (t chegada, float32 mono @16k)
        self.gaps: list[float] = []  # intervalos entre entregas
        self.errors: list[str] = []
        self._last: float | None = None
        self._lock = threading.Lock()

    def push(self, t: float, mono16k: np.ndarray) -> None:
        with self._lock:
            if self._last is not None:
                self.gaps.append(t - self._last)
            self._last = t
            self.chunks.append((t, mono16k))

    def samples(self) -> np.ndarray:
        with self._lock:
            if not self.chunks:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate([c for _, c in self.chunks])

    def rms_by_second(self, seconds: int) -> list[float | None]:
        """dBFS por segundo de relogio (pela hora de chegada do bloco)."""
        buckets: list[list[np.ndarray]] = [[] for _ in range(seconds)]
        with self._lock:
            for t, c in self.chunks:
                i = int(t)
                if 0 <= i < seconds:
                    buckets[i].append(c)
        out: list[float | None] = []
        for b in buckets:
            if not b:
                out.append(None)  # nada chegou nesse segundo
                continue
            x = np.concatenate(b)
            rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
            out.append(20 * np.log10(rms) if rms > 1e-9 else -120.0)
        return out


def to_mono16k(block: np.ndarray, sr: int) -> np.ndarray:
    x = block.astype(np.float32)
    if x.ndim == 2:
        x = x.mean(axis=1)
    if sr != SR:
        x = soxr.resample(x, sr, SR)
    return x


# --------------------------------------------------------------- fontes


def run_mic(track: Track, t0: float, stop: threading.Event) -> None:
    """Igual ao Artemis: sounddevice, dispositivo padrao, 16 kHz int16."""
    import sounddevice as sd

    def cb(indata, frames, time_info, status):
        if status:
            track.errors.append(str(status))
        x = np.frombuffer(bytes(indata), dtype=np.int16).astype(np.float32) / 32768
        track.push(time.monotonic() - t0, x)

    try:
        with sd.RawInputStream(samplerate=SR, channels=1, dtype="int16", callback=cb,
                               blocksize=int(SR * BLOCK_S)):
            stop.wait()
    except Exception as exc:
        track.errors.append(f"abrir: {exc}")


def run_soundcard(track: Track, t0: float, stop: threading.Event) -> None:
    import soundcard as sc

    try:
        speaker = sc.default_speaker()
        lb = sc.get_microphone(id=str(speaker.name), include_loopback=True)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with lb.recorder(samplerate=LOOP_SR, blocksize=int(LOOP_SR * BLOCK_S)) as rec:
                while not stop.is_set():
                    data = rec.record(numframes=int(LOOP_SR * BLOCK_S))
                    track.push(time.monotonic() - t0, to_mono16k(data, LOOP_SR))
            kinds = {}
            for w in caught:
                kinds[str(w.message)[:60]] = kinds.get(str(w.message)[:60], 0) + 1
            for msg, n in kinds.items():
                track.errors.append(f"warning x{n}: {msg}")
    except Exception as exc:
        track.errors.append(f"erro: {exc!r}")


def run_pyaudiowpatch(track: Track, t0: float, stop: threading.Event) -> None:
    import pyaudiowpatch as pyaudio

    p = pyaudio.PyAudio()
    try:
        lb = p.get_default_wasapi_loopback()
        ch = lb["maxInputChannels"]
        sr = int(lb["defaultSampleRate"])

        def cb(in_data, frame_count, time_info, status):
            if status:
                track.errors.append(f"status {status}")
            x = np.frombuffer(in_data, dtype=np.float32).reshape(-1, ch)
            track.push(time.monotonic() - t0, to_mono16k(x, sr))
            return (None, pyaudio.paContinue)

        stream = p.open(format=pyaudio.paFloat32, channels=ch, rate=sr, input=True,
                        input_device_index=lb["index"],
                        frames_per_buffer=int(sr * BLOCK_S), stream_callback=cb)
        stream.start_stream()
        stop.wait()
        stream.stop_stream()
        stream.close()
    except Exception as exc:
        track.errors.append(f"erro: {exc!r}")
    finally:
        p.terminate()


def speak(text: str) -> None:
    """Voz sintetica do Windows na saida padrao (preferindo pt-BR)."""
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'pt-BR' } | Select-Object -First 1;"
        "if ($v) { $s.SelectVoice($v.VoiceInfo.Name) };"
        f"$s.Speak('{text}')"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)


# ----------------------------------------------------------------- main


def save_wav(path: Path, x: np.ndarray) -> None:
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def fmt_db(v: float | None) -> str:
    if v is None:
        return "  ---"
    return f"{v:5.0f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=20)
    ap.add_argument("--tts-at", type=float, default=5.0, help="quando comeca a voz")
    ap.add_argument("--no-tts", action="store_true")
    ap.add_argument("--no-mic", action="store_true", help="testa so o loopback (sem HFP)")
    ap.add_argument("--tag", default="", help="sufixo dos WAVs")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    stop = threading.Event()
    t0 = time.monotonic()
    tracks = {
        "loop_soundcard": Track("loop_soundcard"),
        "loop_pyaudiowpatch": Track("loop_pyaudiowpatch"),
    }
    runners = [(run_soundcard, tracks["loop_soundcard"]),
               (run_pyaudiowpatch, tracks["loop_pyaudiowpatch"])]
    if not args.no_mic:
        tracks = {"mic": Track("mic"), **tracks}
        runners.insert(0, (run_mic, tracks["mic"]))

    threads = [threading.Thread(target=fn, args=(tr, t0, stop), daemon=True)
               for fn, tr in runners]
    for th in threads:
        th.start()

    tts_window = None
    if not args.no_tts:
        time.sleep(args.tts_at)
        ts = time.monotonic() - t0
        tts = threading.Thread(target=speak, args=(TTS_TEXT,), daemon=True)
        tts.start()
        tts.join(timeout=args.seconds)
        tts_window = (ts, time.monotonic() - t0)

    remaining = args.seconds - (time.monotonic() - t0)
    if remaining > 0:
        time.sleep(remaining)
    stop.set()
    for th in threads:
        th.join(timeout=3)
    total = int(np.ceil(time.monotonic() - t0))

    # ------------------------------------------------------- relatorio
    print(f"\nNivel por segundo (dBFS; '---' = nenhuma amostra chegou nesse segundo)")
    if tts_window:
        print(f"voz sintetica tocando de {tts_window[0]:.1f}s a {tts_window[1]:.1f}s")
    names = list(tracks)
    print("  seg " + "".join(f"{n:>20}" for n in names))
    table = {n: tracks[n].rms_by_second(total) for n in names}
    for s in range(total):
        mark = "*" if tts_window and tts_window[0] <= s + 0.5 <= tts_window[1] else " "
        print(f" {mark}{s:3d} " + "".join(f"{fmt_db(table[n][s]):>20}" for n in names))

    print("\nEntregas")
    for n, tr in tracks.items():
        x = tr.samples()
        gaps = np.array(tr.gaps) if tr.gaps else np.zeros(1)
        print(f"  {n:<20} {len(x) / SR:6.1f}s de audio em {total}s | "
              f"blocos={len(tr.chunks):4d} | intervalo max={gaps.max():.2f}s")
        for e in tr.errors[:5]:
            print(f"      ! {e}")
        if len(x):
            save_wav(OUT / f"{n}{args.tag}.wav", x)
    print(f"\nWAVs em {OUT}")


if __name__ == "__main__":
    sys.exit(main())
