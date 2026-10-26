"""Conversao para o formato unico do pipeline: 16 kHz, mono.

Tudo depois das fontes (VAD, chunker, spool, STT) trabalha em 16 kHz mono.
O loopback chega em 48 kHz estereo; o mic ja nasce em 16 kHz.

Para audio ao vivo use `Resampler`: ele guarda o estado do filtro entre um
bloco e outro. Reamostrar cada bloco de 100 ms isoladamente cria um clique
na emenda de cada bloco - dez por segundo.
"""

from __future__ import annotations

import numpy as np
import soxr

SAMPLE_RATE = 16000


def to_mono(block: np.ndarray) -> np.ndarray:
    """(frames,) ou (frames, canais) -> (frames,) float32."""
    x = np.asarray(block, dtype=np.float32)
    if x.ndim == 2:
        x = x.mean(axis=1) if x.shape[1] > 1 else x[:, 0]
    return np.ascontiguousarray(x, dtype=np.float32)


def to_mono16k(block: np.ndarray, sample_rate: int) -> np.ndarray:
    """Conversao de uma vez so (arquivo inteiro). Nao use em streaming."""
    x = to_mono(block)
    if sample_rate != SAMPLE_RATE and len(x):
        x = soxr.resample(x, sample_rate, SAMPLE_RATE).astype(np.float32, copy=False)
    return x


def int16_to_float(pcm: np.ndarray) -> np.ndarray:
    return pcm.astype(np.float32) / 32768.0


def float_to_int16(x: np.ndarray) -> np.ndarray:
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16)


class Resampler:
    """Reamostrador de streaming: blocos de `sample_rate` -> 16 kHz mono."""

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self._stream = (
            soxr.ResampleStream(sample_rate, SAMPLE_RATE, 1, dtype="float32")
            if sample_rate != SAMPLE_RATE
            else None
        )

    def process(self, block: np.ndarray) -> np.ndarray:
        x = to_mono(block)
        if self._stream is None:
            return x
        return self._stream.resample_chunk(x)

    def reset(self) -> None:
        """Descarta o estado do filtro (depois de um buraco no audio)."""
        if self._stream is not None:
            self._stream.clear()
