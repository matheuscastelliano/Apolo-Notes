"""Chunker hibrido guiado por VAD: decide onde cortar o audio de um canal.

Logica pura, sem I/O e sem VAD dentro: recebe janelas ja classificadas como
fala/silencio (ver vad.py) e devolve os trechos prontos para o spool. Por
isso da para testar com fala e silencio sinteticos (tests/test_chunker.py).

Regras (fase 0, rascunho em spike/vad_compare.py), dentro de um trecho:
  - trecho >= alvo (12 s) e pausa >= pausa curta (0,3 s) -> corta
    (a 1a pausa depois do alvo: trechos de tamanho bom para o STT)
  - pausa >= pausa longa (1,5 s)                        -> corta (fim de fala)
  - trecho >= maximo (30 s)                             -> corta a forca
Ao cortar, o trecho fica com `pad` (0,2 s) de silencio de cada lado, para
nao comer a borda das palavras. Trecho com menos de `min_speech` (0,4 s) de
fala e descartado: e clique, tosse ou ruido, e vira alucinacao no STT.
Trecho muito curto (< `min_seconds`) e completado com o audio real que veio
depois dele, ate o minimo - a API rejeita audio curto demais.

Tempo: o chunker conta amostras a partir do ultimo `resync(t)`. Quem chama
(pipeline.py) faz o resync quando o relogio da sessao e a contagem de
amostras se separam, por exemplo quando o Bluetooth troca de perfil e o
stream fica 1 s sem entregar nada.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

from .resample import SAMPLE_RATE

CHANNELS = ("mic", "sys")


@dataclass
class Chunk:
    channel: str  # "mic" (Eu) | "sys" (Outros)
    start: float  # segundos desde o inicio da sessao
    end: float
    speech: float  # segundos de fala dentro do trecho
    pcm: np.ndarray  # int16, 16 kHz, mono

    @property
    def duration(self) -> float:
        return self.end - self.start

    def level_dbfs(self) -> float:
        """Nivel RMS do trecho inteiro, em dBFS."""
        if not len(self.pcm):
            return -120.0
        x = self.pcm.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(x * x)))
        return 20 * np.log10(rms) if rms > 1e-6 else -120.0


class Chunker:
    def __init__(
        self,
        channel: str,
        *,
        sample_rate: int = SAMPLE_RATE,
        target: float = 12.0,
        maximum: float = 30.0,
        min_seconds: float = 1.0,
        short_pause: float = 0.3,
        long_pause: float = 1.5,
        min_speech: float = 0.4,
        pad: float = 0.2,
    ):
        self.channel = channel
        self.sr = sample_rate
        s = lambda seconds: int(round(float(seconds) * sample_rate))  # noqa: E731
        self._target = s(target)
        # Um maximo menor que o alvo nao faz sentido; vale o maior dos dois.
        self._max = max(s(maximum), self._target)
        self._min = s(min_seconds)
        self._short = s(short_pause)
        self._long = s(long_pause)
        self._min_speech = s(min_speech)
        self._pad = s(pad)

        self._t = 0.0  # tempo de sessao da proxima amostra
        self._last_end = 0.0  # fim do ultimo trecho emitido
        self._in_chunk = False
        # (tempo de sessao, janela). _buf = trecho aberto; _pre = o pouco de
        # silencio antes da fala, que vira a margem inicial do proximo trecho.
        self._buf: list[tuple[float, np.ndarray]] = []
        self._buf_samples = 0
        self._pre: deque[tuple[float, np.ndarray]] = deque()
        self._pre_samples = 0
        self._silence = 0  # amostras de silencio seguidas no fim do trecho
        self._speech = 0  # amostras de fala no trecho

    @classmethod
    def from_config(cls, channel: str, config: dict[str, Any]) -> "Chunker":
        def get(key: str, default: float) -> float:
            try:
                return float(config.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            channel,
            target=get("chunk_target_seconds", 12.0),
            maximum=get("chunk_max_seconds", 30.0),
            min_seconds=get("chunk_min_seconds", 1.0),
            short_pause=get("chunk_short_pause_seconds", 0.3),
            long_pause=get("chunk_long_pause_seconds", 1.5),
            min_speech=get("chunk_min_speech_seconds", 0.4),
            pad=get("chunk_pad_seconds", 0.2),
        )

    # -------------------------------------------------------------- estado

    @property
    def t(self) -> float:
        """Tempo de sessao (s) da proxima amostra esperada."""
        return self._t

    @property
    def in_chunk(self) -> bool:
        return self._in_chunk

    @property
    def last_end(self) -> float:
        return self._last_end

    # --------------------------------------------------------------- feed

    def feed(self, frame: np.ndarray, speech: bool) -> list[Chunk]:
        """Uma janela de audio (int16) ja classificada pelo VAD."""
        n = len(frame)
        if not n:
            return []
        item = (self._t, frame)
        self._t += n / self.sr

        if speech:
            if not self._in_chunk:
                self._open()
            self._silence = 0
            self._speech += n
        elif not self._in_chunk:
            self._pre.append(item)
            self._pre_samples += n
            self._trim_pre()
            return []
        else:
            self._silence += n
        self._buf.append(item)
        self._buf_samples += n

        length = self._buf_samples
        if (length >= self._target and self._silence >= self._short) \
                or self._silence >= self._long \
                or length >= self._max:
            return self._emit()
        return []

    def resync(self, t: float) -> list[Chunk]:
        """Salto no relogio: fecha o trecho aberto e passa a contar de `t`."""
        out = self.flush()
        self._pre.clear()
        self._pre_samples = 0
        # Nunca volta para antes do fim do ultimo trecho: os trechos de um
        # canal nao podem se sobrepor.
        self._t = max(float(t), self._last_end)
        return out

    def flush(self) -> list[Chunk]:
        """Fim do audio: emite o trecho aberto, se houver."""
        return self._emit() if self._in_chunk else []

    # ------------------------------------------------------------ interno

    def _open(self) -> None:
        self._in_chunk = True
        self._buf = list(self._pre)
        self._buf_samples = self._pre_samples
        self._pre.clear()
        self._pre_samples = 0
        self._speech = 0
        self._silence = 0

    def _trim_pre(self) -> None:
        # Mantem o minimo de janelas que cobre `pad`.
        while self._pre and self._pre_samples - len(self._pre[0][1]) >= self._pad:
            self._pre_samples -= len(self._pre.popleft()[1])

    def _emit(self) -> list[Chunk]:
        # Silencio no fim alem da margem fica de fora do trecho...
        drop = max(0, self._silence - self._pad)
        keep = self._buf_samples - drop
        # ...a nao ser que o trecho fique curto demais: ai completa com o
        # audio real que veio depois, ate o minimo.
        keep = max(keep, min(self._min, self._buf_samples))

        kept: list[tuple[float, np.ndarray]] = []
        total = 0
        rest_from = len(self._buf)
        for i, (ts, frame) in enumerate(self._buf):
            if total >= keep:
                rest_from = i
                break
            kept.append((ts, frame))
            total += len(frame)
        rest = self._buf[rest_from:]

        speech = self._speech
        self._in_chunk = False
        self._buf = []
        self._buf_samples = 0
        self._silence = 0
        self._speech = 0
        # O silencio que sobrou e a margem inicial do proximo trecho.
        for item in rest:
            self._pre.append(item)
            self._pre_samples += len(item[1])
        self._trim_pre()

        if not kept or speech < self._min_speech:
            return []
        pcm = np.concatenate([f for _, f in kept]).astype(np.int16, copy=False)
        start = kept[0][0]
        end = start + len(pcm) / self.sr
        self._last_end = end
        return [Chunk(self.channel, start, end, speech / self.sr, pcm)]
