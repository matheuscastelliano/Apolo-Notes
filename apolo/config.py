r"""Configuracao persistente, fora do codigo.

Fica em %APPDATA%\Apolo\ (no Linux, ~/.config/Apolo) com dois arquivos:
  config.json   -> dispositivos, modelos, idiomas, pastas, travas
  presets.json  -> os tipos de resumo

Ambos sao criados a partir dos defaults abaixo na primeira execucao. O que e
dado de trabalho e nao configuracao - o spool de audio temporario - mora em
%LOCALAPPDATA%\Apolo, que nao vai junto no roaming do perfil do Windows.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from . import i18n
from .errors import ConfigError
from .presets import SummaryPreset

APP_NAME = "Apolo"

# Como o indicador flutuante se comporta.
OVERLAY_MODES = ("always", "errors", "never")
# O que acontece ao parar: perguntar o preset/titulo ou gerar direto.
ON_STOP_MODES = ("ask", "auto")
# Como a transcricao aparece recolhida na nota.
TRANSCRIPT_BLOCKS = ("callout", "details")

DEFAULT_CONFIG: dict[str, Any] = {
    # None = dispositivo padrao. Caso contrario, o nome do device (nao o
    # indice: indices mudam quando voce pluga/despluga hardware).
    "mic_device": None,
    # Saida cujo loopback vira o canal "Outros". None = saida padrao.
    "loopback_device": None,
    # gpt-transcribe e o STT atual da OpenAI; aceita `languages` em lista,
    # `keywords` e `prompt` (validado na fase 0, ver spike/RESULTADOS.md).
    "stt_model": "gpt-transcribe",
    "text_model": "gpt-5.6-luna",
    # Idiomas da FALA. Reuniao em portugues com termos em ingles: os dois.
    "languages": ["pt", "en"],
    # Termos que valem para todos os presets; cada preset pode somar os seus.
    "keywords": [],
    # Quem e o "Eu" da transcricao, para as tarefas sairem com o seu nome.
    "my_name": "",
    # Onde as notas .md sao escritas. Pode ser o vault do Obsidian.
    "notes_dir": "~/Documents/Apolo",
    # None = %LOCALAPPDATA%\Apolo\spool. Audio temporario, apagado no fim.
    "spool_dir": None,
    # Guardar o audio da reuniao (FLAC por canal). Desligado por padrao.
    "save_audio": False,
    "audio_dir": None,
    "default_preset": "geral",
    # ask | auto - ver ON_STOP_MODES.
    "on_stop": "ask",
    # callout (Obsidian) | details (HTML) - ver TRANSCRIPT_BLOCKS.
    "transcript_block": "callout",
    # Chunker (fase 0): corta na 1a pausa depois do alvo, forca no maximo.
    "chunk_min_seconds": 1.0,
    "chunk_target_seconds": 12,
    "chunk_max_seconds": 30,
    # Pausa que basta para cortar depois do alvo / pausa que corta sempre.
    "chunk_short_pause_seconds": 0.3,
    "chunk_long_pause_seconds": 1.5,
    # Menos fala que isso no trecho = clique ou tosse: descartado.
    "chunk_min_speech_seconds": 0.4,
    # Silencio mantido antes e depois da fala, para nao comer as bordas.
    "chunk_pad_seconds": 0.2,
    "stt_workers": 2,
    "request_timeout": 60,
    # Travas de seguranca.
    "max_pending_audio_hours": 2,
    "silence_alert_minutes": 10,
    "max_session_hours": 3,
    # Atalho global opcional para iniciar/parar (ex.: "<ctrl>+<alt>+m").
    # A bandeja e o caminho principal; no Linux o atalho evdev pode brigar
    # com o do Artemis, por isso nao ha um por padrao.
    "hotkey": None,
    # always | errors | never - ver OVERLAY_MODES.
    "overlay_mode": "always",
    # Idioma da INTERFACE. "auto" segue o idioma do sistema.
    "ui_language": i18n.AUTO,
    "start_with_system": False,
}


def default_presets() -> list[dict[str, Any]]:
    """Os quatro tipos de resumo iniciais, no idioma da interface.

    Gerados na hora, e nao constantes de modulo, porque quem instala o Apolo
    com a interface em ingles deve receber prompts em ingles.
    """
    return [
        {
            "id": pid,
            "name": i18n.t(f"preset.{pid}.name"),
            "description": i18n.t(f"preset.{pid}.description"),
            "system_prompt": i18n.t(f"preset.{pid}.system_prompt"),
            "keywords": [],
            "languages": None,
            "text_model": None,
        }
        for pid in ("geral", "daily", "cliente", "1on1")
    ]


def config_dir() -> Path:
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / ".config"
    return root / APP_NAME


def data_dir() -> Path:
    """Dados locais de trabalho (spool). Nao entram no roaming do perfil."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / APP_NAME
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / APP_NAME.lower()


def config_path() -> Path:
    return config_dir() / "config.json"


def presets_path() -> Path:
    return config_dir() / "presets.json"


def notes_dir(config: dict[str, Any]) -> Path:
    return Path(os.path.expandvars(str(config.get("notes_dir") or DEFAULT_CONFIG["notes_dir"]))).expanduser()


def spool_dir(config: dict[str, Any]) -> Path:
    value = config.get("spool_dir")
    if value:
        return Path(os.path.expandvars(str(value))).expanduser()
    return data_dir() / "spool"


def _read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, fallback)
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ConfigError(
            i18n.t("err.config_read", file=path.name), str(exc)
        ) from exc


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    tmp.replace(path)  # atomico: nunca deixa um config pela metade


def load_config() -> dict[str, Any]:
    """Le config.json, completando com os defaults as chaves que faltarem.

    Define o idioma da interface como efeito colateral, para que tudo que
    vier depois - inclusive os presets padrao - ja saia traduzido.
    """
    data = _read_json(config_path(), DEFAULT_CONFIG)
    if not isinstance(data, dict):
        raise ConfigError(i18n.t("err.config_object"))
    config = {**DEFAULT_CONFIG, **data}

    i18n.set_language(config.get("ui_language"))
    if config.get("overlay_mode") not in OVERLAY_MODES:
        config["overlay_mode"] = "always"
    if config.get("on_stop") not in ON_STOP_MODES:
        config["on_stop"] = "ask"
    if config.get("transcript_block") not in TRANSCRIPT_BLOCKS:
        config["transcript_block"] = "callout"
    if isinstance(config.get("languages"), str):
        # Editado a mao como "pt, en": aceita.
        config["languages"] = [
            code.strip() for code in config["languages"].split(",") if code.strip()
        ]
    return config


def save_config(config: dict[str, Any]) -> None:
    _write_json(config_path(), config)


def load_presets() -> list[SummaryPreset]:
    data = _read_json(presets_path(), default_presets())
    if not isinstance(data, list) or not data:
        raise ConfigError(i18n.t("err.presets_list"))
    presets = []
    for raw in data:
        try:
            presets.append(SummaryPreset.from_dict(raw))
        except (TypeError, ValueError) as exc:
            raise ConfigError(i18n.t("err.preset_invalid"), str(exc)) from exc
    return presets


def save_presets(presets: list[SummaryPreset]) -> None:
    _write_json(presets_path(), [p.to_dict() for p in presets])
