"""--simulate: uma reuniao de mentira a partir de um WAV, com a API de verdade.

    python -m apolo --simulate reuniao.wav [--speed 4]

Para desenvolver sem precisar de uma reuniao: o WAV entra como o canal
"Outros" (loopback) e o mic fica em silencio. Passa pelo mesmo caminho do
app - VAD, chunker, spool, fila de STT, nota ao vivo, resumo com o preset
padrao - e escreve a nota na pasta configurada. `--speed` acelera a leitura
do arquivo; os tempos na nota continuam sendo os do audio.

Custa o mesmo que transcrever o WAV (gpt-transcribe, ~US$ 0,0045/min) mais
um resumo (centavos).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np

from ..app import _queue_detail
from ..audio.cli import read_wav_16k
from ..audio.resample import SAMPLE_RATE
from ..errors import ApoloError
from ..i18n import t
from ..secrets_store import require_api_key
from .session import READY, MeetingSession, SessionStatus
from .summary import FinalizeChoice

BLOCK = SAMPLE_RATE // 10


class WavSource:
    """Fonte que le um array em blocos de 100 ms, no ritmo `speed`x."""

    def __init__(self, channel: str, on_block, samples: np.ndarray, speed: float = 1.0):
        self.channel = channel
        self.on_block = on_block
        self.samples = samples
        self.speed = max(0.1, float(speed))
        self.first_block = threading.Event()
        self.first_block_at: float | None = None
        self.error = None
        self.device_name = channel
        self.done = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"wav-{self.channel}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        t_start = time.monotonic()
        sent = 0
        while sent < len(self.samples) and not self._stop.is_set():
            block = self.samples[sent:sent + BLOCK]
            sent += len(block)
            # Hora "de chegada" coerente com as amostras, mesmo acelerado.
            self.on_block(t_start + sent / SAMPLE_RATE, block)
            if self.first_block_at is None:
                self.first_block_at = time.monotonic()
                self.first_block.set()
            self._stop.wait(len(block) / SAMPLE_RATE / self.speed)
        self.done.set()
        self._stop.wait()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


def cmd_simulate(config: dict, presets: list, path: str, speed: float = 1.0) -> int:
    from ..providers.openai_provider import OpenAIProvider

    try:
        others = read_wav_16k(Path(path))
        provider = OpenAIProvider(
            api_key=require_api_key(),
            stt_model=config["stt_model"],
            text_model=config["text_model"],
            timeout=float(config.get("request_timeout", 60)),
        )
    except ApoloError as exc:
        print(exc)
        return 1

    sources: dict[str, WavSource] = {}

    def factory(channel, on_block, cfg):
        samples = others if channel == "sys" else np.zeros_like(others)
        sources[channel] = WavSource(channel, on_block, samples, speed)
        return sources[channel]

    last = {"line": ""}

    def on_status(s: SessionStatus) -> None:
        line = f"[{s.phase}] {_queue_detail(s)}".rstrip()
        if line != last["line"]:
            last["line"] = line
            print(line, flush=True)

    wanted = config.get("default_preset")
    preset = next((p for p in presets if p.id == wanted), presets[0])
    session = MeetingSession(config, preset, provider, on_status=on_status, source_factory=factory)
    try:
        session.start()
        print(t("cli.simulate.note", path=session.writer.path), flush=True)
        while not all(src.done.is_set() for src in sources.values()):
            time.sleep(0.2)
        note = session.stop()
        if session.phase == READY:
            # Com --speed, o relogio mediu menos que o audio: vale o do WAV.
            session.meta.duration_seconds = len(others) / SAMPLE_RATE
            # Sem janela de parada: resume com o preset padrao.
            note = session.finalize(FinalizeChoice(preset), provider)
            if session.summary_error is not None:
                print(session.summary_error)
    except KeyboardInterrupt:
        session.abort()
        return 130
    except ApoloError as exc:
        print(exc)
        return 1
    print(t("cli.simulate.done", path=note))
    return 0
