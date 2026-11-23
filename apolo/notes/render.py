"""A nota em Markdown, ao vivo e final (PLANO.md, secao 5).

So funcoes puras: recebem os trechos e devolvem texto. Quem le e escreve o
arquivo e o notes/writer.py. A nota e sempre RENDERIZADA de novo a partir
dos trechos (done.jsonl), nunca remendada - o que o usuario escreve nela fica
entre os marcadores de "Minhas anotacoes" e volta por `extract_user_notes`.

Formato:
  ---                      frontmatter YAML (title, date, duration, preset...)
  # Titulo
  > data · duracao · preset · com Fulano
  corpo do resumo          (ao vivo: "gerando ao parar")
  ## Minhas anotacoes      entre <!-- apolo:notas:inicio/fim -->
  ## Transcricao           callout do Obsidian ou <details>, por config
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Union

from .. import i18n
from ..i18n import t
from ..providers.base import Gap, Segment, SummaryResult, normalize_tags
from ..voices.timeline import voice_letter

Entry = Union[Segment, Gap]

NOTES_START = "<!-- apolo:notas:inicio -->"
NOTES_END = "<!-- apolo:notas:fim -->"

# Falas seguidas do mesmo falante com menos que isso entre elas viram um
# paragrafo so. O chunker corta em pausas curtas, entao sem isso uma frase
# longa apareceria picada em 3 ou 4 linhas.
MERGE_GAP_SECONDS = 3.0

# Proibidos no nome de arquivo do Windows, mais os que quebram [[links]] do
# Obsidian.
_FORBIDDEN_FILENAME = re.compile(r'[<>:"/\\|?*#^\[\]\x00-\x1f]')
_MAX_TITLE_CHARS = 80


@dataclass
class NoteMeta:
    start: datetime  # com fuso: vai para o frontmatter como esta
    duration_seconds: float = 0.0
    preset_id: str = ""
    preset_name: str = ""
    participants: list[str] = field(default_factory=list)
    # Titulo dado pelo usuario na janela de parada. Vazio = o do resumo
    # (final) ou "Gravando" (ao vivo).
    title: str = ""
    transcript_block: str = "callout"  # callout | details
    # Seu nome (config "my_name"). Nao vai para o frontmatter: so troca o
    # rotulo "Eu" da transcricao.
    my_name: str = ""
    # Voz dos "Outros" -> nome confirmado ("V1" -> "Marcos"). Voz sem nome
    # aparece como "Voz A". Tambem nao vai para o frontmatter.
    voice_names: dict[str, str] = field(default_factory=dict)


# ------------------------------------------------------------------ notas


def render_live(meta: NoteMeta, segments: Iterable[Entry], user_notes: str = "") -> str:
    title = meta.title or t("note.recording_title")
    summary = f"## {t('note.summary')}\n*{t('note.summary.pending')}*"
    return _render(
        meta,
        title=title,
        tags=_tags(meta, []),
        body=summary,
        segments=segments,
        user_notes=user_notes,
        live=True,
    )


def render_final(
    meta: NoteMeta,
    summary: SummaryResult,
    segments: Iterable[Entry],
    user_notes: str = "",
) -> str:
    title = meta.title or summary.title or t("note.untitled")
    return _render(
        meta,
        title=title,
        tags=_tags(meta, summary.tags),
        body=summary.body_markdown.strip(),
        segments=segments,
        user_notes=user_notes,
        live=False,
    )


def _render(
    meta: NoteMeta,
    *,
    title: str,
    tags: list[str],
    body: str,
    segments: Iterable[Entry],
    user_notes: str,
    live: bool,
) -> str:
    parts = [
        _frontmatter(meta, title, tags),
        f"# {title}",
        "> " + _header_line(meta, live),
    ]
    if body:
        parts.append(body)
    parts.append(f"## {t('note.my_notes')}\n" + _notes_block(user_notes, live))
    parts.append(f"## {t('note.transcript')}\n" + _transcript_block(meta, segments, live))
    return "\n\n".join(parts) + "\n"


def _frontmatter(meta: NoteMeta, title: str, tags: list[str]) -> str:
    lines = [
        "---",
        f"title: {_yaml_scalar(title)}",
        f"date: {meta.start.isoformat(timespec='seconds')}",
        f"duration: {format_duration(meta.duration_seconds, compact=True)}",
    ]
    if meta.preset_id:
        lines.append(f"preset: {_yaml_scalar(meta.preset_id)}")
    lines.append(f"participants: {_yaml_list(meta.participants)}")
    lines.append(f"tags: {_yaml_list(tags)}")
    lines.append("app: apolo")
    lines.append("---")
    return "\n".join(lines)


def _header_line(meta: NoteMeta, live: bool) -> str:
    pieces = [meta.start.strftime(t("note.header.date_fmt"))]
    pieces.append(t("note.header.recording") if live else format_duration(meta.duration_seconds))
    if meta.preset_name:
        pieces.append(meta.preset_name)
    if meta.participants:
        pieces.append(t("note.header.with", names=", ".join(meta.participants)))
    return " · ".join(pieces)


def _tags(meta: NoteMeta, extra: list[str]) -> list[str]:
    return normalize_tags([t("note.tag"), meta.preset_id, *extra])


def _notes_block(user_notes: str, live: bool) -> str:
    notes = user_notes.strip()
    if not notes and live:
        notes = t("note.my_notes.placeholder")
    inner = f"{notes}\n" if notes else ""
    return f"{NOTES_START}\n{inner}{NOTES_END}"


def _transcript_block(meta: NoteMeta, segments: Iterable[Entry], live: bool) -> str:
    if live:
        label = t("note.transcript.live")
    else:
        label = t("note.transcript.full", duration=format_duration(meta.duration_seconds))
    labels = speaker_labels(meta.participants, meta.my_name)
    paragraphs = [_paragraph(e, labels, meta.voice_names) for e in merge_segments(segments)]
    if not paragraphs:
        paragraphs = [f"*{t('note.transcript.empty')}*"]

    if meta.transcript_block == "details":
        opening = "<details open>" if live else "<details>"
        return "\n".join(
            [opening, f"<summary>{label}</summary>", "", "\n\n".join(paragraphs), "", "</details>"]
        )
    # Callout do Obsidian: "+" comeca aberto, "-" comeca recolhido.
    fold = "+" if live else "-"
    lines = [f"> [!note]{fold} {label}"]
    for index, paragraph in enumerate(paragraphs):
        if index:
            lines.append(">")
        lines.append(f"> {paragraph}")
    return "\n".join(lines)


def _paragraph(entry: Entry, labels: dict[str, str], voice_names: dict[str, str]) -> str:
    if isinstance(entry, Gap):
        return f"*[{timestamp(entry.start)}–{timestamp(entry.end)}] {_gap_text(entry)}*"
    return f"**[{timestamp(entry.start)}] {_label(entry, labels, voice_names)}:** {entry.text}"


def _label(entry: Segment, labels: dict[str, str], voice_names: dict[str, str]) -> str:
    if entry.speaker == "others" and entry.voice:
        return voice_label(entry.voice, voice_names)
    return labels.get(entry.speaker, labels["others"])


def voice_label(voice: str, voice_names: dict[str, str] | None = None) -> str:
    """Nome confirmado da voz, ou "Voz A" (na lingua da interface)."""
    name = (voice_names or {}).get(voice, "").strip()
    return name or t("speaker.voice", letter=voice_letter(voice))


def guests(participants: Iterable[str], my_name: str = "") -> list[str]:
    """Os participantes tirando voce (caso tenha se colocado na lista)."""
    me = my_name.strip().casefold()
    names = [p.strip() for p in participants if p and p.strip()]
    return [p for p in names if not me or p.casefold() != me]


def speaker_labels(participants: Iterable[str], my_name: str = "") -> dict[str, str]:
    """Rotulo de cada canal na transcricao da nota.

    O Apolo so sabe de qual CANAL veio a fala: o microfone e voce, o audio do
    computador e todo o resto. Entao:
      - "me" vira o seu nome, se ele estiver nas configuracoes;
      - "others" vira o nome do participante quando so ha UM (fora voce):
        numa conversa a dois, o que nao foi voce foi ele. Com dois ou mais
        nao da para saber quem e quem, e continua "Outros".
    """
    me = my_name.strip() or t("speaker.me")
    others = guests(participants, my_name)
    return {
        "me": me,
        "others": others[0] if len(others) == 1 else t("speaker.others"),
    }


# -------------------------------------------------------------- trechos


def merge_segments(entries: Iterable[Entry], max_gap: float = MERGE_GAP_SECONDS) -> list[Entry]:
    """Ordena por inicio e junta falas seguidas do mesmo falante.

    Uma lacuna no meio sempre separa, mesmo que curta. Trechos vazios somem.
    """
    merged: list[Entry] = []
    for entry in sorted(entries, key=lambda e: e.start):
        if isinstance(entry, Segment):
            text = " ".join(entry.text.split())
            if not text:
                continue
            last = merged[-1] if merged else None
            if (
                isinstance(last, Segment)
                and last.speaker == entry.speaker
                and last.voice == entry.voice
                and entry.start - last.end < max_gap
            ):
                merged[-1] = Segment(
                    last.start, max(last.end, entry.end), last.speaker,
                    f"{last.text} {text}", last.voice,
                )
                continue
            entry = Segment(entry.start, entry.end, entry.speaker, text, entry.voice)
        merged.append(entry)
    return merged


def format_transcript_for_llm(
    segments: Iterable[Entry],
    my_name: str = "",
    participants: Iterable[str] = (),
    voice_names: dict[str, str] | None = None,
) -> str:
    """Transcricao para o resumo, um trecho por linha.

    Aqui os trechos NAO sao juntados como na nota: em "Outros" um trecho novo
    muitas vezes e outra pessoa falando, e a quebra ajuda o modelo a inferir
    os nomes. Com um participante so, "Outros" ja vai com o nome dele.
    """
    labels = speaker_labels(participants, my_name)
    me = t("speaker.me")
    if my_name.strip():
        me = f"{me} ({my_name.strip()})"
    lines = []
    for entry in merge_segments(segments, max_gap=0.0):
        if isinstance(entry, Gap):
            lines.append(
                f"[{timestamp(entry.start)}–{timestamp(entry.end)}] --- {_gap_text(entry)} ---"
            )
        else:
            label = me if entry.speaker == "me" else _label(entry, labels, voice_names or {})
            lines.append(f"[{timestamp(entry.start)}] {label}: {entry.text}")
    return "\n".join(lines)


def _gap_text(gap: Gap) -> str:
    text = t("note.gap")
    if gap.reason:
        key = f"note.gap.reason.{gap.reason}"
        reason = t(key)
        text += f" ({gap.reason if reason == key else reason})"
    return text


# ------------------------------------------------------ anotacoes / nome


def extract_user_notes(text: str) -> str:
    """O que o usuario escreveu entre os marcadores. "" se nao houver nada.

    O texto de exemplo que a nota ao vivo coloca ali nao conta como anotacao,
    em nenhum idioma: a interface pode ter mudado de idioma no meio.
    """
    start = text.find(NOTES_START)
    if start < 0:
        return ""
    start += len(NOTES_START)
    end = text.find(NOTES_END, start)
    if end < 0:
        return ""
    notes = text[start:end].strip()
    placeholders = {
        strings["note.my_notes.placeholder"]
        for strings in i18n._TRANSLATIONS.values()
        if "note.my_notes.placeholder" in strings
    }
    lines = [line for line in notes.splitlines() if line.strip() not in placeholders]
    return "\n".join(lines).strip()


def note_filename(start: datetime, title: str) -> str:
    """'2026-10-26 1430 - Titulo.md', sem caracteres proibidos no Windows."""
    clean = _FORBIDDEN_FILENAME.sub(" ", title)
    clean = " ".join(clean.split())[:_MAX_TITLE_CHARS]
    # O Windows nao aceita nome terminando em ponto ou espaco.
    clean = clean.rstrip(". ") or t("note.untitled")
    return f"{start:%Y-%m-%d %H%M} - {clean}.md"


# ------------------------------------------------------------ formatos


def timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def format_duration(seconds: float, compact: bool = False) -> str:
    """'47 min', '1 h 05 min'; compacto (frontmatter): '47min', '1h05min'."""
    minutes = max(0, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if compact:
        return f"{hours}h{minutes:02d}min" if hours else f"{minutes}min"
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


# Valores YAML que precisam de aspas para nao virar outra coisa.
_YAML_SPECIAL = re.compile(r"""^[\s\-?:,\[\]{}#&*!|>'"%@`]|[:#,\[\]{}]|\s$""")
_YAML_RESERVED = {"", "true", "false", "yes", "no", "on", "off", "null", "~"}


def _yaml_scalar(value: str) -> str:
    if (
        _YAML_SPECIAL.search(value)
        or value.lower() in _YAML_RESERVED
        or re.fullmatch(r"[-+.\d]+", value)
    ):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _yaml_list(values: list[str]) -> str:
    return "[" + ", ".join(_yaml_scalar(str(v)) for v in values) + "]"
