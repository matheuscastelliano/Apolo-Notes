"""Janela de configuracoes: Salvar grava no disco e avisa que salvou.

Bug do teste real: o atributo `_selected` (indice do preset) sobrescrevia o
metodo `_selected(...)`, entao o Salvar quebrava antes de gravar e nada
persistia. Estes testes montam a janela de verdade (Tk escondido) com o
APPDATA num diretorio temporario.
"""

from __future__ import annotations

import time
import tkinter as tk

import pytest

from apolo import config as config_module
from apolo import i18n, secrets_store, startup
from apolo.audio import sources
from apolo.ui import settings_window
from apolo.ui.settings_window import SettingsWindow


@pytest.fixture(scope="module")
def tk_root():
    # Um Tk so para o modulo: criar varios no mesmo processo as vezes falha.
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"sem Tk: {exc}")
    r.withdraw()
    yield r
    r.destroy()


@pytest.fixture
def root(tk_root, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(sources, "list_devices",
                        lambda: {"inputs": [{"name": "Mic", "default": True}],
                                 "outputs": [{"name": "Caixa", "default": True}]})
    monkeypatch.setattr(secrets_store, "get_api_key", lambda: None)
    monkeypatch.setattr(secrets_store, "key_source", lambda: None)
    monkeypatch.setattr(startup, "is_enabled", lambda: False)
    i18n.set_language("en")
    yield tk_root
    for child in tk_root.winfo_children():
        child.destroy()
    i18n.set_language("en")


def make_window(root):
    saved = []
    sw = SettingsWindow(root, on_saved=lambda: saved.append(True),
                        on_reload=lambda: None, on_open_folder=lambda: None)
    sw.open()
    root.update()
    return sw, saved


def test_save_writes_to_disk_and_shows_saved(root):
    sw, saved = make_window(root)
    sw._my_name_var.set("Matheus")
    sw._keywords_text.insert("1.0", "Ceph\nPrometheus")
    sw._save()
    assert saved == [True]
    on_disk = config_module.load_config()
    assert on_disk["my_name"] == "Matheus"
    assert on_disk["keywords"] == ["Ceph", "Prometheus"]
    # A janela fica aberta e diz que salvou.
    assert sw._win is not None and sw._win.winfo_exists()
    assert sw._saved_label.cget("text") == i18n.t("cfg.saved")
    # Salvar de novo (depois de mexer num preset) tambem funciona.
    sw._select_preset(1)
    sw._save()
    assert len(saved) == 2


def test_saved_message_goes_away(root, monkeypatch):
    monkeypatch.setattr(settings_window, "_SAVED_MS", 10)
    sw, _ = make_window(root)
    sw._save()
    end = time.monotonic() + 2
    while sw._saved_label.cget("text") and time.monotonic() < end:
        root.update()
    assert sw._saved_label.cget("text") == ""


def test_changing_language_reopens_the_window_translated(root):
    sw, saved = make_window(root)
    sw._ui_language_var.set(i18n.LANGUAGE_NAMES["pt"])
    sw._save()
    end = time.monotonic() + 2
    while (sw._win is None or not sw._win.winfo_exists()) and time.monotonic() < end:
        root.update()
    assert sw._win is not None and sw._win.winfo_exists()
    assert sw._win.title() == i18n.t_in("pt", "cfg.title")
    assert sw._saved_label.cget("text") == i18n.t_in("pt", "cfg.saved")
    assert config_module.load_config()["ui_language"] == "pt"
    assert [p.name for p in config_module.load_presets()][0] == "Geral"


# ------------------------------------------------ Quem e quem / Quem esta


def _review():
    import numpy as np

    from apolo.voices.identify import VoiceCandidate, VoiceReview

    clip = np.zeros(16000, dtype=np.int16)
    return VoiceReview(
        voices=[VoiceCandidate("V1", "Voz A", 104.0, clip, "eu e o Pedro", "Fernando"),
                VoiceCandidate("V2", "Voz B", 304.0, clip, "três casos")],
        names=["Marcos", "Fernando"],
    )


def _widgets(widget, kind):
    found = []
    for child in widget.winfo_children():
        if isinstance(child, kind):
            found.append(child)
        found += _widgets(child, kind)
    return found


def test_voices_dialog_confirms_names(tk_root, monkeypatch):
    from tkinter import ttk

    from apolo.ui import audio_play, voices_dialog

    played = []
    monkeypatch.setattr(audio_play, "play", lambda pcm: played.append(len(pcm)))
    i18n.set_language("pt")
    got = []
    win = voices_dialog.ask_voices(tk_root, _review(), lambda names, save: got.append((names, save)))
    combos = _widgets(win, ttk.Combobox)
    assert [c.get() for c in combos] == ["Fernando", "Não sei"]
    combos[1].set("Marcos")
    buttons = _widgets(win, ttk.Button)
    next(b for b in buttons if b.cget("text") == "▶").invoke()
    assert played == [16000]
    next(b for b in buttons if b.cget("text") == "Gerar resumo").invoke()
    assert got == [({"V1": "Fernando", "V2": "Marcos"}, True)]


def test_voices_dialog_closed_names_nobody(tk_root):
    from apolo.ui import voices_dialog

    got = []
    win = voices_dialog.ask_voices(tk_root, _review(), lambda names, save: got.append(names))
    win.event_generate("<Escape>")
    tk_root.update()
    assert got == [None]


def test_people_dialog_splits_names(tk_root):
    from tkinter import ttk

    from apolo.ui import people_dialog

    got = []
    win = people_dialog.ask_people(tk_root, ["Marcos"], got.append)
    entry = _widgets(win, ttk.Entry)[0]
    entry.delete(0, "end")
    entry.insert(0, "Marcos, Fernando; Camila ,")
    next(b for b in _widgets(win, ttk.Button)).invoke()
    assert got == [["Marcos", "Fernando", "Camila"]]
