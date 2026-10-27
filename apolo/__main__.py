"""Ponto de entrada do Apolo.

    python -m apolo              # roda o app (bandeja + atalho opcional)
    python -m apolo --settings   # abre as configuracoes (do app que ja roda)
    python -m apolo --set-key    # grava a API key (voce digita)
    python -m apolo --debug      # log detalhado no console

Ferramentas de desenvolvimento:
    python -m apolo --devices               # mics e saidas (loopback), por nome
    python -m apolo --test-capture [20]     # grava mic + loopback e mostra os cortes
    python -m apolo --from-wav arquivo.wav  # passa um WAV pelo VAD + chunker
    python -m apolo --simulate arquivo.wav  # reuniao de mentira, com STT e resumo de verdade

Modelo de threads:
    main          -> Tkinter (overlay, configuracoes, janela de parada, cronometro)
    tray          -> pystray, com seu proprio message loop
    hotkeys       -> hook de teclado do pynput (so se houver atalho configurado)
    capture-*     -> uma por fonte de audio: VAD, chunker e spool
    stt-*         -> fila de transcricao
    note          -> nota ao vivo
    session-*     -> abrir as fontes, terminar a fila, resumir (bloqueiam)

Tudo que vem das outras threads entra na UI por root.after(0, ...).
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import subprocess
import sys
import time
import tkinter as tk
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import filedialog, messagebox

from . import config as config_module
from . import i18n, secrets_store
from .app import AppController, Status
from .errors import ApoloError
from .i18n import t
from .notes.render import format_duration
from .notes.reparse import is_placeholder_title, read_note
from .ui.overlay import Overlay
from .ui.settings_window import SettingsWindow
from .ui.stop_dialog import ask_finalize as ask_finalize_window
from .ui.tray import Tray, tray_hosted

log = logging.getLogger("apolo")

_MUTEX_NAME = "Global\\Apolo.SingleInstance"
_ERROR_ALREADY_EXISTS = 183


def setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    # Sob o pythonw nao ha console: se o arquivo de log falhar, o app fica
    # mudo e um erro some sem rastro. Por isso, se o lugar normal nao
    # aceitar escrita, tenta a pasta de dados locais e registra o motivo.
    failures = []
    for folder in (config_module.config_dir(), config_module.data_dir()):
        try:
            log_path = folder / "apolo.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_path, maxBytes=512_000, backupCount=2, encoding="utf-8"
            )
            file_handler.setFormatter(fmt)
            root_logger.addHandler(file_handler)
            break
        except Exception as exc:
            failures.append(f"{folder}: {exc!r}")
    for failure in failures:
        log.warning("Log fora do lugar normal: %s", failure)

    if debug or sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root_logger.addHandler(stream)


def claim_single_instance() -> bool:
    """False se ja houver um Apolo rodando.

    Duas instancias gravariam a mesma reuniao duas vezes - e cobrariam a
    transcricao em dobro.
    """
    if sys.platform != "win32":
        from .linux import claim_single_instance as _claim

        return _claim()
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        return ctypes.get_last_error() != _ERROR_ALREADY_EXISTS
    except Exception:
        return True  # na duvida, deixa rodar


def cmd_set_key() -> int:
    """Grava a API key. A chave e digitada pelo usuario e nunca ecoa."""
    import getpass

    print(t("cli.set_key.prompt"))
    print(t("cli.set_key.cancel") + "\n")
    try:
        value = getpass.getpass(t("cli.set_key.label")).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return 130
    if not value:
        print(t("cli.set_key.cancelled"))
        return 1
    secrets_store.set_api_key(value)
    print(t("cli.set_key.saved", masked=secrets_store.masked(value)))
    return 0


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        from .linux import open_folder as _open

        _open(path)
        return
    try:
        os.startfile(path)  # type: ignore[attr-defined]
    except Exception:
        subprocess.Popen(["explorer", str(path)])


def open_file(path: Path) -> None:
    """Abre a nota no programa padrao para .md (Obsidian, VS Code...)."""
    if sys.platform != "win32":
        subprocess.Popen(["xdg-open", str(path)])
        return
    os.startfile(path)  # type: ignore[attr-defined]


def format_elapsed(seconds: float) -> str:
    seconds = int(max(0, seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def enable_dpi_awareness() -> None:
    """Sem isto, numa tela com escala (125%, 150%) o Windows estica a janela
    do Tk como bitmap e o texto fica borrado."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # por monitor
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # Windows antigo
        except Exception:
            pass


def run_app(open_settings: bool = False) -> int:
    enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    root.title("Apolo")
    # Um erro dentro de um callback do Tk nao pode fechar a janela invisivel
    # que segura o app inteiro.
    root.report_callback_exception = lambda *args: log.exception(
        "Erro em callback do Tkinter", exc_info=args
    )

    # Clicar no indicador abre a nota (ao vivo durante a reuniao).
    overlay = Overlay(root, on_click=lambda: open_last_note())
    tray: Tray | None = None
    # Sessao em andamento, para o cronometro: (nome do preset, inicio).
    clock: dict = {"preset": "", "started_at": None, "job": None}

    def recording_title() -> str:
        elapsed = format_elapsed(time.monotonic() - clock["started_at"])
        return f"{t('status.recording')} · {clock['preset']} · {elapsed}"

    def tick() -> None:
        """Atualiza o cronometro uma vez por segundo enquanto grava."""
        clock["job"] = None
        if clock["started_at"] is None:
            return
        title = recording_title()
        overlay.set_title(title)
        if tray is not None:
            tray.set_title(title)
        clock["job"] = root.after(1000, tick)

    def apply_status(status: Status) -> None:
        """Roda sempre na main thread."""
        if status.kind == "recording" and status.started_at is not None:
            clock["preset"], clock["started_at"] = status.preset, status.started_at
            title = recording_title()
            if clock["job"] is None:
                clock["job"] = root.after(1000, tick)
        else:
            clock["started_at"] = None
            title = status.message or (
                t(f"status.{status.kind}") if status.kind != "idle" else ""
            )

        if tray is not None:
            tray.set_status(status.kind, title)
            if status.notify:
                tray.notify(t("tray.tooltip"), status.notify)
        if _overlay_wanted(controller.config, status.kind):
            overlay.show(status.kind, title, status.detail)
        else:
            overlay.hide()

    def on_status(status: Status) -> None:
        """Chamado de qualquer thread; devolve o trabalho para a main."""
        try:
            root.after(0, apply_status, status)
        except RuntimeError:
            pass  # app ja esta fechando

    def ask_finalize(preset, duration: float, on_done) -> None:
        """Janela de parada. Chamada de qualquer thread."""
        title = t("stop.ended", duration=format_duration(duration))
        root.after(0, lambda: ask_finalize_window(
            root, title=title, presets=controller.presets, selected=preset,
            on_done=on_done, hint=t("stop.hint"),
        ))

    controller = AppController(on_status, ask_finalize)
    settings = SettingsWindow(
        root,
        on_saved=controller.reload,
        on_reload=controller.reload,
        on_open_folder=lambda: open_folder(config_module.config_dir()),
        is_recording=lambda: controller.busy,
    )

    def open_notes() -> None:
        try:
            open_folder(config_module.notes_dir(controller.config))
        except OSError as exc:
            apply_status(Status("error", t("err.notes_dir"), str(exc)))

    def open_last_note() -> None:
        path = controller.last_note
        if path is None:
            return
        try:
            open_file(path)
        except OSError as exc:
            apply_status(Status("error", t("err.note_open"), str(exc)))

    def resummarize() -> None:
        """Escolhe uma nota pronta e refaz o resumo com outro tipo."""
        if controller.busy:
            return
        notes = config_module.notes_dir(controller.config)
        chosen = filedialog.askopenfilename(
            title=t("tray.resummarize"),
            initialdir=str(notes if notes.exists() else Path.home()),
            filetypes=[("Markdown", "*.md")],
        )
        if not chosen:
            return
        path = Path(chosen)
        try:
            parsed = read_note(path)
        except ApoloError as exc:
            apply_status(Status("error", exc.message, exc.detail or ""))
            return
        title = "" if is_placeholder_title(parsed.title) else parsed.title
        ask_finalize_window(
            root,
            title=t("resummarize.title", name=path.stem),
            presets=controller.presets,
            selected=controller.preset_by_id(parsed.meta.preset_id),
            on_done=lambda choice: controller.resummarize(path, choice),
            hint=t("resummarize.hint"),
            initial_title=title,
            initial_participants=parsed.meta.participants,
            button=t("resummarize.button"),
        )

    def offer_recovery() -> None:
        """Sessao que ficou no spool (crash, app fechado no meio): retomar?"""
        if controller.busy:
            return
        sessions = controller.incomplete_sessions()
        if not sessions:
            return
        session = sessions[-1]  # a mais recente; as outras ficam para depois
        started = session.info.get("started_at") or session.info.get("created_at") or ""
        try:
            when = datetime.fromisoformat(started).strftime(t("note.header.date_fmt"))
        except ValueError:
            when = session.id
        answer = messagebox.askyesnocancel(
            "Apolo",
            t("recover.text", when=when, pending=len(session.pending())),
            icon="question",
        )
        if answer is True:
            controller.resume(session)
        elif answer is False and messagebox.askyesno(
            "Apolo", t("recover.confirm_discard"), icon="warning"
        ):
            controller.discard(session)

    def quit_app() -> None:
        log.info("Encerrando.")
        controller.shutdown()
        if tray is not None:
            tray.stop()
        try:
            root.quit()
        except Exception:
            pass

    tray = Tray(
        controller,
        on_open_notes=lambda: root.after(0, open_notes),
        on_open_last_note=lambda: root.after(0, open_last_note),
        on_resummarize=lambda: root.after(0, resummarize),
        on_settings=lambda: root.after(0, settings.open),
        on_quit=lambda: root.after(0, quit_app),
    )
    tray.run_detached()

    def handle_command(command: str) -> None:
        """Pedido de uma segunda invocacao do Apolo. Roda na main thread."""
        log.info("Comando recebido: %s", command)
        if command == "quit":
            quit_app()
        else:  # "settings" e qualquer coisa que nao reconhecemos
            settings.open()

    if sys.platform != "win32":
        from .linux import install_desktop_entry, serve_commands

        install_desktop_entry()
        serve_commands(lambda command: root.after(0, handle_command, command))

    controller.start()
    log.info(
        "Apolo pronto. Tipos de resumo: %s",
        ", ".join(p.name for p in controller.presets),
    )

    if open_settings:
        root.after(300, settings.open)

    root.after(1500, offer_recovery)

    if not secrets_store.get_api_key():
        root.after(400, settings.open)
        root.after(
            600,
            lambda: tray.notify(t("tray.tooltip"), t("tray.notify.no_key")),
        )

    if not tray_hosted():
        log.warning("Sem area de notificacao na sessao; o icone nao vai aparecer.")
        root.after(900, lambda: tray.notify(t("tray.tooltip"), t("tray.notify.no_tray")))

    try:
        root.mainloop()
    except KeyboardInterrupt:
        quit_app()
    return 0


def _overlay_wanted(config: dict, kind: str) -> bool:
    """O indicador flutuante deve aparecer para este estado?

    "idle" nunca aparece. Fora isso, quem manda e overlay_mode: "never"
    esconde tudo, "errors" deixa passar so os erros - o icone da bandeja
    continua mudando de cor nos tres casos.
    """
    if kind == "idle":
        return False
    mode = config.get("overlay_mode", "always")
    if mode == "never":
        return False
    if mode == "errors":
        return kind == "error"
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apolo")
    parser.add_argument(
        "--settings", action="store_true", help="abre a janela de configuracoes"
    )
    parser.add_argument("--set-key", action="store_true", help="grava a API key da OpenAI")
    parser.add_argument("--debug", action="store_true", help="log detalhado")
    parser.add_argument(
        "--devices", action="store_true", help="lista mics e saidas (loopback)"
    )
    parser.add_argument(
        "--test-capture", type=float, nargs="?", const=20.0, metavar="SEGUNDOS",
        help="grava mic + loopback e imprime os trechos cortados (padrao: 20 s)",
    )
    parser.add_argument(
        "--keep-spool", action="store_true",
        help="com --test-capture: guarda os WAVs dos trechos para ouvir",
    )
    parser.add_argument(
        "--from-wav", metavar="ARQUIVO", help="passa um WAV pelo VAD + chunker"
    )
    parser.add_argument(
        "--simulate", metavar="ARQUIVO",
        help="reuniao de mentira: o WAV entra como 'Outros', com transcricao de verdade",
    )
    parser.add_argument(
        "--speed", type=float, default=1.0, help="com --simulate: le o WAV N vezes mais rapido"
    )
    args = parser.parse_args(argv)

    setup_logging(args.debug)

    # Le o config para definir o idioma antes da primeira mensagem. Se ele
    # estiver quebrado, seguimos com os padroes e o idioma do sistema.
    try:
        config = config_module.load_config()
    except ApoloError:
        config = dict(config_module.DEFAULT_CONFIG)
        i18n.set_language(None)

    if args.set_key:
        return cmd_set_key()

    if args.devices or args.test_capture is not None or args.from_wav:
        from .audio import cli as audio_cli

        if args.devices:
            return audio_cli.cmd_devices(config)
        if args.from_wav:
            return audio_cli.cmd_from_wav(config, args.from_wav)
        return audio_cli.cmd_test_capture(config, args.test_capture, args.keep_spool)

    if args.simulate:
        from .session.simulate import cmd_simulate

        try:
            presets = config_module.load_presets()
        except ApoloError as exc:
            print(exc)
            return 1
        return cmd_simulate(config, presets, args.simulate, args.speed)

    if not claim_single_instance():
        if sys.platform != "win32":
            from .linux import send_command

            if send_command("settings"):
                print(t("cli.already_running.opened"))
                return 0
        print(t("cli.already_running"))
        return 1

    return run_app(open_settings=args.settings)


if __name__ == "__main__":
    raise SystemExit(main())
