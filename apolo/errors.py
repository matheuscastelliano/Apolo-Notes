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


class TransientError(ApoloError):
    """Falha passageira do provedor: vale tentar de novo, com backoff.

    A fila de STT pega esta classe para decidir entre "espera e tenta de
    novo" e "desiste do trecho". As subclasses dizem o motivo, para o
    overlay mostrar "sem conexao" so quando for mesmo falta de rede.
    `retry_after` vem do servidor quando ele diz quanto esperar.
    """

    def __init__(
        self,
        message: str,
        detail: str | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message, detail)
        self.retry_after = retry_after


class OfflineError(TransientError):
    """Sem conexao com o provedor (rede caiu, DNS, proxy)."""


class ProviderTimeoutError(TransientError):
    """O provedor demorou mais que o `request_timeout`."""


class RateLimitedError(TransientError):
    """Limite de requisicoes por minuto. Cota esgotada NAO entra aqui."""


class ServerError(TransientError):
    """Erro 5xx do provedor."""


class NoteError(ApoloError):
    """Falha ao escrever a nota (pasta inexistente, sem permissao...)."""


class HotkeyError(ApoloError):
    """Combinacao de teclas invalida ou em conflito."""
