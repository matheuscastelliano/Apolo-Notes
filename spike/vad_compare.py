"""Fase 0: Silero (pysilero-vad) x WebRTC VAD, e um rascunho do chunker.

    .venv\\Scripts\\python spike\\vad_compare.py [arquivo.wav]   # padrao: spike/out/sample.wav

Para cada VAD: custo de CPU, intervalos de fala detectados e os trechos que
o chunker hibrido mandaria para a API (corta na 1a pausa depois do alvo,
corta em pausa longa, forca o corte no maximo).
"""

from __future__ import annotations

import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SR = 16000
DEFAULT = Path(__file__).parent / "out" / "sample.wav"


# ------------------------------------------------------------------ VADs


def silero_frames(pcm: np.ndarray) -> tuple[float, list[float]]:
    """Probabilidade de fala por frame de 512 amostras (32 ms)."""
    from pysilero_vad import SileroVoiceActivityDetector

    vad = SileroVoiceActivityDetector()
    n = 512
    probs = []
    for i in range(0, len(pcm) - n + 1, n):
        probs.append(vad(pcm[i:i + n].tobytes()))
    return n / SR, probs


def webrtc_frames(pcm: np.ndarray, mode: int = 2) -> tuple[float, list[float]]:
    """Fala (0/1) por frame de 480 amostras (30 ms)."""
    import webrtcvad

    vad = webrtcvad.Vad(mode)
    n = 480
    out = []
    for i in range(0, len(pcm) - n + 1, n):
        out.append(1.0 if vad.is_speech(pcm[i:i + n].tobytes(), SR) else 0.0)
    return n / SR, out


# --------------------------------------------------------------- chunker


@dataclass
class Chunk:
    start: float
    end: float
    speech: float  # segundos de fala dentro do trecho


class Chunker:
    """Rascunho do chunker da fase 2: alimentado frame a frame, sem I/O.

    Estados: fora de fala -> em trecho. Dentro do trecho conta o silencio
    corrente; decide cortar quando:
      - trecho >= alvo e silencio >= pausa_curta  (corta na 1a pausa apos o alvo)
      - silencio >= pausa_longa                    (fim natural de fala)
      - trecho >= maximo                           (forca)
    Trechos com menos de min_fala de fala sao descartados.
    """

    def __init__(self, frame_s: float, *, on: float = 0.5, off: float = 0.35,
                 target: float = 12.0, maximum: float = 30.0,
                 short_pause: float = 0.3, long_pause: float = 1.5,
                 min_speech: float = 0.4, pad: float = 0.2):
        self.f = frame_s
        self.on, self.off = on, off
        self.target, self.maximum = target, maximum
        self.short_pause, self.long_pause = short_pause, long_pause
        self.min_speech, self.pad = min_speech, pad
        self.t = 0.0
        self.in_chunk = False
        self.speaking = False
        self.start = 0.0
        self.silence = 0.0
        self.speech = 0.0
        self.chunks: list[Chunk] = []
        self.speech_intervals: list[tuple[float, float]] = []
        self._sp_start = 0.0

    def feed(self, prob: float) -> None:
        # histerese: entra em fala com `on`, sai com `off`
        was = self.speaking
        self.speaking = prob >= (self.off if was else self.on)
        if self.speaking and not was:
            self._sp_start = self.t
        if was and not self.speaking:
            self.speech_intervals.append((self._sp_start, self.t))

        if self.speaking:
            if not self.in_chunk:
                self.in_chunk = True
                self.start = max(0.0, self.t - self.pad)
                self.speech = 0.0
            self.silence = 0.0
            self.speech += self.f
        elif self.in_chunk:
            self.silence += self.f

        if self.in_chunk:
            length = self.t + self.f - self.start
            if (length >= self.target and self.silence >= self.short_pause) \
                    or self.silence >= self.long_pause \
                    or length >= self.maximum:
                self._emit(self.t + self.f - max(0.0, self.silence - self.pad))
        self.t += self.f

    def flush(self) -> None:
        if self.speaking:
            self.speech_intervals.append((self._sp_start, self.t))
        if self.in_chunk:
            self._emit(self.t)

    def _emit(self, end: float) -> None:
        if self.speech >= self.min_speech:
            self.chunks.append(Chunk(self.start, end, self.speech))
        self.in_chunk = False
        self.silence = 0.0


# ------------------------------------------------------------------ main


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert w.getframerate() == SR and w.getnchannels() == 1 and w.getsampwidth() == 2, \
            "esperava WAV 16 kHz mono PCM16"
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    pcm = read_wav(path)
    dur = len(pcm) / SR
    print(f"{path.name}: {dur:.1f}s\n")

    for name, fn in (("silero", silero_frames), ("webrtc(2)", webrtc_frames)):
        t0 = time.process_time()
        frame_s, probs = fn(pcm)
        cpu = time.process_time() - t0
        ch = Chunker(frame_s)
        for p in probs:
            ch.feed(p)
        ch.flush()
        speech_total = sum(b - a for a, b in ch.speech_intervals)
        sent = sum(c.end - c.start for c in ch.chunks)
        print(f"== {name}: CPU {cpu:.2f}s para {dur:.0f}s de audio "
              f"({cpu / dur * 100:.1f}% de um nucleo)")
        print(f"   fala detectada {speech_total:.1f}s em {len(ch.speech_intervals)} intervalos")
        print(f"   trechos enviados: {len(ch.chunks)}, somando {sent:.1f}s "
              f"({sent / dur * 100:.0f}% do audio)")
        for c in ch.chunks:
            print(f"     {c.start:6.2f} - {c.end:6.2f}s  ({c.end - c.start:5.1f}s, fala {c.speech:4.1f}s)")
        print()


if __name__ == "__main__":
    main()
