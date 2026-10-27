"""Uma reuniao, do "Iniciar" ao "Parar": captura -> spool -> STT -> nota.

    PREPARING --as duas fontes entregaram--> RECORDING --stop()--> DRAINING --> DONE
        |                                                             |
        +-- nenhuma fonte abriu --> ERROR          fila parada (chave) +--> ERROR

Threads (alem das de captura e das de STT):
  `note` - reescreve a nota ao vivo quando chega trecho novo (no maximo uma
           vez por NOTE_INTERVAL) e avisa a UI quando o estado muda.

O audio vai das fontes para o pipeline (VAD + chunker) na propria thread de
captura, e cada trecho cortado vai direto para o spool e para a fila. Nada
fica so na memoria: se o app morrer, o spool e o done.jsonl tem tudo.

Fase 3: no fim a nota fica com a transcricao completa e o resumo ainda como
"gerado ao parar". O resumo, a janela de parada e a limpeza do spool sao a
fase 4.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .. import config as config_module
from ..audio.chunker import Chunk
from ..audio.pipeline import ChannelPipeline
from ..audio.sources import MicSource, SystemSource
from ..errors import ApoloError, AudioError
from ..i18n import t
from ..notes.render import NoteMeta
from ..notes.writer import NoteWriter
from ..presets import SummaryPreset
from ..providers.base import Transcriber
from .spool import Spool, SpoolSession
from .transcript import TranscriptStore, spool_gaps
from .workers import SttQueue

log = logging.getLogger(__name__)

# Quanto esperar o 1o bloco de cada fonte (o mic Bluetooth leva ~1,5 s).
FIRST_BLOCK_TIMEOUT = 5.0
# Intervalo minimo entre duas escritas da nota ao vivo.
NOTE_INTERVAL = 1.5

PREPARING = "preparing"
RECORDING = "recording"
DRAINING = "draining"
DONE = "done"
ERROR = "error"

SourceFactory = Callable[[str, Callable[[float, Any], None], dict], Any]


def default_source_factory(channel: str, on_block, config: dict):
    if channel == "mic":
        return MicSource(on_block, config.get("mic_device"))
    return SystemSource(on_block, config.get("loopback_device"))


@dataclass(frozen=True)
class SessionStatus:
    phase: str
    preset: str = ""
    started_at: float | None = None  # monotonic do inicio da gravacao
    pending: int = 0
    pending_seconds: float = 0.0
    offline: bool = False
    retrying: bool = False
    # Canal que caiu ou nem abriu (o outro segue gravando).
    channel_problems: tuple[str, ...] = ()
    error: ApoloError | None = None
    note_path: Path | None = None


class MeetingSession:
    def __init__(
        self,
        config: dict,
        preset: SummaryPreset,
        transcriber: Transcriber,
        *,
        on_status: Callable[[SessionStatus], None],
        source_factory: SourceFactory = default_source_factory,
        backoff_initial: float = 1.0,
    ):
        self.config = config
        self.preset = preset
        self.transcriber = transcriber
        self.on_status = on_status
        self.source_factory = source_factory
        self.backoff_initial = backoff_initial

        self.phase = PREPARING
        self.started_wall: datetime | None = None
        self.started_at: float | None = None
        self.spool: SpoolSession | None = None
        self.store: TranscriptStore | None = None
        self.writer: NoteWriter | None = None
        self.queue: SttQueue | None = None
        self.meta: NoteMeta | None = None
        self._sources: list = []
        self._pipelines: dict[str, ChannelPipeline] = {}
        self._t0 = 0.0
        self._dirty = threading.Event()
        self._stop_note = threading.Event()
        self._note_thread: threading.Thread | None = None
        self._last_status: SessionStatus | None = None
        self._spool_errors = 0

    # ---------------------------------------------------------------- start

    def start(self) -> None:
        """Abre as fontes e comeca a gravar. Bloqueia ate ~5 s (Bluetooth).

        Levanta ApoloError se nada pode ser gravado; nesse caso nao sobra
        nada no disco.
        """
        self._emit(force=True)
        self.started_wall = datetime.now().astimezone()
        spool = Spool.from_config(self.config)
        self.spool = spool.create_session(
            kind="meeting", preset=self.preset.id, started_at=self.started_wall.isoformat()
        )
        try:
            self.writer = NoteWriter(config_module.notes_dir(self.config), self.started_wall)
            self.spool.update(note_path=str(self.writer.path))
            self.store = TranscriptStore(self.spool.dir)
            self.meta = NoteMeta(
                start=self.started_wall,
                preset_id=self.preset.id,
                preset_name=self.preset.name,
                transcript_block=self.config.get("transcript_block", "callout"),
            )
            self.queue = SttQueue(
                self.transcriber,
                self.spool,
                self.store,
                keywords=[*self.config.get("keywords", []), *self.preset.keywords],
                languages=self.preset.languages or self.config.get("languages"),
                workers=int(self.config.get("stt_workers", 2) or 2),
                on_change=self._dirty.set,
                backoff_initial=self.backoff_initial,
            )
            self._open_sources()
        except BaseException:
            self._teardown_failed_start()
            raise

        self.queue.start()
        self.started_at = time.monotonic()
        self.phase = RECORDING
        self.writer.write_live(self.meta, [])
        self._note_thread = threading.Thread(target=self._note_loop, name="note", daemon=True)
        self._note_thread.start()
        log.info("Gravando: %s (nota em %s)", self.preset.id, self.writer.path)
        self._emit(force=True)

    def _open_sources(self) -> None:
        self._t0 = time.monotonic()
        for channel in ("mic", "sys"):
            self._pipelines[channel] = ChannelPipeline(channel, self.config, self._t0)
            self._sources.append(self.source_factory(channel, self._feeder(channel), self.config))
        for src in self._sources:
            src.start()
        # "Gravando" so depois que as fontes entregarem o 1o bloco.
        deadline = self._t0 + FIRST_BLOCK_TIMEOUT
        for src in self._sources:
            while not src.first_block.is_set() and src.running and time.monotonic() < deadline:
                src.first_block.wait(0.05)
        if not any(src.first_block.is_set() for src in self._sources):
            errors = [src.error for src in self._sources if src.error is not None]
            raise errors[0] if errors else AudioError(t("err.no_audio"))
        for src in self._sources:
            if not src.first_block.is_set():
                log.warning("Canal %s nao abriu: %s", src.channel, src.error)

    def _teardown_failed_start(self) -> None:
        for src in self._sources:
            src.stop()
        self._sources = []
        if self.queue is not None:
            self.queue.stop()
        if self.spool is not None:
            try:
                self.spool.delete()
            except ApoloError:
                log.exception("Nao consegui apagar o spool da sessao que falhou")
        self.phase = ERROR

    def _feeder(self, channel: str):
        def on_block(t_arrival: float, block) -> None:
            self._handle(self._pipelines[channel].push(t_arrival, block))

        return on_block

    def _handle(self, chunks: list[Chunk]) -> None:
        for chunk in chunks:
            try:
                seg = self.spool.add(chunk.channel, chunk.start, chunk.pcm)
            except ApoloError as exc:
                # Disco cheio ou sem permissao: perde este trecho, nao a reuniao.
                self._spool_errors += 1
                log.error("Spool: %s", exc)
                continue
            self.queue.submit(seg)
            self._dirty.set()

    # ----------------------------------------------------------------- stop

    def stop(self) -> Path | None:
        """Para de gravar, espera a fila e escreve a nota. Bloqueia.

        Sem internet, espera ate a conexao voltar (o overlay mostra). Se a
        fila parar por erro de chave, devolve o que tiver e o audio fica no
        spool para retomar depois.
        """
        if self.phase != RECORDING:
            return self.writer.path if self.writer else None
        self.phase = DRAINING
        for src in self._sources:
            src.stop()
        for pipeline in self._pipelines.values():
            self._handle(pipeline.close())
        self.meta.duration_seconds = time.monotonic() - (self.started_at or self._t0)
        self._emit(force=True)

        while not self.queue.wait_idle(timeout=1.0):
            if self.queue.fatal is not None:
                break
            self._emit()

        self._stop_note.set()
        if self._note_thread is not None:
            self._note_thread.join(timeout=5)
        self.queue.stop()
        self._write_note()

        if self.queue.fatal is not None:
            self.phase = ERROR
            self._emit(force=True, error=self.queue.fatal)
            return self.writer.path
        self.spool.finish()
        self.phase = DONE
        log.info("Sessao concluida: %d trechos transcritos", self.queue.transcribed)
        self._emit(force=True)
        return self.writer.path

    def abort(self) -> None:
        """App fechando no meio: para tudo e deixa o spool para retomar."""
        for src in self._sources:
            src.stop(timeout=1.0)
        self._stop_note.set()
        if self.queue is not None:
            self.queue.stop(timeout=1.0)
        if self.writer is not None and self.meta is not None:
            try:
                self._write_note()
            except ApoloError:
                pass

    # ---------------------------------------------------------- nota ao vivo

    def _note_loop(self) -> None:
        last = 0.0
        while not self._stop_note.is_set():
            self._dirty.wait(0.5)
            if self._stop_note.is_set():
                break
            self._emit()
            if not self._dirty.is_set():
                continue
            wait = NOTE_INTERVAL - (time.monotonic() - last)
            if wait > 0:
                self._stop_note.wait(wait)
            self._dirty.clear()
            last = time.monotonic()
            try:
                self._write_note()
            except ApoloError as exc:
                log.error("Nota: %s", exc)

    def _write_note(self) -> None:
        if self.phase in (RECORDING, PREPARING):
            self.meta.duration_seconds = time.monotonic() - (self.started_at or self._t0)
        pending_from = self.queue.pending_from() if self.queue else None
        entries = self.store.visible(spool_gaps(self.spool.gaps()), pending_from)
        self.writer.write_live(self.meta, entries)

    # --------------------------------------------------------------- status

    def _emit(self, force: bool = False, error: ApoloError | None = None) -> None:
        problems = tuple(
            src.channel for src in self._sources
            if src.error is not None or (self.phase == RECORDING and not src.running)
        )
        q = self.queue
        status = SessionStatus(
            phase=self.phase,
            preset=self.preset.name,
            started_at=self.started_at,
            pending=q.pending_count() if q else 0,
            pending_seconds=q.pending_seconds() if q else 0.0,
            offline=bool(q and q.offline),
            retrying=bool(q and q.retrying),
            channel_problems=problems,
            error=error or (q.fatal if q else None),
            note_path=self.writer.path if self.writer else None,
        )
        if not force and status == self._last_status:
            return
        self._last_status = status
        try:
            self.on_status(status)
        except Exception:
            log.exception("Erro ao avisar a UI")
