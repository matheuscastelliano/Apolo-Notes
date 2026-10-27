"""Chunker com fala/silencio sinteticos: janelas de 512 amostras ja marcadas.

Nao passa pelo VAD de verdade - o chunker so ve (janela, e_fala) - entao os
testes sao deterministicos e rapidos.
"""

from __future__ import annotations

import numpy as np
import pytest

from apolo.audio.chunker import Chunk, Chunker
from apolo.audio.pipeline import ChannelPipeline

SR = 16000
FRAME = 512
F = FRAME / SR  # 32 ms
TOL = 2 * F  # tolerancia de duas janelas


def speech_frame() -> np.ndarray:
    return np.full(FRAME, 1000, dtype=np.int16)


def silence_frame() -> np.ndarray:
    return np.zeros(FRAME, dtype=np.int16)


def run(chunker: Chunker, pattern: list[tuple[float, bool]], flush: bool = True) -> list[Chunk]:
    """pattern = [(segundos, e_fala), ...]"""
    out: list[Chunk] = []
    for seconds, speech in pattern:
        for _ in range(int(round(seconds / F))):
            out += chunker.feed(speech_frame() if speech else silence_frame(), speech)
    if flush:
        out += chunker.flush()
    return out


def check_invariants(chunks: list[Chunk], maximum: float = 30.0) -> None:
    for c in chunks:
        assert c.duration == pytest.approx(len(c.pcm) / SR)
        assert c.duration <= maximum + F
        assert c.pcm.dtype == np.int16
    for a, b in zip(chunks, chunks[1:]):
        assert b.start >= a.end - 1e-9, "trechos do mesmo canal nao se sobrepoem"


def test_silence_only_produces_nothing():
    assert run(Chunker("mic"), [(60, False)]) == []


def test_long_pause_cuts_with_padding():
    ch = Chunker("mic")
    chunks = run(ch, [(1, False), (3, True), (2, False), (3, True), (2, False)])
    assert len(chunks) == 2
    a, b = chunks
    # margem de 0,2 s antes e depois da fala
    assert a.start == pytest.approx(1.0 - 0.2, abs=TOL)
    assert a.end == pytest.approx(4.0 + 0.2, abs=TOL)
    assert b.start == pytest.approx(6.0 - 0.2, abs=TOL)
    assert b.end == pytest.approx(9.0 + 0.2, abs=TOL)
    assert a.speech == pytest.approx(3.0, abs=TOL)
    assert a.channel == "mic"
    check_invariants(chunks)


def test_long_pause_emits_without_waiting_for_flush():
    ch = Chunker("sys")
    chunks = run(ch, [(3, True), (1.6, False)], flush=False)
    assert len(chunks) == 1
    assert chunks[0].channel == "sys"


def test_short_pause_before_target_does_not_cut():
    chunks = run(Chunker("mic"), [(5, True), (0.5, False), (5, True), (2, False)])
    assert len(chunks) == 1
    assert chunks[0].duration == pytest.approx(10.5 + 0.2, abs=TOL)


def test_cuts_at_first_short_pause_after_target():
    # Pausa aos 10 s (antes do alvo de 12 s) nao corta; a de 15,35 s corta.
    pattern = [(10, True), (0.35, False), (5, True), (0.35, False), (3, True), (2, False)]
    chunks = run(Chunker("mic"), pattern)
    assert len(chunks) == 2
    a, b = chunks
    assert a.start == pytest.approx(0.0, abs=TOL)
    assert a.end == pytest.approx(15.35 + 0.2, abs=TOL)
    assert b.start >= a.end
    assert b.end == pytest.approx(15.70 + 3 + 0.2, abs=TOL)
    check_invariants(chunks)


def test_forced_cut_at_maximum():
    chunks = run(Chunker("mic"), [(70, True)])
    assert [round(c.duration) for c in chunks] == [30, 30, 10]
    # corte forcado no meio da fala: um trecho emenda no outro
    for a, b in zip(chunks, chunks[1:]):
        assert b.start == pytest.approx(a.end, abs=1e-6)
    check_invariants(chunks)


def test_discards_chunks_with_too_little_speech():
    # 0,2 s de "fala" (clique, tosse) no meio do silencio: nada vai para o STT.
    assert run(Chunker("mic"), [(2, False), (0.2, True), (3, False)]) == []


def test_short_utterance_is_completed_to_minimum():
    # "Sim." de 0,5 s: passa da fala minima e e completado ate 1 s com o
    # silencio real que veio depois.
    chunks = run(Chunker("mic"), [(2, False), (0.5, True), (3, False)])
    assert len(chunks) == 1
    c = chunks[0]
    assert c.start == pytest.approx(2.0 - 0.2, abs=TOL)
    assert c.duration == pytest.approx(1.0, abs=TOL)


def test_flush_emits_open_chunk_trimming_trailing_silence():
    chunks = run(Chunker("mic"), [(4, True), (1, False)])
    assert len(chunks) == 1
    assert chunks[0].end == pytest.approx(4.0 + 0.2, abs=TOL)


def test_resync_closes_chunk_and_moves_clock():
    ch = Chunker("mic")
    first = run(ch, [(2, True)], flush=False)
    assert first == []
    closed = ch.resync(10.0)
    assert len(closed) == 1 and closed[0].end <= 2.0 + F
    chunks = run(ch, [(2, True), (2, False)])
    assert len(chunks) == 1
    assert chunks[0].start == pytest.approx(10.0, abs=TOL)


def test_resync_never_goes_before_last_chunk():
    ch = Chunker("mic")
    run(ch, [(3, True), (2, False)])
    last_end = ch.last_end
    ch.resync(0.5)
    assert ch.t == pytest.approx(last_end)


def test_from_config_reads_chunk_keys():
    config = {"chunk_target_seconds": 5, "chunk_max_seconds": 8,
              "chunk_short_pause_seconds": 0.3, "chunk_long_pause_seconds": 1.5,
              "chunk_min_speech_seconds": 0.4, "chunk_pad_seconds": 0.0,
              "chunk_min_seconds": 0}
    ch = Chunker.from_config("mic", config)
    chunks = run(ch, [(6, True), (0.4, False), (20, True)])
    assert chunks[0].end == pytest.approx(6.0, abs=TOL)  # pausa depois do alvo, sem margem
    assert all(c.duration <= 8 + F for c in chunks)


def test_from_config_survives_bad_values():
    ch = Chunker.from_config("mic", {"chunk_target_seconds": "doze"})
    assert len(run(ch, [(3, True), (2, False)])) == 1


def test_level_dbfs():
    c = run(Chunker("mic"), [(3, True), (2, False)])[0]
    assert -40 < c.level_dbfs() < -25  # 1000/32768 ~ -30 dBFS, com a margem em silencio


# ------------------------------------------------- pipeline (relogio)


class FakeVad:
    """VAD de mentira: fala = amostras altas. Mesma interface do vad.Vad."""

    def __init__(self):
        self._rest = np.zeros(0, dtype=np.int16)

    @property
    def buffered(self) -> int:
        return len(self._rest)

    def process(self, pcm):
        x = np.concatenate([self._rest, pcm])
        n = len(x) - len(x) % FRAME
        out = [(x[i:i + FRAME], bool(np.abs(x[i:i + FRAME]).mean() > 100))
               for i in range(0, n, FRAME)]
        self._rest = x[n:]
        return out

    def reset(self):
        self._rest = np.zeros(0, dtype=np.int16)


def push_blocks(p: ChannelPipeline, t_start: float, seconds: float, speech: bool):
    out = []
    block = 1600
    value = 0.05 if speech else 0.0
    for i in range(int(round(seconds * SR / block))):
        t_arrival = t_start + (i + 1) * block / SR
        out += p.push(t_arrival, np.full(block, value, dtype=np.float32))
    return out


def test_pipeline_uses_session_clock_across_gaps():
    # t0 = 100 (monotonic). O stream comeca 1 s atrasado (Bluetooth) e some
    # por 3 s no meio: os trechos seguem o relogio, nao a contagem de amostras.
    p = ChannelPipeline("mic", {}, t0=100.0, vad=FakeVad())
    chunks = push_blocks(p, 101.0, 2, True)
    chunks += push_blocks(p, 103.0, 2, False)
    chunks += push_blocks(p, 108.0, 2, True)
    chunks += push_blocks(p, 110.0, 2, False)
    chunks += p.close()
    assert len(chunks) == 2
    assert chunks[0].start == pytest.approx(1.0, abs=TOL)
    assert chunks[1].start == pytest.approx(8.0 - 0.0, abs=TOL)  # sem margem: resync zera o pre-roll
    assert p.resyncs == 1
    check_invariants(chunks)


def test_late_delivery_does_not_move_the_clock():
    # Pico de CPU: a captura fica 0,8 s sem rodar e o driver entrega o lote
    # de uma vez. Nada se perdeu - o relogio nao pode pular nem cortar a fala.
    p = ChannelPipeline("mic", {}, t0=100.0, vad=FakeVad())
    chunks = push_blocks(p, 100.0, 3, True)
    block = 1600
    stalled = 103.0 + 0.9  # os 8 blocos de 103.0 a 103.8 chegam juntos aqui
    for i in range(8):
        chunks += p.push(stalled + i * 0.001, np.full(block, 0.05, dtype=np.float32))
    chunks += push_blocks(p, 103.8, 2, True)
    chunks += push_blocks(p, 105.8, 2, False)
    chunks += p.close()
    assert p.resyncs == 0
    assert p.late_deliveries == 1
    assert len(chunks) == 1  # a fala de 0 a 5,8 s continua um trecho so
    assert chunks[0].start == pytest.approx(0.0, abs=TOL)
    check_invariants(chunks)


def test_gap_at_the_end_is_adjusted_on_close():
    # O stream volta depois de um buraco e a sessao para logo em seguida,
    # antes de o atraso se confirmar: o close ainda ajusta o relogio.
    p = ChannelPipeline("mic", {}, t0=100.0, vad=FakeVad())
    chunks = push_blocks(p, 100.0, 2, False)
    chunks += push_blocks(p, 105.0, 0.5, True)
    assert p.resyncs == 0  # ainda segurando
    chunks += p.close()
    assert p.resyncs == 1
    assert len(chunks) == 1
    assert chunks[0].start == pytest.approx(5.0, abs=TOL)
