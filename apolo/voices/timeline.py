"""Casa a linha do tempo das vozes com as falas transcritas. So funcoes puras.

O texto vem do STT, em trechos de ~12 s cortados pelo VAD; quem falou vem da
separacao de vozes, em pedacos menores. Cada trecho dos "Outros" fica com a
voz que ocupa a maior parte dele. Trecho que a separacao nao cobriu (fala
curta, um "valeu") herda a voz mais proxima, se ela estiver perto.
"""

from __future__ import annotations

import string
from collections import defaultdict
from typing import Iterable, Union

from ..providers.base import DiarSegment, Gap, Segment

Entry = Union[Segment, Gap]

# Ate onde uma fala sem voz procura a voz vizinha.
NEAREST_SECONDS = 3.0


def voice_letter(voice: str) -> str:
    """'V1' -> 'A', 'V2' -> 'B'... 'V27' -> 'AA'. Outro formato volta igual."""
    if not voice.startswith("V") or not voice[1:].isdigit():
        return voice
    n = int(voice[1:])
    letters = ""
    while n > 0:
        n, rest = divmod(n - 1, 26)
        letters = string.ascii_uppercase[rest] + letters
    return letters or voice


def voice_id(letter: str) -> str | None:
    """'A' -> 'V1'. None se nao for so letras."""
    letter = letter.strip().upper()
    if not letter or not letter.isalpha() or not letter.isascii():
        return None
    n = 0
    for ch in letter:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return f"V{n}"


def seconds_by_voice(timeline: Iterable[DiarSegment]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for seg in timeline:
        totals[seg.speaker] += max(0.0, seg.end - seg.start)
    return dict(totals)


def assign_voices(
    entries: Iterable[Entry],
    timeline: Iterable[DiarSegment],
    voices: set[str] | None = None,
) -> list[Entry]:
    """As falas dos "Outros" com a voz de quem falou (`Segment.voice`).

    `voices` limita as vozes aceitas (None = todas). As falas do microfone e
    as lacunas passam como vieram.
    """
    spans = sorted(
        (s for s in timeline if voices is None or s.speaker in voices),
        key=lambda s: s.start,
    )
    out: list[Entry] = []
    for entry in entries:
        if isinstance(entry, Segment) and entry.speaker == "others":
            voice = _voice_for(entry.start, max(entry.end, entry.start + 0.5), spans)
            if voice != entry.voice:
                entry = Segment(entry.start, entry.end, entry.speaker, entry.text, voice)
        out.append(entry)
    return out


def _voice_for(start: float, end: float, spans: list[DiarSegment]) -> str | None:
    overlap: dict[str, float] = defaultdict(float)
    for s in spans:
        if s.start >= end:
            break
        if s.end > start:
            overlap[s.speaker] += min(end, s.end) - max(start, s.start)
    if overlap:
        return max(overlap.items(), key=lambda kv: kv[1])[0]
    best, best_distance = None, NEAREST_SECONDS
    for s in spans:
        distance = max(s.start - end, start - s.end, 0.0)
        if distance <= best_distance:
            best, best_distance = s.speaker, distance
    return best
