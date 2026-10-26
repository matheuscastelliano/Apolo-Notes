"""Presets = os tipos de resumo (geral, daily, cliente, 1:1...).

Como no Artemis, um preset nao e codigo: e uma entrada em presets.json. O
preset so define o FOCO e as SECOES do corpo da nota. O formato de saida
(titulo, tags, Markdown), o idioma e as regras gerais ("nao invente nada")
ficam no Summarizer, iguais para todos - assim um preset novo nao tem como
quebrar a estrutura da nota.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SummaryPreset:
    id: str
    name: str
    description: str = ""
    # Foco do resumo e as secoes que o corpo da nota deve ter.
    system_prompt: str = ""
    # Termos que o STT costuma errar. Somados aos keywords globais do config.
    keywords: list[str] = field(default_factory=list)
    # None = usa os idiomas globais do config.json.
    languages: list[str] | None = None
    # None = usa o text_model global do config.json.
    text_model: str | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.name:
            raise ValueError("preset sem id ou sem nome")
        if not self.system_prompt.strip():
            raise ValueError(f"preset '{self.id}': system_prompt vazio")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SummaryPreset":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
