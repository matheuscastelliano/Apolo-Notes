"""Fixtures compartilhadas pelos testes de sessao (fase 3) e de fim (fase 4)."""

from __future__ import annotations

import pytest

from apolo import i18n


@pytest.fixture
def portuguese():
    """Rotulos da nota em portugues ("Eu", "Outros", "Gravando")."""
    i18n.set_language("pt")
    yield
    i18n.set_language("en")


@pytest.fixture
def config(tmp_path):
    return {
        "notes_dir": str(tmp_path / "notas"),
        "spool_dir": str(tmp_path / "spool"),
        "max_pending_audio_hours": 2,
        "keywords": ["deploy"],
        "languages": ["pt", "en"],
        "stt_workers": 2,
        "transcript_block": "callout",
    }
