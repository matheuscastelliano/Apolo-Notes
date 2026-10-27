"""Le uma nota do Apolo de volta: frontmatter, anotacoes e transcricao.

Serve o "Resumir de novo": a nota e autossuficiente, entao da para refazer o
resumo com outro preset mesmo depois que o spool foi limpo, ou com a nota
movida para outra pasta do vault. So entende o formato que o render.py
escreve (nos dois idiomas) - nao e um parser de Markdown generico.

A transcricao volta como paragrafos ja juntados (o render junta falas
seguidas do mesmo falante), o que basta para o resumo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import i18n
from ..errors import NoteError
from ..i18n import t
from ..providers.base import Gap, Segment
from .render import Entry, NoteMeta, extract_user_notes

_SEGMENT = re.compile(r"^\*\*\[(\d+):(\d{2}):(\d{2})\] (.+?):\*\* ?(.*)$")
_GAP = re.compile(r"^\*\[(\d+):(\d{2}):(\d{2})–(\d+):(\d{2}):(\d{2})\] (.*)\*$")
_DURATION = re.compile(r"^(?:(\d+)h)?(\d+)min$")


@dataclass
class ParsedNote:
    meta: NoteMeta
    entries: list[Entry]
    user_notes: str
    title: str


def _all(key: str) -> set[str]:
    return {strings[key] for strings in i18n._TRANSLATIONS.values() if key in strings}


def _seconds(h: str, m: str, s: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def _frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    data = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep:
            data[key.strip()] = value.strip()
    return data


def _yaml_list(value: str) -> list[str]:
    value = value.strip()
    if not (value.startswith("[") and value.endswith("]")):
        return [value] if value else []
    inner = value[1:-1]
    return [_unquote(v) for v in re.findall(r'"(?:[^"\\]|\\.)*"|[^,]+', inner) if v.strip()]


def _gap_reason(text: str) -> str:
    """'trecho nao transcrito (sem conexao)' -> 'offline'."""
    for strings in i18n._TRANSLATIONS.values():
        for key, value in strings.items():
            if key.startswith("note.gap.reason.") and f"({value})" in text:
                return key.rsplit(".", 1)[1]
    return ""


def parse_note(text: str) -> ParsedNote:
    front = _frontmatter(text)
    try:
        start = datetime.fromisoformat(front["date"])
    except (KeyError, ValueError) as exc:
        raise NoteError(t("err.note_not_apolo")) from exc
    duration = 0.0
    m = _DURATION.match(front.get("duration", ""))
    if m:
        duration = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60

    me, others = _all("speaker.me"), _all("speaker.others")
    raw: list[tuple[float, str, str]] = []  # (inicio, falante | "gap", texto)
    gaps: list[Gap] = []
    in_transcript = False
    transcript_headers = {f"## {h}" for h in _all("note.transcript")}
    details = False
    for line in text.splitlines():
        if line.strip() in transcript_headers:
            in_transcript = True
            continue
        if not in_transcript:
            continue
        if line.startswith("<details"):
            details = True
        content = line[1:].lstrip() if line.startswith(">") else line.strip()
        seg = _SEGMENT.match(content)
        if seg:
            label = seg.group(4)
            speaker = "me" if label in me else "others" if label in others else None
            if speaker:
                raw.append((_seconds(*seg.group(1, 2, 3)), speaker, seg.group(5).strip()))
            continue
        gap = _GAP.match(content)
        if gap:
            gaps.append(Gap(_seconds(*gap.group(1, 2, 3)), _seconds(*gap.group(4, 5, 6)),
                            _gap_reason(gap.group(7))))

    entries: list[Entry] = []
    starts = sorted([s for s, _, _ in raw] + [g.start for g in gaps])
    for start_s, speaker, body in raw:
        # O fim nao esta na nota: vai ate o proximo trecho (ou ate o inicio).
        later = [s for s in starts if s > start_s]
        entries.append(Segment(start_s, later[0] if later else start_s, speaker, body))
    entries += gaps
    entries.sort(key=lambda e: e.start)

    title = _unquote(front.get("title", ""))
    meta = NoteMeta(
        start=start,
        duration_seconds=duration,
        preset_id=_unquote(front.get("preset", "")),
        participants=_yaml_list(front.get("participants", "")),
        transcript_block="details" if details else "callout",
    )
    return ParsedNote(meta, entries, extract_user_notes(text), title)


def read_note(path: Path) -> ParsedNote:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise NoteError(t("err.note_read"), str(exc)) from exc
    return parse_note(text)


def is_placeholder_title(title: str) -> bool:
    """'Gravando' e companhia: titulo que o resumo novo deve substituir."""
    return title in _all("note.recording_title") or title in _all("note.untitled") or not title
