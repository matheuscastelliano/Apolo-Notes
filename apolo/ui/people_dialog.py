"""Janelinha "Quem esta na reuniao", aberta pela bandeja durante a gravacao.

Opcional (PLANO_VOZES.md, 3.6). Da para abrir varias vezes e ir
completando, quando alguem entra atrasado. Os nomes ja vem preenchidos no
"Quem estava" da janela de parada. Com uma pessoa so, o Apolo nem separa as
vozes: numa conversa a dois, o nome dela ja diz quem falou.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable

from ..i18n import t

_HINT = "#6b6b70"


def split_names(text: str) -> list[str]:
    return [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]


def ask_people(
    root: tk.Tk,
    initial: list[str],
    on_done: Callable[[list[str]], None],
) -> tk.Toplevel:
    win = tk.Toplevel(root)
    win.title(t("people.title"))
    win.resizable(False, False)
    win.attributes("-topmost", True)

    bold = tkfont.Font(family="Segoe UI", size=10, weight="bold")
    frame = ttk.Frame(win, padding=(18, 14, 18, 14))
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=t("people.title"), font=bold).grid(row=0, column=0, columnspan=2,
                                                            sticky="w")
    ttk.Label(frame, text=t("people.hint"), foreground=_HINT, wraplength=340,
              justify="left").grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 10))
    var = tk.StringVar(value=", ".join(initial))
    entry = ttk.Entry(frame, textvariable=var, width=40)
    entry.grid(row=2, column=0, columnspan=2, sticky="ew")

    state = {"done": False}

    def save(_event=None) -> None:
        if state["done"]:
            return
        state["done"] = True
        names = split_names(var.get())
        win.destroy()
        on_done(names)

    def cancel(_event=None) -> None:
        state["done"] = True
        win.destroy()

    ttk.Button(frame, text=t("people.save"), command=save).grid(row=3, column=1, sticky="e",
                                                                pady=(12, 0))
    win.bind("<Return>", save)
    win.bind("<Escape>", cancel)
    win.protocol("WM_DELETE_WINDOW", cancel)

    win.update_idletasks()
    x = win.winfo_screenwidth() - win.winfo_reqwidth() - 40
    y = win.winfo_screenheight() - win.winfo_reqheight() - 160
    win.geometry(f"+{max(0, x)}+{max(0, y)}")
    win.lift()
    win.focus_force()
    entry.focus_set()
    entry.icursor("end")
    return win
