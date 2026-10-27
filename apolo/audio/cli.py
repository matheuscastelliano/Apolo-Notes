"""Comandos de linha de comando da fase 2, para desenvolver sem reuniao.

    python -m apolo --devices                 # mics e saidas (loopback), por nome
    python -m apolo --test-capture [20]       # grava mic + loopback e mostra os cortes
    python -m apolo --from-wav arquivo.wav    # passa um WAV pelo VAD + chunker

Nenhum deles chama a API. O --test-capture passa pelo spool de verdade e
apaga a sessao no fim (o audio e temporario); --keep-spool guarda os WAVs
para ouvir onde o chunker cortou.
"""

from __future__ import annotations

import logging
import threading
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np

from ..errors import ApoloError, AudioError
from ..i18n import t
from ..session.spool import Spool
from .chunker import Chunk
from .pipeline import ChannelPipeline
from .resample import SAMPLE_RATE, int16_to_float, to_mono16k
from .sources import MicSource, SystemSource, list_devices

log = logging.getLogger(__name__)

# Quanto esperar o 1o bloco de cada fonte antes de desistir dela.
FIRST_BLOCK_TIMEOUT = 5.0
# O --from-wav alimenta o pipeline em blocos do mesmo tamanho das fontes.
BLOCK_SAMPLES = SAMPLE_RATE // 10


def _clock(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return f"{int(seconds // 60):02d}:{seconds % 60:04.1f}"


def _chunk_line(c: Chunk) -> str:
    return (f"{c.channel:<6} {_clock(c.start):>8} {c.duration:>8.1f}s "
            f"{c.speech:>6.1f}s {c.level_dbfs():>5.0f} dB")


def _summary(chunks: list[Chunk], total: float) -> str:
    if not chunks:
        return t("cli.chunks.none")
    sent = sum(c.duration for c in chunks)
    return t("cli.chunks.summary", count=len(chunks), sent=sent, total=total,
             pct=sent / total * 100 if total > 0 else 0)


# ----------------------------------------------------------------- devices


def cmd_devices(config: dict[str, Any]) -> int:
    try:
        devices = list_devices()
    except ApoloError as exc:
        print(exc)
        return 1
    for title, items in ((t("cli.devices.inputs"), devices["inputs"]),
                         (t("cli.devices.outputs"), devices["outputs"])):
        print(title)
        for d in items:
            print(f"  {'*' if d['default'] else ' '} {d['name']}")
        print()
    print(t("cli.devices.legend"))
    print(t("cli.devices.configured",
            mic=config.get("mic_device") or t("cli.default"),
            loopback=config.get("loopback_device") or t("cli.default")))
    return 0


# ---------------------------------------------------------------- from-wav


def read_wav_16k(path: Path) -> np.ndarray:
    """WAV PCM16 de qualquer taxa/canais -> float32 16 kHz mono."""
    try:
        with wave.open(str(path)) as w:
            sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
    except (OSError, EOFError, wave.Error) as exc:
        raise AudioError(t("err.wav_read"), str(exc)) from exc
    if width != 2:
        raise AudioError(t("err.wav_format"), f"{width * 8} bits")
    x = int16_to_float(np.frombuffer(raw, dtype=np.int16))
    if ch > 1:
        x = x.reshape(-1, ch)
    return to_mono16k(x, sr)


def chunk_samples(config: dict[str, Any], x: np.ndarray, channel: str = "mic") -> list[Chunk]:
    """Passa audio 16 kHz pelo pipeline, com o relogio vindo das amostras."""
    pipeline = ChannelPipeline(channel, config, t0=0.0)
    chunks: list[Chunk] = []
    for i in range(0, len(x), BLOCK_SAMPLES):
        block = x[i:i + BLOCK_SAMPLES]
        chunks += pipeline.push((i + len(block)) / SAMPLE_RATE, block)
    chunks += pipeline.close()
    return chunks


def cmd_from_wav(config: dict[str, Any], path: str) -> int:
    try:
        x = read_wav_16k(Path(path))
        total = len(x) / SAMPLE_RATE
        print(t("cli.from_wav.file", name=Path(path).name, seconds=total))
        chunks = chunk_samples(config, x)
    except ApoloError as exc:
        print(exc)
        return 1
    print(t("cli.chunks.header"))
    for c in chunks:
        print(_chunk_line(c))
    print(_summary(chunks, total))
    return 0


# ------------------------------------------------------------ test-capture


def cmd_test_capture(config: dict[str, Any], seconds: float, keep_spool: bool = False) -> int:
    try:
        spool = Spool.from_config(config)
        session = spool.create_session(kind="test-capture")
    except ApoloError as exc:
        print(exc)
        return 1

    t0 = time.monotonic()
    lock = threading.Lock()
    chunks: list[Chunk] = []
    try:
        pipelines = {ch: ChannelPipeline(ch, config, t0) for ch in ("mic", "sys")}
    except ApoloError as exc:
        print(exc)
        session.delete()
        return 1

    def handle(new: list[Chunk]) -> None:
        for c in new:
            try:
                session.add(c.channel, c.start, c.pcm)
            except ApoloError as exc:
                log.error("Spool: %s", exc)
            with lock:
                chunks.append(c)
                print(_chunk_line(c), flush=True)

    def feeder(channel: str):
        return lambda t_arrival, block: handle(pipelines[channel].push(t_arrival, block))

    sources = [
        MicSource(feeder("mic"), config.get("mic_device")),
        SystemSource(feeder("sys"), config.get("loopback_device")),
    ]

    print(t("cli.capture.preparing"), flush=True)
    for src in sources:
        src.start()
    # "Gravando" so depois que as duas fontes entregarem o 1o bloco.
    deadline = t0 + FIRST_BLOCK_TIMEOUT
    for src in sources:
        while not src.first_block.is_set() and src.running and time.monotonic() < deadline:
            src.first_block.wait(0.05)
    alive = []
    for src in sources:
        if src.error is not None:
            print(t("cli.capture.channel_error", channel=src.channel, error=src.error))
        elif not src.first_block.is_set():
            print(t("cli.capture.no_first_block", channel=src.channel,
                    seconds=FIRST_BLOCK_TIMEOUT))
        else:
            alive.append(src)
    if not alive:
        for src in sources:
            src.stop()
        session.delete()
        return 1

    print(t("cli.capture.recording", seconds=seconds), flush=True)
    print(t("cli.chunks.header"), flush=True)
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end and any(s.running for s in alive):
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    for src in sources:
        src.stop()
    for ch, pipeline in pipelines.items():
        handle(pipeline.close())
    total = time.monotonic() - t0

    print()
    with lock:
        print(_summary(sorted(chunks, key=lambda c: c.start), total))
    for src in sources:
        if src.error is not None:
            print(t("cli.capture.channel_error", channel=src.channel, error=src.error))
        first = (f"{src.first_block_at - t0:.1f}s" if src.first_block_at is not None
                 else "---")
        p = pipelines[src.channel]
        print(t("cli.capture.channel_stats", channel=src.channel,
                device=src.device_name or "?", first=first,
                speech=p.speech_seconds, resyncs=p.resyncs))

    if keep_spool:
        print(t("cli.capture.spool_kept", path=session.seg_dir))
    else:
        session.delete()
        print(t("cli.capture.spool_deleted"))
    return 0
