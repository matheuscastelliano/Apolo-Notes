"""Fase 3: done.jsonl, fila de STT, nota ao vivo e a sessao de ponta a ponta.

Tudo sem rede e sem placa de som: transcritor fake e fontes fake.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from apolo.audio.chunker import Chunk
from apolo.errors import CredentialsError, OfflineError, TranscriptionError
from apolo.notes import render
from apolo.notes.render import NOTES_END, NOTES_START, NoteMeta
from apolo.notes.writer import NoteWriter, unique_path
from apolo.presets import SummaryPreset
from apolo.providers.base import Gap, Segment
from apolo.session import session as session_module
from apolo.providers.base import SummaryResult
from apolo.session.session import DONE, ERROR, READY, RECORDING, MeetingSession
from apolo.session.spool import Spool
from apolo.session.summary import FinalizeChoice
from apolo.session.transcript import TranscriptStore, spool_gaps
from apolo.session.workers import SttQueue

SR = 16000


pytestmark = pytest.mark.usefixtures("portuguese")


def pcm(seconds: float) -> np.ndarray:
    return np.full(int(seconds * SR), 500, dtype=np.int16)


class FakeTranscriber:
    """Devolve 'fala N'; `fail` e uma lista de excecoes a levantar antes."""

    def __init__(self, fail=None):
        self.fail = list(fail or [])
        self.calls = []
        self._n = 0
        self._lock = threading.Lock()

    def transcribe(self, wav, *, prompt="", keywords=None, languages=None):
        with self._lock:
            self.calls.append({"prompt": prompt, "keywords": keywords, "languages": languages})
            if self.fail:
                raise self.fail.pop(0)
            self._n += 1
            return f"fala {self._n}"


# --------------------------------------------------------------- store


def test_store_persists_and_reloads(tmp_path):
    store = TranscriptStore(tmp_path)
    store.add(Segment(5.0, 7.0, "others", "oi"))
    store.add(Segment(1.0, 3.0, "me", "bom dia"))
    again = TranscriptStore(tmp_path)
    assert [s.text for s in again.segments()] == ["bom dia", "oi"]


def test_store_ignores_truncated_line(tmp_path):
    store = TranscriptStore(tmp_path)
    store.add(Segment(1.0, 2.0, "me", "inteira"))
    with store.path.open("a", encoding="utf-8") as f:
        f.write('{"start": 3.0, "end"')  # crash no meio da escrita
    assert [s.text for s in TranscriptStore(tmp_path).segments()] == ["inteira"]


def test_store_last_text_is_same_speaker_and_earlier(tmp_path):
    store = TranscriptStore(tmp_path)
    store.add(Segment(1.0, 2.0, "me", "primeira"))
    store.add(Segment(3.0, 4.0, "others", "deles"))
    store.add(Segment(5.0, 6.0, "me", "segunda"))
    assert store.last_text("me", before=5.0) == "primeira"
    assert store.last_text("me", before=9.0) == "segunda"
    assert store.last_text("others", before=1.0) == ""


def test_visible_hides_what_comes_after_the_oldest_pending(tmp_path):
    store = TranscriptStore(tmp_path)
    store.add(Segment(1.0, 2.0, "me", "a"))
    store.add(Segment(10.0, 11.0, "others", "c"))
    gaps = [Gap(4.0, 6.0, "offline")]
    visible = store.visible(gaps, pending_from=8.0)
    assert [e.start for e in visible] == [1.0, 4.0]
    assert [e.start for e in store.visible(gaps, None)] == [1.0, 4.0, 10.0]


def test_spool_gap_reasons_are_translated_codes():
    gaps = spool_gaps([{"start": 1, "end": 2, "reason": "spool_full"},
                       {"start": 3, "end": 4, "reason": "error"}, {"bad": 1}])
    assert [(g.start, g.reason) for g in gaps] == [(1.0, "overflow"), (3.0, "error")]


# --------------------------------------------------------------- fila


def make_queue(tmp_path, transcriber, **kw):
    spool = Spool(tmp_path / "spool").create_session()
    store = TranscriptStore(spool.dir)
    q = SttQueue(transcriber, spool, store, keywords=["deploy"], languages=["pt", "en"],
                 backoff_initial=0.01, backoff_max=0.05, **kw)
    return spool, store, q


def test_queue_transcribes_everything_in_order(tmp_path):
    fake = FakeTranscriber()
    spool, store, q = make_queue(tmp_path, fake, workers=1)
    for start in (0.0, 5.0, 10.0):
        q.submit(spool.add("mic", start, pcm(1)))
    q.start()
    assert q.wait_idle(timeout=5)
    q.stop()
    assert [s.text for s in store.segments()] == ["fala 1", "fala 2", "fala 3"]
    assert spool.pending() == []  # audio apagado depois de transcrito
    # O texto anterior do mesmo canal vira o prompt do proximo trecho.
    assert fake.calls[1]["prompt"] == "fala 1"
    assert fake.calls[0]["languages"] == ["pt", "en"]


def test_queue_survives_going_offline(tmp_path):
    fake = FakeTranscriber(fail=[OfflineError("sem rede")] * 3)
    spool, store, q = make_queue(tmp_path, fake)
    q.submit(spool.add("sys", 0.0, pcm(1)))
    q.submit(spool.add("mic", 2.0, pcm(1)))
    q.start()
    assert q.wait_idle(timeout=5)
    q.stop()
    assert len(store.segments()) == 2
    assert q.offline is False  # voltou
    assert spool.pending() == []
    assert spool.gaps() == []


def test_queue_stops_on_credentials_error_and_keeps_audio(tmp_path):
    fake = FakeTranscriber(fail=[CredentialsError("chave invalida")])
    spool, store, q = make_queue(tmp_path, fake, workers=1)
    q.submit(spool.add("mic", 0.0, pcm(1)))
    q.start()
    assert q.wait_idle(timeout=2) is False
    assert isinstance(q.fatal, CredentialsError)
    q.stop()
    assert len(spool.pending()) == 1  # nada se perdeu
    assert store.segments() == []


def test_queue_turns_other_errors_into_gaps(tmp_path):
    fake = FakeTranscriber(fail=[TranscriptionError("audio invalido")])
    spool, store, q = make_queue(tmp_path, fake, workers=1)
    q.submit(spool.add("mic", 0.0, pcm(1)))
    q.submit(spool.add("mic", 3.0, pcm(1)))
    q.start()
    assert q.wait_idle(timeout=5)
    q.stop()
    assert [s.text for s in store.segments()] == ["fala 1"]
    assert [(g["start"], g["reason"]) for g in spool.gaps()] == [(0.0, "error")]
    assert spool.pending() == []


def test_queue_resumes_what_was_left_in_the_spool(tmp_path):
    spool, store, q = make_queue(tmp_path, FakeTranscriber())
    spool.add("mic", 0.0, pcm(1))
    spool.add("sys", 1.0, pcm(1))
    assert q.submit_pending() == 2
    q.start()
    assert q.wait_idle(timeout=5)
    q.stop()
    assert len(store.segments()) == 2


# --------------------------------------------------------------- nota


def meta(start=None) -> NoteMeta:
    from datetime import datetime, timezone

    return NoteMeta(start=start or datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc),
                    preset_id="daily", preset_name="Daily")


def test_writer_keeps_what_the_user_typed(tmp_path):
    m = meta()
    writer = NoteWriter(tmp_path, m.start)
    assert writer.path.name == "2026-10-27 1430 - Gravando.md"
    writer.write_live(m, [Segment(1, 2, "me", "oi")])

    text = writer.path.read_text(encoding="utf-8")
    start = text.index(NOTES_START) + len(NOTES_START)
    end = text.index(NOTES_END)
    writer.path.write_text(text[:start] + "\nlembrar do orçamento\n" + text[end:], encoding="utf-8")

    writer.write_live(m, [Segment(1, 2, "me", "oi"), Segment(3, 4, "others", "tchau")])
    text = writer.path.read_text(encoding="utf-8")
    assert "lembrar do orçamento" in text
    assert "tchau" in text
    assert not list(tmp_path.glob("*.tmp"))


def test_writer_never_overwrites_an_existing_note(tmp_path):
    m = meta()
    first = NoteWriter(tmp_path, m.start)
    first.write_live(m, [])
    second = NoteWriter(tmp_path, m.start)
    assert second.path != first.path
    assert second.path.name == "2026-10-27 1430 - Gravando (2).md"


def test_writer_rename_for_final_title(tmp_path):
    m = meta()
    writer = NoteWriter(tmp_path, m.start)
    writer.write_live(m, [])
    new = writer.rename_for(m.start, "Daily do time: deploy")
    assert new.name == render.note_filename(m.start, "Daily do time: deploy")
    assert new.exists() and not (tmp_path / "2026-10-27 1430 - Gravando.md").exists()


def test_unique_path(tmp_path):
    (tmp_path / "a.md").write_text("x")
    (tmp_path / "a (2).md").write_text("x")
    assert unique_path(tmp_path, "a.md").name == "a (3).md"


# ------------------------------------------------------------- sessao


class FakeSource:
    """Entrega `blocks` (float32) no ritmo de amostras, sem placa de som."""

    def __init__(self, channel, on_block, blocks, fail=None):
        self.channel = channel
        self.on_block = on_block
        self.blocks = blocks
        self.first_block = threading.Event()
        self.error = fail
        self.device_name = f"fake-{channel}"
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        if self.error is not None:
            return
        t_start = time.monotonic()

        def run():
            n = 0
            for block in self.blocks:
                if self._stop.is_set():
                    return
                n += len(block)
                self.on_block(t_start + n / SR, block)
                self.first_block.set()
            self._stop.wait()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self, timeout=3.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()


class FakePipeline:
    """Sem VAD: cada bloco nao silencioso vira um trecho (os cortes de verdade
    ja sao testados em test_chunker.py)."""

    def __init__(self, channel, config, t0, vad=None):
        self.channel = channel
        self.t = 0.0
        self.speech_seconds = 0.0

    def push(self, t_arrival, block):
        start, self.t = self.t, self.t + len(block) / SR
        if not np.any(block):
            return []
        self.speech_seconds += len(block) / SR
        pcm16 = (block * 32767).astype(np.int16)
        return [Chunk(self.channel, start, self.t, len(block) / SR, pcm16)]

    def close(self):
        return []


PRESET = SummaryPreset(id="daily", name="Daily", system_prompt="x")


class FakeSummarizer:
    def __init__(self, fail=None, title="Daily do time"):
        self.fail = fail
        self.title = title
        self.requests = []

    def summarize(self, request):
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        return SummaryResult(title=self.title, tags=["daily"],
                             body_markdown="## Resumo\nTudo certo.\n\n## Próximos passos\n- [ ] **Ana**: deploy")


def run_session(config, monkeypatch, fake, mic_blocks, sys_blocks, sys_fail=None,
                summarizer=None, choice=None):
    monkeypatch.setattr(session_module, "ChannelPipeline", FakePipeline)
    statuses = []

    def factory(channel, on_block, cfg):
        if channel == "mic":
            return FakeSource("mic", on_block, mic_blocks)
        return FakeSource("sys", on_block, sys_blocks, fail=sys_fail)

    s = MeetingSession(config, PRESET, fake, on_status=statuses.append,
                       source_factory=factory, backoff_initial=0.01)
    s.start()
    assert s.phase == RECORDING
    time.sleep(0.3)  # deixa as fontes entregarem tudo
    s.stop()
    assert s.phase == READY
    path = s.finalize(choice or FinalizeChoice(PRESET), summarizer or FakeSummarizer())
    return s, path, statuses


def speech(seconds=1.0):
    return np.full(int(seconds * SR), 0.1, dtype=np.float32)


def silence(seconds=1.0):
    return np.zeros(int(seconds * SR), dtype=np.float32)


def test_session_end_to_end(config, monkeypatch):
    fake = FakeTranscriber(fail=[OfflineError("caiu")])
    s, path, statuses = run_session(
        config, monkeypatch, fake,
        mic_blocks=[speech(), silence(), speech()],
        sys_blocks=[silence(), speech(), silence()],
    )
    assert s.phase == DONE
    text = path.read_text(encoding="utf-8")
    # Tres falas, na ordem do tempo, com o falante certo.
    lines = [line for line in text.splitlines() if line.startswith("> **[")]
    assert [line.split("]")[1].split(":")[0].strip(" *") for line in lines] == ["Eu", "Outros", "Eu"]
    assert text.count("fala ") == 3
    # Nota final: titulo do resumo no nome, resumo no corpo, transcricao recolhida.
    assert path.name == render.note_filename(s.started_wall, "Daily do time")
    assert "Tudo certo." in text and "> [!note]- " in text
    assert not list(path.parent.glob("*Gravando*"))
    assert not s.spool.dir.exists()  # o audio era temporario
    phases = [st.phase for st in statuses]
    assert phases[0] == "preparing" and RECORDING in phases and READY in phases
    assert phases[-1] == DONE


def test_session_records_with_one_channel_down(config, monkeypatch):
    from apolo.errors import AudioError

    s, path, statuses = run_session(
        config, monkeypatch, FakeTranscriber(),
        mic_blocks=[speech()], sys_blocks=[], sys_fail=AudioError("sem loopback"),
    )
    assert s.phase == DONE
    assert any("sys" in st.channel_problems for st in statuses)
    assert "fala 1" in path.read_text(encoding="utf-8")


def test_session_without_any_audio_fails_and_leaves_nothing(config, monkeypatch, tmp_path):
    from apolo.errors import AudioError

    monkeypatch.setattr(session_module, "ChannelPipeline", FakePipeline)

    def factory(channel, on_block, cfg):
        return FakeSource(channel, on_block, [], fail=AudioError(f"{channel} quebrado"))

    s = MeetingSession(config, PRESET, FakeTranscriber(), on_status=lambda st: None,
                       source_factory=factory)
    with pytest.raises(AudioError):
        s.start()
    assert s.phase == ERROR
    assert list((tmp_path / "spool").iterdir()) == []
    assert not list((tmp_path / "notas").glob("*.md"))
