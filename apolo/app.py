"""AppController: liga a UI (bandeja, overlay, atalho) a sessao de gravacao.

    IDLE --iniciar--> PREPARING --fontes abertas--> RECORDING
      ^                                                 |
      +--- nota pronta / erro <--- FINISHING <------- parar
                                   (resto da fila + janela de parada + resumo)

FINISHING tambem cobre "retomar uma sessao que ficou no spool" e "resumir de
novo uma nota pronta": nos tres casos nao da para comecar outra gravacao.

A sessao em si (captura, spool, fila de STT, nota) mora em
session/session.py. Aqui so ficam o estado que a UI enxerga, a traducao do
SessionStatus para o Status do overlay/bandeja e as threads que impedem a
UI de travar: abrir as fontes leva ate ~5 s (Bluetooth), esperar a fila no
fim leva o tempo que a internet demorar para voltar, e o resumo, alguns
segundos.

A janela de parada abre assim que voce clica em Parar (`ask_finalize`, que
a UI fornece). O resumo comeca quando as duas coisas terminaram: a fila e a
janela.

Regra herdada do Artemis: nenhuma excecao pode derrubar o app. Um erro vira
um aviso no overlay e volta para IDLE.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from . import config as config_module
from . import startup
from .errors import ApoloError, CredentialsError
from .hotkeys import HotkeyManager
from .i18n import t
from .presets import SummaryPreset
from .providers.openai_provider import OpenAIProvider
from .secrets_store import get_api_key
from .session.session import DONE, DRAINING, ERROR, PREPARING, READY, RECORDING, SUMMARIZING
from .session.session import MeetingSession, SessionStatus
from .session.spool import Spool, SpoolSession
from .session.summary import FinalizeChoice, resummarize

log = logging.getLogger(__name__)

# Id do unico atalho do Apolo no HotkeyManager (que vem do Artemis e aceita
# varios, um por preset).
_TOGGLE_ID = "toggle"


class State(Enum):
    IDLE = "idle"
    PREPARING = "preparing"
    RECORDING = "recording"
    FINISHING = "finishing"


@dataclass
class Status:
    """O que a UI precisa saber. O campo kind escolhe o icone e a cor."""

    kind: str  # idle | preparing | recording | processing | done | error
    message: str = ""
    detail: str = ""
    # Em "recording": nome do preset e o instante (monotonic) do inicio, para
    # a UI montar o cronometro sozinha sem o controller emitir a cada segundo.
    preset: str = ""
    started_at: float | None = None


StatusCallback = Callable[[Status], None]
# (preset marcado, duracao em s, callback com a escolha). Chamado de qualquer
# thread; a UI abre a janela na main thread e chama o callback uma vez.
AskFinalize = Callable[[SummaryPreset, float, Callable[[FinalizeChoice], None]], None]


class AppController:
    def __init__(self, on_status: StatusCallback, ask_finalize: AskFinalize | None = None):
        self._on_status = on_status
        self._ask_finalize = ask_finalize
        self._state = State.IDLE
        self._lock = threading.Lock()
        self._session: MeetingSession | None = None

        self.config: dict = {}
        self.presets: list[SummaryPreset] = []
        # Ultima nota escrita, para o "Abrir ultima nota" da bandeja.
        self.last_note: Path | None = None
        self._hotkeys = HotkeyManager(self._on_hotkey_event)

    # ------------------------------------------------------- ciclo de vida

    def start(self) -> None:
        self.reload()
        # No Linux o start() pode falhar por permissao em /dev/input. Isso nao
        # pode derrubar o app: a bandeja continua servindo para gravar.
        try:
            self._hotkeys.start()
        except ApoloError as exc:
            log.error("Atalho indisponivel: %s", exc)
            self._emit(Status("error", exc.message, exc.detail or ""))

    def reload(self) -> None:
        """Recarrega config/presets e reprograma o atalho.

        Chamado no boot e sempre que as configuracoes mudam. Erros aqui sao
        reportados na UI, mas o app continua de pe com o que ja tinha. Uma
        sessao em andamento segue com a config de quando comecou.
        """
        try:
            self.config = config_module.load_config()
            self.presets = config_module.load_presets()
        except ApoloError as exc:
            self._emit(Status("error", exc.message, exc.detail or ""))
            return

        # A inicializacao com o sistema mora no registro (ou no autostart do
        # XDG), nao no config: aplicar aqui mantem os dois em sincronia.
        try:
            startup.sync(bool(self.config.get("start_with_system", False)))
        except ApoloError as exc:
            self._emit(Status("error", exc.message, exc.detail or ""))

        self._hotkeys.clear()
        hotkey = (self.config.get("hotkey") or "").strip()
        if hotkey:
            try:
                self._hotkeys.register(_TOGGLE_ID, hotkey, "toggle")
            except ApoloError as exc:
                self._emit(Status("error", t("err.hotkey_problems"), exc.message))
                return

        # Recarregar no meio de uma reuniao nao pode apagar o "Gravando".
        if self._state is State.IDLE:
            self._emit(Status("idle"))

    def shutdown(self) -> None:
        self._hotkeys.stop()
        session, self._session = self._session, None
        if session is not None:
            # O que nao foi transcrito/resumido fica no spool: na proxima vez
            # que o app abrir, ele oferece retomar.
            session.abort()
        with self._lock:
            self._state = State.IDLE

    # ------------------------------------------------------------ presets

    def default_preset(self) -> SummaryPreset | None:
        wanted = self.config.get("default_preset")
        for preset in self.presets:
            if preset.id == wanted:
                return preset
        return self.presets[0] if self.presets else None

    def preset_by_id(self, preset_id: str | None) -> SummaryPreset | None:
        return next((p for p in self.presets if p.id == preset_id), None) or self.default_preset()

    @property
    def busy(self) -> bool:
        """Ha uma sessao em andamento (o menu mostra 'Parar' ou 'Aguarde')."""
        return self._state is not State.IDLE

    @property
    def can_stop(self) -> bool:
        return self._state is State.RECORDING

    # ------------------------------------------------------------ comandos

    def _on_hotkey_event(self, hotkey_id: str, event: str) -> None:
        if hotkey_id == _TOGGLE_ID and event == "activate":
            self.toggle()

    def toggle(self) -> None:
        if self.busy:
            self.finish()
            return
        preset = self.default_preset()
        if preset is None:
            self._emit(Status("error", t("err.no_presets")))
            return
        self.begin(preset)

    def begin(self, preset: SummaryPreset) -> None:
        if not self._claim(State.PREPARING):
            return  # ja ha uma sessao em andamento
        try:
            provider = self._make_provider()
        except ApoloError as exc:
            self._reset(Status("error", exc.message, exc.detail or ""))
            return
        session = MeetingSession(self.config, preset, provider, on_status=self._on_session_status)
        self._session = session
        self._spawn("session-start", self._start_session, session)

    def _start_session(self, session: MeetingSession) -> None:
        try:
            session.start()
        except Exception as exc:
            self._session = None
            self._fail(exc, "Falha ao iniciar a sessao")

    def finish(self) -> None:
        with self._lock:
            if self._state is not State.RECORDING or self._session is None:
                return  # preparando ou ja finalizando: nada a fazer
            self._state = State.FINISHING
            session = self._session
        choice = self._request_choice(session)
        self._spawn("session-stop", self._finish_session, session, choice, True)

    def _finish_session(self, session: MeetingSession, choice: "_Choice", stop: bool) -> None:
        """Thread: termina a fila, espera a janela de parada e resume."""
        try:
            if stop:
                session.stop()
            else:
                session.drain()
            if session.phase != READY:
                return  # ERROR: o status ja foi para a UI
            session.finalize(choice.wait(), session.transcriber)
        except Exception as exc:
            self._fail(exc, "Falha ao encerrar a sessao")
        finally:
            if self._session is session:
                self._session = None

    def _request_choice(self, session: MeetingSession) -> "_Choice":
        """Abre a janela de parada (ou nao, com on_stop = auto)."""
        choice = _Choice(FinalizeChoice(session.preset))
        if self.config.get("on_stop") == "auto" or self._ask_finalize is None:
            choice.set(choice.default)
            return choice
        if session.started_at is not None:  # acabou de parar: stop() ainda nao mediu
            duration = time.monotonic() - session.started_at
        else:  # sessao retomada
            duration = session.meta.duration_seconds if session.meta else 0.0
        try:
            self._ask_finalize(session.preset, duration, choice.set)
        except Exception:
            log.exception("Nao consegui abrir a janela de parada")
            choice.set(choice.default)
        return choice

    # ------------------------------------------------- retomar / descartar

    def incomplete_sessions(self) -> list[SpoolSession]:
        """Sessoes que ficaram no spool (crash, app fechado no meio)."""
        try:
            spool = Spool.from_config(self.config)
        except Exception:
            return []
        exclude = self._session.spool.id if self._session and self._session.spool else None
        return [s for s in spool.incomplete_sessions(exclude) if s.info.get("kind") == "meeting"]

    def resume(self, spool_session: SpoolSession) -> None:
        if not self._claim(State.FINISHING):
            return
        preset = self.preset_by_id(spool_session.info.get("preset"))
        try:
            if preset is None:
                raise ApoloError(t("err.no_presets"))
            provider = self._make_provider()
            session = MeetingSession.resume(
                self.config, preset, provider, spool_session, on_status=self._on_session_status
            )
        except Exception as exc:
            self._fail(exc, "Falha ao retomar a sessao")
            return
        self._session = session
        choice = self._request_choice(session)
        self._spawn("session-resume", self._finish_session, session, choice, False)

    def discard(self, spool_session: SpoolSession) -> None:
        """Apaga o audio que sobrou. A nota (se houver) fica como esta."""
        try:
            spool_session.delete()
        except ApoloError as exc:
            self._emit(Status("error", exc.message, exc.detail or ""))

    # ------------------------------------------------------ resumir de novo

    def resummarize(self, path: Path, choice: FinalizeChoice) -> None:
        if not self._claim(State.FINISHING):
            return
        self._emit(Status("processing", t("status.summarizing"), path.name))
        self._spawn("resummarize", self._resummarize, path, choice)

    def _resummarize(self, path: Path, choice: FinalizeChoice) -> None:
        try:
            new_path = resummarize(
                path, choice, self._make_provider(), str(self.config.get("my_name") or "")
            )
        except Exception as exc:
            self._fail(exc, "Falha ao resumir de novo")
            return
        self.last_note = new_path
        self._reset(Status("done", t("status.note_saved"), new_path.name))

    def _make_provider(self) -> OpenAIProvider:
        api_key = get_api_key()
        if not api_key:
            raise CredentialsError(t("err.no_api_key"), t("err.no_api_key.detail"))
        return OpenAIProvider(
            api_key=api_key,
            stt_model=self.config["stt_model"],
            text_model=self.config["text_model"],
            timeout=float(self.config.get("request_timeout", 60)),
        )

    # ------------------------------------------------------ sessao -> UI

    def _on_session_status(self, s: SessionStatus) -> None:
        """Traduz o estado da sessao para o overlay/bandeja. Qualquer thread."""
        if s.note_path is not None:
            self.last_note = s.note_path
        if s.phase == PREPARING:
            self._emit(Status("preparing", t("status.preparing"), t("status.preparing.detail")))
        elif s.phase == RECORDING:
            with self._lock:
                if self._state is State.PREPARING:
                    self._state = State.RECORDING
            self._emit(Status("recording", detail=_recording_detail(s),
                              preset=s.preset, started_at=s.started_at))
        elif s.phase == DRAINING:
            self._emit(Status("processing", t("status.draining"), _queue_detail(s)))
        elif s.phase == READY:
            self._emit(Status("processing", t("status.waiting_choice")))
        elif s.phase == SUMMARIZING:
            self._emit(Status("processing", t("status.summarizing")))
        elif s.phase == DONE:
            name = s.note_path.name if s.note_path else ""
            if s.summary_error is not None:
                self._reset(Status("error", t("status.saved_without_summary"),
                                   f"{s.summary_error.message}\n{name}"))
            else:
                self._reset(Status("done", t("status.note_saved"), name))
        elif s.phase == ERROR:
            err = s.error
            message = err.message if err else t("err.unexpected")
            detail = (err.detail or "") if err else ""
            if s.note_path is not None:
                detail = (detail + "\n" if detail else "") + t("status.audio_kept")
            self._reset(Status("error", message, detail))

    # ------------------------------------------------------------ helpers

    def _claim(self, state: State) -> bool:
        with self._lock:
            if self._state is not State.IDLE:
                return False
            self._state = state
            return True

    def _spawn(self, name: str, target, *args) -> None:
        threading.Thread(target=target, args=args, name=name, daemon=True).start()

    def _fail(self, exc: Exception, context: str) -> None:
        if isinstance(exc, ApoloError):
            self._reset(Status("error", exc.message, exc.detail or ""))
        else:
            log.exception(context)
            self._reset(Status("error", t("err.unexpected"), str(exc)))

    def _reset(self, status: Status) -> None:
        with self._lock:
            self._state = State.IDLE
        self._emit(status)

    def _emit(self, status: Status) -> None:
        try:
            self._on_status(status)
        except Exception:
            log.exception("Erro ao atualizar a interface")

    @property
    def state(self) -> State:
        return self._state


class _Choice:
    """A escolha da janela de parada, entregue de uma thread para outra."""

    def __init__(self, default: FinalizeChoice):
        self.default = default
        self._value: FinalizeChoice | None = None
        self._event = threading.Event()

    def set(self, value: FinalizeChoice | None) -> None:
        if not self._event.is_set():
            self._value = value or self.default
            self._event.set()

    def wait(self) -> FinalizeChoice:
        self._event.wait()
        return self._value or self.default


def _queue_detail(s: SessionStatus) -> str:
    parts = []
    if s.pending:
        parts.append(t("status.pending", n=s.pending, seconds=round(s.pending_seconds)))
    if s.offline:
        parts.append(t("status.offline"))
    elif s.retrying:
        parts.append(t("status.retrying"))
    return " · ".join(parts)


def _recording_detail(s: SessionStatus) -> str:
    parts = []
    for channel in s.channel_problems:
        parts.append(t(f"status.channel_down.{channel}"))
    # Fila normal (1-2 trechos) nao interessa; so quando esta acumulando.
    if s.offline or s.retrying or s.pending > 3:
        parts.append(_queue_detail(s))
    return "\n".join(p for p in parts if p)
