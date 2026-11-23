"""Vozes, parte pura: linha do tempo, rotulos na nota, reparse e biblioteca."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from apolo import i18n
from apolo.notes.render import NoteMeta, format_transcript_for_llm, render_final
from apolo.notes.reparse import parse_note
from apolo.providers.base import DiarSegment, Gap, Segment, SummaryResult
from apolo.voices.library import MAX_SAMPLES, VoiceLibrary
from apolo.voices.timeline import assign_voices, seconds_by_voice, voice_id, voice_letter


@pytest.fixture(autouse=True)
def pt_ui():
    previous = i18n.current()
    i18n.set_language("pt")
    yield
    i18n.set_language(previous)


START = datetime(2026, 11, 23, 16, 13, tzinfo=timezone(timedelta(hours=-3)))
SUMMARY = SummaryResult(title="Planning", body_markdown="## Resumo\nok")


# ------------------------------------------------------------ timeline


def test_letters_and_ids_round_trip():
    assert [voice_letter(f"V{n}") for n in (1, 2, 26, 27)] == ["A", "B", "Z", "AA"]
    assert voice_id("A") == "V1" and voice_id("aa") == "V27"
    assert voice_id("Marcos 2") is None
    assert voice_letter("N:Marcos") == "N:Marcos"


def test_each_segment_takes_the_voice_that_talks_most():
    entries = [
        Segment(0, 12, "others", "abertura"),
        Segment(13, 20, "me", "eu"),
        Segment(20, 32, "others", "resposta"),
        Gap(40, 50, "offline"),
    ]
    timeline = [
        DiarSegment(0, 8, "V1"), DiarSegment(8, 12, "V2"),
        DiarSegment(20, 23, "V1"), DiarSegment(23, 32, "V2"),
    ]
    out = assign_voices(entries, timeline)
    assert [getattr(e, "voice", None) for e in out] == ["V1", None, "V2", None]
    assert out[3] == entries[3]


def test_segment_without_voice_takes_the_nearest_one_if_close():
    timeline = [DiarSegment(0, 5, "V1"), DiarSegment(30, 40, "V2")]
    near, far = Segment(6, 7, "others", "valeu"), Segment(15, 16, "others", "hm")
    assert [e.voice for e in assign_voices([near, far], timeline)] == ["V1", None]


def test_assign_limited_to_known_voices():
    timeline = [DiarSegment(0, 10, "V1"), DiarSegment(0, 10, "V9")]
    out = assign_voices([Segment(0, 10, "others", "x")], timeline, voices={"V9"})
    assert out[0].voice == "V9"
    assert seconds_by_voice(timeline) == {"V1": 10, "V9": 10}


# --------------------------------------------------------- nota e reparse


ENTRIES = [
    Segment(0, 6, "others", "A gente podia, mas só temos isso", "V2"),
    Segment(7, 12, "others", "Tem PBI pra ser criado?", "V1"),
    Segment(12.5, 14, "others", "Do dois ainda?", "V1"),
    Segment(16, 19, "me", "Então, eu não sei"),
    Segment(20, 25, "others", "Pode deletar isso aí", "V3"),
    Segment(26, 27, "others", "Beleza"),
]


def meta(**kwargs):
    base = dict(start=START, duration_seconds=13 * 60, preset_id="geral", preset_name="Geral",
                participants=["Marcos", "Fernando", "Camila"], my_name="Matheus",
                voice_names={"V1": "Marcos", "V2": "Fernando"})
    base.update(kwargs)
    return NoteMeta(**base)


def test_note_uses_names_then_voice_letters():
    note = render_final(meta(), SUMMARY, ENTRIES)
    assert "**[00:00:00] Fernando:** A gente podia" in note
    # Mesma voz seguida vira um paragrafo so; voz diferente separa.
    assert "**[00:00:07] Marcos:** Tem PBI pra ser criado? Do dois ainda?" in note
    assert "**[00:00:16] Matheus:** Então" in note
    assert "**[00:00:20] Voz C:** Pode deletar" in note
    assert "**[00:00:26] Outros:** Beleza" in note


def test_llm_transcript_has_the_same_labels():
    text = format_transcript_for_llm(ENTRIES, "Matheus", ["Marcos", "Fernando", "Camila"],
                                     {"V1": "Marcos", "V2": "Fernando"})
    assert "[00:00:00] Fernando: A gente podia" in text
    assert "[00:00:07] Marcos: Tem PBI" in text
    assert "[00:00:20] Voz C: Pode deletar" in text
    assert "[00:00:16] Eu (Matheus): Então" in text


def test_reparse_round_trips_names_and_letters():
    note = render_final(meta(), SUMMARY, ENTRIES)
    parsed = parse_note(note, "Matheus")
    again = render_final(parsed.meta, SUMMARY, parsed.entries)
    for line in ("Fernando:** A gente", "Marcos:** Tem PBI", "Voz C:** Pode",
                 "Outros:** Beleza", "Matheus:** Então"):
        assert line in again
    speakers = [(e.speaker, e.voice) for e in parsed.entries]
    assert speakers == [("others", "N:Fernando"), ("others", "N:Marcos"), ("me", None),
                        ("others", "V3"), ("others", None)]


def test_reparse_reads_english_voice_label():
    note = render_final(meta(voice_names={}), SUMMARY, ENTRIES).replace("Voz C", "Voice C")
    parsed = parse_note(note, "Matheus")
    assert ("others", "V3") in [(e.speaker, e.voice) for e in parsed.entries]


# ------------------------------------------------------------ biblioteca


def tone(seconds=4.0, freq=220.0):
    t = np.arange(int(seconds * 16000)) / 16000
    return (np.sin(2 * np.pi * freq * t) * 8000).astype(np.int16)


def test_library_add_find_reference_and_persist(tmp_path):
    lib = VoiceLibrary(tmp_path)
    person = lib.add_sample("Márcio", tone())
    assert person.id == "marcio"
    assert lib.find("márcio") is not None and lib.names() == ["Márcio"]
    ref = lib.reference("MÁRCIO")
    assert ref is not None and ref[:4] == b"RIFF"
    again = VoiceLibrary(tmp_path)
    assert again.names() == ["Márcio"] and len(again.people()[0].samples) == 1


def test_library_keeps_only_the_newest_samples(tmp_path):
    lib = VoiceLibrary(tmp_path)
    for i in range(MAX_SAMPLES + 2):
        lib.add_sample("Ana", tone(freq=200 + i))
    person = lib.find("Ana")
    assert len(person.samples) == MAX_SAMPLES
    assert len(list((tmp_path / person.id).glob("*.wav"))) == MAX_SAMPLES


def test_library_rename_merges_same_name_and_deletes(tmp_path):
    lib = VoiceLibrary(tmp_path)
    a = lib.add_sample("Fernando", tone())
    b = lib.add_sample("Fer", tone(freq=300))
    lib.rename(b.id, "fernando")
    assert lib.names() == ["Fernando"]
    assert len(lib.find("Fernando").samples) == 2
    lib.delete(a.id)
    assert lib.names() == [] and not (tmp_path / a.id).exists()
    lib.add_sample("Zé", tone())
    lib.delete_all()
    assert VoiceLibrary(tmp_path).names() == []


def test_library_ignores_missing_sample_files(tmp_path):
    lib = VoiceLibrary(tmp_path)
    person = lib.add_sample("Ana", tone())
    for path in lib.sample_paths(person):
        path.unlink()
    assert VoiceLibrary(tmp_path).find("Ana").samples == []
    assert VoiceLibrary(tmp_path).reference("Ana") is None
