"""Vozes com API fake: janelas, identificacao e a sessao de ponta a ponta."""

from __future__ import annotations

import threading
import time
from datetime import datetime

import numpy as np
import pytest

from apolo.errors import CredentialsError, DiarizationError, OfflineError
from apolo.providers.base import DiarSegment, Segment
from apolo.session import session as session_module
from apolo.session.session import DONE, READY, RECORDING, MeetingSession
from apolo.session.summary import FinalizeChoice
from apolo.voices.identify import build_review
from apolo.voices.library import VoiceLibrary
from apolo.voices.tracker import JOIN_SILENCE, VoiceTracker, wav_to_pcm
from test_session import PRESET, FakePipeline, FakeSource, FakeSummarizer, FakeTranscriber

SR = 16000

pytestmark = pytest.mark.usefixtures("portuguese")


def tone(seconds: float, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (np.sin(2 * np.pi * freq * t) * 8000).astype(np.int16)


def wav_seconds(wav: bytes) -> float:
    return len(wav_to_pcm(wav)) / SR


class FakeDiarizer:
    """`script(n, seconds, references)` -> segmentos da n-esima chamada."""

    def __init__(self, script, fail=None):
        self.script = script
        self.fail = list(fail or [])
        self.calls = []
        self._lock = threading.Lock()

    def diarize(self, wav, *, references=None, languages=None):
        with self._lock:
            n = len(self.calls)
            self.calls.append({"seconds": wav_seconds(wav), "refs": dict(references or {}),
                               "languages": languages})
            if self.fail:
                raise self.fail.pop(0)
        return self.script(n, wav_seconds(wav), references or {})


def wait(tracker, timeout=5.0):
    assert tracker.wait_idle(timeout=timeout)


# --------------------------------------------------------------- janelas


def two_windows(n, seconds, refs):
    if n == 0:  # 1a janela: duas vozes, sem referencia
        return [DiarSegment(0, 4, "A"), DiarSegment(4.4, 8.4, "B")]
    # 2a (3 s + 3 s + 4 s): as duas conhecidas voltam pelo nome; uma nova aparece
    return [DiarSegment(0, 3, "V2"), DiarSegment(3.4, 6.4, "V1"), DiarSegment(6.8, 10.8, "A")]


def test_windows_keep_voices_across_windows(tmp_path):
    fake = FakeDiarizer(two_windows)
    tr = VoiceTracker(fake, tmp_path, window_seconds=8, languages=["pt"])
    tr.start()
    tr.add(10.0, tone(4))
    tr.add(20.0, tone(4, 330))  # 8 s de fala: fecha a 1a janela
    wait(tr)
    tr.add(40.0, tone(3, 330))
    tr.add(50.0, tone(3))
    tr.add(60.0, tone(4, 440))
    wait(tr)
    tr.stop()

    assert fake.calls[0]["refs"] == {}
    assert fake.calls[0]["seconds"] == pytest.approx(8 + 2 * JOIN_SILENCE, abs=0.01)
    assert set(fake.calls[1]["refs"]) == {"V1", "V2"}
    assert fake.calls[1]["languages"] == ["pt"]
    # Tempos de volta no relogio da sessao, por trecho.
    timeline = [(round(s.start, 1), round(s.end, 1), s.speaker) for s in tr.timeline()]
    assert timeline == [(10.0, 14.0, "V1"), (20.0, 24.0, "V2"),
                        (40.0, 43.0, "V2"), (50.0, 53.0, "V1"), (60.0, 64.0, "V3")]
    voices = {v.id: v for v in tr.voice_list()}
    assert list(voices) == ["V1", "V2", "V3"]
    assert voices["V1"].seconds == pytest.approx(7.0, abs=0.1)
    assert voices["V1"].clip_seconds == pytest.approx(4.0, abs=0.01)
    assert len(tr.clip_pcm("V3")) == 4 * SR
    # Os trechos sairam do disco; o resultado ficou.
    assert not list(tr.dir.glob("chunk_*.wav"))
    again = VoiceTracker(fake, tmp_path)
    assert [s.speaker for s in again.timeline()] == ["V1", "V2", "V2", "V1", "V3"]
    assert {v.id for v in again.voice_list()} == {"V1", "V2", "V3"}


def test_at_most_four_references_most_recent_first(tmp_path):
    def script(n, seconds, refs):
        if n == 0:
            return [DiarSegment(i * 4.4, i * 4.4 + 4, letter) for i, letter in enumerate("ABCDE")]
        return [DiarSegment(0, 4, "V1")]

    fake = FakeDiarizer(script)
    tr = VoiceTracker(fake, tmp_path, window_seconds=20)
    tr.start()
    for i in range(5):
        tr.add(i * 10.0, tone(4, 200 + 50 * i))
    wait(tr)
    tr.add(100.0, tone(4))
    tr.flush()
    wait(tr)
    tr.stop()
    assert sorted(fake.calls[1]["refs"]) == ["V2", "V3", "V4", "V5"]  # V1 falou primeiro


def test_paused_tracker_waits_and_flush_sends_the_rest(tmp_path):
    fake = FakeDiarizer(lambda n, s, r: [DiarSegment(0, s, "A")])
    tr = VoiceTracker(fake, tmp_path, window_seconds=4)
    tr.set_paused(True)
    tr.start()
    tr.add(0.0, tone(5))
    time.sleep(0.3)
    assert fake.calls == []
    tr.set_paused(False)  # voltou a ter 2+ pessoas: a janela cheia sai
    wait(tr)
    tr.add(10.0, tone(1))
    tr.flush()
    wait(tr)
    tr.stop()
    assert len(fake.calls) == 2


def test_transient_failure_retries_and_other_failures_skip(tmp_path):
    fake = FakeDiarizer(lambda n, s, r: [DiarSegment(0, s, "A")],
                        fail=[OfflineError("caiu"), DiarizationError("ruim")])
    tr = VoiceTracker(fake, tmp_path, window_seconds=4, backoff_initial=0.01)
    tr.start()
    tr.add(0.0, tone(4))   # 1a: cai, tenta de novo, falha de vez -> pulada
    wait(tr)
    tr.add(10.0, tone(4))  # 2a: funciona
    wait(tr)
    tr.stop()
    assert len(fake.calls) == 3
    assert [round(s.start) for s in tr.timeline()] == [10]
    assert not list(tr.dir.glob("chunk_*.wav"))


def test_credentials_error_stops_separating(tmp_path):
    fake = FakeDiarizer(lambda n, s, r: [], fail=[CredentialsError("sem credito")])
    tr = VoiceTracker(fake, tmp_path, window_seconds=4)
    tr.start()
    tr.add(0.0, tone(4))
    wait(tr)
    tr.add(10.0, tone(4))
    wait(tr)
    tr.stop()
    assert isinstance(tr.failed, CredentialsError)
    assert len(fake.calls) == 1


def test_chunks_left_on_disk_are_picked_up_after_a_crash(tmp_path):
    tr = VoiceTracker(FakeDiarizer(lambda n, s, r: []), tmp_path, window_seconds=100)
    tr.add(5.0, tone(3))
    fake = FakeDiarizer(lambda n, s, r: [DiarSegment(0, s, "A")])
    again = VoiceTracker(fake, tmp_path, window_seconds=100)
    again.start()
    again.flush()
    wait(again)
    again.stop()
    assert [(round(s.start), round(s.end)) for s in again.timeline()] == [(5, 8)]


# --------------------------------------------------------- identificacao


def tracker_with(tmp_path, voices: dict[str, float]):
    """Tracker ja com vozes e amostras (sem passar pela API)."""
    order = list(voices)

    def script(n, seconds, refs):
        out, t = [], 0.0
        for letter in order:
            out.append(DiarSegment(t, t + voices[letter], letter))
            t += voices[letter] + JOIN_SILENCE
        return out

    tr = VoiceTracker(FakeDiarizer(script), tmp_path / "sess", window_seconds=1000)
    tr.start()
    for i, letter in enumerate(order):
        tr.add(i * 20.0, tone(voices[letter], 200 + 60 * i))
    tr.flush()
    wait(tr)
    tr.stop()
    return tr


def entries_for(tr):
    return [Segment(s.start, s.end, "others", f"fala de {s.speaker}", s.speaker)
            for s in tr.timeline()]


def naming(mapping):
    """Fake da identificacao: cada amostra (na ordem) volta com o nome dado.
    Na conferencia (referencias V1, V2...), volta com a voz de `back`."""

    def script(n, seconds, refs):
        names = mapping["suggest"] if not any(k.startswith("V") for k in refs) else mapping["back"]
        out, t = [], 0.0
        for clip_seconds, name in zip(mapping["clips"], names):
            out.append(DiarSegment(t, t + clip_seconds, name))
            t += clip_seconds + 1.0
        return out

    return script


def test_full_library_names_everyone_automatically(tmp_path):
    tr = tracker_with(tmp_path, {"A": 5, "B": 6})
    lib = VoiceLibrary(tmp_path / "lib")
    lib.add_sample("Marcos", tone(5))
    lib.add_sample("Fernando", tone(6))
    fake = FakeDiarizer(naming({"clips": [5, 6], "suggest": ["Fernando", "Marcos"],
                                "back": ["V2", "V1"]}))
    review = build_review(tr, entries_for(tr), ["Marcos", "Fernando"], "Matheus", lib, fake)
    assert review.auto is True
    assert review.suggested() == {"V1": "Fernando", "V2": "Marcos"}
    assert [v.label for v in review.voices] == ["Voz A", "Voz B"]
    assert review.voices[0].sample_text == "fala de V1"
    assert len(fake.calls) == 2  # sugestao + conferencia


def test_missing_person_turns_everything_into_suggestions(tmp_path):
    # O caso da fase V0: so o Fernando na biblioteca, o modelo "da" o nome
    # dele ao Marcos.
    tr = tracker_with(tmp_path, {"A": 5, "B": 6})
    lib = VoiceLibrary(tmp_path / "lib")
    lib.add_sample("Fernando", tone(6))
    fake = FakeDiarizer(naming({"clips": [5, 6], "suggest": ["Fernando", "A"], "back": []}))
    review = build_review(tr, entries_for(tr), ["Marcos", "Fernando"], "", lib, fake)
    assert review.auto is False
    assert review.suggested() == {"V1": "Fernando"}
    assert review.names == ["Marcos", "Fernando"]


def test_reverse_check_catches_a_borrowed_name(tmp_path):
    # Ana estava na lista e na biblioteca, mas nao falou: o modelo empresta o
    # nome dela para a voz B. A conferencia ao contrario nao bate.
    tr = tracker_with(tmp_path, {"A": 5, "B": 6})
    lib = VoiceLibrary(tmp_path / "lib")
    lib.add_sample("Marcos", tone(5))
    lib.add_sample("Ana", tone(6, 500))
    fake = FakeDiarizer(naming({"clips": [5, 6], "suggest": ["Marcos", "Ana"],
                                "back": ["V1", "C"]}))
    review = build_review(tr, entries_for(tr), ["Marcos", "Ana"], "", lib, fake)
    assert review.auto is False
    assert review.suggested() == {"V1": "Marcos", "V2": "Ana"}


def test_nothing_to_review(tmp_path):
    tr = tracker_with(tmp_path, {"A": 5, "B": 1})  # B fala 1 s: nao conta
    lib = VoiceLibrary(tmp_path / "lib")
    fake = FakeDiarizer(lambda n, s, r: [])
    assert build_review(tr, [], [], "", lib, fake) is None
    review = build_review(tr, [], ["Ana", "Bia"], "", lib, fake)
    assert [v.id for v in review.voices] == ["V1"] and fake.calls == []


def test_identification_failure_keeps_the_review_without_suggestions(tmp_path):
    tr = tracker_with(tmp_path, {"A": 5, "B": 6})
    lib = VoiceLibrary(tmp_path / "lib")
    lib.add_sample("Marcos", tone(5))
    fake = FakeDiarizer(lambda n, s, r: [], fail=[OfflineError("caiu")])
    review = build_review(tr, [], ["Marcos", "Fernando"], "", lib, fake)
    assert review.auto is False and review.suggested() == {}


# ------------------------------------------------------ sessao inteira


class FakeProvider(FakeTranscriber):
    """Transcreve como o FakeTranscriber e separa vozes por um script."""

    def __init__(self, script):
        super().__init__()
        self.diarizer = FakeDiarizer(script)

    def diarize(self, wav, *, references=None, languages=None):
        return self.diarizer.diarize(wav, references=references, languages=languages)


def window_voices(n, seconds, refs):
    # Cada janela tem um trecho de 4 s. Janelas 1 e 3: a mesma pessoa; 2: outra.
    if not refs:
        return [DiarSegment(0, 4, "A")]
    if n == 1:
        return [DiarSegment(0, 4, "B")]
    return [DiarSegment(0, 4, "V1")]


def speech(seconds=1.0):
    return np.full(int(seconds * SR), 0.1, dtype=np.float32)


def silence(seconds=1.0):
    return np.zeros(int(seconds * SR), dtype=np.float32)


def run(config, monkeypatch, tmp_path, provider, choice_for, expected=None):
    monkeypatch.setattr(session_module, "ChannelPipeline", FakePipeline)
    config.update(voice_window_seconds=4, my_name="Matheus")
    sys_blocks = [speech(4), silence(), speech(4), silence(), speech(4)]

    def factory(channel, on_block, cfg):
        if channel == "mic":
            return FakeSource("mic", on_block, [silence(), speech(1)])
        return FakeSource("sys", on_block, sys_blocks)

    lib = VoiceLibrary(tmp_path / "lib")
    s = MeetingSession(config, PRESET, provider, on_status=lambda st: None,
                       source_factory=factory, backoff_initial=0.01, library=lib)
    if expected is not None:
        s.set_expected_participants(expected)  # antes de comecar tambem vale
    s.start()
    assert s.phase == RECORDING and s.tracker is not None
    time.sleep(0.4)
    s.stop()
    assert s.phase == READY
    choice = choice_for(s)
    path = s.finalize(choice, FakeSummarizer())
    assert s.phase == DONE
    return s, path.read_text(encoding="utf-8"), lib


def test_session_names_voices_and_saves_samples(config, monkeypatch, tmp_path):
    provider = FakeProvider(window_voices)

    def choose(s):
        review = s.voice_review(["Ana", "Bia"])
        assert [v.id for v in review.voices] == ["V1", "V2"]
        return FinalizeChoice(PRESET, participants=["Bia", "Carla"],
                              voice_names={"V1": "Ana", "V2": ""})

    s, note, lib = run(config, monkeypatch, tmp_path, provider, choose)
    labels = [line.split("]")[1].split(":")[0].strip(" *")
              for line in note.splitlines() if line.startswith("> **[")]
    assert "Ana" in labels and "Voz B" in labels and "Matheus" in labels
    assert "participants: [Bia, Carla, Ana]" in note  # quem ganhou nome entra na lista
    assert lib.names() == ["Ana"]  # so quem ganhou nome vira amostra
    assert len(provider.diarizer.calls) == 3


def test_one_expected_participant_pauses_voices(config, monkeypatch, tmp_path):
    provider = FakeProvider(window_voices)

    def choose(s):
        assert s.voice_review(["Marcio"]) is None
        return FinalizeChoice(PRESET, participants=["Marcio"])

    s, note, lib = run(config, monkeypatch, tmp_path, provider, choose, expected=["Marcio"])
    assert provider.diarizer.calls == []
    assert "] Marcio:**" in note and "Voz A" not in note


def test_voices_disabled_in_config(config, monkeypatch, tmp_path):
    config["voices_enabled"] = False
    provider = FakeProvider(window_voices)
    s = MeetingSession(config, PRESET, provider, on_status=lambda st: None)
    from apolo.session.spool import Spool
    s.spool = Spool.from_config(config).create_session()
    s._setup(datetime.now().astimezone())
    assert s.tracker is None
    config["voices_enabled"] = True
    s._setup(datetime.now().astimezone())
    assert s.tracker is not None


# ------------------------------------------------------ controlador


class FakeReviewSession:
    def __init__(self, review):
        self.review = review

    def voice_review(self, participants):
        return self.review


def controller(ask_voices=None):
    from apolo.app import AppController

    return AppController(lambda status: None, None, ask_voices)


def test_controller_auto_review_skips_the_window():
    from apolo.voices.identify import VoiceCandidate, VoiceReview

    clip = np.zeros(10, dtype=np.int16)
    review = VoiceReview([VoiceCandidate("V1", "Voz A", 9, clip, suggestion="Ana")], auto=True)
    asked = []
    c = controller(lambda r, done: asked.append(r))
    choice = FinalizeChoice(PRESET)
    c._name_voices(FakeReviewSession(review), choice, ask=True)
    assert choice.voice_names == {"V1": "Ana"} and asked == []


def test_controller_asks_and_waits_for_the_answer():
    from apolo.voices.identify import VoiceCandidate, VoiceReview

    clip = np.zeros(10, dtype=np.int16)
    review = VoiceReview([VoiceCandidate("V1", "Voz A", 9, clip, suggestion="Ana")])

    def ask(r, done):
        threading.Timer(0.05, done, args=({"V1": "Bia"}, False)).start()

    c = controller(ask)
    choice = FinalizeChoice(PRESET)
    c._name_voices(FakeReviewSession(review), choice, ask=True)
    assert choice.voice_names == {"V1": "Bia"} and choice.save_voices is False
    # Sem ninguem olhando (teto de duracao): nada de nome sem confirmar.
    unattended = FinalizeChoice(PRESET)
    c._name_voices(FakeReviewSession(review), unattended, ask=False)
    assert unattended.voice_names == {}
