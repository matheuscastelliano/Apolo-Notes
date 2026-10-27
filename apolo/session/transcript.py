"""Os trechos ja transcritos de uma sessao: done.jsonl, a fonte da verdade.

    <spool_dir>/<sessao>/done.jsonl     uma linha JSON por trecho transcrito

A nota e sempre renderizada a partir daqui (PLANO.md, 3.1), nunca remendada.
Por isso o arquivo e append-only e cada linha e gravada inteira com flush:
depois de um crash, o que esta aqui somado ao que sobrou em seg/ e a sessao
inteira, e da para refazer a nota ou o resumo sem audio nenhum.

Ordem na nota ao vivo: os resultados chegam fora de ordem (dois workers, dois
canais). `visible()` so devolve o que comeca antes do trecho pendente mais
antigo, entao a transcricao no arquivo so cresce no fim - nunca aparece uma
fala nova no meio do que voce ja leu.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Iterable

from ..providers.base import Gap, Segment

log = logging.getLogger(__name__)

DONE_FILE = "done.jsonl"

SPEAKER_BY_CHANNEL = {"mic": "me", "sys": "others"}

# Motivo da lacuna no spool -> codigo que a nota traduz (note.gap.reason.*).
_GAP_REASONS = {"spool_full": "overflow"}


class TranscriptStore:
    """done.jsonl de uma sessao. Seguro para usar de varias threads."""

    def __init__(self, directory: Path):
        self.path = Path(directory) / DONE_FILE
        self._lock = threading.Lock()
        self._segments: list[Segment] = self._load()

    def _load(self) -> list[Segment]:
        segments = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            log.warning("Nao consegui ler %s: %s", self.path, exc)
            return []
        for line in lines:
            try:
                data = json.loads(line)
                segments.append(
                    Segment(float(data["start"]), float(data["end"]),
                            data["speaker"], str(data.get("text", "")))
                )
            except (ValueError, KeyError, TypeError):
                # Linha cortada por um crash no meio da escrita: so ela se perde.
                log.warning("Linha invalida em %s ignorada", self.path)
        return segments

    def add(self, segment: Segment) -> None:
        line = json.dumps(
            {"start": round(segment.start, 3), "end": round(segment.end, 3),
             "speaker": segment.speaker, "text": segment.text},
            ensure_ascii=False,
        )
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
            self._segments.append(segment)

    def segments(self) -> list[Segment]:
        with self._lock:
            return sorted(self._segments, key=lambda s: s.start)

    def last_text(self, speaker: str, before: float) -> str:
        """Texto do ultimo trecho do mesmo falante antes de `before`.

        Vira o `prompt` do STT: continuidade de frase e de vocabulario.
        """
        with self._lock:
            earlier = [s for s in self._segments if s.speaker == speaker and s.start < before]
        if not earlier:
            return ""
        return max(earlier, key=lambda s: s.start).text

    def visible(self, gaps: Iterable[Gap], pending_from: float | None) -> list[Segment | Gap]:
        """Trechos e lacunas que ja podem aparecer na nota ao vivo.

        `pending_from` e o inicio do trecho pendente mais antigo (na fila ou
        sendo transcrito); None = nada pendente, tudo aparece.
        """
        entries: list[Segment | Gap] = [*self.segments(), *gaps]
        if pending_from is not None:
            entries = [e for e in entries if e.start < pending_from]
        return sorted(entries, key=lambda e: e.start)


def spool_gaps(raw: Iterable[dict]) -> list[Gap]:
    """Lacunas do session.json -> Gap da nota."""
    gaps = []
    for g in raw:
        try:
            reason = str(g.get("reason") or "")
            gaps.append(Gap(float(g["start"]), float(g["end"]), _GAP_REASONS.get(reason, reason)))
        except (KeyError, TypeError, ValueError):
            continue
    return gaps
