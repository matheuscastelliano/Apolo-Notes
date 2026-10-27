"""Um canal do pipeline: blocos com hora de chegada -> VAD -> chunker.

Recebe o que as fontes entregam (sources.py): (t_monotonic, float32 16 kHz
mono). Cuida da unica parte com relogio: os timestamps dos trechos vem do
relogio monotonico da sessao, nao so da contagem de amostras, porque um
stream pode ficar um tempo sem entregar nada (troca de perfil do Bluetooth,
`data discontinuity` do soundcard). Enquanto as duas contagens concordam,
vale a de amostras, que nao tem jitter; quando se separam mais que
RESYNC_SECONDS, o chunker fecha o trecho aberto e recomeca na hora certa.

Atrasado nao e o mesmo que perdido. Com o PC sob carga (compartilhar a tela
numa call), a thread de captura pode passar meio segundo sem rodar; o audio
fica no buffer do driver e chega todo de uma vez. O 1o bloco desse lote
parece "atrasado", mas os seguintes chegam colados e o atraso some. Por isso
um bloco atrasado nao ajusta o relogio na hora: ele e os seguintes ficam
segurados ate o atraso sumir (era so entrega atrasada: processa normal) ou
persistir por CONFIRM_SECONDS (audio perdido de verdade: ajusta pelo menor
atraso visto e processa).
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
# Quanto tempo um atraso precisa durar para ser buraco, e nao entrega atrasada.
CONFIRM_SECONDS = 1.0


class ChannelPipeline:
    def __init__(self, channel: str, config: dict[str, Any], t0: float,
                 vad: Vad | None = None):
        self.channel = channel
        self.t0 = t0
        self.vad = vad or Vad()
        self.chunker = Chunker.from_config(channel, config)
        self._started = False
        # Blocos segurados enquanto um atraso nao se confirma:
        # (t_arrival, block_start, block).
        self._held: list[tuple[float, float, np.ndarray]] = []
        self.resyncs = 0
        self.late_deliveries = 0  # atrasos que eram so entrega atrasada
        self.speech_seconds = 0.0

    def push(self, t_arrival: float, block: np.ndarray) -> list[Chunk]:
        """Um bloco que chegou em `t_arrival` (time.monotonic())."""
        if not len(block):
            return []
        # O bloco termina quando chega; comeca `len` amostras antes.
        block_start = t_arrival - self.t0 - len(block) / SAMPLE_RATE
        if not self._started:
            self._started = True
            return self._resync(max(0.0, block_start)) + self._process(block)
        if self._held:
            self._held.append((t_arrival, block_start, block))
            return self._settle(t_arrival)

        drift = block_start - self._expected()
        if drift > RESYNC_SECONDS:
            self._held.append((t_arrival, block_start, block))
            return self._settle(t_arrival)
        out: list[Chunk] = []
        if drift < -RESYNC_SECONDS and not self.chunker.in_chunk:
            # O stream adiantou (despejou mais que o tempo real): so corrige
            # fora de um trecho, para nao cortar uma frase.
            out += self._adjust(drift)
        return out + self._process(block)

    def close(self) -> list[Chunk]:
        out = self._settle(final=True) if self._held else []
        return out + self.chunker.flush()

    # ----------------------------------------------------------------- interno

    def _expected(self) -> float:
        """Onde, pela contagem de amostras, o proximo bloco deveria comecar."""
        return self.chunker.t + self.vad.buffered / SAMPLE_RATE

    def _settle(self, now: float = 0.0, final: bool = False) -> list[Chunk]:
        """Decide o que fazer com os blocos segurados."""
        expected = self._expected()
        drifts = []
        for _, block_start, block in self._held:
            drifts.append(block_start - expected)
            expected += len(block) / SAMPLE_RATE
        if drifts[-1] <= RESYNC_SECONDS:
            # Os blocos alcancaram o relogio: nada se perdeu.
            self.late_deliveries += 1
            log.debug("Canal %s: entrega atrasada %.2fs, sem perda",
                      self.channel, max(drifts))
            return self._release()
        if not final and now - self._held[0][0] < CONFIRM_SECONDS:
            return []
        # O atraso persistiu: audio perdido. O menor atraso visto e o
        # tamanho do buraco; o resto era jitter de entrega.
        return self._adjust(min(drifts)) + self._release()

    def _adjust(self, drift: float) -> list[Chunk]:
        log.info("Canal %s: relogio ajustado em %+.2fs", self.channel, drift)
        self.resyncs += 1
        return self._resync(self._expected() + drift)

    def _release(self) -> list[Chunk]:
        held, self._held = self._held, []
        out: list[Chunk] = []
        for _, _, block in held:
            out += self._process(block)
        return out

    def _process(self, block: np.ndarray) -> list[Chunk]:
        out: list[Chunk] = []
        for frame, speech in self.vad.process(float_to_int16(block)):
            if speech:
                self.speech_seconds += len(frame) / SAMPLE_RATE
            out += self.chunker.feed(frame, speech)
        return out

    def _resync(self, t: float) -> list[Chunk]:
        self.vad.reset()
        return self.chunker.resync(t)
