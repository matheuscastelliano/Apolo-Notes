"""Implementacoes especificas de Linux (Wayland e X11).

Reune o que no Windows mora em startup_win.py e nos poucos pontos Win32 de
__main__.py e app.py. O modulo startup.py e um shim que escolhe entre este
arquivo e o _win pelo sys.platform.

Veio do Artemis Dictation sem a parte de clipboard e colagem: o Apolo nao
injeta texto em lugar nenhum, so escreve arquivos. Os atalhos globais ficam
em hotkeys_evdev.py, pelo mesmo motivo do Artemis (o pynput nao enxerga
teclas sob Wayland).
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from .errors import ApoloError
from .i18n import t

log = logging.getLogger(__name__)


# -------------------------------------------------------------------- som

def beep(frequency: int, duration_ms: int = 70) -> None:
    """Bipe curto pelo sounddevice, que ja e dependencia (nao ha winsound aqui).

    Sintetiza um seno com fade de 5 ms nas pontas: sem o fade, o corte abrupto
    estala mais alto que o proprio bipe.
    """
    import sounddevice as sd

    rate = 44100
    total = int(rate * duration_ms / 1000)
    fade = int(rate * 0.005)
    samples = bytearray()
    for i in range(total):
        gain = 0.25
        if i < fade:
            gain *= i / fade
        elif i > total - fade:
            gain *= max(0, total - i) / fade
        value = int(32767 * gain * math.sin(2 * math.pi * frequency * i / rate))
        samples += struct.pack("<h", value)

    with sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16") as stream:
        stream.write(bytes(samples))


# ------------------------------------- instancia unica e comandos entre elas

_lock_socket = None

# Socket Unix no namespace abstrato (o \0 inicial). Vantagem sobre um arquivo
# de lock: o kernel libera o nome sozinho quando o processo morre, entao um
# crash nao deixa lock orfao travando a proxima execucao. Alem de garantir a
# instancia unica, ele e por onde a segunda invocacao pede alguma coisa a
# primeira - abrir as configuracoes, tipicamente.
_ADDRESS = "\0Apolo"


def claim_single_instance() -> bool:
    """False se ja houver um Apolo rodando."""
    global _lock_socket
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(_ADDRESS)
    except OSError:
        sock.close()
        return False
    sock.listen(4)
    _lock_socket = sock  # referencia viva pelo resto do processo: solta no exit
    return True


def serve_commands(handler) -> None:
    """Atende os pedidos de outras invocacoes do Apolo, numa thread.

    Sem isto, clicar no Apolo no menu de aplicativos com o app ja rodando
    nao faria nada visivel - e, sem icone na bandeja, nao haveria como chegar
    nas configuracoes.
    """
    sock = _lock_socket
    if sock is None:
        return

    def loop() -> None:
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return  # socket fechado: o app esta encerrando
            try:
                with conn:
                    conn.settimeout(2)
                    command = conn.recv(64).decode("utf-8", "replace").strip()
            except OSError:
                continue
            if not command:
                continue
            try:
                handler(command)
            except Exception:
                log.exception("Erro ao tratar o comando '%s'", command)

    threading.Thread(target=loop, name="apolo-ipc", daemon=True).start()


def send_command(command: str, timeout: float = 2.0) -> bool:
    """Manda um comando para o Apolo que ja esta rodando."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(_ADDRESS)
        sock.sendall(command.encode("utf-8") + b"\n")
        return True
    except OSError:
        return False
    finally:
        sock.close()


# ------------------------------------------------------------ area de notificacao

def status_notifier_available() -> bool:
    """A sessao tem quem hospede um icone de bandeja?

    O GNOME nao implementa area de notificacao no proprio Shell: quem oferece
    o servico (org.kde.StatusNotifierWatcher, o protocolo que o AppIndicator
    fala) e a extensao AppIndicator. Sem ela no ar, o icone do Apolo nao
    aparece em lugar nenhum - e o usuario precisa saber disso, senao o app
    parece nao ter subido.
    """
    try:
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        answer = bus.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
            GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
        return bool(answer.unpack()[0])
    except Exception:
        log.debug("Nao consegui consultar o StatusNotifierWatcher", exc_info=True)
        return True  # na duvida, nao alarma o usuario


def open_folder(path) -> None:
    subprocess.Popen(["xdg-open", str(path)])


# ------------------------------------------------- menu de aplicativos

DESKTOP_FILE = "apolo.desktop"
AUTOSTART_FILE = DESKTOP_FILE
ICON_NAME = "apolo"
# Os tamanhos que o GNOME procura para menu, dock e Alt-Tab.
ICON_SIZES = (48, 64, 128, 256)


def _data_home() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    return Path(base) if base else Path.home() / ".local" / "share"


def _autostart_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "autostart" / AUTOSTART_FILE


def _desktop_path() -> Path:
    return _data_home() / "applications" / DESKTOP_FILE


def _icon_path(size: int) -> Path:
    return (
        _data_home()
        / "icons"
        / "hicolor"
        / f"{size}x{size}"
        / "apps"
        / f"{ICON_NAME}.png"
    )


def install_icon() -> None:
    """Grava o icone do app no tema do usuario, num tamanho por resolucao."""
    from .ui.icon import COLORS, make_icon

    for size in ICON_SIZES:
        path = _icon_path(size)
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        make_icon(COLORS["idle"], size).save(path)


def _entry_text(*, autostart: bool) -> str:
    """O .desktop, igual para o menu e para a inicializacao automatica."""
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=Apolo Dictation",
        f"Comment={t('app.tagline')}",
        f"Exec={command()}",
        f"Icon={ICON_NAME}",
        "Terminal=false",
        "StartupNotify=false",
        "Categories=Utility;Office;",
        f"Keywords={t('desktop.keywords')}",
    ]
    if autostart:
        lines.append("X-GNOME-Autostart-enabled=true")
    else:
        # Botao direito no icone do dock -> "Configuracoes".
        lines += [
            "Actions=settings;",
            "",
            "[Desktop Action settings]",
            f"Name={t('desktop.settings')}",
            f"Exec={command()} --settings",
        ]
    return "\n".join(lines) + "\n"


def install_desktop_entry() -> None:
    """Poe o Apolo no menu de aplicativos (e no dock, se o usuario fixar).

    E o unico caminho garantido ate o app: a bandeja do sistema depende de
    uma extensao do GNOME que pode nao estar no ar, e sem ela nao havia como
    abrir as configuracoes senao pelo terminal. Clicar aqui com o app ja
    rodando abre as configuracoes, via o socket de instancia unica.
    """
    try:
        install_icon()
        path = _desktop_path()
        text = _entry_text(autostart=False)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            log.info("Entrada no menu de aplicativos: %s", path)
            _refresh_desktop_database(path.parent)
    except OSError as exc:
        # Nao ter atalho no menu e um incomodo, nao um motivo para nao subir.
        log.warning("Nao consegui instalar a entrada no menu: %s", exc)


def _refresh_desktop_database(directory: Path) -> None:
    """Faz o GNOME reparar no .desktop novo sem esperar o proximo login."""
    if not shutil.which("update-desktop-database"):
        return
    try:
        subprocess.run(
            ["update-desktop-database", str(directory)], timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        pass


# --------------------------------------------------------------- autostart


def command() -> str:
    """Linha de comando que a sessao vai executar no login."""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    # run.pyw insere a propria pasta no sys.path, entao roda de qualquer cwd -
    # que e o que importa aqui, porque .desktop nao define diretorio de trabalho.
    launcher = Path(__file__).resolve().parent.parent / "run.pyw"
    return f'"{Path(sys.executable)}" "{launcher}"'


def registered_command() -> str | None:
    path = _autostart_path()
    if not path.exists():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("Exec="):
                return line[len("Exec="):].strip()
    except OSError:
        pass
    return None


def is_enabled() -> bool:
    return registered_command() is not None


def set_enabled(enabled: bool) -> None:
    path = _autostart_path()
    try:
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            install_icon()
            path.write_text(_entry_text(autostart=True), encoding="utf-8")
            log.info("Inicializacao automatica ligada: %s", path)
        else:
            path.unlink(missing_ok=True)
            log.info("Inicializacao automatica desligada.")
    except OSError as exc:
        raise ApoloError(t("err.startup_write"), str(exc)) from exc


def sync(enabled: bool) -> None:
    """Aplica o valor do config, so escrevendo quando algo muda de fato."""
    if enabled == is_enabled():
        if enabled and registered_command() != command():
            set_enabled(True)  # mesmo estado, caminho antigo: corrige
        return
    set_enabled(enabled)
