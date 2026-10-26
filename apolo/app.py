"""AppController: liga a UI (bandeja, overlay, atalho) a sessao de gravacao.

    IDLE --iniciar--> RECORDING --parar--> IDLE

Fase 1: a sessao ainda e um esqueleto - iniciar so liga o cronometro e
parar so volta para IDLE. A captura (fase 2), a transcricao (fase 3) e o
resumo (fase 4) entram em session/, e os estados PREPARING, DRAINING e
FINALIZING do PLANO.md entram junto com eles.

Regra herdada do Artemis: nenhuma excecao pode derrubar o app. Um erro vira
um aviso no overlay e volta para IDLE.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from . import config as config_module
from . import startup
from .errors import ApoloError
from .hotkeys import HotkeyManager
from .i18n import t
from .presets import SummaryPreset

log = logging.getLogger(__name__)

# Id do unico atalho do Apolo no HotkeyManager (que vem do Artemis e aceita
# varios, um por preset).
_TOGGLE_ID = "toggle"


class State(Enum):
    IDLE = "idle"
    RECORDING = "recording"


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


class AppController:
    def __init__(self, on_status: StatusCallback):
        self._on_status = on_status
        self._state = State.IDLE
        self._lock = threading.Lock()
        self._active_preset: SummaryPreset | None = None
        self._started_at: float | None = None

        self.config: dict = {}
        self.presets: list[SummaryPreset] = []
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
        reportados na UI, mas o app continua de pe com o que ja tinha.
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
        with self._lock:
            self._state = State.IDLE

    # ------------------------------------------------------------ presets

    def default_preset(self) -> SummaryPreset | None:
        wanted = self.config.get("default_preset")
        for preset in self.presets:
            if preset.id == wanted:
                return preset
        return self.presets[0] if self.presets else None

    @property
    def busy(self) -> bool:
        """Ha uma sessao em andamento (o menu mostra 'Parar')."""
        return self._state is not State.IDLE

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
        with self._lock:
            if self._state is not State.IDLE:
                return  # ja ha uma sessao em andamento
            self._state = State.RECORDING
            self._active_preset = preset
            self._started_at = time.monotonic()
        log.info("Sessao iniciada (%s).", preset.id)
        self._emit(
            Status("recording", preset=preset.name, started_at=self._started_at)
        )

    def finish(self) -> None:
        with self._lock:
            if self._state is not State.RECORDING:
                return
            self._state = State.IDLE
            preset, self._active_preset = self._active_preset, None
            started, self._started_at = self._started_at, None
        elapsed = time.monotonic() - (started or time.monotonic())
        log.info("Sessao encerrada (%s, %.0fs).", preset.id if preset else "?", elapsed)
        self._emit(
            Status("done", t("status.stub_stopped"), t("status.stub_stopped.detail"))
        )

    # ------------------------------------------------------------ helpers

    def _emit(self, status: Status) -> None:
        try:
            self._on_status(status)
        except Exception:
            log.exception("Erro ao atualizar a interface")

    @property
    def state(self) -> State:
        return self._state
