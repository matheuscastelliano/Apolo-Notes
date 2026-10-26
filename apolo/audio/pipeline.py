"""Um canal do pipeline: blocos com hora de chegada -> VAD -> chunker.

Recebe o que as fontes entregam (sources.py): (t_monotonic, float32 16 kHz
mono). Cuida da unica parte com relogio: os timestamps dos trechos vem do
relogio monotonico da sessao, nao so da contagem de amostras, porque um
stream pode ficar um tempo sem entregar nada (troca de perfil do Bluetooth,
`data discontinuity` do soundcard). Enquanto as duas contagens concordam,
vale a de amostras, que nao tem jitter; quando se separam mais que
RESYNC_SECONDS, o chunker fecha o trecho aberto e recomeca na hora certa.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from .chunker import Chunk, Chunker
from .resample import SAMPLE_RATE, float_to_int16
from .vad import Vad

log = logging.getLogger(__name__)

# Diferenca entre o relogio e a contagem de amostras que conta como buraco.
RESYNC_SECONDS = 0.5


class ChannelPipeline:
    def __init__(self, channel: str, config: dict[str, Any], t0: float,
                 vad: Vad | None = None):
        self.channel = channel
        self.t0 = t0
        self.vad = vad or Vad()
        self.chunker = Chunker.from_config(channel, config)
        self._started = False
        self.resyncs = 0
        self.speech_seconds = 0.0

    def push(self, t_arrival: float, block: np.ndarray) -> list[Chunk]:
        """Um bloco que chegou em `t_arrival` (time.monotonic())."""
        if not len(block):
            return []
        out: list[Chunk] = []
        # O bloco termina quando chega; comeca `len` amostras antes.
        block_start = t_arrival - self.t0 - len(block) / SAMPLE_RATE
        expected = self.chunker.t + self.vad.buffered / SAMPLE_RATE
        drift = block_start - expected
        if not self._started:
            self._started = True
            out += self._resync(max(0.0, block_start))
        elif drift > RESYNC_SECONDS or (
            drift < -RESYNC_SECONDS and not self.chunker.in_chunk
        ):
            # Para frente: amostras se perderam. Para tras (o stream atrasou e
            # despejou tudo de uma vez): so corrige fora de um trecho.
            log.info("Canal %s: relogio ajustado em %+.2fs", self.channel, drift)
            self.resyncs += 1
            out += self._resync(block_start)

        for frame, speech in self.vad.process(float_to_int16(block)):
            if speech:
                self.speech_seconds += len(frame) / SAMPLE_RATE
            out += self.chunker.feed(frame, speech)
        return out

    def close(self) -> list[Chunk]:
        return self.chunker.flush()

    def _resync(self, t: float) -> list[Chunk]:
        self.vad.reset()
        return self.chunker.resync(t)
