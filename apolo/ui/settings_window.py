"""Janela de configuracao (Tkinter, que ja vem com o Python).

Esqueleto do Artemis: rodape empacotado ANTES do conteudo (side="bottom")
para o Salvar nunca sumir quando a janela encolhe, conteudo rolavel, e todo
texto passando por i18n.t() na hora de montar.

Quatro abas:
  Geral    - chave, modelos, idiomas da fala, termos, interface, atalhos
  Audio    - mic e saida (loopback), com teste de 5 s e medidor de nivel
  Notas    - pasta, seu nome, tipo padrao, janela de parada, formato da
             transcricao, salvar audio, travas de seguranca
  Presets  - os tipos de resumo (o que vai em presets.json)

Nada aqui e obrigatorio: tudo tambem pode ser editado direto nos JSONs.
"""

from __future__ import annotations

import logging
import threading
import time
import tkinter as tk
from tkinter import filedialog
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Callable

import numpy as np

from .. import config as config_module
from .. import secrets_store, startup
from ..hotkeys import describe
from ..i18n import AUTO, LANGUAGE_NAMES, t
from ..presets import SummaryPreset

log = logging.getLogger(__name__)

_STT_MODELS = ["gpt-transcribe"]
_TEXT_MODELS = ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]

_HINT = "#6b6b70"
_PAD = 12
# Coluna dos rotulos + espacamentos, descontada da largura das dicas.
_LABEL_COLUMN = 165
# Medidor: -60 dBFS (barra vazia) ate 0 dBFS (cheia).
_METER_FLOOR_DB = -60.0
_TEST_SECONDS = 5.0


class SettingsWindow:
    """Uma unica instancia; reabrir traz a janela existente para frente."""

    def __init__(
        self,
        root: tk.Tk,
        *,
        on_saved: Callable[[], None],
        on_reload: Callable[[], None],
        on_open_folder: Callable[[], None],
        is_recording: Callable[[], bool] = lambda: False,
    ):
        self._root = root
        self._on_saved = on_saved
        self._on_reload = on_reload
        self._on_open_folder = on_open_folder
        self._is_recording = is_recording
        self._win: tk.Toplevel | None = None
        self._presets: list[SummaryPreset] = []
        self._selected: int | None = None
        self._suspend_select = False
        self._test: dict | None = None

    def open(self) -> None:
        if self._win is not None and self._win.winfo_exists():
            self._win.deiconify()
            self._win.lift()
            self._win.focus_force()
            return
        self._build()

    # --------------------------------------------------------- construcao

    def _build(self) -> None:
        self._config = config_module.load_config()
        self._presets = config_module.load_presets()

        win = tk.Toplevel(self._root)
        self._win = win
        win.title(t("cfg.title"))
        win.geometry("800x700")
        win.minsize(580, 400)
        win.protocol("WM_DELETE_WINDOW", self._close)
        self._apply_style(win)

        # ORDEM IMPORTA: o rodape reserva seu espaco primeiro, entao ele
        # nunca e cortado quando a janela encolhe.
        footer = ttk.Frame(win, padding=(_PAD, 10))
        footer.pack(side="bottom", fill="x")
        ttk.Separator(win).pack(side="bottom", fill="x")
        ttk.Button(
            footer, text=t("cfg.save"), command=self._save, style="Accent.TButton"
        ).pack(side="right")
        ttk.Button(footer, text=t("cfg.cancel"), command=self._close).pack(
            side="right", padx=(0, 8)
        )

        notebook = ttk.Notebook(win)
        notebook.pack(side="top", fill="both", expand=True, padx=_PAD, pady=(_PAD, 0))
        notebook.add(self._build_general(notebook), text=t("cfg.tab.general"))
        notebook.add(self._build_audio(notebook), text=t("cfg.tab.audio"))
        notebook.add(self._build_notes(notebook), text=t("cfg.tab.notes"))
        notebook.add(self._build_presets(notebook), text=t("cfg.tab.presets"))
        if self._presets:
            self._select_preset(0)

    def _apply_style(self, win: tk.Toplevel) -> None:
        style = ttk.Style(win)
        # Configurar a TkDefaultFont em si, e nao uma copia: os widgets ttk
        # leem a fonte nomeada, entao uma copia deixaria rotulo e campo com
        # tamanhos diferentes.
        base = tkfont.nametofont("TkDefaultFont")
        base.configure(family="Segoe UI", size=9)
        tkfont.nametofont("TkTextFont").configure(family="Segoe UI", size=9)
        self._section_font = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        self._mono_font = tkfont.Font(family="Consolas", size=9)
        style.configure("TLabelframe.Label", font=self._section_font)
        style.configure("TLabelframe", padding=(_PAD, 8, _PAD, _PAD))
        try:
            style.configure("Accent.TButton", font=self._section_font)
        except tk.TclError:
            pass  # tema sem suporte; o botao so fica sem destaque

    # ------------------------------------------------------------ layout

    def _scrollable(self, parent) -> ttk.Frame:
        """Area rolavel. Devolve o frame interno onde o conteudo vai."""
        outer = ttk.Frame(parent)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, padding=(4, 8, 4, 8))

        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        inner.bind(
            "<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        # A roda do mouse so rola enquanto o ponteiro esta sobre esta area.
        def wheel(event) -> None:
            canvas.yview_scroll(-int(event.delta / 120), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        return inner

    def _section(self, parent, title_key: str) -> ttk.Labelframe:
        frame = ttk.Labelframe(parent, text=t(title_key))
        frame.pack(fill="x", expand=False, padx=8, pady=(0, 14))
        frame.columnconfigure(1, weight=1)
        return frame

    def _hint(self, parent, text_value: str) -> ttk.Label:
        """Texto de apoio, cinza, com a quebra acompanhando o container."""
        label = ttk.Label(
            parent, text=text_value, foreground=_HINT, wraplength=420, justify="left"
        )

        def refit(event, lbl=label) -> None:
            try:
                lbl.configure(wraplength=max(180, event.width - _LABEL_COLUMN))
            except tk.TclError:
                pass  # widget ja destruido

        parent.bind("<Configure>", refit, add="+")
        return label

    def _field(self, parent, row: int, label_key: str, widget, hint_key: str = "") -> int:
        """Uma linha rotulo + campo (+ dica). Devolve a proxima linha."""
        ttk.Label(parent, text=t(label_key)).grid(
            row=row, column=0, sticky="nw", pady=(0, 4), padx=(0, 12)
        )
        widget.grid(row=row, column=1, sticky="ew", pady=(0, 4))
        row += 1
        if hint_key:
            self._hint(parent, t(hint_key)).grid(
                row=row, column=1, sticky="w", pady=(0, 10)
            )
            row += 1
        else:
            parent.grid_rowconfigure(row - 1, pad=6)
        return row

    def _check(self, parent, row: int, label_key: str, variable, hint_key: str = "") -> int:
        ttk.Checkbutton(parent, text=t(label_key), variable=variable).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 2)
        )
        row += 1
        if hint_key:
            self._hint(parent, t(hint_key)).grid(
                row=row, column=0, columnspan=2, sticky="w", padx=(22, 0), pady=(0, 10)
            )
            row += 1
        return row

    def _path_row(self, parent, variable: tk.StringVar, title_key: str) -> ttk.Frame:
        """Campo de pasta com botao "Escolher..."."""
        row = ttk.Frame(parent)
        row.columnconfigure(0, weight=1)
        ttk.Entry(row, textvariable=variable).grid(row=0, column=0, sticky="ew")

        def browse() -> None:
            current = variable.get().strip()
            initial = config_module.notes_dir({"notes_dir": current}) if current else None
            chosen = filedialog.askdirectory(
                parent=self._win,
                title=t(title_key),
                initialdir=str(initial) if initial and initial.exists() else None,
                mustexist=False,
            )
            if chosen:
                variable.set(chosen)

        ttk.Button(row, text=t("cfg.browse"), width=12, command=browse).grid(
            row=0, column=1, padx=(6, 0)
        )
        return row

    def _hotkey_row(self, parent, variable: tk.StringVar) -> ttk.Frame:
        row = ttk.Frame(parent)
        row.columnconfigure(0, weight=1)
        ttk.Entry(row, textvariable=variable, font=self._mono_font).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(
            row, text=t("cfg.capture"), width=11,
            command=lambda: self._capture_hotkey(variable),
        ).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(row, text=t("cfg.clear"), width=8, command=lambda: variable.set("")).grid(
            row=0, column=2, padx=(6, 0)
        )
        return row

    # ------------------------------------------------------------- Geral

    def _build_general(self, parent) -> ttk.Frame:
        page = ttk.Frame(parent)
        body = self._scrollable(page)

        # --- OpenAI ------------------------------------------------------
        openai = self._section(body, "cfg.section.openai")
        row = 0
        self._api_key_var = tk.StringVar()
        row = self._field(
            openai, row, "cfg.api_key",
            ttk.Entry(openai, textvariable=self._api_key_var, show="•"),
        )
        self._hint(openai, self._api_key_hint()).grid(
            row=row, column=1, sticky="w", pady=(0, 10)
        )
        row += 1
        self._stt_var = tk.StringVar(value=self._config["stt_model"])
        row = self._field(
            openai, row, "cfg.stt_model",
            ttk.Combobox(openai, textvariable=self._stt_var, values=_STT_MODELS),
            "cfg.stt_model.hint",
        )
        self._text_model_var = tk.StringVar(value=self._config["text_model"])
        row = self._field(
            openai, row, "cfg.text_model",
            ttk.Combobox(openai, textvariable=self._text_model_var, values=_TEXT_MODELS),
            "cfg.text_model.hint",
        )

        # --- Transcricao -------------------------------------------------
        speech = self._section(body, "cfg.section.transcription")
        row = 0
        self._languages_var = tk.StringVar(
            value=", ".join(self._config.get("languages") or [])
        )
        row = self._field(
            speech, row, "cfg.languages",
            ttk.Entry(speech, textvariable=self._languages_var, width=14),
            "cfg.languages.hint",
        )
        self._keywords_text = tk.Text(speech, height=6, wrap="word", font=self._mono_font)
        self._keywords_text.insert("1.0", "\n".join(self._config.get("keywords", [])))
        row = self._field(
            speech, row, "cfg.keywords", self._keywords_text, "cfg.keywords.hint"
        )

        # --- Interface ---------------------------------------------------
        interface = self._section(body, "cfg.section.interface")
        row = 0
        self._ui_language_var = tk.StringVar(
            value=LANGUAGE_NAMES.get(self._config.get("ui_language", AUTO), "")
        )
        row = self._field(
            interface, row, "cfg.ui_language",
            ttk.Combobox(
                interface, textvariable=self._ui_language_var,
                values=list(LANGUAGE_NAMES.values()), state="readonly",
            ),
            "cfg.ui_language.hint",
        )
        self._overlay_labels = {
            "always": t("cfg.overlay.always"),
            "errors": t("cfg.overlay.errors"),
            "never": t("cfg.overlay.never"),
        }
        self._overlay_var = tk.StringVar(
            value=self._overlay_labels.get(
                self._config.get("overlay_mode", "always"), self._overlay_labels["always"]
            )
        )
        row = self._field(
            interface, row, "cfg.overlay",
            ttk.Combobox(
                interface, textvariable=self._overlay_var,
                values=list(self._overlay_labels.values()), state="readonly", width=20,
            ),
            "cfg.overlay.hint",
        )

        # --- Atalhos -----------------------------------------------------
        keys = self._section(body, "cfg.section.hotkeys")
        row = 0
        self._hotkey_var = tk.StringVar(value=self._config.get("hotkey") or "")
        row = self._field(keys, row, "cfg.hotkey", self._hotkey_row(keys, self._hotkey_var),
                          "cfg.hotkey.hint")
        self._pause_hotkey_var = tk.StringVar(value=self._config.get("pause_hotkey") or "")
        row = self._field(keys, row, "cfg.pause_hotkey",
                          self._hotkey_row(keys, self._pause_hotkey_var), "cfg.pause_hotkey.hint")

        # --- Comportamento -----------------------------------------------
        behavior = self._section(body, "cfg.section.behavior")
        behavior.columnconfigure(0, weight=1)
        row = 0
        # Estado real do registro, nao do config: se alguem apagou a entrada
        # por fora, a caixa precisa refletir o que existe de fato.
        self._autostart_var = tk.BooleanVar(value=startup.is_enabled())
        row = self._check(
            behavior, row, "cfg.autostart", self._autostart_var, "cfg.autostart.hint"
        )
        self._hint(
            behavior, t("cfg.autostart.frozen_hint", command=startup.command())
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=(22, 0))

        # --- Arquivos ----------------------------------------------------
        files = self._section(body, "cfg.section.files")
        files.columnconfigure(0, weight=1)
        path_entry = ttk.Entry(files)
        path_entry.insert(0, str(config_module.config_dir()))
        path_entry.configure(state="readonly")  # selecionavel, mas nao editavel
        path_entry.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Button(
            files, text=t("cfg.open_folder"), command=self._on_open_folder, width=18
        ).grid(row=1, column=0, sticky="w")
        ttk.Button(
            files, text=t("cfg.reload"), command=self._reload_from_disk, width=20
        ).grid(row=1, column=1, sticky="w", padx=(8, 0))
        self._hint(files, t("cfg.files.hint")).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        return page

    def _api_key_hint(self) -> str:
        """A dica diz de onde vem a chave em uso, inclusive a do Artemis."""
        current = secrets_store.masked(secrets_store.get_api_key())
        source = secrets_store.key_source()
        if source == "artemis":
            return t("cfg.api_key.hint_artemis", current=current)
        if source == "env":
            return t("cfg.api_key.hint_env", current=current)
        return t("cfg.api_key.hint", current=current)

    # ------------------------------------------------------------- Audio

    def _build_audio(self, parent) -> ttk.Frame:
        page = ttk.Frame(parent)
        body = self._scrollable(page)
        self._system_default = t("cfg.system_default")
        try:
            from ..audio.sources import list_devices

            devices = list_devices()
        except Exception as exc:
            log.warning("Nao consegui listar os dispositivos: %s", exc)
            devices = {"inputs": [], "outputs": []}

        section = self._section(body, "cfg.section.devices")
        row = 0
        self._mic_var = tk.StringVar(value=self._config.get("mic_device") or self._system_default)
        row = self._field(
            section, row, "cfg.mic_device",
            ttk.Combobox(section, textvariable=self._mic_var, state="readonly",
                         values=[self._system_default] + [d["name"] for d in devices["inputs"]]),
            "cfg.mic_device.hint",
        )
        self._loopback_var = tk.StringVar(
            value=self._config.get("loopback_device") or self._system_default
        )
        row = self._field(
            section, row, "cfg.loopback_device",
            ttk.Combobox(section, textvariable=self._loopback_var, state="readonly",
                         values=[self._system_default] + [d["name"] for d in devices["outputs"]]),
            "cfg.loopback_device.hint",
        )

        test = self._section(body, "cfg.section.test")
        test.columnconfigure(1, weight=1)
        self._meters = {}
        for row, channel in enumerate(("mic", "sys")):
            ttk.Label(test, text=t(f"cfg.meter.{channel}")).grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=3
            )
            bar = ttk.Progressbar(test, maximum=100, mode="determinate")
            bar.grid(row=row, column=1, sticky="ew", pady=3)
            self._meters[channel] = bar
        self._test_button = ttk.Button(test, text=t("cfg.test"), command=self._start_test)
        self._test_button.grid(row=2, column=1, sticky="w", pady=(8, 4))
        self._test_result = ttk.Label(test, text="", foreground=_HINT, justify="left",
                                      wraplength=420)
        self._test_result.grid(row=3, column=0, columnspan=2, sticky="w")
        self._hint(test, t("cfg.test.hint")).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        return page

    def _start_test(self) -> None:
        """Abre mic e loopback por 5 s e mostra o nivel de cada um."""
        if self._test is not None:
            return
        if self._is_recording():
            self._test_result.configure(text=t("cfg.test.busy"))
            return
        from ..audio.sources import MicSource, SystemSource

        levels = {"mic": _METER_FLOOR_DB, "sys": _METER_FLOOR_DB}
        peaks = {"mic": _METER_FLOOR_DB, "sys": _METER_FLOOR_DB}

        def meter(channel):
            def on_block(_t, block) -> None:
                rms = float(np.sqrt(np.mean(np.square(block)))) if len(block) else 0.0
                db = 20 * np.log10(rms) if rms > 1e-6 else -120.0
                levels[channel] = db
                peaks[channel] = max(peaks[channel], db)

            return on_block

        mic = self._mic_var.get()
        out = self._loopback_var.get()
        sources = [
            MicSource(meter("mic"), None if mic == self._system_default else mic),
            SystemSource(meter("sys"), None if out == self._system_default else out),
        ]
        started = time.monotonic()
        self._test = {"sources": sources, "levels": levels, "peaks": peaks, "started": started}
        self._test_button.configure(state="disabled")
        self._test_result.configure(text=t("cfg.test.running"))
        for src in sources:
            src.start()
        self._poll_test()

    def _poll_test(self) -> None:
        test = self._test
        if test is None or self._win is None:
            return
        for channel, bar in self._meters.items():
            db = max(_METER_FLOOR_DB, test["levels"][channel])
            bar.configure(value=(db - _METER_FLOOR_DB) / -_METER_FLOOR_DB * 100)
        if time.monotonic() - test["started"] < _TEST_SECONDS:
            self._win.after(80, self._poll_test)
            return
        self._finish_test()

    def _finish_test(self) -> None:
        test, self._test = self._test, None
        if test is None:
            return
        sources = test["sources"]
        # Parar as fontes pode levar ate ~1 s (Bluetooth): fora da main thread.
        threading.Thread(target=lambda: [s.stop() for s in sources], daemon=True).start()
        lines = []
        for src in sources:
            if src.error is not None:
                lines.append(t("cfg.test.error", channel=t(f"cfg.meter.{src.channel}"),
                               error=src.error))
                continue
            first = (f"{src.first_block_at - test['started']:.1f}s"
                     if src.first_block_at is not None else "---")
            lines.append(t("cfg.test.line", channel=t(f"cfg.meter.{src.channel}"),
                           device=src.device_name or "?", first=first,
                           peak=round(test["peaks"][src.channel])))
        if self._win is not None:
            for bar in self._meters.values():
                bar.configure(value=0)
            self._test_result.configure(text="\n".join(lines))
            self._test_button.configure(state="normal")

    # ------------------------------------------------------------- Notas

    def _build_notes(self, parent) -> ttk.Frame:
        page = ttk.Frame(parent)
        body = self._scrollable(page)

        where = self._section(body, "cfg.section.notes")
        row = 0
        self._notes_dir_var = tk.StringVar(value=self._config.get("notes_dir") or "")
        row = self._field(where, row, "cfg.notes_dir",
                          self._path_row(where, self._notes_dir_var, "cfg.notes_dir"),
                          "cfg.notes_dir.hint")
        self._my_name_var = tk.StringVar(value=self._config.get("my_name") or "")
        row = self._field(where, row, "cfg.my_name",
                          ttk.Entry(where, textvariable=self._my_name_var), "cfg.my_name.hint")
        self._block_labels = {
            "callout": t("cfg.transcript_block.callout"),
            "details": t("cfg.transcript_block.details"),
        }
        self._block_var = tk.StringVar(
            value=self._block_labels.get(self._config.get("transcript_block"),
                                         self._block_labels["callout"])
        )
        row = self._field(
            where, row, "cfg.transcript_block",
            ttk.Combobox(where, textvariable=self._block_var, state="readonly",
                         values=list(self._block_labels.values())),
            "cfg.transcript_block.hint",
        )

        end = self._section(body, "cfg.section.ending")
        row = 0
        self._default_preset_var = tk.StringVar(value=self._preset_name(
            self._config.get("default_preset")))
        self._default_preset_combo = ttk.Combobox(
            end, textvariable=self._default_preset_var, state="readonly",
            values=[p.name for p in self._presets],
        )
        row = self._field(end, row, "cfg.default_preset", self._default_preset_combo,
                          "cfg.default_preset.hint")
        self._on_stop_labels = {"ask": t("cfg.on_stop.ask"), "auto": t("cfg.on_stop.auto")}
        self._on_stop_var = tk.StringVar(
            value=self._on_stop_labels.get(self._config.get("on_stop"), self._on_stop_labels["ask"])
        )
        row = self._field(
            end, row, "cfg.on_stop",
            ttk.Combobox(end, textvariable=self._on_stop_var, state="readonly",
                         values=list(self._on_stop_labels.values())),
            "cfg.on_stop.hint",
        )

        audio = self._section(body, "cfg.section.save_audio")
        audio.columnconfigure(1, weight=1)
        row = 0
        self._save_audio_var = tk.BooleanVar(value=bool(self._config.get("save_audio")))
        row = self._check(audio, row, "cfg.save_audio", self._save_audio_var,
                          "cfg.save_audio.hint")
        self._audio_dir_var = tk.StringVar(value=self._config.get("audio_dir") or "")
        row = self._field(audio, row, "cfg.audio_dir",
                          self._path_row(audio, self._audio_dir_var, "cfg.audio_dir"),
                          "cfg.audio_dir.hint")

        limits = self._section(body, "cfg.section.limits")
        row = 0
        self._silence_var = tk.StringVar(value=str(self._config.get("silence_alert_minutes", 10)))
        row = self._field(limits, row, "cfg.silence_alert",
                          ttk.Spinbox(limits, from_=0, to=120, increment=5, width=6,
                                      textvariable=self._silence_var),
                          "cfg.silence_alert.hint")
        self._max_hours_var = tk.StringVar(value=str(self._config.get("max_session_hours", 3)))
        row = self._field(limits, row, "cfg.max_hours",
                          ttk.Spinbox(limits, from_=0, to=12, increment=0.5, width=6,
                                      textvariable=self._max_hours_var),
                          "cfg.max_hours.hint")
        return page

    def _preset_name(self, preset_id: str | None) -> str:
        for p in self._presets:
            if p.id == preset_id:
                return p.name
        return self._presets[0].name if self._presets else ""

    # ----------------------------------------------------------- Presets

    def _build_presets(self, parent) -> ttk.Frame:
        page = ttk.Frame(parent, padding=(8, 10))
        page.columnconfigure(1, weight=1)
        page.rowconfigure(0, weight=1)

        left = ttk.Frame(page)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 14))
        left.rowconfigure(0, weight=1)
        self._listbox = tk.Listbox(
            left, width=18, exportselection=False, activestyle="none",
            borderwidth=1, relief="solid", highlightthickness=0,
        )
        self._listbox.grid(row=0, column=0, columnspan=2, sticky="ns")
        self._listbox.bind("<<ListboxSelect>>", self._on_list_select)
        ttk.Button(left, text=t("cfg.preset.new"), width=9, command=self._add_preset).grid(
            row=1, column=0, sticky="ew", pady=(8, 0)
        )
        ttk.Button(left, text=t("cfg.preset.remove"), width=10,
                   command=self._remove_preset).grid(
            row=1, column=1, sticky="ew", pady=(8, 0), padx=(4, 0)
        )

        right_container = ttk.Frame(page)
        right_container.grid(row=0, column=1, sticky="nsew")
        form = ttk.Frame(self._scrollable(right_container), padding=(0, 0, 8, 0))
        form.pack(fill="both", expand=True)
        form.columnconfigure(1, weight=1)
        row = 0

        self._name_var = tk.StringVar()
        row = self._field(form, row, "cfg.preset.name", ttk.Entry(form, textvariable=self._name_var))
        self._description_var = tk.StringVar()
        row = self._field(form, row, "cfg.preset.description",
                          ttk.Entry(form, textvariable=self._description_var))
        self._prompt_text = tk.Text(form, height=12, wrap="word", font=("Segoe UI", 9))
        row = self._field(form, row, "cfg.preset.prompt", self._prompt_text,
                          "cfg.preset.prompt.hint")
        self._preset_keywords_text = tk.Text(form, height=4, wrap="word", font=self._mono_font)
        row = self._field(form, row, "cfg.preset.keywords", self._preset_keywords_text,
                          "cfg.preset.keywords.hint")
        self._preset_languages_var = tk.StringVar()
        row = self._field(form, row, "cfg.preset.languages",
                          ttk.Entry(form, textvariable=self._preset_languages_var, width=14),
                          "cfg.preset.languages.hint")
        self._preset_model_var = tk.StringVar()
        row = self._field(form, row, "cfg.preset.model",
                          ttk.Combobox(form, textvariable=self._preset_model_var,
                                       values=[""] + _TEXT_MODELS, width=18),
                          "cfg.preset.model.hint")
        self._refresh_list()
        return page

    def _refresh_list(self) -> None:
        # Mexer na Listbox dispara <<ListboxSelect>>; sem esta trava a troca
        # de selecao se chamaria de volta no meio da atualizacao.
        self._suspend_select = True
        try:
            self._listbox.delete(0, tk.END)
            for preset in self._presets:
                self._listbox.insert(tk.END, f" {preset.name}")
        finally:
            self._suspend_select = False
        names = [p.name for p in self._presets]
        self._default_preset_combo.configure(values=names)
        if self._default_preset_var.get() not in names and names:
            self._default_preset_var.set(names[0])

    def _on_list_select(self, _event=None) -> None:
        if self._suspend_select:
            return
        selection = self._listbox.curselection()
        if selection:
            self._select_preset(selection[0])

    def _select_preset(self, index: int) -> None:
        self._commit_form()  # nao perde o que estava sendo editado
        self._refresh_list()  # o nome pode ter mudado no formulario
        self._selected = index
        preset = self._presets[index]
        self._name_var.set(preset.name)
        self._description_var.set(preset.description)
        self._set_text(self._prompt_text, preset.system_prompt)
        self._set_text(self._preset_keywords_text, "\n".join(preset.keywords))
        self._preset_languages_var.set(", ".join(preset.languages or []))
        self._preset_model_var.set(preset.text_model or "")
        self._suspend_select = True
        try:
            self._listbox.selection_clear(0, tk.END)
            self._listbox.selection_set(index)
        finally:
            self._suspend_select = False

    def _commit_form(self) -> None:
        """Copia o formulario para o preset selecionado, sem validar ainda."""
        if self._selected is None or self._selected >= len(self._presets):
            return
        preset = self._presets[self._selected]
        old_name = preset.name
        preset.name = self._name_var.get().strip() or preset.name
        if self._default_preset_var.get() == old_name:
            self._default_preset_var.set(preset.name)  # renomeou o padrao
        preset.description = self._description_var.get().strip()
        preset.system_prompt = self._get_text(self._prompt_text)
        preset.keywords = _lines(self._get_text(self._preset_keywords_text))
        preset.languages = _codes(self._preset_languages_var.get()) or None
        preset.text_model = self._preset_model_var.get().strip() or None

    def _add_preset(self) -> None:
        self._commit_form()
        existing = {p.id for p in self._presets}
        index = 1
        while f"preset{index}" in existing:
            index += 1
        self._presets.append(SummaryPreset(
            id=f"preset{index}",
            name=t("cfg.preset.new_name", n=index),
            # Comeca com o do primeiro preset: mais facil ajustar do que
            # escrever as secoes do zero.
            system_prompt=(self._presets[0].system_prompt if self._presets else "")
            or t("cfg.preset.new_prompt"),
        ))
        self._refresh_list()
        self._select_preset(len(self._presets) - 1)

    def _remove_preset(self) -> None:
        if self._selected is None or len(self._presets) <= 1:
            messagebox.showinfo("Apolo", t("dlg.keep_one_preset"), parent=self._win)
            return
        name = self._presets[self._selected].name
        if not messagebox.askyesno("Apolo", t("dlg.remove_preset", name=name), parent=self._win):
            return
        del self._presets[self._selected]
        self._selected = None
        self._refresh_list()
        self._select_preset(0)

    # ----------------------------------------------------- captura de atalho

    def _capture_hotkey(self, variable: tk.StringVar) -> None:
        """Escuta o teclado e acumula o CONJUNTO de teclas seguradas.

        Igual ao Artemis: so fecha quando todas as teclas foram soltas, e
        modificador sozinho (Ctrl, Alt, Shift) nao vale.
        """
        dialog = tk.Toplevel(self._win)
        dialog.title(t("cfg.capture.title"))
        dialog.transient(self._win)
        dialog.resizable(False, False)
        dialog.grab_set()
        ttk.Label(dialog, text=t("cfg.capture.prompt"), font=self._section_font,
                  padding=(30, 22, 30, 4)).pack()
        feedback = ttk.Label(dialog, text="", foreground=_HINT)
        feedback.pack()
        ttk.Label(dialog, text=t("cfg.capture.hint"), foreground=_HINT,
                  padding=(0, 2, 0, 2)).pack()
        ttk.Label(dialog, text=t("cfg.capture.escape"), foreground=_HINT,
                  padding=(0, 0, 0, 22)).pack()
        dialog.update_idletasks()
        x = self._win.winfo_rootx() + (self._win.winfo_width() - dialog.winfo_width()) // 2
        y = self._win.winfo_rooty() + 160
        dialog.geometry(f"+{x}+{y}")

        down: set[str] = set()
        seen: set[str] = set()
        pending_release: dict[str, object] = {}
        state = {"pending_finish": None, "closed": False}

        def finish(spec: str | None) -> None:
            if state["closed"]:
                return
            state["closed"] = True
            if spec:
                variable.set(spec)
            dialog.destroy()

        def refresh_feedback() -> None:
            if not seen:
                feedback.configure(text="")
                return
            spec = _spec_from_keys(seen)
            feedback.configure(text=f"{describe(spec)}{'' if down else '  ✓'}")

        def schedule_finish() -> None:
            token = object()
            state["pending_finish"] = token
            dialog.after(150, lambda: state["pending_finish"] is token
                         and finish(_spec_from_keys(seen)))

        def on_press(event) -> None:
            if event.keysym == "Escape":
                finish(None)
                return
            state["pending_finish"] = None
            pending_release.pop(event.keysym, None)  # KeyPress novo = autorepeat
            if event.keysym not in down:
                down.add(event.keysym)
                seen.add(event.keysym)
                refresh_feedback()

        def on_release(event) -> None:
            keysym = event.keysym
            if keysym == "Escape" or keysym not in down:
                return
            token = object()
            pending_release[keysym] = token

            def confirm_release() -> None:
                if pending_release.get(keysym) is not token:
                    return  # um KeyPress chegou antes: era autorepeat
                pending_release.pop(keysym, None)
                down.discard(keysym)
                refresh_feedback()
                if down:
                    return
                if any(k not in _MODIFIER_KEYSYMS for k in seen) or any(
                    _MODIFIER_TOKEN.get(k) == "cmd" for k in seen
                ):
                    schedule_finish()
                else:
                    seen.clear()  # so modificadores: tenta de novo
                    refresh_feedback()

            dialog.after(1, confirm_release)

        def on_focus_out(_event) -> None:
            down.clear()
            seen.clear()
            pending_release.clear()
            state["pending_finish"] = None
            refresh_feedback()

        dialog.bind("<KeyPress>", on_press)
        dialog.bind("<KeyRelease>", on_release)
        dialog.bind("<FocusOut>", on_focus_out)
        dialog.protocol("WM_DELETE_WINDOW", lambda: finish(None))
        dialog.focus_force()

    # ------------------------------------------------------------- salvar

    def _save(self) -> None:
        self._commit_form()
        try:
            validated = [SummaryPreset.from_dict(p.to_dict()) for p in self._presets]
        except (TypeError, ValueError) as exc:
            messagebox.showerror("Apolo", t("dlg.preset_invalid", error=exc), parent=self._win)
            return

        hotkey = self._hotkey_var.get().strip()
        pause_hotkey = self._pause_hotkey_var.get().strip()
        if hotkey and hotkey == pause_hotkey:
            messagebox.showerror("Apolo", t("dlg.hotkey_same"), parent=self._win)
            return

        default_name = self._default_preset_var.get()
        default_id = next((p.id for p in validated if p.name == default_name), validated[0].id)
        mic = self._mic_var.get()
        out = self._loopback_var.get()
        config = {
            **self._config,
            "stt_model": self._stt_var.get().strip() or "gpt-transcribe",
            "text_model": self._text_model_var.get().strip() or "gpt-5.6-luna",
            "languages": _codes(self._languages_var.get()),
            "keywords": _lines(self._get_text(self._keywords_text)),
            "ui_language": self._selected(self._ui_language_var, LANGUAGE_NAMES, AUTO),
            "overlay_mode": self._selected(self._overlay_var, self._overlay_labels, "always"),
            "hotkey": hotkey or None,
            "pause_hotkey": pause_hotkey or None,
            "start_with_system": bool(self._autostart_var.get()),
            "mic_device": None if mic in ("", self._system_default) else mic,
            "loopback_device": None if out in ("", self._system_default) else out,
            "notes_dir": self._notes_dir_var.get().strip()
            or config_module.DEFAULT_CONFIG["notes_dir"],
            "my_name": self._my_name_var.get().strip(),
            "transcript_block": self._selected(self._block_var, self._block_labels, "callout"),
            "default_preset": default_id,
            "on_stop": self._selected(self._on_stop_var, self._on_stop_labels, "ask"),
            "save_audio": bool(self._save_audio_var.get()),
            "audio_dir": self._audio_dir_var.get().strip() or None,
            "silence_alert_minutes": _number(self._silence_var.get(), 10),
            "max_session_hours": _number(self._max_hours_var.get(), 3),
        }

        try:
            new_key = self._api_key_var.get().strip()
            if new_key:
                secrets_store.set_api_key(new_key)
            config_module.save_config(config)
            config_module.save_presets(validated)
        except Exception as exc:
            messagebox.showerror("Apolo", t("dlg.save_failed", error=exc), parent=self._win)
            return

        self._close()
        self._on_saved()  # aplica idioma, atalhos e inicializacao automatica

    def _reload_from_disk(self) -> None:
        """Recarrega o app e reabre a janela com o que esta no disco."""
        self._on_reload()
        self._close()
        self._root.after(50, self.open)

    def _close(self) -> None:
        if self._test is not None:
            self._finish_test()
        win, self._win = self._win, None
        self._selected = None
        if win is not None:
            try:
                win.unbind_all("<MouseWheel>")
            except Exception:
                pass
            win.destroy()

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _selected(variable: tk.StringVar, labels: dict, default: str) -> str:
        """Rotulo exibido -> valor salvo no config."""
        chosen = variable.get()
        return next((value for value, label in labels.items() if label == chosen), default)

    @staticmethod
    def _get_text(widget: tk.Text) -> str:
        return widget.get("1.0", "end-1c").strip()

    @staticmethod
    def _set_text(widget: tk.Text, value: str) -> None:
        widget.delete("1.0", tk.END)
        widget.insert("1.0", value or "")


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _codes(text: str) -> list[str]:
    return [c.strip().lower() for c in text.replace(";", ",").split(",") if c.strip()]


def _number(text: str, default: float) -> float:
    try:
        value = float(str(text).replace(",", "."))
    except ValueError:
        return default
    return int(value) if value.is_integer() else value


# Teclas -> spec de atalho, igual ao Artemis (o formato que hotkeys_*.py le).

_KEYSYM_ALIASES = {
    "space": "<space>", "Return": "<enter>", "Tab": "<tab>", "BackSpace": "<backspace>",
    "Insert": "<insert>", "Delete": "<delete>", "Home": "<home>", "End": "<end>",
    "Prior": "<page_up>", "Next": "<page_down>",
}
_XF86_ALIASES = {
    "XF86AudioPlay": "<media_playpause>", "XF86AudioPause": "<media_playpause>",
    "XF86AudioStop": "<media_stop>", "XF86AudioNext": "<media_next>",
    "XF86AudioPrev": "<media_prev>", "XF86AudioMute": "<media_mute>",
    "XF86AudioRaiseVolume": "<media_volume_up>", "XF86AudioLowerVolume": "<media_volume_down>",
}
_MODIFIER_KEYSYMS = {
    "Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R",
    "Win_L", "Win_R", "Super_L", "Super_R", "Meta_L", "Meta_R",
}
_MODIFIER_TOKEN = {
    "Control_L": "ctrl", "Control_R": "ctrl", "Alt_L": "alt", "Alt_R": "alt",
    "Shift_L": "shift", "Shift_R": "shift", "Win_L": "cmd", "Win_R": "cmd",
    "Super_L": "cmd", "Super_R": "cmd", "Meta_L": "cmd", "Meta_R": "cmd",
}
_MODIFIER_ORDER = ("ctrl", "alt", "shift", "cmd")


def _spec_from_keys(keys: set[str]) -> str:
    """Conjunto de keysyms segurados durante a captura -> spec de atalho."""
    mods = {_MODIFIER_TOKEN[k] for k in keys if k in _MODIFIER_TOKEN}
    others = []
    for keysym in keys:
        if keysym in _MODIFIER_TOKEN:
            continue
        if keysym in _KEYSYM_ALIASES:
            others.append(_KEYSYM_ALIASES[keysym])
        elif keysym in _XF86_ALIASES:
            others.append(_XF86_ALIASES[keysym])
        elif len(keysym) == 1:
            others.append(keysym.lower())
        else:
            others.append(f"<{keysym.lower()}>")
    parts = [f"<{m}>" for m in _MODIFIER_ORDER if m in mods]
    parts.extend(sorted(others))
    return "+".join(parts)
