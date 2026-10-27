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
