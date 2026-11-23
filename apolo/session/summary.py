"""Resumo da reuniao: a ponte entre a transcricao e o Summarizer.

Usado no fim de toda sessao e pelo "Resumir de novo" (que le a propria nota,
ver notes/reparse.py). Regras:
  - Sem nenhuma fala transcrita, nem chama o modelo: a nota diz isso.
  - Falha passageira (timeout, rede, limite de uso, 5xx) tenta de novo
    algumas vezes antes de desistir: no 1o teste real um timeout isolado
    deixou a nota sem resumo.
  - Se o resumo falhar mesmo assim no fim da reuniao, a nota sai com a
    transcricao completa e um aviso no lugar do resumo - da para resumir de
    novo depois. No "Resumir de novo" a falha nao toca na nota.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..errors import ApoloError, TransientError
from ..i18n import t
from ..notes.render import Entry, NoteMeta, format_transcript_for_llm
from ..notes.reparse import is_placeholder_title, read_note
from ..notes.writer import NoteWriter
from ..presets import SummaryPreset
from ..providers.base import Segment, Summarizer, SummaryRequest, SummaryResult

log = logging.getLogger(__name__)


@dataclass
class FinalizeChoice:
    """O que a janela de parada devolve (ou os padroes, sem janela)."""

    preset: SummaryPreset
    title: str = ""
    participants: list[str] = field(default_factory=list)


def build_summary(
    summarizer: Summarizer,
    meta: NoteMeta,
    entries: list[Entry],
    user_notes: str,
    preset: SummaryPreset,
    my_name: str = "",
) -> tuple[SummaryResult, ApoloError | None]:
    """Resumo pronto para o render_final, e o erro se o modelo falhou."""
    fallback_title = meta.title or t("note.untitled")
    meta.my_name = my_name  # o rotulo "Eu" da transcricao vira o seu nome
    if not any(isinstance(e, Segment) and e.text.strip() for e in entries):
        body = f"## {t('note.summary')}\n*{t('note.summary.nothing')}*"
        return SummaryResult(title=fallback_title, body_markdown=body), None
    request = SummaryRequest(
        transcript=format_transcript_for_llm(entries, my_name, meta.participants),
        user_notes=user_notes,
        participants=list(meta.participants),
        my_name=my_name,
        date=meta.start,
        duration_seconds=meta.duration_seconds,
        system_prompt=preset.system_prompt,
        model=preset.text_model,
    )
    try:
        return summarize_with_retry(summarizer, request), None
    except ApoloError as exc:
        log.error("Resumo falhou: %s", exc)
        body = f"## {t('note.summary')}\n*{t('note.summary.failed', error=exc.message)}*"
        return SummaryResult(title=fallback_title, body_markdown=body), exc


# Espera entre as tentativas do resumo, em segundos (3 tentativas no total).
RETRY_DELAYS = (5.0, 20.0)


def summarize_with_retry(
    summarizer: Summarizer,
    request: SummaryRequest,
    delays: tuple[float, ...] = RETRY_DELAYS,
    sleep: Callable[[float], None] = time.sleep,
) -> SummaryResult:
    """Tenta de novo nas falhas passageiras; as outras sobem na hora."""
    for attempt, delay in enumerate((*delays, None)):
        try:
            return summarizer.summarize(request)
        except TransientError as exc:
            if delay is None:
                raise
            wait = max(delay, exc.retry_after or 0.0)
            log.warning("Resumo: %s; tentativa %d, de novo em %.0fs",
                        exc.message, attempt + 1, wait)
            sleep(wait)
    raise AssertionError("inalcancavel")


def resummarize(
    path: Path,
    choice: FinalizeChoice,
    summarizer: Summarizer,
    my_name: str = "",
) -> Path:
    """Refaz o resumo de uma nota pronta, com outro preset. Devolve o caminho.

    O titulo so muda se o usuario digitou um, ou se a nota ainda tinha um
    titulo provisorio ("Gravando"): renomear quebraria links do Obsidian.
    """
    parsed = read_note(path, my_name)
    meta = parsed.meta
    meta.preset_id, meta.preset_name = choice.preset.id, choice.preset.name
    if choice.participants:
        meta.participants = list(choice.participants)
    placeholder = is_placeholder_title(parsed.title)
    meta.title = choice.title.strip() or ("" if placeholder else parsed.title)

    summary, error = build_summary(
        summarizer, meta, parsed.entries, parsed.user_notes, choice.preset, my_name
    )
    if error is not None:
        raise error  # a nota antiga continua como estava
    writer = NoteWriter(path.parent, meta.start, path=path)
    writer.write_final(meta, summary, parsed.entries)
    if choice.title.strip() or placeholder:
        return writer.rename_for(meta.start, meta.title or summary.title)
    return writer.path
