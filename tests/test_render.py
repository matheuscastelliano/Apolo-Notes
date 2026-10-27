from datetime import datetime, timedelta, timezone

import pytest

from apolo import i18n
from apolo.notes.render import (
    NOTES_END,
    NOTES_START,
    NoteMeta,
    extract_user_notes,
    format_transcript_for_llm,
    merge_segments,
    note_filename,
    render_final,
    render_live,
)
from apolo.providers.base import Gap, Segment, SummaryResult

@pytest.fixture(autouse=True)
def pt_ui():
    """Os testes conferem textos em portugues; o idioma do sistema nao importa."""
    previous = i18n.current()
    i18n.set_language("pt")
    yield
    i18n.set_language(previous)


BRT = timezone(timedelta(hours=-3))
START = datetime(2026, 10, 26, 14, 30, tzinfo=BRT)

SEGMENTS = [
    Segment(12, 18, "me", "Bom dia, pessoal,"),
    Segment(19.5, 22, "me", "vamos começar pelo escopo?"),
    Segment(20, 30, "others", "Bom dia! Então, sobre a integração..."),
    Gap(1900, 1985, "offline"),
]


def meta(**kwargs) -> NoteMeta:
    base = dict(
        start=START,
        duration_seconds=47 * 60,
        preset_id="cliente",
        preset_name="Cliente",
        participants=["João", "Ana"],
    )
    base.update(kwargs)
    return NoteMeta(**base)


SUMMARY = SummaryResult(
    title="Alinhamento de escopo com cliente X",
    tags=["Cliente", "escopo", "#integração api", "versão 2.3"],
    body_markdown="## Resumo\nConversamos sobre o escopo.\n\n## Próximos passos\n- [ ] **João**: enviar proposta",
)


def test_render_final_callout_matches_plan_format():
    note = render_final(meta(), SUMMARY, SEGMENTS, "lembrar do prazo")
    expected = """---
title: Alinhamento de escopo com cliente X
date: 2026-10-26T14:30:00-03:00
duration: 47min
preset: cliente
participants: [João, Ana]
tags: [reuniao, cliente, escopo, integração-api, versão-2-3]
app: apolo
---

# Alinhamento de escopo com cliente X

> 26/10/2026 14:30 · 47 min · Cliente · com João, Ana

## Resumo
Conversamos sobre o escopo.

## Próximos passos
- [ ] **João**: enviar proposta

## Minhas anotações
<!-- apolo:notas:inicio -->
lembrar do prazo
<!-- apolo:notas:fim -->

## Transcrição
> [!note]- Transcrição completa (47 min)
> **[00:00:12] Eu:** Bom dia, pessoal, vamos começar pelo escopo?
>
> **[00:00:20] Outros:** Bom dia! Então, sobre a integração...
>
> *[00:31:40–00:33:05] trecho não transcrito (sem conexão)*
"""
    assert note == expected


def test_render_live_callout_open_and_pending_summary():
    note = render_live(meta(participants=[]), SEGMENTS[:2])
    assert "title: Gravando\n" in note
    assert "# Gravando\n" in note
    assert "> 26/10/2026 14:30 · gravando · Cliente\n" in note
    assert "## Resumo\n*Gerando ao parar.*" in note
    assert "> [!note]+ Transcrição (ao vivo)\n" in note
    # Sem anotacoes ainda: o texto de exemplo fica entre os marcadores.
    assert f"{NOTES_START}\n(o que você escrever" in note
    assert "participants: []" in note
    assert "tags: [reuniao, cliente]" in note


def test_render_live_without_segments():
    note = render_live(meta(), [])
    assert "> [!note]+ Transcrição (ao vivo)\n> *nada transcrito ainda*\n" in note


def test_render_final_empty_notes_has_no_placeholder():
    note = render_final(meta(), SUMMARY, SEGMENTS, "")
    assert f"{NOTES_START}\n{NOTES_END}" in note


def test_render_details_block():
    live = render_live(meta(transcript_block="details"), SEGMENTS)
    assert "<details open>\n<summary>Transcrição (ao vivo)</summary>\n\n**[00:00:12] Eu:**" in live
    assert "[!note]" not in live

    final = render_final(meta(transcript_block="details"), SUMMARY, SEGMENTS)
    block = final.split("## Transcrição\n", 1)[1]
    assert block == (
        "<details>\n"
        "<summary>Transcrição completa (47 min)</summary>\n"
        "\n"
        "**[00:00:12] Eu:** Bom dia, pessoal, vamos começar pelo escopo?\n"
        "\n"
        "**[00:00:20] Outros:** Bom dia! Então, sobre a integração...\n"
        "\n"
        "*[00:31:40–00:33:05] trecho não transcrito (sem conexão)*\n"
        "\n"
        "</details>\n"
    )


def test_user_title_wins_over_summary_title():
    note = render_final(meta(title="Kickoff: projeto Y"), SUMMARY, [])
    # ':' obriga aspas no YAML.
    assert 'title: "Kickoff: projeto Y"\n' in note
    assert "# Kickoff: projeto Y\n" in note


def test_long_meeting_duration():
    note = render_final(meta(duration_seconds=72 * 60), SUMMARY, [])
    assert "duration: 1h12min\n" in note
    assert "· 1 h 12 min ·" in note


def test_merge_joins_same_speaker_under_three_seconds():
    merged = merge_segments(
        [
            Segment(0, 5, "me", "um"),
            Segment(7.9, 10, "me", "dois"),  # 2,9 s depois: junta
            Segment(13.5, 15, "me", "três"),  # 3,5 s depois: separa
        ]
    )
    assert [(s.start, s.end, s.text) for s in merged] == [
        (0, 10, "um dois"),
        (13.5, 15, "três"),
    ]


def test_merge_sorts_and_does_not_cross_other_speaker_or_gap():
    merged = merge_segments(
        [
            Segment(10, 12, "others", "b"),
            Segment(0, 9, "me", "a"),
            Segment(12.5, 14, "me", "c"),
            Gap(14, 14.5),
            Segment(15, 16, "me", "d"),
            Segment(17, 18, "me", "  "),  # vazio some
        ]
    )
    assert [getattr(e, "text", "GAP") for e in merged] == ["a", "b", "c", "GAP", "d"]


def test_gap_with_unknown_and_empty_reason():
    note = render_live(meta(), [Gap(0, 10, "disco cheio"), Gap(20, 30)])
    assert "*[00:00:00–00:00:10] trecho não transcrito (disco cheio)*" in note
    assert "*[00:00:20–00:00:30] trecho não transcrito*" in note


def test_english_labels():
    i18n.set_language("en")
    note = render_live(meta(), SEGMENTS)
    assert "**[00:00:12] Me:**" in note
    assert "**[00:00:20] Others:**" in note
    assert "> [!note]+ Transcript (live)" in note


def test_format_transcript_for_llm():
    text = format_transcript_for_llm(SEGMENTS, "Matheus")
    assert text == (
        "[00:00:12] Eu (Matheus): Bom dia, pessoal,\n"
        "[00:00:19] Eu (Matheus): vamos começar pelo escopo?\n"
        "[00:00:20] Outros: Bom dia! Então, sobre a integração...\n"
        "[00:31:40–00:33:05] --- trecho não transcrito (sem conexão) ---"
    )
    assert format_transcript_for_llm(SEGMENTS[:1]).startswith("[00:00:12] Eu: ")


def test_extract_user_notes_roundtrip_with_user_edits():
    note = render_live(meta(), SEGMENTS)
    assert extract_user_notes(note) == ""

    # O usuario apagou o exemplo e escreveu por cima, no Obsidian.
    edited = note.replace(
        "(o que você escrever aqui durante a reunião é preservado e usado no resumo)",
        "- prazo: 30/10\n- **perguntar** sobre a API\n\nfalar com a Ana",
    )
    notes = extract_user_notes(edited)
    assert notes == "- prazo: 30/10\n- **perguntar** sobre a API\n\nfalar com a Ana"

    # Rerender preserva exatamente o que foi escrito.
    again = render_live(meta(), SEGMENTS, notes)
    assert extract_user_notes(again) == notes


def test_extract_user_notes_keeps_text_next_to_placeholder():
    i18n.set_language("en")
    note = render_live(meta(), [])
    i18n.set_language("pt")
    # Escreveu embaixo do exemplo (em ingles) sem apaga-lo.
    edited = note.replace(NOTES_END, "minha nota\n" + NOTES_END)
    assert extract_user_notes(edited) == "minha nota"


def test_extract_user_notes_without_markers():
    assert extract_user_notes("# nota sem marcadores") == ""
    assert extract_user_notes(f"{NOTES_START}\nsem fim") == ""


def test_note_filename():
    assert note_filename(START, "Alinhamento de escopo") == "2026-10-26 1430 - Alinhamento de escopo.md"
    assert (
        note_filename(START, 'Cliente: X/Y <v2> "final"?*|#^[ok]')
        == "2026-10-26 1430 - Cliente X Y v2 final ok.md"
    )
    assert note_filename(START, "Termina com ponto...") == "2026-10-26 1430 - Termina com ponto.md"
    assert note_filename(START, "  ???  ") == "2026-10-26 1430 - Reunião.md"
    assert note_filename(START, "Gravando") == "2026-10-26 1430 - Gravando.md"
    long_name = note_filename(START, "x" * 300)
    assert len(long_name) == len("2026-10-26 1430 - .md") + 80
