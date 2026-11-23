"""Ponte com a API da OpenAI: transcricao dos trechos e resumo da reuniao.

Este e o unico modulo que sabe que o provedor e a OpenAI. Trocar de provedor
ou de modelo depois significa mexer aqui, e so aqui.

Sobre os modelos (pesquisa em set/2026, fase 0 do Apolo):
  gpt-transcribe  -> STT. Aceita `prompt`, `keywords` e `languages` em lista;
                     ["pt", "en"] foi validado na fase 0.
  whisper-1 & cia -> legados. So aceitam `language` (singular) e `prompt`.
  gpt-5.6-luna    -> modelo de texto barato, usado no resumo.

Como os parametros novos (`keywords`, `languages`) nao existem em SDKs
antigos nem nos modelos legados, o STT degrada sozinho: tenta o conjunto
completo e vai removendo o que o servidor/SDK recusar (igual ao Artemis).
O resumo faz o mesmo com a saida estruturada: se o modelo nao aceitar JSON
schema, pede Markdown puro com o titulo na 1a linha.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import openai

from ..errors import (
    ApoloError,
    CredentialsError,
    OfflineError,
    ProviderTimeoutError,
    RateLimitedError,
    ServerError,
    SummaryError,
    TranscriptionError,
)
from ..i18n import t
from .base import SummaryRequest, SummaryResult, normalize_tags

log = logging.getLogger(__name__)

# Modelos que ainda usam `language` (singular) em vez de `languages`.
_LEGACY_STT_MODELS = {"whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"}

# Saida estruturada do resumo. strict=True: o modelo nao pode inventar campos.
_SUMMARY_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "meeting_note",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "body_markdown": {"type": "string"},
            },
            "required": ["title", "tags", "body_markdown"],
            "additionalProperties": False,
        },
    },
}


class OpenAIProvider:
    def __init__(
        self,
        api_key: str,
        stt_model: str = "gpt-transcribe",
        text_model: str = "gpt-5.6-luna",
        timeout: float = 60.0,
        max_retries: int = 0,
        client: Any = None,
        summary_timeout: float = 180.0,
    ):
        self.stt_model = stt_model
        self.text_model = text_model
        # O resumo de uma reuniao de 1-2 h pode levar bem mais que um trecho
        # de 12 s para transcrever: prazo proprio (no 1o teste real, 60 s nao
        # bastaram).
        self.summary_timeout = summary_timeout
        # max_retries=0: quem repete e a fila de workers, com backoff e
        # sabendo se esta offline. Retry escondido no SDK so atrasaria isso.
        # `client` existe para os testes injetarem um fake.
        self._client = client or openai.OpenAI(
            api_key=api_key, timeout=timeout, max_retries=max_retries
        )
        # Lembra o que este modelo recusou, para nao pagar o custo da
        # degradacao a cada trecho.
        self._unsupported: set[str] = set()
        # Modelos de texto que nao aceitaram saida estruturada.
        self._no_schema: set[str] = set()

    # ---------------------------------------------------------------- STT

    def transcribe(
        self,
        wav: bytes,
        *,
        prompt: str = "",
        keywords: list[str] | None = None,
        languages: list[str] | None = None,
    ) -> str:
        base = {
            "model": self.stt_model,
            "file": ("audio.wav", wav, "audio/wav"),
            "response_format": "text",
        }
        extras: dict[str, object] = {}
        if prompt:
            extras["prompt"] = prompt
        if keywords:
            extras["keywords"] = list(keywords)
        if languages:
            if self.stt_model in _LEGACY_STT_MODELS:
                # So cabe um: o primeiro e o idioma principal da reuniao.
                extras["language"] = languages[0]
            else:
                extras["languages"] = list(languages)

        result = self._call_with_degradation(base, extras)
        text = result if isinstance(result, str) else getattr(result, "text", "")
        # Vazio nao e erro aqui: o VAD as vezes deixa passar um trecho sem
        # fala de verdade, e a fila so precisa pular ele.
        return (text or "").strip()

    def _call_with_degradation(self, base: dict, extras: dict):
        """Chama a API removendo os parametros que ela recusar.

        Mantem o registro do que nao funcionou para os proximos trechos.
        """
        optional = [k for k in extras if k not in self._unsupported]
        while True:
            params = {**base, **{k: extras[k] for k in optional}}
            try:
                return self._client.audio.transcriptions.create(**params)
            except TypeError as exc:
                # O SDK instalado nao conhece o parametro.
                dropped = self._drop_offender(optional, str(exc))
                if dropped is None:
                    raise self._wrap(exc, TranscriptionError, "err.transcription_failed")
                log.warning("SDK nao aceita '%s' no STT; seguindo sem ele.", dropped)
            except openai.BadRequestError as exc:
                # O modelo nao aceita o parametro.
                dropped = self._drop_offender(optional, str(exc))
                if dropped is None:
                    raise self._wrap(exc, TranscriptionError, "err.transcription_failed")
                log.warning(
                    "Modelo %s nao aceita '%s'; seguindo sem ele.",
                    base["model"],
                    dropped,
                )
            except Exception as exc:
                raise self._wrap(exc, TranscriptionError, "err.transcription_failed")

    def _drop_offender(self, optional: list[str], message: str) -> str | None:
        """Remove da lista o parametro citado no erro; senao, o ultimo."""
        for name in optional:
            if name in message:
                optional.remove(name)
                self._unsupported.add(name)
                return name
        if optional:
            name = optional.pop()
            self._unsupported.add(name)
            return name
        return None

    # ------------------------------------------------------------- resumo

    def summarize(self, request: SummaryRequest) -> SummaryResult:
        model = request.model or self.text_model
        rules = summary_system_prompt(request)
        user = summary_user_message(request)

        if model not in self._no_schema:
            content = self._chat(
                model,
                rules + "\n\n" + t("summary.format.json"),
                user,
                response_format=_SUMMARY_SCHEMA,
            )
            if content is not None:
                result = _parse_json_result(content)
                if result is not None:
                    return result
                log.warning("Resumo em JSON veio invalido; pedindo em Markdown.")

        content = self._chat(model, rules + "\n\n" + t("summary.format.markdown"), user)
        return parse_markdown_result(content or "")

    def _chat(
        self,
        model: str,
        system: str,
        user: str,
        response_format: dict | None = None,
    ) -> str | None:
        """Uma chamada de chat. None = o modelo recusou o `response_format`."""
        params: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "timeout": self.summary_timeout,
        }
        if response_format is not None:
            params["response_format"] = response_format
        try:
            response = self._client.chat.completions.create(**params)
        except openai.BadRequestError as exc:
            if response_format is not None and _mentions_format(str(exc)):
                log.warning(
                    "Modelo %s nao aceita saida estruturada; usando Markdown.", model
                )
                self._no_schema.add(model)
                return None
            raise self._wrap(exc, SummaryError, "err.summary_failed")
        except Exception as exc:
            raise self._wrap(exc, SummaryError, "err.summary_failed")

        message = response.choices[0].message
        refusal = getattr(message, "refusal", None)
        if refusal:
            raise SummaryError(t("err.summary_refused"), refusal)
        content = (message.content or "").strip()
        if not content:
            raise SummaryError(t("err.summary_empty"))
        return content

    # ------------------------------------------------------------ erros

    @staticmethod
    def _wrap(
        exc: Exception, fallback: type[ApoloError], fallback_key: str
    ) -> ApoloError:
        """Traduz erros da OpenAI em mensagens que cabem num toast.

        Rede, timeout, limite de uso e 5xx viram `TransientError` (a fila
        tenta de novo); o resto vira `fallback`.
        """
        if isinstance(exc, openai.AuthenticationError):
            return CredentialsError(
                t("err.invalid_api_key"), t("err.invalid_api_key.detail")
            )
        if isinstance(exc, openai.PermissionDeniedError):
            return CredentialsError(t("err.no_model_access"), str(exc))
        if isinstance(exc, openai.RateLimitError):
            # 429 tambem e "sem credito". Esse nao passa esperando.
            if getattr(exc, "code", None) == "insufficient_quota":
                return CredentialsError(t("err.quota"), t("err.quota.detail"))
            return RateLimitedError(
                t("err.rate_limit"), retry_after=_retry_after(exc)
            )
        # APITimeoutError e subclasse de APIConnectionError: vem antes.
        if isinstance(exc, openai.APITimeoutError):
            return ProviderTimeoutError(t("err.timeout"))
        if isinstance(exc, openai.APIConnectionError):
            return OfflineError(t("err.offline"), t("err.offline.detail"))
        if isinstance(exc, openai.APIStatusError):
            message = t("err.api_status", status=exc.status_code)
            if exc.status_code >= 500:
                return ServerError(message, str(exc), retry_after=_retry_after(exc))
            return fallback(message, str(exc))
        return fallback(t(fallback_key), str(exc))


# ------------------------------------------------------------- prompts


def summary_system_prompt(request: SummaryRequest) -> str:
    """Regras comuns a todos os presets + o foco/secoes do preset."""
    rules = t(
        "summary.rules",
        me=t("speaker.me"),
        others=t("speaker.others"),
        my_name=request.my_name.strip() or t("summary.me_unknown"),
        gap=t("note.gap"),
    )
    preset = request.system_prompt.strip()
    return f"{rules}\n\n{preset}" if preset else rules


def summary_user_message(request: SummaryRequest) -> str:
    """Contexto da reuniao + anotacoes + transcricao, num texto so."""
    lines = []
    if request.date is not None:
        lines.append(
            t("summary.ctx.date", date=request.date.strftime(t("note.header.date_fmt")))
        )
    if request.duration_seconds:
        minutes = max(1, round(request.duration_seconds / 60))
        lines.append(t("summary.ctx.duration", minutes=minutes))
    if request.participants:
        lines.append(
            t("summary.ctx.participants", names=", ".join(request.participants))
        )
    if request.my_name.strip():
        lines.append(t("summary.ctx.me", me=t("speaker.me"), name=request.my_name.strip()))
    if request.has_voices:
        lines.append(t("summary.ctx.voices", others=t("speaker.others"),
                       voice=t("speaker.voice", letter="A")))

    parts = ["\n".join(lines)] if lines else []
    notes = request.user_notes.strip()
    parts.append(f"## {t('summary.ctx.notes')}\n{notes or t('summary.ctx.no_notes')}")
    parts.append(f"## {t('summary.ctx.transcript')}\n{request.transcript.strip()}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- parse


def _parse_json_result(content: str) -> SummaryResult | None:
    """SummaryResult a partir do JSON do modelo, ou None se nao der."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    body = str(data.get("body_markdown") or "").strip()
    if not body:
        return None
    tags = data.get("tags")
    return SummaryResult(
        title=_clean_title(str(data.get("title") or "")),
        tags=normalize_tags(tags if isinstance(tags, list) else []),
        body_markdown=body,
    )


def parse_markdown_result(content: str) -> SummaryResult:
    """Fallback: 1a linha nao vazia = titulo, o resto = corpo."""
    lines = content.strip().splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return SummaryResult(title=t("note.untitled"))
    first = lines[0].strip()
    if first.startswith("##"):
        # O modelo pulou o titulo e comecou direto nas secoes.
        return SummaryResult(title=t("note.untitled"), body_markdown="\n".join(lines))
    return SummaryResult(
        title=_clean_title(first),
        body_markdown="\n".join(lines[1:]).strip(),
    )


def _clean_title(title: str) -> str:
    title = title.strip().lstrip("#").strip().strip("*_").strip()
    return title or t("note.untitled")


def _mentions_format(message: str) -> bool:
    message = message.lower()
    return "response_format" in message or "json_schema" in message


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None
