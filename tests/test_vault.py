"""A nota no vault: projetos, tags que ja existem e a pasta do projeto (26/11)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from apolo import config as config_module
from apolo.notes import render, vault
from apolo.notes.reparse import parse_note
from apolo.notes.render import NoteMeta
from apolo.notes.writer import NoteWriter
from apolo.presets import SummaryPreset
from apolo.providers.base import Segment, SummaryResult
from apolo.session.summary import FinalizeChoice, build_summary
from test_session import PRESET, FakeTranscriber, run_session, silence, speech

pytestmark = pytest.mark.usefixtures("portuguese")

START = datetime(2026, 11, 26, 10, 1, 14, tzinfo=timezone.utc)


def note(path, front: str, body: str = "texto") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}\n---\n{body}\n", encoding="utf-8")


@pytest.fixture
def brain(tmp_path):
    """Um vault parecido com o real: INBOX/Apolo Notes e 02 PROJETOS/<projeto>."""
    root = tmp_path / "Matheus' 2nd Brain"
    (root / ".obsidian").mkdir(parents=True)
    inner = root / "2nd Brain"
    projects = inner / "02 PROJETOS 🏆"
    for name in ("Vertis", "SoulTech", ".escondida"):
        (projects / name).mkdir(parents=True)
    (projects / "PROJETOS.md").write_text("indice", encoding="utf-8")
    note(projects / "Vertis" / "Daily.md", "tags:\n  - vertis\n  - SmartOSS")
    note(inner / "03 AREAS" / "Saude.md", "tags: [saude, '#treino']")
    note(inner / "03 AREAS" / "Solta.md", "tags: pessoal")
    # Notas do proprio Apolo (tags antigas dele) nao contam.
    apolo = inner / "01 INBOX" / "Apolo Notes"
    note(apolo / "velha.md", "tags: [meeting, daily]")
    note(projects / "Vertis" / "Apolo Notes" / "outra.md", "tags: [geral]")
    note(root / ".obsidian" / "x.md", "tags: [config]")
    return root, apolo, projects


# ---------------------------------------------------------------- leitura


def test_frontmatter_tags_in_every_shape():
    assert vault.frontmatter_tags("---\ntags: [a, \"b\", '#c']\n---") == ["a", "b", "c"]
    assert vault.frontmatter_tags("---\ntags: a b\n---") == ["a", "b"]
    block = "---\ntitle: x\ntags:\n  - SmartOSS\n  - vertis/daily\nfoo: 1\n---"
    assert vault.frontmatter_tags(block) == ["SmartOSS", "vertis/daily"]
    assert vault.frontmatter_tags("sem frontmatter\ntags: [a]") == []


def test_scan_finds_vault_tags_but_not_apolo_notes(brain):
    root, apolo, projects = brain
    assert vault.find_vault_root(apolo) == root
    assert vault.scan_tags(root, skip=[apolo]) == ["pessoal", "saude", "SmartOSS", "treino",
                                                    "vertis"]
    assert vault.guess_projects_dir(root) == projects
    assert vault.list_projects(projects) == ["SoulTech", "Vertis"]


def test_project_hints_come_from_its_folder_and_links(brain):
    """26/11: sem pista, uma reuniao sobre o OSS nao era ligada a Vertis."""
    root, apolo, projects = brain
    note(apolo / "2026-11-25 Bate papo.md",
         "participants:\n  - Thiago\ntags: [SmartVTC]\nprojects:\n  - \"[[Vertis]]\"")
    note(projects / "SoulTech" / "Card.md", "participants: [Rodrigo]\ntags: [soultech, meeting]")
    tags, hints = vault.scan_vault(root, skip=[apolo], projects_dir=projects)
    assert "SmartVTC" not in tags  # so aparece numa nota do Apolo
    assert "meeting" not in tags  # tag generica do proprio Apolo
    assert hints["Vertis"].startswith("tags: ")
    assert "SmartOSS" in hints["Vertis"] and "pessoas: Thiago" in hints["Vertis"]
    assert hints["SoulTech"] == "tags: soultech; pessoas: Rodrigo"


def test_no_vault_no_context(tmp_path):
    assert vault.find_vault_root(tmp_path / "notas") is None
    assert vault.start_scan({"notes_dir": str(tmp_path / "notas")}) is None


def test_match_tags_uses_the_vault_spelling():
    ctx = vault.VaultContext(projects=["Vertis"], tags=["SmartOSS", "vertis"])
    assert ctx.match_tags(["smartoss", "#Vertis", "openshift"]) == (["SmartOSS", "vertis"],
                                                                   ["openshift"])
    assert ctx.match_project("[[vertis]]") == "Vertis"
    assert ctx.match_project("Mavi") is None


def test_scan_falls_back_to_saved_tags_while_running(brain, tmp_path):
    root, apolo, projects = brain
    cache = tmp_path / "cache.json"
    first = vault.VaultScan(apolo, projects, cache).start()
    assert first.result(wait=10).tags == ["pessoal", "saude", "SmartOSS", "treino", "vertis"]
    # Uma leitura que ainda nao terminou (nem comecou): vale a lista guardada.
    slow = vault.VaultScan(apolo, projects, cache)
    slow._thread = object()  # finge que esta rodando
    ctx = slow.result(wait=0)
    assert ctx.tags == ["pessoal", "saude", "SmartOSS", "treino", "vertis"]
    assert ctx.projects == ["SoulTech", "Vertis"]


# ----------------------------------------------------------------- resumo


class VaultSummarizer:
    def __init__(self):
        self.requests = []

    def summarize(self, request):
        self.requests.append(request)
        return SummaryResult(
            title="Planejamento POs", tags=["smartoss", "inventada"],
            keywords=["dashboard"], projects=["vertis", "Mavi"],
            body_markdown="## Resumo\nok",
        )


def test_summary_only_keeps_what_the_vault_has():
    ctx = vault.VaultContext(projects=["SoulTech", "Vertis"], tags=["SmartOSS", "vertis"])
    meta = NoteMeta(start=START, preset_id="geral", participants=["Marcos"])
    summarizer = VaultSummarizer()
    entries = [Segment(1, 2, "others", "o dashboard da Vertis")]
    result, error = build_summary(summarizer, meta, entries, "", PRESET, "Matheus", ctx)
    assert error is None
    request = summarizer.requests[0]
    assert request.projects == ["SoulTech", "Vertis"] and request.known_tags == ["SmartOSS",
                                                                                 "vertis"]
    assert result.tags == ["SmartOSS"]
    assert result.keywords == ["dashboard", "inventada"]
    assert meta.projects == ["Vertis"]

    text = render.render_final(meta, result, entries)
    front = text.split("---")[1]
    assert "tags: [SmartOSS]" in front
    assert "apolo_tags: [reuniao, geral, dashboard, inventada]" in front
    assert 'projects: ["[[Vertis]]"]' in front
    assert "app: apolo" in front


def test_without_vault_tags_stay_as_before():
    meta = NoteMeta(start=START, preset_id="geral")
    summary = SummaryResult(title="x", tags=["Deploy"], keywords=["ci"], body_markdown="## R")
    front = render.render_final(meta, summary, []).split("---")[1]
    assert "tags: [reuniao, geral, deploy, ci]" in front
    assert "apolo_tags" not in front and "projects" not in front


def test_resummarize_reads_lists_the_obsidian_rewrote():
    """O Obsidian grava as listas em bloco quando voce mexe nas propriedades."""
    text = (
        "---\ntitle: Planejamento POs\ndate: 2026-11-26T10:01:14-03:00\nduration: 19min\n"
        "preset: geral\nparticipants:\n  - Marcos\n  - Gustavo\ntags:\n  - vertis\n"
        "app: apolo\nprojects:\n  - \"[[Vertis]]\"\n---\n\n# Planejamento POs\n\n"
        "## Transcrição\n**[00:00:05] Marcos:** bom dia\n"
    )
    parsed = parse_note(text, "Matheus")
    assert parsed.meta.participants == ["Marcos", "Gustavo"]
    assert parsed.title == "Planejamento POs"
    assert parsed.entries[0].speaker == "others"


# ------------------------------------------------------------------ pasta


def test_writer_moves_the_note_to_the_project(tmp_path):
    writer = NoteWriter(tmp_path / "inbox", START)
    writer.write_live(NoteMeta(start=START), [])
    old = writer.path
    target = tmp_path / "Vertis" / "Apolo Notes"
    moved = writer.move_to(target)
    assert moved.parent == target and moved.exists() and not old.exists()
    assert writer.path == moved


def test_session_files_the_note_under_its_project(config, monkeypatch, brain, tmp_path):
    root, apolo, projects = brain
    config["notes_dir"] = str(apolo)
    monkeypatch.setattr(config_module, "config_dir", lambda: tmp_path / "appdata")
    summarizer = VaultSummarizer()
    s, path, _ = run_session(config, monkeypatch, FakeTranscriber(), [speech()], [silence()],
                             summarizer=summarizer, choice=FinalizeChoice(PRESET))
    assert path.parent == projects / "Vertis" / "Apolo Notes"
    assert not list(apolo.glob("2026*.md"))
    text = path.read_text(encoding="utf-8")
    assert 'projects: ["[[Vertis]]"]' in text and "tags: [SmartOSS]" in text


def test_session_keeps_the_note_in_place_when_turned_off(config, monkeypatch, brain, tmp_path):
    root, apolo, projects = brain
    config["notes_dir"] = str(apolo)
    config["organize_in_vault"] = False
    monkeypatch.setattr(config_module, "config_dir", lambda: tmp_path / "appdata")
    s, path, _ = run_session(config, monkeypatch, FakeTranscriber(), [speech()], [silence()],
                             summarizer=VaultSummarizer(), choice=FinalizeChoice(PRESET))
    assert path.parent == apolo
    assert "projects" not in path.read_text(encoding="utf-8").split("---")[1]
