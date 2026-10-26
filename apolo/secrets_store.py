"""Armazenamento da API key da OpenAI.

Usa o Gerenciador de Credenciais do Windows (ou o Secret Service no Linux)
via keyring. Nada em texto plano, nada no repo.

A leitura cai, nesta ordem, para:
  1. a chave do proprio Apolo;
  2. a do Artemis Dictation, se estiver instalado - e a mesma conta da
     OpenAI na pratica, e assim quem ja usa o Artemis nao precisa colar a
     chave de novo;
  3. a variavel de ambiente OPENAI_API_KEY, pratica durante o desenvolvimento.

A escrita e sempre so na entrada do Apolo: mexer aqui nunca altera o Artemis.
"""

from __future__ import annotations

import os

import keyring

from .errors import CredentialsError
from .i18n import t

SERVICE = "Apolo"
USERNAME = "openai_api_key"
# Entrada do Artemis Dictation, so para leitura.
ARTEMIS_SERVICE = "ArtemisDictation"


def _read(service: str) -> str | None:
    try:
        return keyring.get_password(service, USERNAME) or None
    except keyring.errors.KeyringError:
        return None


def get_api_key() -> str | None:
    """Retorna a API key, ou None se nao houver nenhuma configurada."""
    return (
        _read(SERVICE)
        or _read(ARTEMIS_SERVICE)
        or os.environ.get("OPENAI_API_KEY")
        or None
    )


def key_source() -> str | None:
    """De onde vem a chave em uso: 'apolo', 'artemis', 'env' ou None."""
    if _read(SERVICE):
        return "apolo"
    if _read(ARTEMIS_SERVICE):
        return "artemis"
    if os.environ.get("OPENAI_API_KEY"):
        return "env"
    return None


def require_api_key() -> str:
    key = get_api_key()
    if not key:
        raise CredentialsError(t("err.no_api_key"), t("err.no_api_key.cli"))
    return key


def set_api_key(value: str) -> None:
    try:
        keyring.set_password(SERVICE, USERNAME, value.strip())
    except keyring.errors.KeyringError as exc:
        raise CredentialsError(t("err.keyring_save"), str(exc)) from exc


def delete_api_key() -> None:
    try:
        keyring.delete_password(SERVICE, USERNAME)
    except keyring.errors.PasswordDeleteError:
        pass
    except keyring.errors.KeyringError:
        pass


def masked(key: str | None) -> str:
    """Versao segura para exibir na UI: sk-...a1b2."""
    if not key:
        return t("cfg.api_key.none")
    return f"{key[:3]}...{key[-4:]}" if len(key) > 12 else "***"
