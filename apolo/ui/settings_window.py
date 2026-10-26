"""Janela de configuracao (Tkinter, que ja vem com o Python).

Esqueleto do Artemis: rodape empacotado ANTES do conteudo (side="bottom")
para o Salvar nunca sumir quando a janela encolhe, conteudo rolavel, e todo
texto passando por i18n.t() na hora de montar.

Por enquanto so a aba Geral. As abas Audio (dispositivos com medidor de
nivel) e Presets (editor dos tipos de resumo) chegam na fase 5; ate la os
presets se editam em presets.json, pelo botao "Abrir pasta".
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import filedialog
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Callable

from .. import config as config_module
from .. import secrets_store, startup
from ..i18n import AUTO, LANGUAGE_NAMES, t

log = logging.getLogger(__name__)

_STT_MODELS = ["gpt-transcribe"]
_TEXT_MODELS = ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]

_HINT = "#6b6b70"
_PAD = 12
# Coluna dos rotulos + espacamentos, descontada da largura das dicas.
_LABEL_COLUMN = 165


class SettingsWindow:
    """Uma unica instancia; reabrir traz a janela existente para frente."""

    def __init__(
        self,
        root: tk.Tk,
        *,
        on_saved: Callable[[], None],
        on_reload: Callable[[], None],
        on_open_folder: Callable[[], None],
    ):
        self._root = root
        self._on_saved = on_saved
        self._on_reload = on_reload
        self._on_open_folder = on_open_folder
        self._win: tk.Toplevel | None = None

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

        win = tk.Toplevel(self._root)
        self._win = win
        win.title(t("cfg.title"))
        win.geometry("760x680")
        win.minsize(560, 380)
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

    # ------------------------------------------------------------- Geral

    def _build_general(self, parent) -> ttk.Frame:
        page = ttk.Frame(parent)
        body = self._scrollable(page)

        # --- OpenAI ------------------------------------------------------
        openai = self._section(body, "cfg.section.openai")
        row = 0
        self._api_key_var = tk.StringVar()
        row = self._field(
            openai,
            row,
            "cfg.api_key",
            ttk.Entry(openai, textvariable=self._api_key_var, show="•"),
        )
        self._hint(openai, self._api_key_hint()).grid(
            row=row, column=1, sticky="w", pady=(0, 10)
        )
        row += 1
        self._stt_var = tk.StringVar(value=self._config["stt_model"])
        row = self._field(
            openai,
            row,
            "cfg.stt_model",
            ttk.Combobox(openai, textvariable=self._stt_var, values=_STT_MODELS),
            "cfg.stt_model.hint",
        )
        self._text_model_var = tk.StringVar(value=self._config["text_model"])
        row = self._field(
            openai,
            row,
            "cfg.text_model",
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
            speech,
            row,
            "cfg.languages",
            ttk.Entry(speech, textvariable=self._languages_var, width=14),
            "cfg.languages.hint",
        )
        self._keywords_text = tk.Text(speech, height=6, wrap="word", font=self._mono_font)
        self._keywords_text.insert("1.0", "\n".join(self._config.get("keywords", [])))
        row = self._field(
            speech, row, "cfg.keywords", self._keywords_text, "cfg.keywords.hint"
        )
        self._my_name_var = tk.StringVar(value=self._config.get("my_name") or "")
        row = self._field(
            speech,
            row,
            "cfg.my_name",
            ttk.Entry(speech, textvariable=self._my_name_var),
            "cfg.my_name.hint",
        )

        # --- Notas -------------------------------------------------------
        notes = self._section(body, "cfg.section.notes")
        row = 0
        notes_row = ttk.Frame(notes)
        notes_row.columnconfigure(0, weight=1)
        self._notes_dir_var = tk.StringVar(value=self._config.get("notes_dir") or "")
        ttk.Entry(notes_row, textvariable=self._notes_dir_var).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(
            notes_row, text=t("cfg.browse"), width=12, command=self._browse_notes_dir
        ).grid(row=0, column=1, padx=(6, 0))
        row = self._field(notes, row, "cfg.notes_dir", notes_row, "cfg.notes_dir.hint")

        # --- Interface ---------------------------------------------------
        interface = self._section(body, "cfg.section.interface")
        row = 0
        self._ui_language_var = tk.StringVar(
            value=LANGUAGE_NAMES.get(self._config.get("ui_language", AUTO), "")
        )
        row = self._field(
            interface,
            row,
            "cfg.ui_language",
            ttk.Combobox(
                interface,
                textvariable=self._ui_language_var,
                values=list(LANGUAGE_NAMES.values()),
                state="readonly",
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
                self._config.get("overlay_mode", "always"),
                self._overlay_labels["always"],
            )
        )
        row = self._field(
            interface,
            row,
            "cfg.overlay",
            ttk.Combobox(
                interface,
                textvariable=self._overlay_var,
                values=list(self._overlay_labels.values()),
                state="readonly",
                width=20,
            ),
            "cfg.overlay.hint",
        )

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

    def _browse_notes_dir(self) -> None:
        initial = config_module.notes_dir({"notes_dir": self._notes_dir_var.get()})
        chosen = filedialog.askdirectory(
            parent=self._win,
            title=t("cfg.notes_dir"),
            initialdir=str(initial if initial.exists() else initial.parent),
            mustexist=False,
        )
        if chosen:
            self._notes_dir_var.set(chosen)

    def _reload_from_disk(self) -> None:
        """Recarrega o app e reabre a janela com o que esta no disco."""
        self._on_reload()
        self._close()
        self._root.after(50, self.open)

    # ------------------------------------------------------------- salvar

    def _save(self) -> None:
        config = {
            **self._config,
            "stt_model": self._stt_var.get().strip() or "gpt-transcribe",
            "text_model": self._text_model_var.get().strip() or "gpt-5.6-luna",
            "languages": [
                code.strip().lower()
                for code in self._languages_var.get().replace(";", ",").split(",")
                if code.strip()
            ],
            "keywords": [
                line.strip()
                for line in self._get_text(self._keywords_text).splitlines()
                if line.strip()
            ],
            "my_name": self._my_name_var.get().strip(),
            "notes_dir": self._notes_dir_var.get().strip()
            or config_module.DEFAULT_CONFIG["notes_dir"],
            "ui_language": self._selected_language(),
            "overlay_mode": self._selected_overlay_mode(),
            "start_with_system": bool(self._autostart_var.get()),
        }

        try:
            new_key = self._api_key_var.get().strip()
            if new_key:
                secrets_store.set_api_key(new_key)
            config_module.save_config(config)
        except Exception as exc:
            messagebox.showerror(
                "Apolo", t("dlg.save_failed", error=exc), parent=self._win
            )
            return

        self._close()
        self._on_saved()  # aplica idioma, atalho e inicializacao automatica

    def _close(self) -> None:
        win, self._win = self._win, None
        if win is not None:
            try:
                win.unbind_all("<MouseWheel>")
            except Exception:
                pass
            win.destroy()

    # ------------------------------------------------------------ helpers

    def _selected_language(self) -> str:
        """Nome exibido -> codigo do idioma."""
        chosen = self._ui_language_var.get()
        for code, name in LANGUAGE_NAMES.items():
            if name == chosen:
                return code
        return AUTO

    def _selected_overlay_mode(self) -> str:
        chosen = self._overlay_var.get()
        for mode, label in self._overlay_labels.items():
            if label == chosen:
                return mode
        return "always"

    @staticmethod
    def _get_text(widget: tk.Text) -> str:
        return widget.get("1.0", "end-1c").strip()
