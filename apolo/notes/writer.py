"""Escreve a nota no disco: ao vivo durante a reuniao e no fim.

Regras:
  - Antes de cada escrita le o arquivo atual e guarda o que o usuario
    escreveu em "Minhas anotacoes" (render.extract_user_notes). O Apolo
    nunca apaga o que voce digitou ali.
  - Escrita atomica (.tmp + replace): o Obsidian nunca ve meia nota. No
    Windows o replace pode falhar se outro programa estiver com o arquivo
    aberto (o Obsidian/Sync segurou por mais de 1 s em 26/11); tenta de novo
    com esperas crescentes, ~7 s no total, antes de desistir.
  - So escreve se o conteudo mudou: menos recargas no editor, menos chance
    de conflito com quem esta digitando (risco R5 do PLANO.md).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

from ..errors import NoteError
from ..i18n import t
from ..providers.base import SummaryResult
from . import render
from .render import Entry, NoteMeta

log = logging.getLogger(__name__)

# Esperas entre as tentativas de trocar o .tmp pela nota (arquivo preso).
_REPLACE_WAITS = (0.2, 0.5, 1.0, 2.0, 3.0)


def unique_path(directory: Path, filename: str) -> Path:
    """`filename` dentro de `directory`, com ' (2)', ' (3)'... se ja existir."""
    path = directory / filename
    stem, suffix = path.stem, path.suffix
    n = 2
    while path.exists():
        path = directory / f"{stem} ({n}){suffix}"
        n += 1
    return path


class NoteWriter:
    def __init__(self, directory: Path, start: datetime, path: Path | None = None):
        self.directory = Path(directory)
        self._lock = threading.Lock()
        self._last_written: str | None = None
        if path is not None:
            self.path = Path(path)  # sessao retomada: a nota ja existe
        else:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise NoteError(t("err.notes_dir"), str(exc)) from exc
            self.path = unique_path(
                self.directory, render.note_filename(start, t("note.recording_title"))
            )

    def user_notes(self) -> str:
        try:
            return render.extract_user_notes(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ""
        except OSError as exc:
            log.warning("Nao consegui ler a nota para guardar as anotacoes: %s", exc)
            return ""

    def write_live(self, meta: NoteMeta, entries: Iterable[Entry]) -> None:
        with self._lock:
            self._write(render.render_live(meta, entries, self.user_notes()))

    def write_final(self, meta: NoteMeta, summary: SummaryResult, entries: Iterable[Entry]) -> None:
        with self._lock:
            self._write(render.render_final(meta, summary, entries, self.user_notes()))

    def rename_for(self, start: datetime, title: str) -> Path:
        """Troca o 'Gravando' do nome do arquivo pelo titulo final."""
        with self._lock:
            target = self.directory / render.note_filename(start, title)
            if target == self.path:
                return self.path
            target = unique_path(self.directory, target.name)
            try:
                self.path.replace(target)
            except OSError as exc:
                # Nome antigo e feio, mas a nota esta inteira: nao e erro fatal.
                log.warning("Nao consegui renomear a nota: %s", exc)
                return self.path
            self.path = target
            return target

    def move_to(self, directory: Path) -> Path:
        """Leva a nota pronta para outra pasta (a do projeto). Se nao der, ela
        fica onde esta - inteira - e so registra o motivo."""
        with self._lock:
            directory = Path(directory)
            if directory == self.path.parent:
                return self.path
            try:
                directory.mkdir(parents=True, exist_ok=True)
                target = unique_path(directory, self.path.name)
                for wait in (*_REPLACE_WAITS, None):
                    try:
                        self.path.replace(target)
                        break
                    except PermissionError:
                        if wait is None:
                            raise
                        time.sleep(wait)
            except OSError as exc:
                log.warning("Nao consegui mover a nota para %s: %s", directory, exc)
                return self.path
            self.path, self.directory = target, directory
            return target

    def discard(self) -> None:
        """Apaga a nota (reuniao sem fala nenhuma)."""
        with self._lock:
            try:
                self.path.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Nao consegui apagar a nota vazia: %s", exc)

    def _write(self, text: str) -> None:
        if text == self._last_written:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8", newline="\n")
        except OSError as exc:
            raise NoteError(t("err.note_write"), str(exc)) from exc
        for wait in (*_REPLACE_WAITS, None):
            try:
                tmp.replace(self.path)
                self._last_written = text
                return
            except PermissionError as exc:
                if wait is None:
                    tmp.unlink(missing_ok=True)
                    raise NoteError(t("err.note_write"), str(exc)) from exc
                time.sleep(wait)
            except OSError as exc:
                tmp.unlink(missing_ok=True)
                raise NoteError(t("err.note_write"), str(exc)) from exc
