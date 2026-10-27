"""OpenAIProvider com um cliente fake: nada sai para a rede."""

import json
from datetime import datetime
from types import SimpleNamespace

import httpx2 as httpx  # o transporte do SDK da OpenAI 3.x
import openai
import pytest

from apolo import i18n
from apolo.errors import (
    CredentialsError,
    OfflineError,
    ProviderTimeoutError,
    RateLimitedError,
    ServerError,
    SummaryError,
    TranscriptionError,
    TransientError,
)
from apolo.providers.base import SummaryRequest
from apolo.providers.openai_provider import OpenAIProvider, parse_markdown_result


@pytest.fixture(autouse=True)
def pt_ui():
    previous = i18n.current()
    i18n.set_language("pt")
    yield
    i18n.set_language(previous)


# ------------------------------------------------------------- fakes

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/test")


def status_error(cls, status, message="erro", headers=None, code=None):
    response = httpx.Response(status, request=_REQUEST, headers=headers or {})
    body = {"message": message, "code": code} if code else None
    return cls(message, response=response, body=body)


class FakeEndpoint:
    """Responde com a proxima reacao da fila: valor, excecao ou funcao."""

    def __init__(self, *reactions):
        self.reactions = list(reactions)
        self.calls: list[dict] = []

    def create(self, **params):
        self.calls.append(params)
        reaction = self.reactions.pop(0) if len(self.reactions) > 1 else self.reactions[0]
        if isinstance(reaction, BaseException):
            raise reaction
        if callable(reaction):
            return reaction(params)
        return reaction


def make_provider(stt=None, chat=None, **kwargs):
    client = SimpleNamespace(
        audio=SimpleNamespace(transcriptions=stt or FakeEndpoint("")),
        chat=SimpleNamespace(completions=chat or FakeEndpoint(None)),
    )
    return OpenAIProvider("sk-test", client=client, **kwargs)


def chat_response(content, refusal=None):
    message = SimpleNamespace(content=content, refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


# --------------------------------------------------------------- STT


def test_transcribe_sends_languages_list_keywords_and_prompt():
    stt = FakeEndpoint("  texto do trecho \n")
    provider = make_provider(stt)
    text = provider.transcribe(
        b"RIFF", prompt="final anterior", keywords=["staging"], languages=["pt", "en"]
    )
    assert text == "texto do trecho"
    call = stt.calls[0]
    assert call["model"] == "gpt-transcribe"
    assert call["languages"] == ["pt", "en"]
    assert call["keywords"] == ["staging"]
    assert call["prompt"] == "final anterior"
    assert "language" not in call


def test_transcribe_empty_result_is_not_an_error():
    assert make_provider(FakeEndpoint("   ")).transcribe(b"x") == ""
    assert make_provider(FakeEndpoint(SimpleNamespace(text=None))).transcribe(b"x") == ""


def test_transcribe_legacy_model_uses_first_language():
    stt = FakeEndpoint("ok")
    make_provider(stt, stt_model="whisper-1").transcribe(b"x", languages=["pt", "en"])
    assert stt.calls[0]["language"] == "pt"
    assert "languages" not in stt.calls[0]


def test_degradation_drops_param_named_by_model_and_remembers():
    stt = FakeEndpoint(
        status_error(openai.BadRequestError, 400, "Unsupported parameter: 'keywords'"),
        "ok",
    )
    provider = make_provider(stt)
    kwargs = dict(prompt="p", keywords=["k"], languages=["pt", "en"])
    assert provider.transcribe(b"x", **kwargs) == "ok"
    assert "keywords" in stt.calls[0]
    assert "keywords" not in stt.calls[1]
    assert stt.calls[1]["languages"] == ["pt", "en"]

    # Proximo trecho ja vai sem o parametro, sem pagar outra recusa.
    provider.transcribe(b"x", **kwargs)
    assert len(stt.calls) == 3
    assert "keywords" not in stt.calls[2]


def test_degradation_on_old_sdk_type_error():
    stt = FakeEndpoint(TypeError("create() got an unexpected keyword argument 'languages'"), "ok")
    provider = make_provider(stt)
    assert provider.transcribe(b"x", keywords=["k"], languages=["pt"]) == "ok"
    assert "languages" not in stt.calls[1]
    assert stt.calls[1]["keywords"] == ["k"]


def test_degradation_gives_up_when_nothing_left_to_drop():
    stt = FakeEndpoint(status_error(openai.BadRequestError, 400, "audio too short"))
    with pytest.raises(TranscriptionError) as info:
        make_provider(stt).transcribe(b"x")
    assert not isinstance(info.value, TransientError)


@pytest.mark.parametrize(
    "exc, expected",
    [
        (openai.APIConnectionError(request=_REQUEST), OfflineError),
        (openai.APITimeoutError(request=_REQUEST), ProviderTimeoutError),
        (status_error(openai.RateLimitError, 429), RateLimitedError),
        (status_error(openai.InternalServerError, 503), ServerError),
        (status_error(openai.AuthenticationError, 401), CredentialsError),
        (status_error(openai.PermissionDeniedError, 403), CredentialsError),
        (status_error(openai.NotFoundError, 404), TranscriptionError),
        (RuntimeError("boom"), TranscriptionError),
    ],
)
def test_stt_errors_are_classified(exc, expected):
    with pytest.raises(expected) as info:
        make_provider(FakeEndpoint(exc)).transcribe(b"x")
    transient = expected in (OfflineError, ProviderTimeoutError, RateLimitedError, ServerError)
    assert isinstance(info.value, TransientError) is transient
    assert info.value.message  # mensagem amigavel, via i18n


def test_rate_limit_carries_retry_after():
    exc = status_error(openai.RateLimitError, 429, headers={"retry-after": "7"})
    with pytest.raises(RateLimitedError) as info:
        make_provider(FakeEndpoint(exc)).transcribe(b"x")
    assert info.value.retry_after == 7.0
    assert info.value.message == "Limite de uso da OpenAI atingido. Tentando de novo em instantes."


def test_insufficient_quota_is_not_retryable():
    exc = status_error(openai.RateLimitError, 429, code="insufficient_quota")
    with pytest.raises(CredentialsError) as info:
        make_provider(FakeEndpoint(exc)).transcribe(b"x")
    assert "crédito" in info.value.message


# ------------------------------------------------------------ resumo

REQUEST = SummaryRequest(
    transcript="[00:00:12] Eu (Matheus): vamos subir pro staging amanhã",
    user_notes="- prazo sexta",
    participants=["João", "Ana"],
    my_name="Matheus",
    date=datetime(2026, 10, 26, 14, 30),
    duration_seconds=47 * 60,
    system_prompt="## Resumo\n## Próximos passos",
)

JSON_NOTE = json.dumps(
    {
        "title": "Deploy no staging",
        "tags": ["Deploy", "#staging", "deploy"],
        "body_markdown": "## Resumo\nVai para staging amanhã.",
    }
)


def test_summarize_structured_output():
    chat = FakeEndpoint(chat_response(JSON_NOTE))
    result = make_provider(chat=chat).summarize(REQUEST)
    assert result.title == "Deploy no staging"
    assert result.tags == ["deploy", "staging"]
    assert result.body_markdown == "## Resumo\nVai para staging amanhã."

    call = chat.calls[0]
    assert call["model"] == "gpt-5.6-luna"
    assert call["response_format"]["type"] == "json_schema"
    system, user = (m["content"] for m in call["messages"])
    assert "'Eu' é Matheus" in system
    assert "trecho não transcrito" in system
    assert system.index("Não invente nada") < system.index("## Próximos passos")
    assert "Participantes: João, Ana" in user
    assert "Data: 26/10/2026 14:30" in user
    assert "Duração: 47 min" in user
    assert "- prazo sexta" in user
    assert user.endswith(REQUEST.transcript)


def test_summarize_uses_preset_model():
    chat = FakeEndpoint(chat_response(JSON_NOTE))
    request = SummaryRequest(transcript="x", system_prompt="p", model="outro-modelo")
    make_provider(chat=chat).summarize(request)
    assert chat.calls[0]["model"] == "outro-modelo"


def test_summarize_falls_back_to_markdown_when_schema_refused():
    chat = FakeEndpoint(
        status_error(openai.BadRequestError, 400, "Invalid parameter: 'response_format' of type 'json_schema'"),
        chat_response("# Deploy no staging\n\n## Resumo\nVai amanhã."),
    )
    provider = make_provider(chat=chat)
    result = provider.summarize(REQUEST)
    assert result.title == "Deploy no staging"
    assert result.tags == []
    assert result.body_markdown == "## Resumo\nVai amanhã."
    assert "response_format" not in chat.calls[1]
    assert "1ª linha" in chat.calls[1]["messages"][0]["content"]

    # O modelo fica marcado: o proximo resumo ja vai direto em Markdown.
    provider.summarize(REQUEST)
    assert len(chat.calls) == 3
    assert "response_format" not in chat.calls[2]


def test_summarize_falls_back_when_json_is_invalid():
    chat = FakeEndpoint(
        chat_response("isto nao e json"),
        chat_response("Título\ncorpo"),
    )
    result = make_provider(chat=chat).summarize(REQUEST)
    assert (result.title, result.body_markdown) == ("Título", "corpo")
    assert len(chat.calls) == 2


def test_summarize_refusal_and_empty():
    refused = FakeEndpoint(chat_response(None, refusal="nao posso"))
    with pytest.raises(SummaryError) as info:
        make_provider(chat=refused).summarize(REQUEST)
    assert info.value.detail == "nao posso"

    with pytest.raises(SummaryError):
        make_provider(chat=FakeEndpoint(chat_response("  "))).summarize(REQUEST)


def test_summarize_errors_are_classified():
    with pytest.raises(OfflineError):
        make_provider(chat=FakeEndpoint(openai.APIConnectionError(request=_REQUEST))).summarize(REQUEST)
    other_400 = status_error(openai.BadRequestError, 400, "context length exceeded")
    with pytest.raises(SummaryError):
        make_provider(chat=FakeEndpoint(other_400)).summarize(REQUEST)


def test_summary_prompt_without_my_name():
    chat = FakeEndpoint(chat_response(JSON_NOTE))
    make_provider(chat=chat).summarize(SummaryRequest(transcript="x", system_prompt="p"))
    system, user = (m["content"] for m in chat.calls[0]["messages"])
    assert "'Eu' é a pessoa que gravou a reunião" in system
    assert "(nenhuma)" in user


def test_parse_markdown_result_variants():
    r = parse_markdown_result("\n\n**Kickoff do projeto**\n\n## Temas\n- a")
    assert (r.title, r.body_markdown) == ("Kickoff do projeto", "## Temas\n- a")
    r = parse_markdown_result("## Resumo\nsem titulo")
    assert (r.title, r.body_markdown) == ("Reunião", "## Resumo\nsem titulo")
