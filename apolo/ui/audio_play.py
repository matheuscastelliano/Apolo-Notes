"""Toca uma amostra de voz (o botao ▶ do "Quem e quem" e da aba Vozes).

Usa o sounddevice, que o Apolo ja usa para o microfone e que funciona no
Linux tambem. Tocar e um extra: qualquer erro so vai para o log.
"""

from __future__ import annotations

import logging

import numpy as np

from ..session.spool import SAMPLE_RATE

log = logging.getLogger(__name__)


def play(pcm: np.ndarray, sample_rate: int = SAMPLE_RATE) -> None:
    """Toca sem bloquear. Um play novo interrompe o anterior."""
    try:
        import sounddevice

        sounddevice.stop()
        sounddevice.play(np.asarray(pcm, dtype=np.int16), sample_rate)
    except Exception as exc:
        log.warning("Nao consegui tocar a amostra: %s", exc)


def stop() -> None:
    try:
        import sounddevice

        sounddevice.stop()
    except Exception:
        pass
