"""Fase 4: fim da sessao, nota final, retomar, "Resumir de novo", salvar audio."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from apolo.errors import OfflineError, SummaryError
from apolo.notes import render
from apolo.notes.render import NOTES_END, NOTES_START, NoteMeta
from apolo.notes.reparse import is_placeholder_title, parse_note
from apolo.presets import SummaryPreset
from apolo.providers.base import Gap, Segment, SummaryResult
from apolo.session.session import DONE, READY, MeetingSession
from apolo.session.spool import Spool
from apolo.session.summary import FinalizeChoice, resummarize
from apolo.session.transcript import TranscriptStore
from test_session import (
    PRESET,
    FakeSummarizer,
    FakeTranscriber,
    pcm,
    run_session,
    silence,
    speech,
)

pytestmark = pytest.mark.usefixtures("portuguese")

CLIENTE = SummaryPreset(id="cliente", name="Cliente", system_prompt="foco no cliente")
START = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)


# ------------------------------------------------------------- finalize


def test_choice_title_and_people_win(config, monkeypatch):
    summarizer = FakeSummarizer()
    s, path, _ = run_session(
        config, monkeypatch, FakeTranscriber(), [speech()], [silence()],
        summarizer=summarizer,
        choice=FinalizeChoice(CLIENTE, "Alinhamento ACME", ["João", "Ana"]),
    )
    text = path.read_text(encoding="utf-8")
    assert path.name == render.note_filename(s.started_wall, "Alinhamento ACME")
    assert "preset: cliente" in text and "participants: [João, Ana]" in text
    request = summarizer.requests[0]
    assert request.system_prompt == "foco no cliente"
    assert request.participants == ["João", "Ana"]
    assert "fala 1" in request.transcript


def test_summary_failure_still_saves_the_note(config, monkeypatch):
    s, path, statuses = run_session(
        config, monkeypatch, FakeTranscriber(), [speech()], [silence()],
        summarizer=FakeSummarizer(fail=SummaryError("modelo fora do ar")),
    )
    assert s.phase == DONE
    text = path.read_text(encoding="utf-8")
    assert "fala 1" in text and "modelo fora do ar" in text
    assert isinstance(statuses[-1].summary_error, SummaryError)
    assert not s.spool.dir.exists()


def test_nothing_transcribed_skips_the_model(config, monkeypatch):
    summarizer = FakeSummarizer()
    _, path, _ = run_session(
        config, monkeypatch, FakeTranscriber(), [silence()], [silence()], summarizer=summarizer,
    )
    assert summarizer.requests == []
    assert "Nada foi transcrito" in path.read_text(encoding="utf-8")


def test_user_notes_survive_to_the_final_note(config, monkeypatch):
    from apolo.session import session as session_module
    from test_session import FakePipeline, FakeSource

    monkeypatch.setattr(session_module, "ChannelPipeline", FakePipeline)

    def factory(channel, on_block, cfg):
        return FakeSource(channel, on_block, [speech()] if channel == "mic" else [silence()])

    s = MeetingSession(config, PRESET, FakeTranscriber(), on_status=lambda st: None,
                       source_factory=factory)
    s.start()
    time.sleep(0.3)
    text = s.writer.path.read_text(encoding="utf-8")
    i = text.index(NOTES_START) + len(NOTES_START)
    s.writer.path.write_text(text[:i] + "\ncobrar o contrato\n" + text[text.index(NOTES_END):],
                             encoding="utf-8")
    s.stop()
    summarizer = FakeSummarizer()
    path = s.finalize(FinalizeChoice(PRESET), summarizer)
    assert "cobrar o contrato" in path.read_text(encoding="utf-8")
    assert summarizer.requests[0].user_notes == "cobrar o contrato"


# --------------------------------------------------------------- resume


def crashed_session(config, tmp_path):
    """Spool como um crash deixaria: um trecho feito, dois pendentes."""
    spool = Spool(tmp_path / "spool").create_session(
        kind="meeting", preset="daily", started_at=START.isoformat(),
    )
    notes = tmp_path / "notas"
    notes.mkdir()
    note = notes / "2026-10-27 1430 - Gravando.md"
    spool.update(note_path=str(note))
    TranscriptStore(spool.dir).add(Segment(0.0, 2.0, "me", "antes do crash"))
    spool.add("sys", 3.0, pcm(1))
    spool.add("mic", 5.0, pcm(1))
    return spool, note


def test_resume_finishes_a_crashed_session(config, tmp_path):
    spool, note = crashed_session(config, tmp_path)
    s = MeetingSession.resume(config, PRESET, FakeTranscriber(), spool,
                              on_status=lambda st: None, backoff_initial=0.01)
    s.drain()
    assert s.phase == READY
    path = s.finalize(FinalizeChoice(PRESET), FakeSummarizer())
    text = path.read_text(encoding="utf-8")
    assert "antes do crash" in text and "fala 1" in text and "fala 2" in text
    assert path.parent == note.parent
    assert not spool.dir.exists()


def test_incomplete_sessions_lists_crashed_but_not_finished(tmp_path):
    root = Spool(tmp_path / "spool")
    crashed = root.create_session(kind="meeting")
    crashed.add("mic", 0.0, pcm(1))
    done = root.create_session(kind="meeting")
    done.finish()
    assert [s.id for s in root.incomplete_sessions()] == [crashed.id]


# -------------------------------------------------------------- reparse


def final_note(transcript_block="callout") -> str:
    meta = NoteMeta(start=START, duration_seconds=47 * 60, preset_id="daily",
                    preset_name="Daily", participants=["João", "Ana Maria"],
                    transcript_block=transcript_block)
    entries = [
        Segment(1, 5, "me", "bom dia"),
        Segment(12, 20, "others", "o deploy: saiu?"),
        Gap(30, 40, "offline"),
        Segment(50, 55, "me", "valeu"),
    ]
    summary = SummaryResult(title="Daily do time", body_markdown="## Resumo\nok")
    return render.render_final(meta, summary, entries, "minha anotação")


@pytest.mark.parametrize("block", ["callout", "details"])
def test_parse_note_round_trip(block):
    parsed = parse_note(final_note(block))
    assert parsed.title == "Daily do time"
    assert parsed.meta.start == START
    assert parsed.meta.duration_seconds == 47 * 60
    assert parsed.meta.preset_id == "daily"
    assert parsed.meta.participants == ["João", "Ana Maria"]
    assert parsed.meta.transcript_block == block
    assert parsed.user_notes == "minha anotação"
    kinds = [(type(e).__name__, e.start) for e in parsed.entries]
    assert kinds == [("Segment", 1), ("Segment", 12), ("Gap", 30), ("Segment", 50)]
    assert parsed.entries[1].text == "o deploy: saiu?"
    assert parsed.entries[1].speaker == "others"
    assert parsed.entries[2].reason == "offline"


def test_parse_rejects_other_markdown():
    from apolo.errors import NoteError

    with pytest.raises(NoteError):
        parse_note("# Minha nota\n\nsem frontmatter")


def test_placeholder_titles():
    assert is_placeholder_title("Gravando") and is_placeholder_title("Recording")
    assert is_placeholder_title("") and not is_placeholder_title("Daily do time")


# ----------------------------------------------------------- resummarize


def test_resummarize_keeps_title_notes_and_transcript(tmp_path):
    path = tmp_path / render.note_filename(START, "Daily do time")
    path.write_text(final_note(), encoding="utf-8")
    summarizer = FakeSummarizer(title="Outro titulo")
    new = resummarize(path, FinalizeChoice(CLIENTE), summarizer)
    assert new == path  # titulo de verdade nao muda: links do Obsidian seguem valendo
    text = path.read_text(encoding="utf-8")
    assert "preset: cliente" in text and "Tudo certo." in text
    assert "minha anotação" in text and "o deploy: saiu?" in text
    assert summarizer.requests[0].system_prompt == "foco no cliente"


def test_resummarize_renames_a_placeholder_title(tmp_path):
    path = tmp_path / render.note_filename(START, "Gravando")
    path.write_text(final_note().replace("title: Daily do time", "title: Gravando"),
                    encoding="utf-8")
    new = resummarize(path, FinalizeChoice(PRESET), FakeSummarizer(title="Daily novo"))
    assert new.name == render.note_filename(START, "Daily novo")
    assert not path.exists()


def test_resummarize_failure_leaves_the_note_alone(tmp_path):
    path = tmp_path / "nota.md"
    original = final_note()
    path.write_text(original, encoding="utf-8")
    with pytest.raises(OfflineError):
        resummarize(path, FinalizeChoice(CLIENTE), FakeSummarizer(fail=OfflineError("sem rede")))
    assert path.read_text(encoding="utf-8") == original


# ----------------------------------------------------------- salvar audio


def test_save_audio_writes_one_flac_per_channel(config, monkeypatch, tmp_path):
    import soundfile

    config["save_audio"] = True
    s, path, _ = run_session(config, monkeypatch, FakeTranscriber(),
                             [speech(), silence()], [silence(), speech()])
    folder = tmp_path / "notas" / "audio" / f"{s.started_wall:%Y-%m-%d %H%M}"
    eu, outros = folder / "Eu.flac", folder / "Outros.flac"
    assert eu.exists() and outros.exists()
    assert soundfile.info(str(eu)).duration == pytest.approx(2.0, abs=0.01)
