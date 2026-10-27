"""Icone na bandeja do sistema.

Mesma montagem do Artemis: pystray numa thread propria, Tkinter com a main
thread, e todo callback que mexa em UI volta para a main via root.after().
  Windows - run_detached(), que bombeia as mensagens da janela escondida.
  Linux - run() dentro de uma thread nossa, porque o run_detached() do
    backend AppIndicator nao sobe laco GLib nenhum (ver o tray do Artemis).

O menu:
  Gravando ha 00:12 (Daily)        <- estado, nao clicavel
  Iniciar gravacao (Geral)         <- item default: liga/desliga
  Iniciar com o tipo de resumo  >  <- um item por preset
  Abrir ultima nota                <- depois da primeira gravacao
  Abrir pasta das notas
  Configuracoes...
  Sair
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Callable

import pystray

from ..i18n import t
from .icon import COLORS as _COLORS
from .icon import make_icon

log = logging.getLogger(__name__)

# Traduzidos na hora de exibir, nao aqui: o idioma pode mudar em tempo
# de execucao e o menu do tray e reconstruido a cada abertura.
_LABEL_KEYS = {
    "idle": "status.ready",
    "preparing": "status.preparing",
    "recording": "status.recording",
    "processing": "status.processing",
    "done": "status.ready",
    "error": "status.error",
}


def tray_hosted() -> bool:
    """A sessao tem quem exiba um icone de bandeja?"""
    if sys.platform == "win32":
        return True
    from ..linux import status_notifier_available

    return status_notifier_available()


def _menu_text(text: str) -> str:
    # No menu do Windows, "&" vira marcador de atalho e some da tela.
    return text.replace("&", "&&")


class Tray:
    def __init__(
        self,
        controller,
        *,
        on_open_notes: Callable[[], None],
        on_open_last_note: Callable[[], None],
        on_settings: Callable[[], None],
        on_quit: Callable[[], None],
    ):
        self._controller = controller
        self._status_kind = "idle"
        self._status_text = t("status.ready")
        self._icons = {kind: make_icon(color) for kind, color in _COLORS.items()}

        self._on_open_notes = on_open_notes
        self._on_open_last_note = on_open_last_note
        self._on_settings = on_settings
        self._on_quit = on_quit
        self._thread: threading.Thread | None = None

        # O menu inteiro e um callable: pystray o reavalia a cada abertura,
        # entao mudar presets.json e recarregar ja muda o menu.
        self._icon = pystray.Icon(
            "apolo",
            icon=self._icons["idle"],
            title="Apolo",
            menu=pystray.Menu(self._menu_items),
        )

    def _menu_items(self):
        yield pystray.MenuItem(_menu_text(self._status_text), None, enabled=False)
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem(
            self._toggle_label(),
            self._toggle,
            default=True,
            # Preparando ou terminando de transcrever: nao ha o que alternar.
            enabled=lambda item: self._controller.can_stop or not self._controller.busy,
        )
        yield pystray.MenuItem(
            t("tray.start_with"),
            pystray.Menu(self._preset_items),
            enabled=lambda item: not self._controller.busy,
        )
        yield pystray.Menu.SEPARATOR
        if self._controller.last_note is not None:
            yield pystray.MenuItem(t("tray.open_last_note"), self._on_open_last_note)
        yield pystray.MenuItem(t("tray.open_notes"), self._on_open_notes)
        yield pystray.MenuItem(t("tray.settings"), self._on_settings)
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem(t("tray.quit"), self._on_quit)

    def _preset_items(self):
        default = self._controller.default_preset()
        for preset in self._controller.presets:
            yield pystray.MenuItem(
                _menu_text(preset.name),
                self._make_start(preset),
                checked=lambda item, p=preset: default is not None and p.id == default.id,
                radio=True,
            )

    def _toggle_label(self) -> str:
        if self._controller.can_stop:
            return t("tray.toggle.stop")
        if self._controller.busy:
            return t("tray.toggle.wait")
        preset = self._controller.default_preset()
        return _menu_text(t("tray.toggle.start", name=preset.name if preset else ""))

    def _toggle(self, icon=None, item=None) -> None:
        self._controller.toggle()

    def _make_start(self, preset):
        def handler(icon=None, item=None):
            self._controller.begin(preset)

        return handler

    def run_detached(self) -> None:
        """Sobe o icone sem tomar a main thread, que e do Tkinter."""
        if sys.platform == "win32":
            self._icon.run_detached()
            return
        self._thread = threading.Thread(
            target=self._icon.run, name="apolo-tray", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        try:
            self._icon.stop()
        except Exception:
            pass

    def set_status(self, kind: str, text: str = "") -> None:
        """`text` ja vem pronto (com cronometro e preset); senao usa o rotulo."""
        self._status_kind = kind
        self._status_text = text or t(_LABEL_KEYS.get(kind, "status.ready"))

        try:
            self._icon.icon = self._icons.get(kind, self._icons["idle"])
            # 128 caracteres e o limite do tooltip da bandeja no Windows.
            self._icon.title = f"Apolo - {self._status_text}"[:127]
            self._icon.update_menu()
        except Exception:
            log.debug("Nao consegui atualizar o icone da bandeja", exc_info=True)

    def set_title(self, text: str) -> None:
        """So o tooltip e a linha de estado (cronometro), sem trocar o icone."""
        self._status_text = text
        try:
            self._icon.title = f"Apolo - {text}"[:127]
        except Exception:
            pass

    def notify(self, title: str, message: str) -> None:
        try:
            self._icon.notify(message, title)
        except Exception:
            log.debug("Notificacao nao suportada nesta plataforma", exc_info=True)
