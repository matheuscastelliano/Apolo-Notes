"""Indicador visual flutuante: Preparando / Gravando 00:12:34 / Processando.

Veio do Artemis. La o motivo do WS_EX_NOACTIVATE era a colagem; aqui e a
reuniao: clicar em Iniciar nao pode tirar o foco da janela do Teams/Meet.

Diferencas para o Artemis: durante a gravacao o titulo muda a cada segundo
(o cronometro) - `set_title` troca so o texto, sem reposicionar nem piscar -
e clicar no indicador abre a nota.
"""

from __future__ import annotations

import ctypes
import tkinter as tk

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080  # tambem tira o overlay do Alt+Tab

# kind -> (cor do ponto, ms ate sumir sozinho; 0 = fica)
_STYLES = {
    "preparing": ("#f5a524", 0),
    "recording": ("#e5484d", 0),
    "processing": ("#f5a524", 0),
    "done": ("#30a46c", 4000),
    "error": ("#e5484d", 8000),
}

_BG = "#1c1c1e"
_FG = "#f2f2f7"
_DIM = "#98989d"


class Overlay:
    """Uma janelinha sem borda no canto inferior direito."""

    def __init__(self, root: tk.Tk, on_click=None):
        self._root = root
        self._hide_job: str | None = None
        self._on_click = on_click

        self._win = tk.Toplevel(root)
        self._win.withdraw()
        self._win.overrideredirect(True)
        self._win.attributes("-topmost", True)
        self._win.attributes("-alpha", 0.94)
        self._win.configure(bg=_BG)

        frame = tk.Frame(self._win, bg=_BG, padx=14, pady=10)
        frame.pack()

        self._dot = tk.Canvas(
            frame, width=12, height=12, bg=_BG, highlightthickness=0
        )
        self._dot_id = self._dot.create_oval(1, 1, 11, 11, fill=_BG, outline="")
        self._dot.pack(side="left", padx=(0, 10))

        text_frame = tk.Frame(frame, bg=_BG)
        text_frame.pack(side="left")
        self._title = tk.Label(
            text_frame, text="", bg=_BG, fg=_FG, font=("Segoe UI", 11, "bold")
        )
        self._title.pack(anchor="w")
        self._detail = tk.Label(
            text_frame, text="", bg=_BG, fg=_DIM, font=("Segoe UI", 9), wraplength=340,
            justify="left",
        )

        # Clique em qualquer parte: o NOACTIVATE deixa clicar sem tirar o foco
        # da janela da reuniao.
        if on_click is not None:
            for widget in (self._win, frame, self._dot, text_frame, self._title, self._detail):
                widget.bind("<Button-1>", lambda _e: self._on_click())
                widget.configure(cursor="hand2")

        self._apply_noactivate()

    def _apply_noactivate(self) -> None:
        """Marca a janela como nao-ativavel no nivel do Win32."""
        try:
            self._win.update_idletasks()
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self._win.winfo_id()) or self._win.winfo_id()
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
            )
        except Exception:
            # Sem isto (ou no Linux) o overlay ainda funciona; so fica mais
            # propenso a atrapalhar o foco. Nao vale derrubar nada.
            pass

    def show(
        self,
        kind: str,
        title: str,
        detail: str = "",
        duration: int | None = None,
    ) -> None:
        style = _STYLES.get(kind)
        if style is None:
            self.hide()
            return
        color, auto_hide = style
        if duration is not None:
            auto_hide = duration

        self._cancel_hide()
        self._dot.itemconfigure(self._dot_id, fill=color)
        self._title.configure(text=title)
        if detail:
            self._detail.configure(text=detail)
            self._detail.pack(anchor="w", pady=(2, 0))
        else:
            self._detail.pack_forget()

        self._win.deiconify()
        self._position()
        self._win.lift()
        if auto_hide:
            self._hide_job = self._root.after(auto_hide, self.hide)

    def set_title(self, title: str) -> None:
        """Atualiza so o texto (cronometro), se o overlay estiver visivel."""
        if self._win.state() != "withdrawn":
            self._title.configure(text=title)

    def hide(self) -> None:
        self._cancel_hide()
        self._win.withdraw()

    def _cancel_hide(self) -> None:
        if self._hide_job is not None:
            try:
                self._root.after_cancel(self._hide_job)
            except Exception:
                pass
            self._hide_job = None

    def _position(self) -> None:
        self._win.update_idletasks()
        width = self._win.winfo_width()
        height = self._win.winfo_height()
        screen_w = self._win.winfo_screenwidth()
        screen_h = self._win.winfo_screenheight()
        # Folga embaixo para nao ficar atras da barra de tarefas.
        x = screen_w - width - 24
        y = screen_h - height - 72
        self._win.geometry(f"+{x}+{y}")
