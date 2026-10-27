"""Contrato entre o Apolo e quem transcreve/resume.

O resto do app so conhece estes Protocols e dataclasses. A interface e em
lote (trecho de audio -> texto, transcricao inteira -> nota), o que encaixa
tanto na OpenAI quanto num faster-whisper ou Ollama local no futuro.

Erros: as implementacoes levantam `ApoloError` e subclasses. As de
`TransientError` (sem conexao, timeout, limite de uso, 5xx) valem retry;
o resto nao adianta repetir.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

Speaker = Literal["me", "others"]


@dataclass(frozen=True)
class Segment:
    """Um trecho transcrito. Tempos em segundos desde o inicio da sessao."""

    start: float
    end: float
    speaker: Speaker
    text: str


@dataclass(frozen=True)
class Gap:
    """Trecho de audio que nao virou texto (sem conexao, teto do spool...).

    `reason` e um codigo curto ("offline", "overflow", "error") que a nota
    traduz; um codigo desconhecido aparece como veio.
    """

    start: float
    end: float
    reason: str = ""


@dataclass
class SummaryRequest:
    # Ja formatada para o modelo (ver notes.render.format_transcript_for_llm).
    transcript: str
    # O que o usuario escreveu em "Minhas anotacoes".
    user_notes: str = ""
    participants: list[str] = field(default_factory=list)
    # Quem e o "Eu" da transcricao. Vazio = "a pessoa que gravou".
    my_name: str = ""
    date: datetime | None = None
    duration_seconds: float = 0.0
    # Foco e secoes do preset; as regras gerais ficam no Summarizer.
    system_prompt: str = ""
    # None = o text_model padrao do provider.
    model: str | None = None


@dataclass
class SummaryResult:
    # Vai para o frontmatter, o H1 e o nome do arquivo.
    title: str
    tags: list[str] = field(default_factory=list)
    # Corpo da nota, comecando no primeiro "##". Sem H1 e sem frontmatter.
    body_markdown: str = ""


def normalize_tags(tags: list) -> list[str]:
    """Minusculas, sem '#', sem repetir, so com o que o Obsidian aceita numa
    tag (letras, numeros, '_', '-', '/'): o resto vira hifen."""
    seen: list[str] = []
    for tag in tags:
        tag = re.sub(r"[^\w/-]+", "-", str(tag).lstrip().lstrip("#").lower())
        tag = re.sub(r"-{2,}", "-", tag).strip("-")
        if tag and tag not in seen:
            seen.append(tag)
    return seen


class Transcriber(Protocol):
    def transcribe(
        self,
        wav: bytes,
        *,
        prompt: str = "",
        keywords: list[str] | None = None,
        languages: list[str] | None = None,
    ) -> str:
        """Texto do trecho. String vazia = nao havia fala de verdade."""
        ...


class Summarizer(Protocol):
    def summarize(self, request: SummaryRequest) -> SummaryResult: ...
