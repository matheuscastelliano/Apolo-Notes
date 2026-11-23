"""No fim da reuniao: quem e cada voz? (PLANO_VOZES.md, 3.3)

Para cada participante que ja tem amostra na biblioteca, o Apolo monta um
audio curto com a amostra de cada voz da reuniao (com 1 s de silencio entre
elas) e pede ao diarize para separar, passando as amostras da biblioteca
como referencia. A voz que voltar com o nome de alguem ganha a sugestao.

Fase V0: com a biblioteca completa, acertou todos os nomes. Com uma pessoa
faltando, o modelo DISTRIBUIU os nomes que tinha e chamou o Marcos de
Fernando. Por isso o nome so entra sozinho (`auto`) quando:
  - cada voz da reuniao recebeu uma pessoa diferente da biblioteca; e
  - a conferencia ao contrario bate: as amostras da biblioteca, separadas
    com as vozes da reuniao como referencia, caem cada uma na voz que
    recebeu o nome dela. Isso pega o caso de alguem da lista que nao falou
    e "emprestou" o nome para outra voz.
Fora disso, tudo e sugestao, e voce confirma ouvindo.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np

from ..errors import ApoloError
from ..notes.render import guests, voice_label
from ..providers.base import Diarizer, Segment
from .library import VoiceLibrary, same_name
from .tracker import MAX_REFS, VoiceTracker, concat_wav

log = logging.getLogger(__name__)

# Voz com menos fala que isso (um "valeu" no fim) nao entra na validacao.
MIN_VOICE_SECONDS = 3.0


@dataclass
class VoiceCandidate:
    id: str
    label: str  # "Voz A"
    seconds: float
    clip: np.ndarray  # int16, 16 kHz
    sample_text: str = ""
    suggestion: str = ""


@dataclass
class VoiceReview:
    voices: list[VoiceCandidate]
    # Todas as vozes tem nome da biblioteca: da para seguir sem perguntar.
    auto: bool = False
    # Opcoes do campo de nome: os participantes e quem esta na biblioteca.
    names: list[str] = field(default_factory=list)

    def suggested(self) -> dict[str, str]:
        return {v.id: v.suggestion for v in self.voices if v.suggestion}


def candidates(tracker: VoiceTracker, entries: Iterable) -> list[VoiceCandidate]:
    """As vozes que merecem validacao, com a amostra e uma frase de exemplo."""
    entries = list(entries)
    out = []
    for info in tracker.voice_list():
        if info.seconds < MIN_VOICE_SECONDS or info.clip_seconds <= 0:
            continue
        clip = tracker.clip_pcm(info.id)
        if clip is None or not len(clip):
            continue
        out.append(VoiceCandidate(info.id, voice_label(info.id), round(info.seconds, 1), clip,
                                  _sample_text(entries, info)))
    return out


def _sample_text(entries: list, info) -> str:
    a, b = info.clip_start, info.clip_start + info.clip_seconds
    mine = [e for e in entries if isinstance(e, Segment) and e.voice == info.id and e.text]
    overlapping = [e for e in mine if e.start < b and e.end > a]
    pick = overlapping[0] if overlapping else max(mine, key=lambda e: len(e.text), default=None)
    if pick is None:
        return ""
    text = " ".join(pick.text.split())
    return text if len(text) <= 90 else text[:87].rstrip() + "..."


def build_review(
    tracker: VoiceTracker,
    entries: Iterable,
    participants: list[str],
    my_name: str,
    library: VoiceLibrary,
    diarizer: Diarizer,
    languages: list[str] | None = None,
) -> VoiceReview | None:
    """None = nao ha o que validar (nenhuma voz, ou so uma e ninguem listado)."""
    people = guests(participants, my_name)
    voices = candidates(tracker, entries)
    if not voices or (len(voices) == 1 and len(people) < 2):
        return None

    names: list[str] = []
    for name in [*people, *library.names()]:
        if not any(same_name(name, n) for n in names):
            names.append(name)

    refs = {}
    for name in people:
        person = library.find(name)
        ref = library.reference(name) if person else None
        if ref is not None:
            refs[person.name] = ref
    if refs:
        try:
            _suggest(voices, refs, diarizer, languages)
        except ApoloError as exc:
            log.warning("Vozes: identificacao falhou: %s", exc)
            for v in voices:
                v.suggestion = ""

    suggested = [v.suggestion for v in voices]
    auto = (
        bool(refs)
        and len(refs) <= MAX_REFS
        and all(suggested)
        and len({s.casefold() for s in suggested}) == len(voices)
    )
    if auto:
        try:
            auto = _confirm(voices, refs, diarizer, languages)
        except ApoloError as exc:
            log.warning("Vozes: conferencia falhou: %s", exc)
            auto = False
    log.info("Vozes: %d para validar, sugestoes %s, automatico=%s",
             len(voices), {v.id: v.suggestion for v in voices}, auto)
    return VoiceReview(voices, auto=auto, names=names)


def _confirm(voices: list[VoiceCandidate], refs: dict[str, bytes], diarizer, languages) -> bool:
    """Ao contrario: cada amostra da biblioteca cai na voz que levou o nome dela?"""
    from .tracker import wav_to_pcm

    by_name = {v.suggestion.casefold(): v.id for v in voices}
    names = [n for n in refs if n.casefold() in by_name]
    wav, spans = concat_wav([wav_to_pcm(refs[n]) for n in names])
    references = {}
    for v in voices:
        clip_wav, _ = concat_wav([v.clip], gap_seconds=0.0)
        references[v.id] = clip_wav
    segs = diarizer.diarize(wav, references=references, languages=languages)
    for name, (a, b) in zip(names, spans):
        overlap: Counter = Counter()
        for s in segs:
            overlap[s.speaker] += max(0.0, min(b, s.end) - max(a, s.start))
        top = overlap.most_common(1)
        if not top or top[0][0] != by_name[name.casefold()] or top[0][1] < 0.5 * (b - a):
            log.info("Vozes: conferencia nao bateu para %s (%s)", name, dict(overlap))
            return False
    return True


def _suggest(voices: list[VoiceCandidate], refs: dict[str, bytes], diarizer, languages) -> None:
    wav, spans = concat_wav([v.clip for v in voices])
    best: dict[str, tuple[str, float]] = {}
    names = list(refs)
    for i in range(0, len(names), MAX_REFS):
        batch = {n: refs[n] for n in names[i:i + MAX_REFS]}
        segs = diarizer.diarize(wav, references=batch, languages=languages)
        for voice, (a, b) in zip(voices, spans):
            overlap: Counter = Counter()
            for s in segs:
                if s.speaker in batch:
                    overlap[s.speaker] += max(0.0, min(b, s.end) - max(a, s.start))
            if overlap:
                name, seconds = overlap.most_common(1)[0]
                # Mais da metade da amostra com esse nome, para contar.
                if seconds >= 0.5 * (b - a) and seconds > best.get(voice.id, ("", 0.0))[1]:
                    best[voice.id] = (name, seconds)
    # Duas vozes com o mesmo nome: fica com quem bateu mais.
    owner: dict[str, tuple[str, float]] = {}
    for vid, (name, seconds) in best.items():
        key = name.casefold()
        if key not in owner or seconds > owner[key][1]:
            owner[key] = (vid, seconds)
    for voice in voices:
        name, _ = best.get(voice.id, ("", 0.0))
        voice.suggestion = name if name and owner[name.casefold()][0] == voice.id else ""
