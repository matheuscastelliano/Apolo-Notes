"""Janela de parada: tipo de resumo, titulo e quem estava na reuniao.

    +- Reuniao encerrada (47 min) ---------+
    | Tipo:          [Cliente        v]    |
    | Titulo:        [opcional_________]   |
    | Quem estava:   [Joao, Ana________]   |
    |                     [Gerar resumo]   |
    +--------------------------------------+

Aparece assim que voce clica em Parar, enquanto o resto da transcricao
termina por tras - preencher nao atrasa nada. Fechar a janela (ou Esc)
gera o resumo com o tipo que ja estava marcado: o resumo nunca fica para
tras por um clique que faltou.

Tambem serve o "Resumir de novo", com os campos ja preenchidos pela nota.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable

from ..i18n import t
from ..presets import SummaryPreset
from ..session.summary import FinalizeChoice

_HINT = "#6b6b70"


def ask_finalize(
    root: tk.Tk,
    *,
    title: str,
    presets: list[SummaryPreset],
    selected: SummaryPreset,
    on_done: Callable[[FinalizeChoice], None],
    hint: str = "",
    initial_title: str = "",
    initial_participants: list[str] | None = None,
    button: str = "",
) -> tk.Toplevel:
    """Abre a janela e chama `on_done` uma unica vez, na main thread."""
    win = tk.Toplevel(root)
    win.title(title)
    win.resizable(False, False)
    win.attributes("-topmost", True)

    bold = tkfont.Font(family="Segoe UI", size=10, weight="bold")
    frame = ttk.Frame(win, padding=(18, 14, 18, 14))
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)

    ttk.Label(frame, text=title, font=bold).grid(row=0, column=0, columnspan=2, sticky="w")
    row = 1
    if hint:
        ttk.Label(frame, text=hint, foreground=_HINT, wraplength=360, justify="left").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(2, 10)
        )
        row += 1

    names = [p.name for p in presets]
    preset_var = tk.StringVar(value=selected.name)
    title_var = tk.StringVar(value=initial_title)
    people_var = tk.StringVar(value=", ".join(initial_participants or []))

    fields = (
        ("stop.type", ttk.Combobox(frame, textvariable=preset_var, values=names,
                                   state="readonly", width=32)),
        ("stop.title", ttk.Entry(frame, textvariable=title_var, width=34)),
        ("stop.people", ttk.Entry(frame, textvariable=people_var, width=34)),
    )
    for key, widget in fields:
        ttk.Label(frame, text=t(key)).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=3)
        widget.grid(row=row, column=1, sticky="ew", pady=3)
        row += 1
    ttk.Label(frame, text=t("stop.people.hint"), foreground=_HINT).grid(
        row=row, column=1, sticky="w"
    )
    row += 1

    state = {"done": False}

    def finish(_event=None) -> None:
        if state["done"]:
            return
        state["done"] = True
        chosen = next((p for p in presets if p.name == preset_var.get()), selected)
        people = [p.strip() for p in people_var.get().replace(";", ",").split(",") if p.strip()]
        win.destroy()
        on_done(FinalizeChoice(chosen, title_var.get().strip(), people))

    ttk.Button(frame, text=button or t("stop.generate"), command=finish).grid(
        row=row, column=1, sticky="e", pady=(12, 0)
    )
    win.bind("<Return>", finish)
    win.bind("<Escape>", finish)
    win.protocol("WM_DELETE_WINDOW", finish)

    win.update_idletasks()
    x = win.winfo_screenwidth() - win.winfo_reqwidth() - 40
    y = win.winfo_screenheight() - win.winfo_reqheight() - 160
    win.geometry(f"+{x}+{y}")
    win.lift()
    win.focus_force()
    fields[1][1].focus_set()  # o titulo e o campo que mais se preenche
    return win
