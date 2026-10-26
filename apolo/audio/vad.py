"""Deteccao de fala com o Silero VAD (pysilero-vad, ggml, sem torch).

Escolhido na fase 0 (spike/RESULTADOS.md): o WebRTC VAD marcava ruido como
fala, e trecho so de ruido e exatamente o que faz o STT alucinar.

O Silero trabalha em janelas fixas de 512 amostras (32 ms a 16 kHz). Este
wrapper aceita blocos de qualquer tamanho, guarda a sobra para a proxima
chamada e devolve uma decisao fala/silencio por janela, ja com histerese:
entra em fala com prob >= 0,5 e so sai quando cai abaixo de 0,35. Sem a
histerese, uma silaba mais fraca no meio da frase vira uma "pausa".
"""

from __future__ import annotations

import numpy as np

from ..errors import AudioError
from ..i18n import t

FRAME_SAMPLES = 512
SPEECH_ON = 0.5
SPEECH_OFF = 0.35


class Vad:
    def __init__(self, on: float = SPEECH_ON, off: float = SPEECH_OFF):
        try:
            from pysilero_vad import SileroVoiceActivityDetector

            self._model = SileroVoiceActivityDetector()
        except Exception as exc:
            raise AudioError(t("err.vad_load"), str(exc)) from exc
        self.on, self.off = on, off
        self.speaking = False
        self._rest = np.zeros(0, dtype=np.int16)

    def process(self, pcm: np.ndarray) -> list[tuple[np.ndarray, bool]]:
        """Bloco int16 16 kHz mono -> [(janela de 512 amostras, e_fala), ...]."""
        x = np.concatenate([self._rest, pcm.astype(np.int16, copy=False)])
        out = []
        n = len(x) - len(x) % FRAME_SAMPLES
        for i in range(0, n, FRAME_SAMPLES):
            frame = x[i:i + FRAME_SAMPLES]
            prob = self._model(frame.tobytes())
            self.speaking = prob >= (self.off if self.speaking else self.on)
            out.append((frame, self.speaking))
        self._rest = x[n:].copy()
        return out

    @property
    def buffered(self) -> int:
        """Amostras guardadas esperando completar uma janela."""
        return len(self._rest)

    def reset(self) -> None:
        """Depois de um buraco no audio: esquece a sobra e o estado do modelo."""
        self._rest = np.zeros(0, dtype=np.int16)
        self.speaking = False
        self._model.reset()
