"""A fila de transcricao: trechos do spool -> STT -> done.jsonl.

Alguns workers (`stt_workers`, padrao 2) pegam sempre o trecho pendente mais
antigo. O que acontece com cada falha:

  TransientError (sem conexao, timeout, limite de uso, 5xx)
      -> o trecho volta para a fila e TODOS os workers esperam (backoff
         exponencial de 1 s ate 60 s, ou o que o servidor pedir). O audio
         continua no spool; nada se perde. `offline` liga para o overlay
         mostrar "sem conexao" - so quando e mesmo falta de rede.
  CredentialsError (chave invalida, sem credito, sem acesso ao modelo)
      -> nao adianta repetir: a fila PARA e guarda o erro em `fatal`. O
         audio fica no spool, para retomar depois de arrumar a chave.
  qualquer outro erro
      -> esse trecho vira lacuna ("trecho nao transcrito") e a fila segue.

O teto do spool (2 h de audio pendente) e aplicado pelo proprio spool, que
descarta o mais antigo e registra a lacuna.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from typing import Callable

from ..errors import ApoloError, CredentialsError, OfflineError, TransientError
from ..providers.base import Segment, Transcriber
from .spool import SpooledSegment, SpoolSession
from .transcript import SPEAKER_BY_CHANNEL, TranscriptStore

log = logging.getLogger(__name__)

# Quanto do texto anterior vai no `prompt` do STT.
PROMPT_TAIL_CHARS = 200


class SttQueue:
    def __init__(
        self,
        transcriber: Transcriber,
        spool: SpoolSession,
        store: TranscriptStore,
        *,
        keywords: list[str] | None = None,
        languages: list[str] | None = None,
        workers: int = 2,
        on_change: Callable[[], None] | None = None,
        backoff_initial: float = 1.0,
        backoff_max: float = 60.0,
    ):
        self.transcriber = transcriber
        self.spool = spool
        self.store = store
        self.keywords = list(keywords or [])
        self.languages = list(languages or []) or None
        self.on_change = on_change or (lambda: None)
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max

        self._n_workers = max(1, int(workers))
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._queued: dict[str, SpooledSegment] = {}
        self._inflight: dict[str, SpooledSegment] = {}
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._paused_until = 0.0
        self._failures = 0

        # Estado para a UI.
        self.offline = False
        self.retrying = False  # falha passageira que nao e falta de rede
        self.fatal: ApoloError | None = None
        self.transcribed = 0

    # ------------------------------------------------------------ ciclo

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for i in range(self._n_workers):
            thread = threading.Thread(target=self._run, name=f"stt-{i}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 5.0) -> None:
        """Para os workers. O que nao foi transcrito continua no spool."""
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []

    def submit(self, seg: SpooledSegment) -> None:
        key = str(seg.path)
        with self._lock:
            if key in self._queued or key in self._inflight:
                return
            self._queued[key] = seg
        self._queue.put((seg.start, seg.channel, next(self._seq), seg))

    def submit_pending(self) -> int:
        """Poe na fila tudo que ja estava no spool (sessao retomada)."""
        pending = self.spool.pending()
        for seg in pending:
            self.submit(seg)
        return len(pending)

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Espera a fila esvaziar. False se estourou o tempo ou parou."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._idle:
            while self._queued or self._inflight:
                if self._stop.is_set() or self.fatal is not None:
                    return False
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._idle.wait(0.2 if remaining is None else min(0.2, remaining))
        return True

    # ------------------------------------------------------------- estado

    def pending_from(self) -> float | None:
        """Inicio do trecho pendente mais antigo (fila ou em andamento)."""
        with self._lock:
            starts = [s.start for s in (*self._queued.values(), *self._inflight.values())]
        return min(starts) if starts else None

    def pending_count(self) -> int:
        with self._lock:
            return len(self._queued) + len(self._inflight)

    def pending_seconds(self) -> float:
        with self._lock:
            return sum(s.duration for s in (*self._queued.values(), *self._inflight.values()))

    # ------------------------------------------------------------ workers

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.fatal is not None:
                time.sleep(0.2)
                continue
            wait = self._paused_until - time.monotonic()
            if wait > 0:
                self._stop.wait(min(wait, 0.5))
                continue
            try:
                _, _, _, seg = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            key = str(seg.path)
            with self._lock:
                if key not in self._queued:
                    continue  # ja tratado (submit duplicado)
                self._inflight[key] = self._queued.pop(key)
            try:
                self._process(seg)
            except Exception:
                # _process ja trata tudo; isto e so a rede de seguranca da regra
                # "nada derruba o app".
                log.exception("Falha inesperada na fila de STT")
                self._drop(seg, "error")
            finally:
                with self._idle:
                    self._inflight.pop(key, None)
                    self._idle.notify_all()
                self._changed()

    def _process(self, seg: SpooledSegment) -> None:
        try:
            wav = seg.read_wav()
        except FileNotFoundError:
            return  # descartado pelo teto do spool enquanto esperava
        speaker = SPEAKER_BY_CHANNEL.get(seg.channel, "others")
        prompt = self.store.last_text(speaker, seg.start)[-PROMPT_TAIL_CHARS:]
        try:
            text = self.transcriber.transcribe(
                wav, prompt=prompt, keywords=self.keywords, languages=self.languages
            )
        except TransientError as exc:
            self._backoff(seg, exc)
            return
        except CredentialsError as exc:
            log.error("Fila de STT parada: %s", exc)
            self.fatal = exc
            self._requeue(seg)
            return
        except ApoloError as exc:
            log.warning("Trecho %s nao transcrito: %s", seg.path.name, exc)
            self._drop(seg, "error")
            return

        with self._lock:
            self._failures = 0
        self.offline = False
        self.retrying = False
        if text:
            self.store.add(Segment(seg.start, seg.end, speaker, text))
            self.transcribed += 1
        self.spool.mark_done(seg)

    def _backoff(self, seg: SpooledSegment, exc: TransientError) -> None:
        with self._lock:
            self._failures += 1
            delay = exc.retry_after or min(
                self.backoff_max, self.backoff_initial * 2 ** (self._failures - 1)
            )
            self._paused_until = max(self._paused_until, time.monotonic() + delay)
        self.offline = isinstance(exc, OfflineError)
        self.retrying = not self.offline
        log.info("STT: %s; tentando de novo em %.0fs", exc.message, delay)
        self._requeue(seg)

    def _requeue(self, seg: SpooledSegment) -> None:
        key = str(seg.path)
        with self._lock:
            self._inflight.pop(key, None)
            self._queued[key] = seg
        self._queue.put((seg.start, seg.channel, next(self._seq), seg))

    def _drop(self, seg: SpooledSegment, reason: str) -> None:
        try:
            self.spool.add_gap(seg.start, seg.end, seg.channel, reason)
        except ApoloError as exc:
            log.error("Nao consegui registrar a lacuna: %s", exc)
        self.spool.mark_done(seg)

    def _changed(self) -> None:
        try:
            self.on_change()
        except Exception:
            log.exception("Erro no aviso de mudanca da fila")

    def resume(self) -> None:
        """Depois de arrumar a chave: limpa o erro fatal e volta a processar."""
        self.fatal = None
        with self._lock:
            self._failures = 0
            self._paused_until = 0.0
