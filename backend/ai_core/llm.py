"""Shared LLM call plumbing for prompt-backed services.

Fetches the active Prompt, calls the model, and records a PromptCallMetadata
row. Callers supply the prompt key and the Template substitution values, so each
service keeps its own prompt logic while the call and logging live here, next to
the Prompt and PromptCallMetadata models. AI calls require OPENROUTER_API_KEY,
or OPENROUTER_API_KEYS (comma-separated) to fall back across multiple keys when
one is rate-limited or rejected. Clients are initialized on the first AI call,
so missing credentials do not prevent unrelated routes from loading.
"""

import json
import logging
import os
import re
import time
from string import Template

import httpx2
from openai import (
    APIStatusError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    OpenAI,
    OpenAIError,
    PermissionDeniedError,
    RateLimitError,
)

from .models import Prompt, PromptCallMetadata

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT = httpx2.Timeout(60.0, connect=5.0)
DEFAULT_MAX_RETRIES = 2
RATE_LIMIT_MAX_WAIT_SECONDS = 10.0
RATE_LIMIT_DEFAULT_WAIT_SECONDS = 2.0
RETRY_AFTER_MESSAGE_PATTERN = re.compile(r"try again in ([\d.]+)s")
JSON_VALIDATION_ERROR_PATTERN = re.compile(r"failed to validate json", re.IGNORECASE)


class LLMConfigurationError(OpenAIError):
    """Local AI configuration is missing and requires an operator to fix it."""


def load_api_keys() -> list[str | None]:
    """Return the configured OpenRouter API keys, in fallback order.

    OPENROUTER_API_KEYS (comma-separated) takes precedence over the
    single OPENROUTER_API_KEY.
    """
    raw = os.environ.get("OPENROUTER_API_KEYS", "")
    keys = [key.strip() for key in raw.split(",") if key.strip()]
    return keys or [os.environ.get("OPENROUTER_API_KEY")]


def create_client(api_key: str | None, *, http_client: httpx2.Client | None = None) -> OpenAI:
    """Build an OpenRouter client with the previous timeout and retry defaults."""
    if not api_key:
        # Do not let the SDK silently fall back to an unrelated OPENAI_API_KEY.
        raise LLMConfigurationError("Set OPENROUTER_API_KEY or OPENROUTER_API_KEYS.")
    return OpenAI(
        api_key=api_key,
        base_url=OPENROUTER_BASE_URL,
        timeout=DEFAULT_TIMEOUT,
        max_retries=DEFAULT_MAX_RETRIES,
        http_client=http_client,
    )


API_KEYS = load_api_keys()
clients: list[OpenAI] = []
MODEL = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct")


def is_configuration_error(exc: APIStatusError | LLMConfigurationError) -> bool:
    """Distinguish permanent configuration/credit errors from transient failures."""
    if isinstance(
        exc,
        (
            LLMConfigurationError,
            AuthenticationError,
            PermissionDeniedError,
            BadRequestError,
            NotFoundError,
        ),
    ):
        return True
    if exc.status_code != 402:
        return False
    body = exc.body if isinstance(exc.body, dict) else {}
    metadata = body.get("metadata") or {}
    # A temporary reservation of credits is retryable; adding credits is not needed.
    return not (
        isinstance(metadata, dict)
        and metadata.get("limit_source") == "openrouter_in_flight_budget"
    ) and "retry-after" not in exc.response.headers


def _create_completion(client: OpenAI, request_kwargs: dict):
    """Normalize errors embedded in HTTP 200 completions into SDK exceptions."""
    response = client.chat.completions.create(**request_kwargs)
    error = getattr(response, "error", None)
    if error is None and response.choices:
        choice = response.choices[0]
        error = getattr(choice, "error", None)
        if error is None and choice.finish_reason == "error":
            error = {"code": 502, "message": "OpenRouter returned an incomplete completion."}
    if error is None:
        return response

    error = error if isinstance(error, dict) else {"message": str(error)}
    try:
        status_code = int(error.get("code", 502))
    except (TypeError, ValueError):
        status_code = 502
    if not 400 <= status_code <= 599:
        status_code = 502
    error_cls = {
        400: BadRequestError,
        401: AuthenticationError,
        403: PermissionDeniedError,
        404: NotFoundError,
        429: RateLimitError,
    }.get(status_code, InternalServerError if status_code >= 500 else APIStatusError)
    error_response = httpx2.Response(
        status_code,
        request=httpx2.Request("POST", f"{OPENROUTER_BASE_URL}/chat/completions"),
        json={"error": error},
    )
    raise error_cls(
        error.get("message") or "OpenRouter returned a completion error.",
        response=error_response,
        body=error,
    )


def rate_limit_wait_seconds(exc: RateLimitError) -> float:
    """Return how long to wait before retrying a rate-limited call.

    Reads the standard ``retry-after`` response header first, falling back to
    parsing an upstream "please try again in Xs" message text, and finally a fixed
    default. Capped at RATE_LIMIT_MAX_WAIT_SECONDS so a single call can't hold
    up the caller's request indefinitely.
    """
    header_value = exc.response.headers.get("retry-after") if exc.response is not None else None
    if header_value is not None:
        try:
            return min(float(header_value), RATE_LIMIT_MAX_WAIT_SECONDS)
        except ValueError:
            pass
    match = RETRY_AFTER_MESSAGE_PATTERN.search(str(exc))
    if match:
        return min(float(match.group(1)), RATE_LIMIT_MAX_WAIT_SECONDS)
    return RATE_LIMIT_DEFAULT_WAIT_SECONDS


def create_completion(request_kwargs: dict):
    """Call chat.completions.create, waiting out a rate limit once per key
    and retrying once on a JSON-validation 400 (the model failed its own
    json_object constraint for that generation -- model variance, not a
    real config problem) before falling back to the next key on a repeated
    failure or an auth/permission error from the current one.
    """
    if not clients:
        # Build the whole pool before caching it; never retain a partially built pool.
        clients.extend([create_client(key) for key in API_KEYS])
    last_exc: Exception | None = None
    for index, client in enumerate(clients):
        try:
            return _create_completion(client, request_kwargs)
        except RateLimitError as exc:
            wait_seconds = rate_limit_wait_seconds(exc)
            logger.warning(
                "llm: key %d/%d rate limited, waiting %.2fs before retrying",
                index + 1,
                len(clients),
                wait_seconds,
            )
            time.sleep(wait_seconds)
            try:
                return _create_completion(client, request_kwargs)
            except (RateLimitError, AuthenticationError, PermissionDeniedError) as retry_exc:
                last_exc = retry_exc
                if index < len(clients) - 1:
                    logger.warning(
                        "llm: key %d/%d failed again (%s), falling back to next key",
                        index + 1,
                        len(clients),
                        type(retry_exc).__name__,
                    )
        except BadRequestError as exc:
            if not JSON_VALIDATION_ERROR_PATTERN.search(str(exc)):
                raise
            logger.warning(
                "llm: key %d/%d got a JSON-validation error from the model, retrying once",
                index + 1,
                len(clients),
            )
            try:
                return _create_completion(client, request_kwargs)
            except (
                RateLimitError,
                AuthenticationError,
                PermissionDeniedError,
                BadRequestError,
            ) as retry_exc:
                last_exc = retry_exc
                if index < len(clients) - 1:
                    logger.warning(
                        "llm: key %d/%d failed again (%s), falling back to next key",
                        index + 1,
                        len(clients),
                        type(retry_exc).__name__,
                    )
        except (AuthenticationError, PermissionDeniedError) as exc:
            last_exc = exc
            if index < len(clients) - 1:
                logger.warning(
                    "llm: key %d/%d failed (%s), falling back to next key",
                    index + 1,
                    len(clients),
                    type(exc).__name__,
                )
    raise last_exc


def run_prompt(
    prompt_key: str, variables: dict, *, timeout: int | None = None, temperature: float = 0.2
) -> dict:
    """Run the active prompt for prompt_key with variables and return parsed JSON.

    A top-level result that isn't a JSON object (e.g. the model wraps it in
    an array) is treated the same as invalid JSON -- every prompt's contract
    expects an object, so callers can trust a non-error return is always a
    dict without re-checking its shape themselves.

    Logs a PromptCallMetadata row per call, with a status distinguishing
    missing prompt, invalid JSON, rate limit, config error (auth/permission/
    bad request/not found), and other API errors; re-raises in every case so
    the caller can degrade. When timeout is None the preserved 60-second
    client timeout (5 seconds for connection establishment) is used.
    """
    prompt_row = None
    formatted_prompt = ""
    output_text = None
    error_message = None
    usage = None
    status = PromptCallMetadata.Status.API_ERROR
    started_at = time.monotonic()
    try:
        prompt_row = Prompt.objects.get(prompt_description=prompt_key, is_active=True)
        formatted_prompt = Template(prompt_row.prompt_detail).safe_substitute(**variables)
        request_kwargs = {
            "model": MODEL,
            "messages": [{"role": "user", "content": formatted_prompt}],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "extra_body": {
                "provider": {
                    "require_parameters": True,
                    "order": ["DeepInfra"],
                    "allow_fallbacks": False,
                }
            },
        }
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        response = create_completion(request_kwargs)
        usage = response.usage
        output_text = response.choices[0].message.content
        output_data = json.loads(output_text)
        if not isinstance(output_data, dict):
            raise json.JSONDecodeError("Expected a JSON object", output_text, 0)
        status = PromptCallMetadata.Status.SUCCESS
        return output_data
    except Prompt.DoesNotExist as exc:
        error_message = str(exc)
        status = PromptCallMetadata.Status.PROMPT_MISSING
        raise
    except json.JSONDecodeError as exc:
        error_message = str(exc)
        status = PromptCallMetadata.Status.INVALID_JSON
        raise
    except RateLimitError as exc:
        error_message = str(exc)
        status = PromptCallMetadata.Status.RATE_LIMITED
        raise
    except (APIStatusError, LLMConfigurationError) as exc:
        error_message = str(exc)
        status = (
            PromptCallMetadata.Status.CONFIG_ERROR
            if is_configuration_error(exc)
            else PromptCallMetadata.Status.API_ERROR
        )
        raise
    except Exception as exc:
        error_message = str(exc)
        status = PromptCallMetadata.Status.API_ERROR
        raise
    finally:
        PromptCallMetadata.objects.create(
            prompt=prompt_row,
            prompt_description=prompt_row.prompt_description if prompt_row else prompt_key,
            version=prompt_row.version if prompt_row else None,
            model_name=MODEL,
            status=status,
            input_text=formatted_prompt,
            output_text=output_text,
            error_message=error_message,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )


def run_prompt_safe(
    prompt_key: str, variables: dict, *, missing_prompt_message: str, timeout: int | None = None
) -> dict:
    """Run run_prompt, degrading any failure to an {"error", "retryable"} dict.

    ``missing_prompt_message`` is returned when the prompt is not configured.
    Auth/permission/bad-request/not-found and insufficient-credit errors are
    non-retryable config issues; transient budget, rate limit and other call
    errors are retryable. This is the service-boundary entry point:
    callers never see the LLM exception, so a
    failed call cannot break the surrounding request.
    """
    try:
        return run_prompt(prompt_key, variables, timeout=timeout)
    except Prompt.DoesNotExist:
        return {"error": missing_prompt_message, "retryable": False}
    except json.JSONDecodeError:
        return {"error": "O LLM retornou um JSON inválido", "retryable": True}
    except RateLimitError:
        return {
            "error": "Você atingiu o limite de uso. Tente novamente mais tarde.",
            "retryable": True,
        }
    except (APIStatusError, LLMConfigurationError) as exc:
        config_error = is_configuration_error(exc)
        return {
            "error": (
                "Falha de configuração ao chamar o LLM."
                if config_error
                else "Falha ao chamar o LLM."
            ),
            "retryable": not config_error,
        }
    except Exception:  # pylint: disable=broad-exception-caught
        return {"error": "Falha ao chamar o LLM.", "retryable": True}
