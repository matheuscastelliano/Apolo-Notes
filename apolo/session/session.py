"""Uma reuniao, do "Iniciar" a nota pronta: captura -> spool -> STT -> nota.

    PREPARING --fontes abertas--> RECORDING --stop()--> DRAINING --fila vazia--> READY
        |                                                   |                     |
        +-- nenhuma fonte abriu --> ERROR    fila parada --> ERROR       finalize()
                                             (chave)                          |
                                                              SUMMARIZING --> DONE

Uma sessao retomada (crash, app fechado no meio) entra direto em DRAINING
por `resume()`, com o que sobrou no spool.

Threads (alem das de captura e das de STT):
  `note`   - reescreve a nota ao vivo quando chega trecho novo (no maximo uma
             vez por NOTE_INTERVAL) e avisa a UI quando o estado muda.
  `voices` - separa as vozes dos "Outros" em janelas (voices/tracker.py),
             quando `voices_enabled` e o provider sabe separar.

O audio vai das fontes para o pipeline (VAD + chunker) na propria thread de
captura, e cada trecho cortado vai direto para o spool e para a fila. Nada
fica so na memoria: se o app morrer, o spool e o done.jsonl tem tudo.

No fim (finalize): resumo, nota final com a transcricao recolhida, nome do
arquivo com o titulo e o spool apagado - o audio era temporario.
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
from ..audio.resample import SAMPLE_RATE
from ..audio.sources import MicSource, SystemSource
from ..errors import ApoloError, AudioError
from ..i18n import t
from ..notes.render import NoteMeta, guests
from ..notes.writer import NoteWriter
from ..presets import SummaryPreset
from ..providers.base import Segment, Summarizer, Transcriber
from .spool import Spool, SpoolSession
from .summary import FinalizeChoice, build_summary
from .transcript import TranscriptStore, spool_gaps
from ..voices.identify import VoiceReview, build_review
from ..voices.library import VoiceLibrary
from ..voices.timeline import assign_voices
from ..voices.tracker import VoiceTracker
from .workers import SttQueue

log = logging.getLogger(__name__)

# Quanto esperar o 1o bloco de cada fonte (o mic Bluetooth leva ~1,5 s).
FIRST_BLOCK_TIMEOUT = 5.0
# Intervalo minimo entre duas escritas da nota ao vivo.
NOTE_INTERVAL = 1.5
# "Nao estou ouvindo os outros": tanto tempo sem fala no loopback enquanto
# voce falou pelo menos MY_SPEECH_FOR_ALERT. Na daily de 28/10 o loopback
# ouviu a saida errada a reuniao inteira e ninguem percebeu ate o fim.
OTHERS_SILENT_SECONDS = 120.0
MY_SPEECH_FOR_ALERT = 20.0

PREPARING = "preparing"
RECORDING = "recording"
DRAINING = "draining"
READY = "ready"
SUMMARIZING = "summarizing"
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
    # Em DONE: o resumo falhou, mas a nota (com a transcricao) foi salva.
    summary_error: ApoloError | None = None
    note_path: Path | None = None
    # "Pausar meu microfone" ligado.
    mic_paused: bool = False
    # Minutos sem fala nenhuma nos dois canais, quando passa do alerta (0 = ok).
    silent_minutes: int = 0
    # Gravando ha mais que `max_session_hours`: o app para sozinho.
    limit_reached: bool = False
    # Voce fala e o canal "Outros" segue mudo: a call deve estar tocando
    # numa saida que o Apolo nao ouve.
    others_silent: bool = False
    # Em DONE: ninguem falou, a nota foi descartada.
    discarded: bool = False
    # Em DRAINING: a transcricao acabou, falta separar as ultimas vozes.
    voices_pending: bool = False
    # Reconhecendo quem e cada voz (entre READY e o resumo).
    identifying: bool = False


class AudioKeeper:
    """Opcao "salvar audio": um FLAC continuo por canal, ao lado das notas.

    Escreve o audio cru das fontes (antes do VAD), na thread de captura de
    cada canal. Qualquer erro so desliga a gravacao daquele canal: guardar o
    audio e um extra, nunca motivo para perder a reuniao.
    """

    LABELS = {"mic": "speaker.me", "sys": "speaker.others"}

    def __init__(self, directory: Path):
        self.directory = directory
        self._files: dict[str, Any] = {}
        self._broken: set[str] = set()

    def write(self, channel: str, block) -> None:
        if channel in self._broken:
            return
        try:
            f = self._files.get(channel)
            if f is None:
                import soundfile

                self.directory.mkdir(parents=True, exist_ok=True)
                path = self.directory / f"{t(self.LABELS[channel])}.flac"
                f = soundfile.SoundFile(
                    str(path), "w", samplerate=SAMPLE_RATE, channels=1, format="FLAC"
                )
                self._files[channel] = f
            f.write(block)
        except Exception as exc:
            log.error("Nao consegui guardar o audio do canal %s: %s", channel, exc)
            self._broken.add(channel)

    def close(self) -> None:
        for f in self._files.values():
            try:
                f.close()
            except Exception:
                pass
        self._files = {}


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
        library: VoiceLibrary | None = None,
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
        self.tracker: VoiceTracker | None = None
        # "Quem esta na reuniao", informado durante a gravacao (opcional).
        self.expected_participants: list[str] = []
        self._library = library
        self._voices_pending = False
        self._identifying = False
        self.meta: NoteMeta | None = None
        self.summary_error: ApoloError | None = None
        self._sources: list = []
        self._pipelines: dict[str, ChannelPipeline] = {}
        self._audio: AudioKeeper | None = None
        self.mic_paused = False
        self.silent_minutes = 0
        self.limit_reached = False
        self.discarded = False
        self.others_silent = False
        self._last_speech: float | None = None
        self._last_others_speech: float | None = None
        # Segundos de fala no mic desde a ultima fala no loopback.
        self._my_speech_since_others = 0.0
        self._t0 = 0.0
        self._dirty = threading.Event()
        self._stop_note = threading.Event()
        self._note_thread: threading.Thread | None = None
        self._last_status: SessionStatus | None = None

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
            self.spool.update(note_path=str(self.writer.path),
                              expected_participants=self.expected_participants)
            self._setup(self.started_wall)
            if self.config.get("save_audio"):
                self._audio = AudioKeeper(self._audio_dir())
            self._open_sources()
        except BaseException:
            self._teardown_failed_start()
            raise

        self.queue.start()
        if self.tracker is not None:
            self.tracker.start()
        self.started_at = time.monotonic()
        self.phase = RECORDING
        self.writer.write_live(self.meta, [])
        self._start_note_thread()
        log.info("Gravando: %s (nota em %s)", self.preset.id, self.writer.path)
        self._emit(force=True)

    @classmethod
    def resume(
        cls,
        config: dict,
        preset: SummaryPreset,
        transcriber: Transcriber,
        spool: SpoolSession,
        *,
        on_status: Callable[[SessionStatus], None],
        backoff_initial: float = 1.0,
    ) -> "MeetingSession":
        """Sessao que um crash deixou no spool: transcreve o que faltou.

        Devolve em DRAINING; quem chama segue com `drain()` e `finalize()`.
        """
        session = cls(config, preset, transcriber, on_status=on_status,
                      backoff_initial=backoff_initial)
        session.spool = spool
        info = spool.info
        try:
            started = datetime.fromisoformat(info.get("started_at") or info.get("created_at"))
        except (TypeError, ValueError):
            started = datetime.now().astimezone()
        session.started_wall = started
        note_path = info.get("note_path")
        session.writer = NoteWriter(
            config_module.notes_dir(config), started,
            path=Path(note_path) if note_path else None,
        )
        session._setup(started)
        # Sessao que nao chegou a parar nao tem duracao salva: estima pelo
        # fim do ultimo trecho conhecido.
        ends = [s.end for s in session.store.segments()] + [s.end for s in spool.pending()]
        session.meta.duration_seconds = float(info.get("duration_seconds") or max(ends, default=0.0))
        session.phase = DRAINING
        session.expected_participants = [str(p) for p in info.get("expected_participants") or []]
        session._apply_expected()  # _setup ja rodou, antes de saber quem estava
        session.queue.submit_pending()
        session.queue.start()
        if session.tracker is not None:
            session.tracker.start()
        session._start_note_thread()
        log.info("Retomando a sessao %s (%d trechos pendentes)", spool.id, session.queue.pending_count())
        session._emit(force=True)
        return session

    def _setup(self, started: datetime) -> None:
        self.store = TranscriptStore(self.spool.dir)
        self.meta = NoteMeta(
            start=started,
            preset_id=self.preset.id,
            preset_name=self.preset.name,
            transcript_block=self.config.get("transcript_block", "callout"),
            my_name=str(self.config.get("my_name") or ""),
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
        if self.config.get("voices_enabled", True) and hasattr(self.transcriber, "diarize"):
            self.tracker = VoiceTracker(
                self.transcriber,
                self.spool.dir,
                window_seconds=float(self.config.get("voice_window_seconds") or 120),
                languages=self.preset.languages or self.config.get("languages"),
                on_change=self._dirty.set,
                backoff_initial=self.backoff_initial,
            )
            self._apply_expected()

    @property
    def library(self) -> VoiceLibrary:
        if self._library is None:
            self._library = VoiceLibrary()
        return self._library

    # ------------------------------------------------- quem esta na reuniao

    def set_expected_participants(self, names: list[str]) -> None:
        """Quem esta na reuniao, dito durante a gravacao. Com uma pessoa so,
        separar vozes nao serve para nada e a separacao pausa."""
        self.expected_participants = [n.strip() for n in names if n and n.strip()]
        if self.spool is not None:
            try:
                self.spool.update(expected_participants=self.expected_participants)
            except ApoloError as exc:
                log.error("Spool: %s", exc)
        self._apply_expected()
        log.info("Quem esta: %s", ", ".join(self.expected_participants) or "-")

    def _apply_expected(self) -> None:
        if self.tracker is not None:
            people = guests(self.expected_participants, str(self.config.get("my_name") or ""))
            self.tracker.set_paused(len(people) == 1)

    def _voices_active(self, participants: list[str]) -> bool:
        """As vozes valem para esta nota? Nao numa conversa a dois: ali o
        nome do outro participante ja diz tudo (de34905)."""
        if self.tracker is None:
            return False
        return len(guests(participants, str(self.config.get("my_name") or ""))) != 1

    def _audio_dir(self) -> Path:
        base = self.config.get("audio_dir")
        root = Path(base).expanduser() if base else config_module.notes_dir(self.config) / "audio"
        return root / f"{self.started_wall:%Y-%m-%d %H%M}"

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
        if self._audio is not None:
            self._audio.close()
        if self.queue is not None:
            self.queue.stop()
        if self.spool is not None:
            try:
                self.spool.delete()
            except ApoloError:
                log.exception("Nao consegui apagar o spool da sessao que falhou")
        self.phase = ERROR

    def _source(self, channel: str):
        return next((src for src in self._sources if src.channel == channel), None)

    def _feeder(self, channel: str):
        pipeline = self._pipelines[channel]

        def on_block(t_arrival: float, block) -> None:
            if channel == "mic" and self.mic_paused:
                # Silencio no lugar do audio, e nao "nada": o relogio do canal
                # segue andando e o FLAC (se ligado) nao guarda o que foi dito.
                block = block * 0
            if self._audio is not None:
                self._audio.write(channel, block)
            before = pipeline.speech_seconds
            self._handle(pipeline.push(t_arrival, block))
            spoke = pipeline.speech_seconds - before
            if spoke > 0:
                self._last_speech = time.monotonic()
                if channel == "sys":
                    self._last_others_speech = self._last_speech
                    self._my_speech_since_others = 0.0
                else:
                    self._my_speech_since_others += spoke

        return on_block

    # ------------------------------------------------------- mic e travas

    def set_mic_paused(self, paused: bool) -> None:
        """Voce mutou no Teams? Pause aqui tambem: o mic para de ser transcrito."""
        if self.phase != RECORDING:
            return
        self.mic_paused = bool(paused)
        log.info("Mic %s", "pausado" if self.mic_paused else "retomado")
        self._emit(force=True)

    def check_limits(self, now: float | None = None) -> None:
        """Alerta de silencio e teto de duracao. Chamado pela thread `note`."""
        if self.phase != RECORDING or self.started_at is None:
            return
        now = time.monotonic() if now is None else now
        alert = float(self.config.get("silence_alert_minutes") or 0) * 60
        quiet = now - max(self._last_speech or 0.0, self.started_at)
        self.silent_minutes = int(quiet // 60) if alert > 0 and quiet >= alert else 0
        others_quiet = now - max(self._last_others_speech or 0.0, self.started_at)
        silent = (
            others_quiet >= OTHERS_SILENT_SECONDS
            and self._my_speech_since_others >= MY_SPEECH_FOR_ALERT
        )
        if silent and not self.others_silent:
            src = self._source("sys")
            outputs = [getattr(src, "device_name", ""), *getattr(src, "extra_names", [])]
            log.warning(
                "Canal Outros sem fala ha %.0f s, com %.0f s de fala no mic. "
                "A call pode estar tocando numa saida que nao estou ouvindo (ouvindo: %s).",
                others_quiet, self._my_speech_since_others, ", ".join(filter(None, outputs)) or "-",
            )
        self.others_silent = silent
        limit = float(self.config.get("max_session_hours") or 0) * 3600
        if limit > 0 and now - self.started_at >= limit and not self.limit_reached:
            log.warning("Teto de %.1f h atingido; parando sozinho.", limit / 3600)
            self.limit_reached = True

    def _handle(self, chunks: list[Chunk]) -> None:
        for chunk in chunks:
            try:
                seg = self.spool.add(chunk.channel, chunk.start, chunk.pcm)
            except ApoloError as exc:
                # Disco cheio ou sem permissao: perde este trecho, nao a reuniao.
                log.error("Spool: %s", exc)
                continue
            self.queue.submit(seg)
            if chunk.channel == "sys" and self.tracker is not None:
                self.tracker.add(chunk.start, chunk.pcm)
            self._dirty.set()

    # ----------------------------------------------------------------- stop

    def stop(self) -> Path | None:
        """Para de gravar e espera a fila (ver `drain`). Bloqueia."""
        if self.phase != RECORDING:
            return self.writer.path if self.writer else None
        self.phase = DRAINING
        for src in self._sources:
            src.stop()
        for pipeline in self._pipelines.values():
            self._handle(pipeline.close())
        if self._audio is not None:
            self._audio.close()
        self.meta.duration_seconds = time.monotonic() - (self.started_at or self._t0)
        log.info(
            "Fala detectada: mic %.0f s, outros %.0f s em %.0f s de gravacao",
            *(self._pipelines[c].speech_seconds if c in self._pipelines else 0.0
              for c in ("mic", "sys")),
            self.meta.duration_seconds,
        )
        try:
            self.spool.update(duration_seconds=round(self.meta.duration_seconds, 1))
        except ApoloError as exc:
            log.error("Spool: %s", exc)
        return self.drain()

    def drain(self) -> Path | None:
        """Espera a fila esvaziar e deixa a sessao em READY. Bloqueia.

        Sem internet, espera ate a conexao voltar (o overlay mostra). Se a
        fila parar por erro de chave, vai para ERROR e o audio fica no spool
        para retomar depois.
        """
        self._emit(force=True)
        while not self.queue.wait_idle(timeout=1.0):
            if self.queue.fatal is not None:
                break
            self._emit()
        self._drain_voices()

        self._stop_note.set()
        if self._note_thread is not None:
            self._note_thread.join(timeout=5)
        self.queue.stop()
        self._write_note()

        if self.queue.fatal is not None:
            self.phase = ERROR
            self._emit(force=True, error=self.queue.fatal)
            return self.writer.path
        self.phase = READY
        log.info("Transcricao completa: %d trechos", self.queue.transcribed)
        self._emit(force=True)
        return self.writer.path

    def _drain_voices(self) -> None:
        """A ultima janela de vozes. Espera ate `voice_wait_seconds`; o que
        nao chegar a tempo sai como "Outros" - a nota nunca fica presa."""
        tracker = self.tracker
        if tracker is None or self.queue.fatal is not None:
            return
        if not tracker.paused:
            tracker.flush()
        if tracker.busy():
            self._voices_pending = True
            deadline = time.monotonic() + float(self.config.get("voice_wait_seconds") or 240)
            while not tracker.wait_idle(timeout=1.0):
                self._emit()
                if time.monotonic() > deadline:
                    log.warning("Vozes: a ultima janela nao ficou pronta a tempo.")
                    break
            self._voices_pending = False
        tracker.stop(timeout=1.0)

    def voice_review(self, participants: list[str]) -> VoiceReview | None:
        """Vozes para o "Quem e quem", com as sugestoes da biblioteca.

        Faz uma ou duas chamadas curtas a API (~10 s cada). None = nada a
        perguntar. Nunca levanta: sem validacao, a nota sai com "Voz A".
        """
        if self.phase != READY or not self._voices_active(participants):
            return None
        self._identifying = True
        self._emit(force=True)
        try:
            entries = assign_voices(self.store.segments(), self.tracker.timeline())
            return build_review(
                self.tracker, entries, participants, str(self.config.get("my_name") or ""),
                self.library, self.transcriber,
                self.preset.languages or self.config.get("languages"),
            )
        except Exception:
            log.exception("Vozes: falha ao montar a validacao")
            return None
        finally:
            self._identifying = False
            self._emit(force=True)

    def _apply_voices(self, choice: FinalizeChoice, entries: list) -> list:
        """Poe as vozes nas falas e os nomes escolhidos na nota."""
        if not self._voices_active(self.meta.participants):
            return entries
        entries = assign_voices(entries, self.tracker.timeline())
        names = {v: n.strip() for v, n in (choice.voice_names or {}).items() if n and n.strip()}
        self.meta.voice_names = names
        # Quem ganhou nome estava na reuniao: entra na lista (e o "Resumir de
        # novo" reconhece o nome como alguem dos "Outros").
        for name in names.values():
            if not any(p.casefold() == name.casefold() for p in self.meta.participants):
                self.meta.participants.append(name)
        if choice.save_voices and names:
            for voice, name in names.items():
                clip = self.tracker.clip_pcm(voice)
                if clip is None or not len(clip):
                    continue
                try:
                    self.library.add_sample(name, clip)
                except Exception as exc:
                    log.error("Vozes: nao consegui guardar a amostra de %s: %s", name, exc)
        return entries

    def finalize(self, choice: FinalizeChoice, summarizer: Summarizer) -> Path | None:
        """Resumo, nota final, nome com o titulo, spool apagado. Bloqueia.

        None = nada foi dito nem anotado, e a nota foi descartada.
        """
        if self.phase != READY:
            raise RuntimeError(f"finalize() em {self.phase}")
        self.phase = SUMMARIZING
        self._emit(force=True)

        preset = choice.preset
        self.meta.preset_id, self.meta.preset_name = preset.id, preset.name
        self.meta.title = choice.title.strip()
        self.meta.participants = list(choice.participants)
        entries = self.store.visible(spool_gaps(self.spool.gaps()), None)
        entries = self._apply_voices(choice, entries)
        user_notes = self.writer.user_notes()
        if not user_notes and not any(isinstance(e, Segment) and e.text.strip() for e in entries):
            # Ninguem falou e voce nao anotou nada (iniciou sem querer, a
            # reuniao nem comecou): uma nota vazia so faria barulho no vault.
            return self._discard()
        summary, self.summary_error = build_summary(
            summarizer, self.meta, entries, user_notes, preset,
            str(self.config.get("my_name") or ""),
        )
        self.writer.write_final(self.meta, summary, entries)
        path = self.writer.rename_for(self.meta.start, self.meta.title or summary.title)

        # O audio era temporario. O que nao foi transcrito ja virou lacuna.
        try:
            self.spool.finish()
            self.spool.delete()
        except ApoloError as exc:
            log.error("Nao consegui limpar o spool: %s", exc)
        self.phase = DONE
        log.info("Nota pronta: %s", path)
        self._emit(force=True)
        return path

    def _discard(self) -> None:
        """Fim sem nota: apaga a nota ao vivo e o spool."""
        self.writer.discard()
        try:
            self.spool.finish()
            self.spool.delete()
        except ApoloError as exc:
            log.error("Nao consegui limpar o spool: %s", exc)
        self.discarded = True
        self.phase = DONE
        log.info("Nada foi transcrito; nenhuma nota criada.")
        self._emit(force=True)
        return None

    def abort(self) -> None:
        """App fechando no meio: para tudo e deixa o spool para retomar."""
        for src in self._sources:
            src.stop(timeout=1.0)
        if self._audio is not None:
            self._audio.close()
        self._stop_note.set()
        if self.queue is not None:
            self.queue.stop(timeout=1.0)
        if self.tracker is not None:
            self.tracker.stop(timeout=1.0)
        if self.writer is not None and self.meta is not None and self.phase in (
            RECORDING, DRAINING, PREPARING
        ):
            try:
                if self.phase == RECORDING:
                    self.spool.update(
                        duration_seconds=round(time.monotonic() - (self.started_at or self._t0), 1)
                    )
                self._write_note()
            except ApoloError:
                pass

    # ---------------------------------------------------------- nota ao vivo

    def _start_note_thread(self) -> None:
        self._note_thread = threading.Thread(target=self._note_loop, name="note", daemon=True)
        self._note_thread.start()

    def _note_loop(self) -> None:
        last = 0.0
        while not self._stop_note.is_set():
            self._dirty.wait(0.5)
            if self._stop_note.is_set():
                break
            self.check_limits()
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
        if self.tracker is not None and not self.tracker.paused:
            # Ao vivo: "Voz A", "Voz B". Os nomes so vem no fim.
            entries = assign_voices(entries, self.tracker.timeline())
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
            summary_error=self.summary_error,
            note_path=self.writer.path if self.writer else None,
            mic_paused=self.mic_paused and self.phase == RECORDING,
            silent_minutes=self.silent_minutes if self.phase == RECORDING else 0,
            limit_reached=self.limit_reached,
            others_silent=self.others_silent and self.phase == RECORDING,
            discarded=self.discarded,
            voices_pending=self._voices_pending,
            identifying=self._identifying,
        )
        if not force and status == self._last_status:
            return
        self._last_status = status
        try:
            self.on_status(status)
        except Exception:
            log.exception("Erro ao avisar a UI")
