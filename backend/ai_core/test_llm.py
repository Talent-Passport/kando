"""Unit tests for ai_core.llm: error classification, logging, and key fallback."""

# The active_prompt fixture below is intentionally reused as a test argument
# name, matching pytest's fixture-injection convention already used in
# conftest.py.
# pylint: disable=redefined-outer-name

import json
from unittest.mock import MagicMock

import httpx2
import pytest
from openai import (
    OpenAIError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

from openai.types.chat import ChatCompletion

from . import llm
from .models import Prompt, PromptCallMetadata


def make_openrouter_error(error_cls, status_code, message="openrouter error", headers=None):
    """Build a real OpenAI SDK exception instance with a minimal httpx2 response."""
    request = httpx2.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx2.Response(status_code=status_code, request=request, headers=headers or {})
    return error_cls(message, response=response, body=None)


def make_fake_client(side_effect):
    """Return a MagicMock standing in for a OpenRouter client, with the given create() behavior."""
    client = MagicMock()
    client.chat.completions.create.side_effect = side_effect
    return client


def make_success_response(content='{"ok": true}'):
    """Return a real SDK completion, including token usage."""
    return ChatCompletion(
        id="test-completion",
        object="chat.completion",
        created=0,
        model=llm.MODEL,
        choices=[
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )


@pytest.fixture
def transport_client(monkeypatch):
    """Install the production SDK client with a mock HTTP transport, never the network."""
    requests = []
    replies = []

    def handle_request(request):
        requests.append(request)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    with httpx2.Client(transport=httpx2.MockTransport(handle_request)) as http_client:
        with llm.create_client("test-openrouter-key", http_client=http_client) as client:
            monkeypatch.setattr(llm, "clients", [client])
            monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)
            yield requests, replies


@pytest.fixture
def active_prompt(db):  # pylint: disable=unused-argument
    """Return an active Prompt usable as the run_prompt() target."""
    return Prompt.objects.create(prompt_description="test_prompt", prompt_detail="hello $name")


class TestRunPromptErrorClassification:
    """run_prompt must raise the right exception and log the matching status."""

    @pytest.mark.django_db
    def test_missing_prompt_raises_and_logs(self):
        """An unknown prompt_key raises Prompt.DoesNotExist and logs PROMPT_MISSING."""
        with pytest.raises(Prompt.DoesNotExist):
            llm.run_prompt("does_not_exist", {})

        call = PromptCallMetadata.objects.get()
        assert call.status == PromptCallMetadata.Status.PROMPT_MISSING
        assert call.prompt_description == "does_not_exist"

    @pytest.mark.django_db
    def test_invalid_json_raises_and_logs(self, active_prompt, monkeypatch):
        """A non-JSON completion raises JSONDecodeError and logs INVALID_JSON."""
        monkeypatch.setattr(
            llm, "clients", [make_fake_client([make_success_response("not json")])]
        )

        with pytest.raises(json.JSONDecodeError):
            llm.run_prompt(active_prompt.prompt_description, {"name": "x"})

        call = PromptCallMetadata.objects.get()
        assert call.status == PromptCallMetadata.Status.INVALID_JSON
        assert call.output_text == "not json"

    @pytest.mark.django_db
    def test_non_dict_json_raises_and_logs_invalid_json(self, active_prompt, monkeypatch):
        """A top-level JSON array (valid JSON, wrong shape) is treated as invalid JSON,
        since every prompt's contract expects a top-level object."""
        monkeypatch.setattr(
            llm, "clients", [make_fake_client([make_success_response('[{"ok": true}]')])]
        )

        with pytest.raises(json.JSONDecodeError):
            llm.run_prompt(active_prompt.prompt_description, {"name": "x"})

        call = PromptCallMetadata.objects.get()
        assert call.status == PromptCallMetadata.Status.INVALID_JSON

    @pytest.mark.django_db
    def test_rate_limit_raises_and_logs(self, active_prompt, monkeypatch):
        """A rate limit persisting after the retry raises and logs RATE_LIMITED."""
        exc = make_openrouter_error(RateLimitError, 429)
        monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(llm, "clients", [make_fake_client([exc, exc])])

        with pytest.raises(RateLimitError):
            llm.run_prompt(active_prompt.prompt_description, {"name": "x"})

        call = PromptCallMetadata.objects.get()
        assert call.status == PromptCallMetadata.Status.RATE_LIMITED
        assert call.error_message == str(exc)

    @pytest.mark.django_db
    @pytest.mark.parametrize(
        "error_cls,status_code",
        [
            (AuthenticationError, 401),
            (PermissionDeniedError, 403),
            (BadRequestError, 400),
            (NotFoundError, 404),
        ],
    )
    def test_config_errors_raise_and_log(self, active_prompt, monkeypatch, error_cls, status_code):
        """Auth/permission/bad-request/not-found errors raise and log CONFIG_ERROR."""
        exc = make_openrouter_error(error_cls, status_code)
        monkeypatch.setattr(llm, "clients", [make_fake_client([exc])])

        with pytest.raises(error_cls):
            llm.run_prompt(active_prompt.prompt_description, {"name": "x"})

        call = PromptCallMetadata.objects.get()
        assert call.status == PromptCallMetadata.Status.CONFIG_ERROR

    @pytest.mark.django_db
    def test_generic_error_raises_and_logs(self, active_prompt, monkeypatch):
        """Any other exception raises as-is and logs API_ERROR."""
        monkeypatch.setattr(
            llm, "clients", [make_fake_client([ConnectionError("boom")])]
        )

        with pytest.raises(ConnectionError):
            llm.run_prompt(active_prompt.prompt_description, {"name": "x"})

        call = PromptCallMetadata.objects.get()
        assert call.status == PromptCallMetadata.Status.API_ERROR
        assert call.error_message == "boom"

    @pytest.mark.django_db
    def test_success_logs_and_returns_parsed_json(self, active_prompt, monkeypatch):
        """A successful call returns the parsed JSON and logs SUCCESS with token usage."""
        monkeypatch.setattr(
            llm, "clients", [make_fake_client([make_success_response('{"ok": true}')])]
        )

        result = llm.run_prompt(active_prompt.prompt_description, {"name": "x"})

        assert result == {"ok": True}
        call = PromptCallMetadata.objects.get()
        assert call.status == PromptCallMetadata.Status.SUCCESS
        assert call.prompt_tokens == 10
        assert call.completion_tokens == 5
        assert call.total_tokens == 15
        assert call.model_name == llm.MODEL
        assert call.prompt == active_prompt
        assert call.version == active_prompt.version
        assert call.input_text == "hello x"
        assert call.output_text == '{"ok": true}'
        assert call.error_message is None
        assert call.duration_ms >= 0


class TestRunPromptSafe:
    """run_prompt_safe must degrade every failure to an {error, retryable} dict."""

    @pytest.mark.django_db
    def test_missing_prompt_is_not_retryable(self):
        """A missing prompt returns the caller-supplied message, not retryable."""
        result = llm.run_prompt_safe(
            "does_not_exist", {}, missing_prompt_message="Prompt não configurado."
        )

        assert result == {"error": "Prompt não configurado.", "retryable": False}

    @pytest.mark.django_db
    def test_invalid_json_is_retryable(self, active_prompt, monkeypatch):
        """Invalid JSON from the LLM is reported as retryable."""
        monkeypatch.setattr(
            llm, "clients", [make_fake_client([make_success_response("not json")])]
        )

        result = llm.run_prompt_safe(
            active_prompt.prompt_description, {"name": "x"}, missing_prompt_message="x"
        )

        assert result["retryable"] is True
        assert "JSON" in result["error"]

    @pytest.mark.django_db
    def test_rate_limit_is_retryable(self, active_prompt, monkeypatch):
        """A persistent rate limit is retryable with the existing PT-BR message."""
        exc = make_openrouter_error(RateLimitError, 429)
        monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(llm, "clients", [make_fake_client([exc, exc])])

        result = llm.run_prompt_safe(
            active_prompt.prompt_description, {"name": "x"}, missing_prompt_message="x"
        )

        assert result == {
            "error": "Você atingiu o limite de uso. Tente novamente mais tarde.",
            "retryable": True,
        }

    @pytest.mark.django_db
    @pytest.mark.parametrize(
        "error_cls,status_code", [(AuthenticationError, 401), (BadRequestError, 400)]
    )
    def test_config_error_is_not_retryable(
        self, active_prompt, monkeypatch, error_cls, status_code
    ):
        """Config errors (auth/bad request/etc.) are reported as not retryable."""
        exc = make_openrouter_error(error_cls, status_code)
        monkeypatch.setattr(llm, "clients", [make_fake_client([exc])])

        result = llm.run_prompt_safe(
            active_prompt.prompt_description, {"name": "x"}, missing_prompt_message="x"
        )

        assert result["retryable"] is False
        assert "Falha de configuração" in result["error"]

    @pytest.mark.django_db
    def test_generic_error_is_retryable(self, active_prompt, monkeypatch):
        """Any other exception falls back to a generic retryable error."""
        monkeypatch.setattr(
            llm, "clients", [make_fake_client([ConnectionError("boom")])]
        )

        result = llm.run_prompt_safe(
            active_prompt.prompt_description, {"name": "x"}, missing_prompt_message="x"
        )

        assert result == {"error": "Falha ao chamar o LLM.", "retryable": True}


class TestMultiKeyFallback:
    """create_completion must fail over across keys only for key-specific errors."""

    @pytest.mark.django_db
    def test_retries_same_key_once_after_rate_limit_then_succeeds(self, active_prompt, monkeypatch):
        """A rate limit is retried on the same key and, if that retry
        succeeds, the call completes normally without moving to another key."""
        exc = make_openrouter_error(RateLimitError, 429)
        client = make_fake_client([exc, make_success_response('{"ok": true}')])
        monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(llm, "clients", [client])

        result = llm.run_prompt(active_prompt.prompt_description, {"name": "x"})

        assert result == {"ok": True}
        assert client.chat.completions.create.call_count == 2
        call = PromptCallMetadata.objects.get()
        assert call.status == PromptCallMetadata.Status.SUCCESS

    @pytest.mark.django_db
    def test_falls_back_to_next_key_on_rate_limit(self, active_prompt, monkeypatch):
        """A key rate-limited on its attempt and retry falls back to a working second key."""
        exc = make_openrouter_error(RateLimitError, 429)
        failing_client = make_fake_client([exc, exc])
        working_client = make_fake_client([make_success_response('{"ok": true}')])
        monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(llm, "clients", [failing_client, working_client])

        result = llm.run_prompt(active_prompt.prompt_description, {"name": "x"})

        assert result == {"ok": True}
        assert failing_client.chat.completions.create.call_count == 2
        working_client.chat.completions.create.assert_called_once()
        call = PromptCallMetadata.objects.get()
        assert call.status == PromptCallMetadata.Status.SUCCESS

    @pytest.mark.django_db
    @pytest.mark.parametrize(
        "error_cls,status_code", [(AuthenticationError, 401), (PermissionDeniedError, 403)]
    )
    def test_falls_back_to_next_key_on_auth_error(
        self, active_prompt, monkeypatch, error_cls, status_code
    ):
        """A rejected first key falls back to a working second key."""
        failing_client = make_fake_client([make_openrouter_error(error_cls, status_code)])
        working_client = make_fake_client([make_success_response('{"ok": true}')])
        monkeypatch.setattr(llm, "clients", [failing_client, working_client])

        result = llm.run_prompt(active_prompt.prompt_description, {"name": "x"})

        assert result == {"ok": True}

    @pytest.mark.django_db
    def test_raises_last_error_when_all_keys_exhausted(self, active_prompt, monkeypatch):
        """When every key fails, the last key's error is raised and logged."""
        first_exc = make_openrouter_error(RateLimitError, 429, message="first key limited")
        second_exc = make_openrouter_error(RateLimitError, 429, message="second key limited")
        client_a = make_fake_client([first_exc, first_exc])
        client_b = make_fake_client([second_exc, second_exc])
        monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(llm, "clients", [client_a, client_b])

        with pytest.raises(RateLimitError) as excinfo:
            llm.run_prompt(active_prompt.prompt_description, {"name": "x"})

        assert str(excinfo.value) == "second key limited"
        call = PromptCallMetadata.objects.get()
        assert call.status == PromptCallMetadata.Status.RATE_LIMITED
        assert call.error_message == "second key limited"

    @pytest.mark.django_db
    def test_does_not_fall_back_on_bad_request(self, active_prompt, monkeypatch):
        """A malformed request fails the same way on every key, so no point retrying."""
        failing_client = make_fake_client([make_openrouter_error(BadRequestError, 400)])
        untouched_client = make_fake_client([make_success_response('{"ok": true}')])
        monkeypatch.setattr(llm, "clients", [failing_client, untouched_client])

        with pytest.raises(BadRequestError):
            llm.run_prompt(active_prompt.prompt_description, {"name": "x"})

        untouched_client.chat.completions.create.assert_not_called()

    @pytest.mark.django_db
    def test_retries_same_key_once_after_json_validation_error_then_succeeds(
        self, active_prompt, monkeypatch
    ):
        """A 400 where the model failed its own json_object constraint is
        retried on the same key -- it's model variance, not a real config
        problem -- and if that retry succeeds, the call completes normally."""
        exc = make_openrouter_error(
            BadRequestError, 400, message="Failed to validate JSON. Please adjust your prompt."
        )
        client = make_fake_client([exc, make_success_response('{"ok": true}')])
        monkeypatch.setattr(llm, "clients", [client])

        result = llm.run_prompt(active_prompt.prompt_description, {"name": "x"})

        assert result == {"ok": True}
        assert client.chat.completions.create.call_count == 2
        call = PromptCallMetadata.objects.get()
        assert call.status == PromptCallMetadata.Status.SUCCESS

    @pytest.mark.django_db
    def test_falls_back_to_next_key_when_json_validation_error_persists(
        self, active_prompt, monkeypatch
    ):
        """A first key failing json validation on both its attempt and its
        retry falls back to a working second key."""
        exc = make_openrouter_error(
            BadRequestError, 400, message="Failed to validate JSON. Please adjust your prompt."
        )
        failing_client = make_fake_client([exc, exc])
        working_client = make_fake_client([make_success_response('{"ok": true}')])
        monkeypatch.setattr(llm, "clients", [failing_client, working_client])

        result = llm.run_prompt(active_prompt.prompt_description, {"name": "x"})

        assert result == {"ok": True}
        assert failing_client.chat.completions.create.call_count == 2
        working_client.chat.completions.create.assert_called_once()


class TestRateLimitWaitSeconds:
    """rate_limit_wait_seconds must read the retry-after header, fall back to
    the message text, then a fixed default -- always capped at the maximum.
    """

    def test_reads_retry_after_header(self):
        """The retry-after response header takes precedence when present."""
        exc = make_openrouter_error(RateLimitError, 429, headers={"retry-after": "3.5"})

        assert llm.rate_limit_wait_seconds(exc) == 3.5

    def test_parses_wait_from_message_when_header_missing(self):
        """Without a header, the wait is parsed from the upstream message text."""
        exc = make_openrouter_error(
            RateLimitError, 429, message="rate limited, please try again in 2.085s"
        )

        assert llm.rate_limit_wait_seconds(exc) == 2.085

    def test_falls_back_to_default_when_nothing_available(self):
        """With neither a header nor a parseable message, use the fixed default."""
        exc = make_openrouter_error(RateLimitError, 429, message="rate limited")

        assert llm.rate_limit_wait_seconds(exc) == llm.RATE_LIMIT_DEFAULT_WAIT_SECONDS

    def test_caps_wait_at_maximum(self):
        """A very long indicated wait is capped so a single call can't hang the request."""
        exc = make_openrouter_error(RateLimitError, 429, headers={"retry-after": "999"})

        assert llm.rate_limit_wait_seconds(exc) == llm.RATE_LIMIT_MAX_WAIT_SECONDS


class TestLoadApiKeys:
    """load_api_keys must parse OPENROUTER_API_KEYS with a OPENROUTER_API_KEY fallback."""

    def test_parses_comma_separated_keys(self, monkeypatch):
        """OPENROUTER_API_KEYS is split into an ordered list of keys."""
        monkeypatch.setenv("OPENROUTER_API_KEYS", "key1,key2,key3")
        monkeypatch.setenv("OPENROUTER_API_KEY", "ignored-single-key")

        assert llm.load_api_keys() == ["key1", "key2", "key3"]

    def test_strips_whitespace_and_drops_empty_entries(self, monkeypatch):
        """Whitespace around keys and empty comma-separated entries are dropped."""
        monkeypatch.setenv("OPENROUTER_API_KEYS", " key1 ,, key2,")

        assert llm.load_api_keys() == ["key1", "key2"]

    def test_falls_back_to_single_key_when_openrouter_api_keys_unset(self, monkeypatch):
        """Without OPENROUTER_API_KEYS, the single OPENROUTER_API_KEY is used."""
        monkeypatch.delenv("OPENROUTER_API_KEYS", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "single-key")

        assert llm.load_api_keys() == ["single-key"]

    def test_returns_none_entry_when_nothing_configured(self, monkeypatch):
        """With neither env var set, a single None entry preserves the old behavior."""
        monkeypatch.delenv("OPENROUTER_API_KEYS", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        assert llm.load_api_keys() == [None]

    def test_empty_list_falls_back_to_single_key(self, monkeypatch):
        """An empty/whitespace-only list does not mask the single-key setting."""
        monkeypatch.setenv("OPENROUTER_API_KEYS", " , , ")
        monkeypatch.setenv("OPENROUTER_API_KEY", "single-key")

        assert llm.load_api_keys() == ["single-key"]

    def test_ignores_other_provider_keys(self, monkeypatch):
        """Groq and OpenAI credentials cannot configure OpenRouter accidentally."""
        monkeypatch.delenv("OPENROUTER_API_KEYS", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("GROQ_API_KEYS", "old-key-list")
        monkeypatch.setenv("GROQ_API_KEY", "old-single-key")
        monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-key")

        assert llm.load_api_keys() == [None]
        with pytest.raises(OpenAIError, match="OPENROUTER_API_KEY"):
            llm.create_client(None)


@pytest.mark.django_db
class TestOpenRouterTransport:
    """Exercise request serialization, SDK retries and actual OpenRouter wire responses."""

    @pytest.mark.parametrize("timeout", [None, 30, 60])
    def test_payload_timeout_and_client_defaults(self, active_prompt, transport_client, timeout):
        """The real HTTP request preserves payload, credentials, timeout and SDK retries."""
        requests, replies = transport_client
        replies.append(httpx2.Response(200, json=make_success_response().model_dump()))

        assert llm.run_prompt(active_prompt.prompt_description, {"name": "x"}, timeout=timeout) == {
            "ok": True
        }

        assert len(requests) == 1
        request = requests[0]
        assert request.method == "POST"
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-openrouter-key"
        assert json.loads(request.content) == {
            "model": llm.MODEL,
            "messages": [{"role": "user", "content": "hello x"}],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "provider": {"require_parameters": True},
        }
        assert request.extensions["timeout"] == {
            "connect": timeout if timeout is not None else 5.0,
            "read": timeout if timeout is not None else 60.0,
            "write": timeout if timeout is not None else 60.0,
            "pool": timeout if timeout is not None else 60.0,
        }
        assert llm.clients[0].max_retries == 2

    def test_custom_temperature(self, active_prompt, transport_client):
        """Explicit run_prompt temperature overrides are still forwarded."""
        requests, replies = transport_client
        replies.append(httpx2.Response(200, json=make_success_response().model_dump()))

        llm.run_prompt(active_prompt.prompt_description, {"name": "x"}, temperature=0.7)

        assert json.loads(requests[0].content)["temperature"] == 0.7

    @pytest.mark.parametrize(
        "case",
        [
            (400, "config_error", False, 1),
            (401, "config_error", False, 1),
            (402, "config_error", False, 1),
            (403, "config_error", False, 1),
            (404, "config_error", False, 1),
            (408, "api_error", True, 3),
            (429, "rate_limited", True, 6),
            (500, "api_error", True, 3),
            (502, "api_error", True, 3),
            (503, "api_error", True, 3),
        ],
    )
    def test_http_error_classification(
        self, active_prompt, transport_client, case
    ):
        """HTTP errors retain service-facing messages and one audit row per logical call."""
        status_code, status, retryable, attempts = case
        requests, replies = transport_client
        replies.extend(
            httpx2.Response(
                status_code,
                json={"error": {"code": status_code, "message": "Provider rejected request"}},
                headers={"retry-after": "0"} if status_code == 429 else {},
            )
            for _ in range(attempts)
        )

        result = llm.run_prompt_safe(
            active_prompt.prompt_description, {"name": "x"}, missing_prompt_message="missing"
        )

        assert result == {
            "error": {
                "config_error": "Falha de configuração ao chamar o LLM.",
                "rate_limited": "Você atingiu o limite de uso. Tente novamente mais tarde.",
                "api_error": "Falha ao chamar o LLM.",
            }[status],
            "retryable": retryable,
        }
        assert len(requests) == attempts
        call = PromptCallMetadata.objects.get()
        assert call.status == status
        assert "Provider rejected request" in call.error_message
        assert call.output_text is None

    @pytest.mark.parametrize(
        "metadata,headers",
        [
            ({"limit_source": "openrouter_in_flight_budget"}, {}),
            ({}, {"retry-after": "2"}),
        ],
    )
    def test_transient_budget_error_is_retryable(
        self, active_prompt, transport_client, metadata, headers
    ):
        """Temporary credit reservations do not become permanent configuration errors."""
        requests, replies = transport_client
        replies.append(
            httpx2.Response(
                402,
                json={"error": {"code": 402, "message": "Budget reserved", "metadata": metadata}},
                headers=headers,
            )
        )

        result = llm.run_prompt_safe(
            active_prompt.prompt_description, {"name": "x"}, missing_prompt_message="missing"
        )

        assert result == {"error": "Falha ao chamar o LLM.", "retryable": True}
        assert len(requests) == 1
        assert PromptCallMetadata.objects.get().status == "api_error"

    @pytest.mark.parametrize("location", ["top_without_choices", "top_with_content", "choice"])
    @pytest.mark.parametrize(
        "case",
        [
            (400, "config_error", False, 1),
            (401, "config_error", False, 1),
            (402, "config_error", False, 1),
            (403, "config_error", False, 1),
            (404, "config_error", False, 1),
            (429, "rate_limited", True, 2),
            (502, "api_error", True, 1),
        ],
    )
    def test_embedded_error_cannot_be_success(
        self, active_prompt, transport_client, location, case
    ):
        """HTTP 200 error envelopes, even alongside valid JSON content, are failed calls."""
        code, status, retryable, attempts = case
        requests, replies = transport_client
        payload = make_success_response().model_dump()
        error = {"code": code, "message": "Embedded provider failure"}
        if location == "choice":
            payload["choices"][0].update(error=error, finish_reason="error")
        else:
            payload["error"] = error
            if location == "top_without_choices":
                payload.pop("choices")
        replies.extend(httpx2.Response(200, json=payload) for _ in range(attempts))

        result = llm.run_prompt_safe(
            active_prompt.prompt_description, {"name": "x"}, missing_prompt_message="missing"
        )

        assert "error" in result
        assert result["retryable"] is retryable
        assert len(requests) == attempts
        call = PromptCallMetadata.objects.get()
        assert call.status == status
        assert "Embedded provider failure" in call.error_message

    def test_error_finish_reason_without_details(self, active_prompt, transport_client):
        """An error finish reason alone prevents partial JSON from being accepted."""
        _, replies = transport_client
        payload = make_success_response().model_dump()
        payload["choices"][0]["finish_reason"] = "error"
        replies.append(httpx2.Response(200, json=payload))

        result = llm.run_prompt_safe(
            active_prompt.prompt_description, {"name": "x"}, missing_prompt_message="missing"
        )

        assert result == {"error": "Falha ao chamar o LLM.", "retryable": True}
        assert PromptCallMetadata.objects.get().status == "api_error"

    @pytest.mark.parametrize("content,status", [("", "invalid_json"), (None, "api_error")])
    def test_empty_content_is_retryable(self, active_prompt, transport_client, content, status):
        """Empty/absent answer text keeps the existing controlled failure behavior."""
        _, replies = transport_client
        replies.append(httpx2.Response(200, json=make_success_response(content).model_dump()))

        result = llm.run_prompt_safe(
            active_prompt.prompt_description, {"name": "x"}, missing_prompt_message="missing"
        )

        assert result["retryable"] is True
        assert PromptCallMetadata.objects.get().status == status

    def test_timeout_retains_sdk_retries(self, active_prompt, transport_client):
        """A transport timeout is retried twice by the SDK and remains retryable."""
        requests, replies = transport_client
        replies.extend(httpx2.ReadTimeout("Provider timed out") for _ in range(3))

        result = llm.run_prompt_safe(
            active_prompt.prompt_description, {"name": "x"}, missing_prompt_message="missing"
        )

        assert result == {"error": "Falha ao chamar o LLM.", "retryable": True}
        assert len(requests) == 3
        assert PromptCallMetadata.objects.get().status == "api_error"

    def test_optional_usage_is_supported(self, active_prompt, transport_client):
        """Providers omitting token counts can still return a successful JSON object."""
        _, replies = transport_client
        payload = make_success_response().model_dump()
        payload.pop("usage")
        replies.append(httpx2.Response(200, json=payload))

        assert llm.run_prompt(active_prompt.prompt_description, {"name": "x"}) == {"ok": True}
        call = PromptCallMetadata.objects.get()
        assert call.status == "success"
        assert (call.prompt_tokens, call.completion_tokens, call.total_tokens) == (None, None, None)
