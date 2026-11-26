"""Janela "Quem e quem": ouvir cada voz dos "Outros" e dizer quem e.

    +- Quem é quem ---------------------------------------------+
    | Ouça cada voz e diga quem é...                             |
    | Voz A  [▶]  [Fernando      v]  1 min 44 s · sugestão       |
    |             "eu e o Pedro estamos puxando os cards..."     |
    | Voz B  [▶]  [Marcos        v]  5 min 04 s                  |
    | [x] Guardar as vozes para reconhecer da próxima vez        |
    |                                        [Gerar resumo]      |
    +------------------------------------------------------------+

Aparece depois da janela de parada, so quando ha o que perguntar: duas ou
mais vozes e nem todas reconhecidas pela biblioteca. O campo de nome aceita
digitar um nome novo. Dar o mesmo nome a duas vozes junta as duas.

Fechar a janela (X ou Esc) nao poe nome em ninguem: as vozes saem como
"Voz A", "Voz B". So o botao confirma, porque uma sugestao errada aceita
sem ouvir trocaria os nomes na nota calada (fase V0).

So vira amostra na biblioteca a voz que voce OUVIU (▶) ou cujo nome voce
escolheu/digitou. Sugestao aceita sem ouvir vale para a nota, mas nao
ensina a biblioteca: em 26/11 uma sugestao errada aceita assim virou a
amostra mais nova do Fernando e da Vitoria, e o erro se repetiu.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable

from ..i18n import t
from ..voices.identify import VoiceReview
from . import audio_play

_HINT = "#6b6b70"


def _speech_time(seconds: float) -> str:
    if seconds < 60:
        return f"{int(round(seconds))} s"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes} min {rest:02d} s"


def ask_voices(
    root: tk.Tk,
    review: VoiceReview,
    on_done: Callable[[dict[str, str] | None, set[str]], None],
) -> tk.Toplevel:
    """Abre a janela e chama `on_done(nomes, guardar)` uma vez, na main thread.

    `nomes` = voz -> nome ("" = nao sei); None = fechou sem confirmar.
    `guardar` = as vozes que podem virar amostra (ouvidas ou com o nome
    mexido, e com a caixa "Guardar" marcada).
    """
    win = tk.Toplevel(root)
    win.title(t("voices.title"))
    win.resizable(False, False)
    win.attributes("-topmost", True)

    bold = tkfont.Font(family="Segoe UI", size=10, weight="bold")
    frame = ttk.Frame(win, padding=(18, 14, 18, 14))
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(2, weight=1)

    ttk.Label(frame, text=t("voices.title"), font=bold).grid(row=0, column=0, columnspan=4,
                                                            sticky="w")
    ttk.Label(frame, text=t("voices.hint"), foreground=_HINT, wraplength=440,
              justify="left").grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 10))

    unknown = t("voices.unknown")
    options = [*review.names, unknown]
    variables: dict[str, tk.StringVar] = {}
    heard: set[str] = set()  # vozes que voce ouviu ou cujo nome mexeu
    suggested = {v.id: v.suggestion or unknown for v in review.voices}

    def play(voice) -> None:
        heard.add(voice.id)
        audio_play.play(voice.clip)

    row = 2
    for voice in review.voices:
        ttk.Label(frame, text=voice.label, font=bold).grid(row=row, column=0, sticky="w",
                                                           padx=(0, 8))
        ttk.Button(frame, text="▶", width=3,
                   command=lambda v=voice: play(v)).grid(
            row=row, column=1, sticky="w", padx=(0, 8))
        var = tk.StringVar(value=voice.suggestion or unknown)
        variables[voice.id] = var
        ttk.Combobox(frame, textvariable=var, values=options, width=24).grid(
            row=row, column=2, sticky="ew")
        info = t("voices.speech", time=_speech_time(voice.seconds))
        if voice.suggestion:
            info += " · " + t("voices.suggested")
        ttk.Label(frame, text=info, foreground=_HINT).grid(row=row, column=3, sticky="w",
                                                           padx=(10, 0))
        row += 1
        if voice.sample_text:
            ttk.Label(frame, text=f"“{voice.sample_text}”", foreground=_HINT, wraplength=420,
                      justify="left").grid(row=row, column=2, columnspan=2, sticky="w",
                                           pady=(0, 6))
            row += 1

    if review.skipped:
        short = ", ".join(f"{label} ({_speech_time(sec)})" for label, sec in review.skipped)
        ttk.Label(frame, text=t("voices.skipped", voices=short), foreground=_HINT,
                  wraplength=440, justify="left").grid(row=row, column=0, columnspan=4,
                                                       sticky="w", pady=(6, 0))
        row += 1

    save_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(frame, text=t("voices.save"), variable=save_var).grid(
        row=row, column=0, columnspan=4, sticky="w", pady=(10, 0))
    row += 1

    state = {"done": False}

    def close(confirmed: bool) -> None:
        if state["done"]:
            return
        state["done"] = True
        audio_play.stop()
        names = None
        if confirmed:
            names = {}
            for vid, var in variables.items():
                value = var.get().strip()
                names[vid] = "" if not value or value == unknown else value
        save: set[str] = set()
        if save_var.get():
            save = {vid for vid, var in variables.items()
                    if vid in heard or var.get().strip() != suggested[vid]}
        win.destroy()
        on_done(names, save)

    ttk.Button(frame, text=t("stop.generate"), command=lambda: close(True)).grid(
        row=row, column=0, columnspan=4, sticky="e", pady=(12, 0))
    win.bind("<Return>", lambda _e: close(True))
    win.bind("<Escape>", lambda _e: close(False))
    win.protocol("WM_DELETE_WINDOW", lambda: close(False))

    win.update_idletasks()
    x = win.winfo_screenwidth() - win.winfo_reqwidth() - 40
    y = win.winfo_screenheight() - win.winfo_reqheight() - 160
    win.geometry(f"+{max(0, x)}+{max(0, y)}")
    win.lift()
    win.focus_force()
    return win

