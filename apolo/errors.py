"""Erros do Apolo.

Regra herdada do Artemis: tudo que pode dar errado durante uma reuniao vira
um `ApoloError` com uma mensagem curta, pronta para o overlay/tray. Nada de
traceback na cara do usuario, e nada derruba o app - ele fica ligado o dia
todo e uma reuniao gravando nao pode morrer por causa de um detalhe.
"""


class ApoloError(Exception):
    """Erro esperado, com mensagem amigavel para exibir ao usuario."""

    def __init__(self, message: str, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        if self.detail:
            return f"{self.message} ({self.detail})"
        return self.message


class ConfigError(ApoloError):
    """config.json / presets.json invalido ou ilegivel."""


class CredentialsError(ApoloError):
    """API key ausente ou rejeitada pelo provedor."""


class AudioError(ApoloError):
    """Microfone ou loopback indisponivel, ocupado ou com audio invalido."""


class TranscriptionError(ApoloError):
    """Falha na chamada de speech-to-text."""


class SummaryError(ApoloError):
    """Falha ao gerar o resumo da reuniao."""


class NoteError(ApoloError):
    """Falha ao escrever a nota (pasta inexistente, sem permissao...)."""


class HotkeyError(ApoloError):
    """Combinacao de teclas invalida ou em conflito."""
