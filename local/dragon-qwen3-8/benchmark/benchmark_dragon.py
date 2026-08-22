from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import socket
import statistics
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Literal, Mapping, Sequence


@dataclass(frozen=True)
class RouteSpec:
    name: str
    protocol: Literal["openai", "anthropic"]
    url: str
    model: str
    api_key_env: str | None


@dataclass(frozen=True)
class RequestSpec:
    case_id: str
    target_prompt_tokens: int
    max_output_tokens: int
    repetition: int
    candidate_id: str
    phase_id: str = "default"
    attempt_id: str = "1"


@dataclass
class Observation:
    route: str
    case_id: str
    candidate_id: str
    repetition: int
    status: str
    http_status: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    finish_reason: str | None
    elapsed_seconds: float
    ttft_seconds: float | None
    last_content_seconds: float | None
    client_decode_tokens_per_second: float | None
    end_to_end_output_tokens_per_second: float | None
    effective_prompt_tokens_per_second: float | None
    server_decode_tokens_per_second: float | None
    error_category: str | None
    phase_id: str = "default"
    attempt_id: str = "1"


@dataclass
class StreamResult:
    first_content_ns: int | None = None
    last_content_ns: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    finish_reason: str | None = None


@dataclass
class _PreparedRequest:
    route: RouteSpec
    request: RequestSpec
    http_request: urllib.request.Request
    expected_exact_prompt_tokens: int | None
    expected_max_output_tokens: int
    near_ceiling: bool


SCHEMA_VERSION = 3


DECISION_THRESHOLDS = {
    "max_context_tokens": 262_144,
    "near_ceiling_combined_tokens": 262_143,
    "promotion_gain_percent": 10.0,
    "confirmation_gain_floor_percent": 5.0,
    "max_median_decode_regression_percent": 5.0,
    "max_p95_ttft_ratio": 1.5,
}
_NEAR_CEILING_EXACT_COUNT_FLOOR = (
    DECISION_THRESHOLDS["near_ceiling_combined_tokens"] - 1
)
_MAX_SAFE_JSON_INTEGER = (1 << 53) - 1


def _bounded_float(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if isinstance(value, int) and abs(value) > _MAX_SAFE_JSON_INTEGER:
        return None
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(numeric) or abs(numeric) > _MAX_SAFE_JSON_INTEGER:
        return None
    return numeric


def _bounded_nonnegative_integer(value: object) -> int | None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > _MAX_SAFE_JSON_INTEGER
    ):
        return None
    return value


def _extract_sse_line(buffer: bytearray, *, final: bool) -> bytes | None:
    for index, value in enumerate(buffer):
        if value == 10:
            line = bytes(buffer[:index])
            del buffer[: index + 1]
            return line
        if value == 13:
            if index + 1 == len(buffer) and not final:
                return None
            consumed = 2 if index + 1 < len(buffer) and buffer[index + 1] == 10 else 1
            line = bytes(buffer[:index])
            del buffer[: index + consumed]
            return line
    if final and buffer:
        line = bytes(buffer)
        buffer.clear()
        return line
    return None


def _iter_sse_lines(events: Iterable[bytes]) -> Iterator[bytes]:
    buffer = bytearray()
    for chunk in events:
        if not isinstance(chunk, bytes):
            raise ValueError("invalid_stream")
        buffer.extend(chunk)
        while True:
            line = _extract_sse_line(buffer, final=False)
            if line is None:
                break
            yield line
    while buffer:
        line = _extract_sse_line(buffer, final=True)
        if line is None:
            break
        yield line


def _iter_sse_records(events: Iterable[bytes]) -> Iterator[tuple[str | None, bytes]]:
    event_name: str | None = None
    data_lines: list[bytes] = []
    for line in _iter_sse_lines(events):
        if not line:
            if data_lines:
                payload = b"\n".join(data_lines)
                data_lines.clear()
                yield event_name, payload
                payload = b""
            event_name = None
            continue
        if line.startswith(b":"):
            continue
        field, separator, value = line.partition(b":")
        if separator and value.startswith(b" "):
            value = value[1:]
        if field == b"event":
            try:
                event_name = value.decode("ascii")
            except UnicodeDecodeError as exc:
                raise ValueError("invalid_stream") from exc
        elif field == b"data":
            data_lines.append(bytes(value))
    if data_lines:
        payload = b"\n".join(data_lines)
        data_lines.clear()
        yield event_name, payload
        payload = b""


def _decode_json_object(payload: bytes) -> dict[str, object]:
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_stream") from exc
    if not isinstance(document, dict):
        raise ValueError("invalid_stream")
    return document


def _token_count(mapping: Mapping[str, object], key: str) -> int | None:
    return _bounded_nonnegative_integer(mapping.get(key))


def _discard_text_delta(mapping: dict[str, object], key: str) -> bool:
    value = mapping.get(key)
    has_content = isinstance(value, str) and bool(value)
    if key in mapping:
        mapping[key] = None
    value = None
    return has_content


def derive_rates(
    prompt_tokens: int | None,
    completion_tokens: int | None,
    elapsed_seconds: float,
    ttft_seconds: float | None,
    last_content_seconds: float | None,
    server_metric_count_delta: float | None = None,
    server_metric_sum_delta: float | None = None,
) -> dict[str, float | None]:
    prompt_count = _bounded_float(prompt_tokens)
    completion_count = _bounded_float(completion_tokens)
    elapsed = _bounded_float(elapsed_seconds)
    ttft = _bounded_float(ttft_seconds)
    last_content = _bounded_float(last_content_seconds)
    server_count = _bounded_float(server_metric_count_delta)
    server_sum = _bounded_float(server_metric_sum_delta)

    client_decode: float | None = None
    if (
        completion_count is not None
        and completion_count >= 2
        and ttft is not None
        and last_content is not None
    ):
        decode_seconds = last_content - ttft
        if decode_seconds > 0:
            client_decode = (completion_count - 1) / decode_seconds

    end_to_end_output: float | None = None
    if completion_count is not None and completion_count >= 0 and elapsed is not None and elapsed > 0:
        end_to_end_output = completion_count / elapsed

    effective_prompt: float | None = None
    if prompt_count is not None and prompt_count >= 0 and ttft is not None and ttft > 0:
        effective_prompt = prompt_count / ttft

    server_decode: float | None = None
    if server_count is not None and server_sum is not None and server_count >= 0 and server_sum > 0:
        server_decode = server_count / server_sum

    return {
        "client_decode_tokens_per_second": client_decode,
        "end_to_end_output_tokens_per_second": end_to_end_output,
        "effective_prompt_tokens_per_second": effective_prompt,
        "server_decode_tokens_per_second": server_decode,
    }


def calculate_candidate_decision(
    *,
    hard_gates_passed: bool | None,
    same_work_throughput_gain_percent: float | None,
    median_decode_regression_percent: float | None,
    p95_ttft_ratio: float | None,
    confirmation_completed: bool = False,
) -> str:
    if hard_gates_passed is False:
        return "REJECT"
    if hard_gates_passed is None:
        return "PENDING"
    gain = _bounded_float(same_work_throughput_gain_percent)
    regression = _bounded_float(median_decode_regression_percent)
    ratio = _bounded_float(p95_ttft_ratio)
    if gain is None or regression is None or ratio is None or ratio < 0:
        return "PENDING"
    if (
        regression > DECISION_THRESHOLDS["max_median_decode_regression_percent"]
        or ratio > DECISION_THRESHOLDS["max_p95_ttft_ratio"]
    ):
        return "RETAIN"
    if (
        gain >= DECISION_THRESHOLDS["promotion_gain_percent"]
    ):
        return "PROMOTE"
    if (
        gain >= DECISION_THRESHOLDS["confirmation_gain_floor_percent"]
    ):
        return "RETAIN" if confirmation_completed else "PENDING"
    return "RETAIN"


def nearest_rank_percentile(
    values: Sequence[float], percentile: float
) -> float | None:
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100]")
    ordered: list[float] = []
    for value in values:
        numeric = _bounded_float(value)
        if numeric is None:
            raise ValueError("values must be finite and bounded")
        ordered.append(numeric)
    ordered.sort()
    if not ordered:
        return None
    rank = math.ceil((percentile / 100.0) * len(ordered))
    return ordered[rank - 1]


def summarize_values(values: Sequence[float]) -> dict[str, float | int | None]:
    samples: list[float] = []
    for value in values:
        numeric = _bounded_float(value)
        if numeric is None:
            raise ValueError("values must be finite and bounded")
        samples.append(numeric)
    if not samples:
        return {
            "count": 0,
            "minimum": None,
            "maximum": None,
            "median": None,
            "mean": None,
            "population_standard_deviation": None,
            "p50": None,
            "p95": None,
        }
    return {
        "count": len(samples),
        "minimum": min(samples),
        "maximum": max(samples),
        "median": statistics.median(samples),
        "mean": statistics.fmean(samples),
        "population_standard_deviation": statistics.pstdev(samples),
        "p50": nearest_rank_percentile(samples, 50.0),
        "p95": nearest_rank_percentile(samples, 95.0),
    }


def parse_openai_sse(
    events: Iterable[bytes], clock_ns: Callable[[], int]
) -> StreamResult:
    result = StreamResult()
    for _event_name, payload in _iter_sse_records(events):
        event_ns = clock_ns()
        if payload.strip() == b"[DONE]":
            continue
        document = _decode_json_object(payload)
        payload = b""
        choices = document.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if isinstance(delta, dict) and _discard_text_delta(delta, "content"):
                    if result.first_content_ns is None:
                        result.first_content_ns = event_ns
                    result.last_content_ns = event_ns
                finish_reason = choice.get("finish_reason")
                if isinstance(finish_reason, str) and finish_reason:
                    result.finish_reason = finish_reason
        usage = document.get("usage")
        if isinstance(usage, Mapping):
            prompt_tokens = _token_count(usage, "prompt_tokens")
            completion_tokens = _token_count(usage, "completion_tokens")
            total_tokens = _token_count(usage, "total_tokens")
            if prompt_tokens is not None:
                result.prompt_tokens = prompt_tokens
            if completion_tokens is not None:
                result.completion_tokens = completion_tokens
            if total_tokens is not None:
                result.total_tokens = total_tokens
        document = None
    return result


def parse_anthropic_sse(
    events: Iterable[bytes], clock_ns: Callable[[], int]
) -> StreamResult:
    result = StreamResult()
    for event_name, payload in _iter_sse_records(events):
        event_ns = clock_ns()
        document = _decode_json_object(payload)
        payload = b""
        event_type = document.get("type")
        if not isinstance(event_type, str):
            event_type = event_name

        if event_type == "message_start":
            message = document.get("message")
            if isinstance(message, Mapping):
                usage = message.get("usage")
                if isinstance(usage, Mapping):
                    input_tokens = _token_count(usage, "input_tokens")
                    if input_tokens is not None:
                        result.prompt_tokens = input_tokens
        elif event_type == "content_block_delta":
            delta = document.get("delta")
            if isinstance(delta, dict):
                delta_type = delta.get("type")
                if delta_type == "text_delta" and _discard_text_delta(delta, "text"):
                    if result.first_content_ns is None:
                        result.first_content_ns = event_ns
                    result.last_content_ns = event_ns
        elif event_type == "message_delta":
            delta = document.get("delta")
            if isinstance(delta, Mapping):
                stop_reason = delta.get("stop_reason")
                if isinstance(stop_reason, str) and stop_reason:
                    result.finish_reason = stop_reason
            usage = document.get("usage")
            if isinstance(usage, Mapping):
                output_tokens = _token_count(usage, "output_tokens")
                if output_tokens is not None:
                    result.completion_tokens = output_tokens
        document = None

    if result.prompt_tokens is not None and result.completion_tokens is not None:
        result.total_tokens = result.prompt_tokens + result.completion_tokens
    return result


def _synthetic_prompt(target_prompt_tokens: int) -> str:
    return "benchmark " * target_prompt_tokens


def _request_bytes(
    route: RouteSpec, request: RequestSpec, prompt: str
) -> bytes:
    if route.protocol == "openai":
        document: dict[str, object] = {
            "model": route.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": request.max_output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    elif route.protocol == "anthropic":
        document = {
            "model": route.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": request.max_output_tokens,
            "stream": True,
            "temperature": 0,
        }
    else:
        raise ValueError("invalid_protocol")
    encoded = json.dumps(document, separators=(",", ":")).encode("utf-8")
    document = {}
    return encoded


def _request_headers(route: RouteSpec) -> dict[str, str] | None:
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if route.api_key_env is None:
        return headers
    credential = os.environ.get(route.api_key_env)
    if credential is None:
        return None
    if route.protocol == "openai":
        headers["Authorization"] = f"Bearer {credential}"
    else:
        headers["x-api-key"] = credential
        headers["anthropic-version"] = "2023-06-01"
    credential = ""
    return headers


def _count_tokens_via_tokenize(
    tokenize_url: str,
    route: RouteSpec,
    prompt: str,
    *,
    deadline_ns: int,
    cancel_event: threading.Event | None,
    urlopen: Callable[..., object],
) -> int | None:
    headers = _request_headers(route)
    if headers is None:
        return None
    headers["Accept"] = "application/json"
    document = {
        "model": route.model,
        "messages": [{"role": "user", "content": prompt}],
        "chat_template_kwargs": {"enable_thinking": False},
    }
    body = json.dumps(document, separators=(",", ":")).encode("utf-8")
    document = {}
    tokenize_request: urllib.request.Request | None = None
    stopper = _RequestStopper(
        deadline_ns=deadline_ns,
        cancel_event=cancel_event,
    )
    try:
        stopper.raise_if_stopped()
        tokenize_request = urllib.request.Request(
            tokenize_url, data=body, headers=headers, method="POST"
        )
        body = b""
        headers = {}
        with urlopen(
            tokenize_request, timeout=stopper.remaining_seconds()
        ) as response:
            stopper.attach_response(response)
            stopper.raise_if_stopped()
            status = getattr(response, "status", None)
            if isinstance(status, int) and not 200 <= status < 300:
                return None
            payload = response.read(1_048_577)
            stopper.raise_if_stopped()
        if len(payload) > 1_048_576:
            payload = b""
            return None
        parsed = _decode_json_object(payload)
        payload = b""
        count = _bounded_nonnegative_integer(parsed.get("count"))
        if count is not None:
            parsed = {}
            return count
        tokens = parsed.get("tokens")
        if isinstance(tokens, list):
            count = len(tokens)
            tokens.clear()
            parsed = {}
            return count
    except urllib.error.HTTPError as exc:
        exc.close()
        return None
    except (
        _Cancelled,
        _DeadlineExpired,
        AttributeError,
        OSError,
        TimeoutError,
        ValueError,
        urllib.error.URLError,
    ):
        return None
    finally:
        stopper.finish()
        body = b""
        headers = {}
        tokenize_request = None
    return None


def _terminate_and_reap_process(process_id: int) -> None:
    try:
        os.kill(process_id, signal.SIGTERM)
    except ProcessLookupError:
        pass
    termination_deadline_ns = time.perf_counter_ns() + 50_000_000
    while time.perf_counter_ns() < termination_deadline_ns:
        try:
            waited, _status = os.waitpid(process_id, os.WNOHANG)
        except ChildProcessError:
            return
        if waited == process_id:
            return
        time.sleep(0.001)
    try:
        os.kill(process_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(process_id, 0)
    except ChildProcessError:
        pass


def _count_tokens_via_pinned_isolate(
    pinned_tokenizer: Callable[[RouteSpec, str], int],
    route: RouteSpec,
    prompt: str,
    *,
    deadline_ns: int,
    cancel_event: threading.Event | None,
) -> int | None:
    if not hasattr(os, "fork"):
        return None
    try:
        read_descriptor, write_descriptor = os.pipe()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            process_id = os.fork()
    except OSError:
        for descriptor in locals().get("read_descriptor"), locals().get(
            "write_descriptor"
        ):
            if isinstance(descriptor, int):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        return None

    if process_id == 0:
        try:
            os.close(read_descriptor)
            count = _bounded_nonnegative_integer(pinned_tokenizer(route, prompt))
            if count is not None:
                os.write(write_descriptor, str(count).encode("ascii"))
        except BaseException:
            pass
        finally:
            try:
                os.close(write_descriptor)
            except OSError:
                pass
            os._exit(0)

    os.close(write_descriptor)
    reaped = False
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _terminate_and_reap_process(process_id)
                reaped = True
                return None
            if time.perf_counter_ns() >= deadline_ns:
                _terminate_and_reap_process(process_id)
                reaped = True
                return None
            try:
                waited, _status = os.waitpid(process_id, os.WNOHANG)
            except ChildProcessError:
                reaped = True
                return None
            if waited == process_id:
                reaped = True
                break
            remaining_seconds = max(
                0.0, (deadline_ns - time.perf_counter_ns()) / 1_000_000_000.0
            )
            time.sleep(min(0.005, remaining_seconds))

        payload = os.read(read_descriptor, 32)
        if not payload or len(payload) > 16 or not payload.isdigit():
            return None
        return _bounded_nonnegative_integer(int(payload))
    finally:
        try:
            os.close(read_descriptor)
        except OSError:
            pass
        if not reaped:
            _terminate_and_reap_process(process_id)


def _failure_observation(
    route: RouteSpec,
    request: RequestSpec,
    *,
    elapsed_seconds: float,
    category: str,
    http_status: int | None = None,
    stream_result: StreamResult | None = None,
    status: str = "failed",
) -> Observation:
    return Observation(
        route=route.name,
        case_id=request.case_id,
        candidate_id=request.candidate_id,
        repetition=request.repetition,
        status=status,
        http_status=http_status,
        prompt_tokens=None if stream_result is None else stream_result.prompt_tokens,
        completion_tokens=None
        if stream_result is None
        else stream_result.completion_tokens,
        total_tokens=None if stream_result is None else stream_result.total_tokens,
        finish_reason=None if stream_result is None else stream_result.finish_reason,
        elapsed_seconds=max(0.0, elapsed_seconds),
        ttft_seconds=None,
        last_content_seconds=None,
        client_decode_tokens_per_second=None,
        end_to_end_output_tokens_per_second=None,
        effective_prompt_tokens_per_second=None,
        server_decode_tokens_per_second=None,
        error_category=category,
        phase_id=request.phase_id,
        attempt_id=request.attempt_id,
    )


def _seconds_since(start_ns: int, end_ns: int) -> float:
    return max(0, end_ns - start_ns) / 1_000_000_000.0


def _stopper_failure_observation(
    stopper: "_RequestStopper",
    route: RouteSpec,
    request: RequestSpec,
    *,
    start_ns: int,
    clock_ns: Callable[[], int],
    http_status: int | None,
) -> Observation | None:
    reason = stopper.reason
    if reason not in {"cancelled", "timeout"}:
        return None
    return _failure_observation(
        route,
        request,
        elapsed_seconds=_seconds_since(start_ns, clock_ns()),
        category=reason,
        http_status=http_status,
        status="cancelled" if reason == "cancelled" else "failed",
    )


class _Cancelled(Exception):
    pass


class _DeadlineExpired(Exception):
    pass


class _CombinedCancelEvent:
    def __init__(self, *events: threading.Event | None) -> None:
        self._events = tuple(event for event in events if event is not None)

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


class _RequestStopper:
    def __init__(
        self,
        *,
        timeout_seconds: float | None = None,
        deadline_ns: int | None = None,
        cancel_event: threading.Event | _CombinedCancelEvent | None,
    ) -> None:
        if deadline_ns is None:
            if timeout_seconds is None:
                raise ValueError("stopper deadline is required")
            deadline_ns = time.perf_counter_ns() + int(
                timeout_seconds * 1_000_000_000
            )
        self._deadline_ns = deadline_ns
        self._cancel_event = cancel_event
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._response: object | None = None
        self._reason: str | None = None
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    @property
    def reason(self) -> str | None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            self._stop("cancelled")
        elif time.perf_counter_ns() >= self._deadline_ns:
            self._stop("timeout")
        with self._lock:
            return self._reason

    def remaining_seconds(self) -> float:
        remaining_ns = self._deadline_ns - time.perf_counter_ns()
        return max(0.001, remaining_ns / 1_000_000_000.0)

    def attach_response(self, response: object) -> None:
        with self._lock:
            self._response = response
            should_interrupt = self._reason is not None
        if should_interrupt:
            self._interrupt(response)

    def raise_if_stopped(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            self._stop("cancelled")
        elif time.perf_counter_ns() >= self._deadline_ns:
            self._stop("timeout")
        reason = self.reason
        if reason == "cancelled":
            raise _Cancelled
        if reason == "timeout":
            raise _DeadlineExpired

    def finish(self) -> None:
        self._done.set()
        self._thread.join(timeout=0.1)
        with self._lock:
            self._response = None

    def _watch(self) -> None:
        while not self._done.wait(0.005):
            if self._cancel_event is not None and self._cancel_event.is_set():
                self._stop("cancelled")
                return
            if time.perf_counter_ns() >= self._deadline_ns:
                self._stop("timeout")
                return

    def _stop(self, reason: str) -> None:
        with self._lock:
            if self._reason is not None:
                return
            self._reason = reason
            response = self._response
        if response is not None:
            self._interrupt(response)

    @staticmethod
    def _interrupt(response: object) -> None:
        stream = getattr(response, "fp", None)
        raw = getattr(stream, "raw", None)
        active_socket = getattr(raw, "_sock", None)
        if active_socket is not None:
            try:
                active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                active_socket.close()
            except OSError:
                pass
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except OSError:
                pass


def _cancellable_events(
    response: Iterable[bytes], stopper: _RequestStopper
) -> Iterator[bytes]:
    stopper.raise_if_stopped()
    for chunk in response:
        stopper.raise_if_stopped()
        yield chunk
    stopper.raise_if_stopped()


def _valid_http_endpoint(value: object) -> bool:
    if not isinstance(value, str) or not value or any(
        ord(character) < 32 or character.isspace() for character in value
    ):
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and parsed.fragment == ""
        and (port is None or 1 <= port <= 65_535)
    )


def _valid_model_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 512
        and all(ord(character) >= 32 and character not in "\r\n" for character in value)
    )


def _exact_count_is_required(
    request: RequestSpec, require_exact_count: bool | None
) -> bool:
    declared_combined_tokens = (
        request.target_prompt_tokens + request.max_output_tokens
    )
    return (
        declared_combined_tokens >= _NEAR_CEILING_EXACT_COUNT_FLOOR
        or request.case_id == "near-ceiling"
        or bool(require_exact_count)
    )


def _request_validation_category(
    route: RouteSpec,
    request: RequestSpec,
    *,
    timeout_seconds: float,
    require_exact_count: bool | None,
    tokenize_url: str | None,
    pinned_tokenizer: Callable[[RouteSpec, str], int] | None,
) -> str | None:
    declared_combined_tokens = (
        request.target_prompt_tokens + request.max_output_tokens
    )
    if (
        request.target_prompt_tokens <= 0
        or request.max_output_tokens <= 0
        or request.repetition <= 0
        or declared_combined_tokens > DECISION_THRESHOLDS["max_context_tokens"]
        or (
            request.case_id == "near-ceiling"
            and declared_combined_tokens
            != DECISION_THRESHOLDS["near_ceiling_combined_tokens"]
        )
        or timeout_seconds <= 0
        or not math.isfinite(timeout_seconds)
        or route.protocol not in {"openai", "anthropic"}
        or not _valid_http_endpoint(route.url)
        or not _valid_model_name(route.model)
        or (
            tokenize_url is not None and not _valid_http_endpoint(tokenize_url)
        )
        or any(
            _IDENTIFIER_RE.fullmatch(identifier) is None
            for identifier in (
                route.name,
                request.case_id,
                request.candidate_id,
                request.phase_id,
                request.attempt_id,
            )
        )
        or (
            route.api_key_env is not None
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", route.api_key_env)
            is None
        )
    ):
        return "invalid_request"
    if (
        _exact_count_is_required(request, require_exact_count)
        and tokenize_url is None
        and pinned_tokenizer is None
    ):
        return "exact_count_unavailable"
    return None


def _measure_prompt_tokens(
    route: RouteSpec,
    prompt: str,
    *,
    tokenize_url: str | None,
    pinned_tokenizer: Callable[[RouteSpec, str], int] | None,
    deadline_ns: int,
    cancel_event: threading.Event | None,
    urlopen: Callable[..., object],
) -> int | None:
    measured: int | None = None
    if cancel_event is not None and cancel_event.is_set():
        return None
    if time.perf_counter_ns() >= deadline_ns:
        return None
    if tokenize_url is not None:
        measured = _count_tokens_via_tokenize(
            tokenize_url,
            route,
            prompt,
            deadline_ns=deadline_ns,
            cancel_event=cancel_event,
            urlopen=urlopen,
        )
    if (
        measured is None
        and pinned_tokenizer is not None
        and (cancel_event is None or not cancel_event.is_set())
        and time.perf_counter_ns() < deadline_ns
    ):
        measured = _count_tokens_via_pinned_isolate(
            pinned_tokenizer,
            route,
            prompt,
            deadline_ns=deadline_ns,
            cancel_event=cancel_event,
        )
    if cancel_event is not None and cancel_event.is_set():
        return None
    if time.perf_counter_ns() >= deadline_ns:
        return None
    return measured


def _prepare_request(
    route: RouteSpec,
    request: RequestSpec,
    *,
    timeout_seconds: float,
    urlopen: Callable[..., object],
    cancel_event: threading.Event | None,
    require_exact_count: bool | None,
    tokenize_url: str | None,
    pinned_tokenizer: Callable[[RouteSpec, str], int] | None,
) -> _PreparedRequest | Observation:
    if cancel_event is not None and cancel_event.is_set():
        return _failure_observation(
            route,
            request,
            elapsed_seconds=0.0,
            category="cancelled",
            status="cancelled",
        )

    validation_category = _request_validation_category(
        route,
        request,
        timeout_seconds=timeout_seconds,
        require_exact_count=require_exact_count,
        tokenize_url=tokenize_url,
        pinned_tokenizer=pinned_tokenizer,
    )
    if validation_category is not None:
        return _failure_observation(
            route,
            request,
            elapsed_seconds=0.0,
            category=validation_category,
        )
    exact_count_required = _exact_count_is_required(request, require_exact_count)
    has_token_counter = tokenize_url is not None or pinned_tokenizer is not None

    headers = _request_headers(route)
    if headers is None:
        return _failure_observation(
            route,
            request,
            elapsed_seconds=0.0,
            category="credential_missing",
        )

    repeat_count = request.target_prompt_tokens
    prompt = _synthetic_prompt(repeat_count)
    exact_prompt_tokens: int | None = None
    calibration_deadline_ns = time.perf_counter_ns() + int(
        timeout_seconds * 1_000_000_000
    )
    if has_token_counter:
        for _attempt in range(16):
            exact_prompt_tokens = _measure_prompt_tokens(
                route,
                prompt,
                tokenize_url=tokenize_url,
                pinned_tokenizer=pinned_tokenizer,
                deadline_ns=calibration_deadline_ns,
                cancel_event=cancel_event,
                urlopen=urlopen,
            )
            if exact_prompt_tokens is None or exact_prompt_tokens == request.target_prompt_tokens:
                break
            adjusted_repeat_count = repeat_count + (
                request.target_prompt_tokens - exact_prompt_tokens
            )
            if adjusted_repeat_count <= 0 or adjusted_repeat_count == repeat_count:
                break
            repeat_count = adjusted_repeat_count
            prompt = _synthetic_prompt(repeat_count)

    if cancel_event is not None and cancel_event.is_set():
        prompt = ""
        return _failure_observation(
            route,
            request,
            elapsed_seconds=0.0,
            category="cancelled",
            status="cancelled",
        )
    if exact_count_required and exact_prompt_tokens is None:
        prompt = ""
        return _failure_observation(
            route,
            request,
            elapsed_seconds=0.0,
            category="exact_count_unavailable",
        )
    if exact_prompt_tokens is not None and (
        exact_prompt_tokens != request.target_prompt_tokens
        or exact_prompt_tokens + request.max_output_tokens
        > DECISION_THRESHOLDS["max_context_tokens"]
    ):
        prompt = ""
        return _failure_observation(
            route,
            request,
            elapsed_seconds=0.0,
            category="invalid_request",
        )

    try:
        body = _request_bytes(route, request, prompt)
        prompt = ""
        http_request = urllib.request.Request(
            route.url, data=body, headers=headers, method="POST"
        )
    except (TypeError, ValueError):
        prompt = ""
        return _failure_observation(
            route,
            request,
            elapsed_seconds=0.0,
            category="invalid_request",
        )
    finally:
        body = b""
        headers = {}
    if cancel_event is not None and cancel_event.is_set():
        http_request = None
        return _failure_observation(
            route,
            request,
            elapsed_seconds=0.0,
            category="cancelled",
            status="cancelled",
        )
    return _PreparedRequest(
        route=route,
        request=request,
        http_request=http_request,
        expected_exact_prompt_tokens=exact_prompt_tokens,
        expected_max_output_tokens=request.max_output_tokens,
        near_ceiling=request.case_id == "near-ceiling",
    )


def _execute_prepared_request(
    prepared: _PreparedRequest,
    *,
    timeout_seconds: float,
    clock_ns: Callable[[], int],
    urlopen: Callable[..., object],
    cancel_event: threading.Event | None,
    start_barrier: threading.Barrier | None = None,
) -> Observation:
    route = prepared.route
    request = prepared.request
    http_request: urllib.request.Request | None = prepared.http_request
    if start_barrier is not None:
        start_barrier.wait()
    if cancel_event is not None and cancel_event.is_set():
        http_request = None
        return _failure_observation(
            route,
            request,
            elapsed_seconds=0.0,
            category="cancelled",
            status="cancelled",
        )
    start_ns = clock_ns()
    http_status: int | None = None
    stream_result: StreamResult | None = None
    stopper = _RequestStopper(
        timeout_seconds=timeout_seconds,
        cancel_event=cancel_event,
    )
    try:
        stopper.raise_if_stopped()
        with urlopen(
            http_request, timeout=stopper.remaining_seconds()
        ) as response:
            stopper.attach_response(response)
            stopper.raise_if_stopped()
            response_status = getattr(response, "status", None)
            if isinstance(response_status, int):
                http_status = response_status
            if http_status is not None and not 200 <= http_status < 300:
                category = (
                    "http_4xx"
                    if 400 <= http_status < 500
                    else "http_5xx"
                    if 500 <= http_status < 600
                    else "http_error"
                )
                return _failure_observation(
                    route,
                    request,
                    elapsed_seconds=_seconds_since(start_ns, clock_ns()),
                    category=category,
                    http_status=http_status,
                )
            parser = (
                parse_openai_sse
                if route.protocol == "openai"
                else parse_anthropic_sse
            )
            stream_result = parser(_cancellable_events(response, stopper), clock_ns)
            stopper.raise_if_stopped()
    except urllib.error.HTTPError as exc:
        http_status = exc.code if isinstance(exc.code, int) else None
        exc.close()
        category = (
            "http_4xx"
            if http_status is not None and 400 <= http_status < 500
            else "http_5xx"
            if http_status is not None and 500 <= http_status < 600
            else "http_error"
        )
        return _failure_observation(
            route,
            request,
            elapsed_seconds=_seconds_since(start_ns, clock_ns()),
            category=category,
            http_status=http_status,
        )
    except _Cancelled:
        return _failure_observation(
            route,
            request,
            elapsed_seconds=_seconds_since(start_ns, clock_ns()),
            category="cancelled",
            http_status=http_status,
            status="cancelled",
        )
    except _DeadlineExpired:
        return _failure_observation(
            route,
            request,
            elapsed_seconds=_seconds_since(start_ns, clock_ns()),
            category="timeout",
            http_status=http_status,
        )
    except (TimeoutError, socket.timeout):
        stopped = _stopper_failure_observation(
            stopper,
            route,
            request,
            start_ns=start_ns,
            clock_ns=clock_ns,
            http_status=http_status,
        )
        if stopped is not None:
            return stopped
        return _failure_observation(
            route,
            request,
            elapsed_seconds=_seconds_since(start_ns, clock_ns()),
            category="timeout",
            http_status=http_status,
        )
    except urllib.error.URLError as exc:
        stopped = _stopper_failure_observation(
            stopper,
            route,
            request,
            start_ns=start_ns,
            clock_ns=clock_ns,
            http_status=http_status,
        )
        if stopped is not None:
            return stopped
        category = (
            "timeout"
            if isinstance(exc.reason, (TimeoutError, socket.timeout))
            else "connection"
        )
        return _failure_observation(
            route,
            request,
            elapsed_seconds=_seconds_since(start_ns, clock_ns()),
            category=category,
            http_status=http_status,
        )
    except (ConnectionError, OSError):
        stopped = _stopper_failure_observation(
            stopper,
            route,
            request,
            start_ns=start_ns,
            clock_ns=clock_ns,
            http_status=http_status,
        )
        if stopped is not None:
            return stopped
        return _failure_observation(
            route,
            request,
            elapsed_seconds=_seconds_since(start_ns, clock_ns()),
            category="connection",
            http_status=http_status,
        )
    except ValueError:
        stopped = _stopper_failure_observation(
            stopper,
            route,
            request,
            start_ns=start_ns,
            clock_ns=clock_ns,
            http_status=http_status,
        )
        if stopped is not None:
            return stopped
        return _failure_observation(
            route,
            request,
            elapsed_seconds=_seconds_since(start_ns, clock_ns()),
            category="invalid_stream",
            http_status=http_status,
        )
    except AttributeError:
        stopped = _stopper_failure_observation(
            stopper,
            route,
            request,
            start_ns=start_ns,
            clock_ns=clock_ns,
            http_status=http_status,
        )
        if stopped is not None:
            return stopped
        raise
    finally:
        stopper.finish()
        http_request = None

    end_ns = clock_ns()
    if stream_result is None or stream_result.first_content_ns is None:
        return _failure_observation(
            route,
            request,
            elapsed_seconds=_seconds_since(start_ns, end_ns),
            category="invalid_stream",
            http_status=http_status,
            stream_result=stream_result,
        )
    if (
        stream_result.prompt_tokens is None
        or stream_result.completion_tokens is None
        or stream_result.total_tokens is None
    ):
        return _failure_observation(
            route,
            request,
            elapsed_seconds=_seconds_since(start_ns, end_ns),
            category="usage_missing",
            http_status=http_status,
            stream_result=stream_result,
        )

    reported_prompt_tokens = stream_result.prompt_tokens
    reported_combined_allowance = (
        reported_prompt_tokens + prepared.expected_max_output_tokens
    )
    usage_mismatch = (
        prepared.expected_exact_prompt_tokens is not None
        and reported_prompt_tokens != prepared.expected_exact_prompt_tokens
    ) or reported_combined_allowance > DECISION_THRESHOLDS["max_context_tokens"]
    if prepared.near_ceiling and (
        reported_combined_allowance
        != DECISION_THRESHOLDS["near_ceiling_combined_tokens"]
    ):
        usage_mismatch = True
    if usage_mismatch:
        return _failure_observation(
            route,
            request,
            elapsed_seconds=_seconds_since(start_ns, end_ns),
            category="usage_mismatch",
            http_status=http_status,
            stream_result=stream_result,
        )

    elapsed_seconds = _seconds_since(start_ns, end_ns)
    ttft_seconds = _seconds_since(start_ns, stream_result.first_content_ns)
    last_content_seconds = (
        None
        if stream_result.last_content_ns is None
        else _seconds_since(start_ns, stream_result.last_content_ns)
    )
    rates = derive_rates(
        prompt_tokens=stream_result.prompt_tokens,
        completion_tokens=stream_result.completion_tokens,
        elapsed_seconds=elapsed_seconds,
        ttft_seconds=ttft_seconds,
        last_content_seconds=last_content_seconds,
    )
    return Observation(
        route=route.name,
        case_id=request.case_id,
        candidate_id=request.candidate_id,
        repetition=request.repetition,
        status="ok",
        http_status=http_status,
        prompt_tokens=stream_result.prompt_tokens,
        completion_tokens=stream_result.completion_tokens,
        total_tokens=stream_result.total_tokens,
        finish_reason=stream_result.finish_reason,
        elapsed_seconds=elapsed_seconds,
        ttft_seconds=ttft_seconds,
        last_content_seconds=last_content_seconds,
        client_decode_tokens_per_second=rates[
            "client_decode_tokens_per_second"
        ],
        end_to_end_output_tokens_per_second=rates[
            "end_to_end_output_tokens_per_second"
        ],
        effective_prompt_tokens_per_second=rates[
            "effective_prompt_tokens_per_second"
        ],
        server_decode_tokens_per_second=rates[
            "server_decode_tokens_per_second"
        ],
        error_category=None,
        phase_id=request.phase_id,
        attempt_id=request.attempt_id,
    )


def run_request(
    route: RouteSpec,
    request: RequestSpec,
    *,
    timeout_seconds: float = 600.0,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
    urlopen: Callable[..., object] = urllib.request.urlopen,
    cancel_event: threading.Event | None = None,
    require_exact_count: bool | None = None,
    tokenize_url: str | None = None,
    pinned_tokenizer: Callable[[RouteSpec, str], int] | None = None,
) -> Observation:
    prepared = _prepare_request(
        route,
        request,
        timeout_seconds=timeout_seconds,
        urlopen=urlopen,
        cancel_event=cancel_event,
        require_exact_count=require_exact_count,
        tokenize_url=tokenize_url,
        pinned_tokenizer=pinned_tokenizer,
    )
    if isinstance(prepared, Observation):
        return prepared
    return _execute_prepared_request(
        prepared,
        timeout_seconds=timeout_seconds,
        clock_ns=clock_ns,
        urlopen=urlopen,
        cancel_event=cancel_event,
    )


_PROMETHEUS_SAMPLE_RE = re.compile(
    rb"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{[^}]*\})?\s+"
    rb"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
    rb"(?:\s+\d+)?\s*$"
)


def parse_prometheus_allowlist(
    payload: bytes, names: set[str]
) -> dict[str, float]:
    allowed: dict[bytes, str] = {}
    for name in names:
        try:
            allowed[name.encode("ascii")] = name
        except UnicodeEncodeError:
            continue
    retained: dict[str, float] = {}
    for line in payload.splitlines():
        match = _PROMETHEUS_SAMPLE_RE.fullmatch(line.strip())
        if match is None:
            continue
        metric_name = allowed.get(match.group(1))
        if metric_name is None:
            continue
        try:
            value = float(match.group(2))
        except ValueError:
            continue
        if not math.isfinite(value) or value < 0:
            continue
        retained[metric_name] = retained.get(metric_name, 0.0) + value
    return retained


def aggregate_prometheus_samples(
    *,
    before: Mapping[str, float],
    after: Mapping[str, float],
    samples: Sequence[Mapping[str, float]],
    counter_names: set[str],
    gauge_names: set[str],
) -> dict[str, dict[str, float | None]]:
    counter_deltas: dict[str, float | None] = {}
    for name in sorted(counter_names):
        start = before.get(name)
        end = after.get(name)
        if (
            start is None
            or end is None
            or not math.isfinite(start)
            or not math.isfinite(end)
            or end < start
        ):
            counter_deltas[name] = None
        else:
            counter_deltas[name] = end - start

    gauge_peaks: dict[str, float | None] = {}
    for name in sorted(gauge_names):
        values = [
            sample[name]
            for sample in samples
            if name in sample and math.isfinite(sample[name]) and sample[name] >= 0
        ]
        gauge_peaks[name] = max(values) if values else None
    return {
        "counter_deltas": counter_deltas,
        "gauge_peaks": gauge_peaks,
    }


def _fetch_prometheus_allowlist(
    url: str,
    names: set[str],
    *,
    timeout_seconds: float,
    cancel_event: threading.Event | _CombinedCancelEvent | None,
    urlopen: Callable[..., object],
) -> dict[str, float]:
    metrics_request: urllib.request.Request | None = None
    stopper = _RequestStopper(
        timeout_seconds=timeout_seconds,
        cancel_event=cancel_event,
    )
    try:
        stopper.raise_if_stopped()
        metrics_request = urllib.request.Request(
            url, headers={"Accept": "text/plain"}, method="GET"
        )
        with urlopen(
            metrics_request, timeout=stopper.remaining_seconds()
        ) as response:
            stopper.attach_response(response)
            stopper.raise_if_stopped()
            status = getattr(response, "status", None)
            if isinstance(status, int) and not 200 <= status < 300:
                return {}
            payload = response.read(4_194_305)
            stopper.raise_if_stopped()
        if len(payload) > 4_194_304:
            payload = b""
            return {}
        retained = parse_prometheus_allowlist(payload, names)
        payload = b""
        return retained
    except urllib.error.HTTPError as exc:
        exc.close()
        return {}
    except (
        _Cancelled,
        _DeadlineExpired,
        AttributeError,
        OSError,
        TimeoutError,
        ValueError,
        urllib.error.URLError,
    ):
        return {}
    finally:
        stopper.finish()
        metrics_request = None


def run_batch(
    route: RouteSpec,
    requests: Sequence[RequestSpec],
    *,
    concurrency: int | None = None,
    timeout_seconds: float = 600.0,
    cancel_event: threading.Event | None = None,
    request_runner: Callable[..., Observation] = run_request,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
    urlopen: Callable[..., object] = urllib.request.urlopen,
    require_exact_count: bool | None = None,
    tokenize_url: str | None = None,
    pinned_tokenizer: Callable[[RouteSpec, str], int] | None = None,
    metrics_url: str | None = None,
    metric_names: set[str] | None = None,
    counter_names: set[str] | None = None,
    gauge_names: set[str] | None = None,
    server_decode_count_metric: str | None = None,
    server_decode_sum_metric: str | None = None,
    metric_sample_interval_seconds: float = 0.1,
    metrics_callback: Callable[[dict[str, object]], None] | None = None,
    batch_callback: Callable[[dict[str, object]], None] | None = None,
) -> list[Observation]:
    if not requests:
        if batch_callback is not None:
            batch_callback({"batch_wall_seconds": 0.0})
        return []
    worker_count = len(requests) if concurrency is None else concurrency
    if worker_count <= 0:
        raise ValueError("concurrency must be positive")
    if timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
        raise ValueError("timeout must be finite and positive")
    if (
        metric_sample_interval_seconds <= 0
        or not math.isfinite(metric_sample_interval_seconds)
    ):
        raise ValueError("metric sample interval must be finite and positive")

    discovered_names = set() if metric_names is None else set(metric_names)
    counters = set() if counter_names is None else set(counter_names)
    gauges = set() if gauge_names is None else set(gauge_names)
    if any(
        not isinstance(name, str) or _METRIC_NAME_RE.fullmatch(name) is None
        for name in discovered_names | counters | gauges
    ):
        raise ValueError("metric names are invalid")
    if not counters.issubset(discovered_names) or not gauges.issubset(
        discovered_names
    ):
        raise ValueError("metric subsets are invalid")
    if metrics_url is None:
        if discovered_names or counters or gauges:
            raise ValueError("metric names require a metrics endpoint")
    elif not _valid_http_endpoint(metrics_url) or not discovered_names:
        raise ValueError("metrics endpoint and allowlist are invalid")
    server_metric_names = {server_decode_count_metric, server_decode_sum_metric}
    if (server_decode_count_metric is None) != (server_decode_sum_metric is None):
        raise ValueError("server decode metrics must be paired")
    if server_decode_count_metric is not None and (
        None in server_metric_names
        or not server_metric_names.issubset(counters)
        or not server_metric_names.issubset(discovered_names)
    ):
        raise ValueError("server decode metrics must be allowlisted counters")

    observations: list[Observation | None] = [None] * len(requests)
    observation_lock = threading.Lock()
    sealed_observation_indices: set[int] = set()
    prepared_requests: list[_PreparedRequest | None] = [None] * len(requests)
    default_runner = request_runner is run_request
    if default_runner:
        for index, request in enumerate(requests):
            category = _request_validation_category(
                route,
                request,
                timeout_seconds=timeout_seconds,
                require_exact_count=require_exact_count,
                tokenize_url=tokenize_url,
                pinned_tokenizer=pinned_tokenizer,
            )
            if category is not None:
                observations[index] = _failure_observation(
                    route,
                    request,
                    elapsed_seconds=0.0,
                    category=category,
                )
        for index, request in enumerate(requests):
            if observations[index] is not None:
                continue
            prepared = _prepare_request(
                route,
                request,
                timeout_seconds=timeout_seconds,
                urlopen=urlopen,
                cancel_event=cancel_event,
                require_exact_count=require_exact_count,
                tokenize_url=tokenize_url,
                pinned_tokenizer=pinned_tokenizer,
            )
            if isinstance(prepared, Observation):
                observations[index] = prepared
            else:
                prepared_requests[index] = prepared
    runnable_indices = [
        index
        for index, observation in enumerate(observations)
        if observation is None
    ]

    metrics_enabled = (
        bool(runnable_indices)
        and metrics_url is not None
        and bool(discovered_names)
    )
    metric_timeout = min(timeout_seconds, 0.1)
    before: dict[str, float] = {}
    samples: list[dict[str, float]] = []
    stop_sampling = threading.Event()
    sampler_thread: threading.Thread | None = None

    if metrics_enabled and metrics_url is not None:
        before = _fetch_prometheus_allowlist(
            metrics_url,
            discovered_names,
            timeout_seconds=metric_timeout,
            cancel_event=cancel_event,
            urlopen=urlopen,
        )
        sampler_cancel = _CombinedCancelEvent(stop_sampling, cancel_event)

        def sample_metrics() -> None:
            while not stop_sampling.wait(metric_sample_interval_seconds):
                sample = _fetch_prometheus_allowlist(
                    metrics_url,
                    discovered_names,
                    timeout_seconds=metric_timeout,
                    cancel_event=sampler_cancel,
                    urlopen=urlopen,
                )
                if sample:
                    samples.append(sample)

        if cancel_event is None or not cancel_event.is_set():
            sampler_thread = threading.Thread(
                target=sample_metrics,
                name="benchmark-metric-sampler",
                daemon=True,
            )
            sampler_thread.start()

    worker_errors: list[bool] = []
    total_batch_wall_ns = 0
    try:
        for wave_start in range(0, len(runnable_indices), worker_count):
            wave_indices = runnable_indices[wave_start : wave_start + worker_count]
            wave_release_ns: list[int] = []
            barrier = threading.Barrier(
                len(wave_indices) + 1,
                action=lambda: wave_release_ns.append(clock_ns()),
            )
            threads: list[threading.Thread] = []

            def worker(index: int) -> None:
                try:
                    if default_runner:
                        prepared = prepared_requests[index]
                        if prepared is None:
                            raise RuntimeError("request_preflight_missing")
                        result = _execute_prepared_request(
                            prepared,
                            timeout_seconds=timeout_seconds,
                            clock_ns=clock_ns,
                            urlopen=urlopen,
                            cancel_event=cancel_event,
                            start_barrier=barrier,
                        )
                    else:
                        barrier.wait()
                        result = request_runner(
                            route,
                            requests[index],
                            timeout_seconds=timeout_seconds,
                            clock_ns=clock_ns,
                            urlopen=urlopen,
                            cancel_event=cancel_event,
                            require_exact_count=require_exact_count,
                            tokenize_url=tokenize_url,
                            pinned_tokenizer=pinned_tokenizer,
                        )
                    with observation_lock:
                        if index not in sealed_observation_indices:
                            observations[index] = result
                except Exception:
                    with observation_lock:
                        if index not in sealed_observation_indices:
                            worker_errors.append(True)

            for index in wave_indices:
                thread = threading.Thread(target=worker, args=(index,), daemon=True)
                thread.start()
                threads.append(thread)
            barrier.wait()
            join_deadline_ns = time.perf_counter_ns() + int(
                (timeout_seconds + 0.1) * 1_000_000_000
            )
            for index, thread in zip(wave_indices, threads):
                remaining_ns = max(0, join_deadline_ns - time.perf_counter_ns())
                thread.join(timeout=remaining_ns / 1_000_000_000.0)
                if thread.is_alive():
                    cancelled = cancel_event is not None and cancel_event.is_set()
                    with observation_lock:
                        sealed_observation_indices.add(index)
                        observations[index] = _failure_observation(
                            route,
                            requests[index],
                            elapsed_seconds=timeout_seconds,
                            category="cancelled" if cancelled else "timeout",
                            status="cancelled" if cancelled else "failed",
                        )
            if wave_release_ns:
                total_batch_wall_ns += max(0, clock_ns() - wave_release_ns[0])
            if worker_errors:
                raise RuntimeError("request_runner_failed") from None
    finally:
        if sampler_thread is not None:
            stop_sampling.set()
            sampler_thread.join()

    if metrics_enabled and metrics_url is not None:
        after = _fetch_prometheus_allowlist(
            metrics_url,
            discovered_names,
            timeout_seconds=metric_timeout,
            cancel_event=cancel_event,
            urlopen=urlopen,
        )
        aggregates = aggregate_prometheus_samples(
            before=before,
            after=after,
            samples=[before, *samples, after],
            counter_names=counters,
            gauge_names=gauges,
        )
        if metrics_callback is not None:
            counter_deltas = aggregates["counter_deltas"]
            server_count_delta = (
                None
                if server_decode_count_metric is None
                else counter_deltas.get(server_decode_count_metric)
            )
            server_sum_delta = (
                None
                if server_decode_sum_metric is None
                else counter_deltas.get(server_decode_sum_metric)
            )
            server_rates = derive_rates(
                None,
                None,
                0.0,
                None,
                None,
                server_count_delta,
                server_sum_delta,
            )
            metrics_callback(
                {
                    "counter_deltas": counter_deltas,
                    "gauge_peaks": aggregates["gauge_peaks"],
                    "server_decode_count_metric": server_decode_count_metric,
                    "server_decode_sum_metric": server_decode_sum_metric,
                    "server_decode_count_delta": server_count_delta,
                    "server_decode_sum_delta": server_sum_delta,
                    "server_decode_tokens_per_second": server_rates[
                        "server_decode_tokens_per_second"
                    ],
                }
            )

    batch_wall_seconds = total_batch_wall_ns / 1_000_000_000.0
    if batch_callback is not None:
        batch_callback({"batch_wall_seconds": batch_wall_seconds})

    completed: list[Observation] = []
    with observation_lock:
        observation_snapshot = list(observations)
    for index, observation in enumerate(observation_snapshot):
        if observation is None:
            completed.append(
                _failure_observation(
                    route,
                    requests[index],
                    elapsed_seconds=0.0,
                    category="invalid_stream",
                )
            )
        else:
            completed.append(observation)
    return completed


_FORBIDDEN_NAMES = {
    "prompt",
    "completion",
    "request_body",
    "response_body",
    "body",
    "headers",
    "authorization",
    "api_key",
    "credential",
    "tool_arguments",
    "raw_error",
    "raw_metrics",
    "docker_environment_values",
    "token_fingerprint",
}
DECISION_POLICY = {
    "baseline identity/context/health/privacy failure": "HOLD; do not tune",
    "contaminated metric delta": "invalidate only that repetition and rerun when quiet",
    "exact near-ceiling token counting unavailable": "HOLD; do not tune",
    "candidate hard-gate failure": "restore previous last-good; stop the staircase",
    "same-work useful-throughput gain >=10% with all gates passing": "promote and advance",
    "gain >=5% and <10%": "repeat once; promote only if the combined result reaches >=10%",
    "confirmed gain <5%": "retain previous last-good; stop the staircase",
    "latency regression without compensating throughput": "retain previous last-good; stop",
    "unavailable optional server metric": "record null; use canonical client timing, do not invent",
    "winner full-matrix/soak failure": "restore previous last-good and requalify it",
    "no tuned candidate survives": "restore untouched max-num-seqs=1 baseline",
}
_ROOT_KEYS = {
    "schema_version",
    "decision_policy",
    "decision_thresholds",
    "prometheus_metric_name_allowlist",
    "observations",
    "batches",
    "candidate_decisions",
    "summaries",
    "final_verdict",
}
_OBSERVATION_KEYS = {
    "route",
    "case_id",
    "candidate_id",
    "phase_id",
    "attempt_id",
    "repetition",
    "status",
    "http_status",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "finish_reason",
    "elapsed_seconds",
    "ttft_seconds",
    "last_content_seconds",
    "client_decode_tokens_per_second",
    "end_to_end_output_tokens_per_second",
    "effective_prompt_tokens_per_second",
    "server_decode_tokens_per_second",
    "error_category",
}
_BATCH_KEYS = {
    "route",
    "case_id",
    "candidate_id",
    "phase_id",
    "attempt_id",
    "requested_concurrency",
    "request_count",
    "target_prompt_tokens",
    "max_output_tokens",
    "decode_mode",
    "counting_mode",
    "attempted_count",
    "successful_count",
    "failed_count",
    "timed_out_count",
    "cancelled_count",
    "total_prompt_tokens",
    "total_completion_tokens",
    "total_tokens",
    "batch_wall_seconds",
    "aggregate_output_tokens_per_second",
    "counter_deltas",
    "gauge_peaks",
    "server_decode_count_metric",
    "server_decode_sum_metric",
    "server_decode_count_delta",
    "server_decode_sum_delta",
    "server_decode_tokens_per_second",
}
_SUMMARY_KEYS = {
    "count",
    "minimum",
    "maximum",
    "median",
    "mean",
    "population_standard_deviation",
    "p50",
    "p95",
}
_ALLOWED_STATUSES = {"ok", "failed", "cancelled", "skipped", "invalid"}
_ALLOWED_DECODE_MODES = {"openai_sse", "anthropic_sse"}
_ALLOWED_COUNTING_MODES = {
    "declared",
    "tokenize_exact",
    "pinned_exact",
    "tokenize_then_pinned_exact",
}
_ALLOWED_ERROR_CATEGORIES = {
    "timeout",
    "connection",
    "http_4xx",
    "http_5xx",
    "http_error",
    "invalid_stream",
    "usage_missing",
    "usage_mismatch",
    "invalid_request",
    "credential_missing",
    "exact_count_unavailable",
    "cancelled",
}
_ALLOWED_DECISIONS = {
    "PENDING",
    "PASS",
    "PROMOTE",
    "REJECT",
    "RETAIN",
    "SKIP",
    "HOLD",
    "INVALID",
}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_METRIC_NAME_RE = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:]*$")


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _scan_forbidden(value: object, path: str, errors: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if not isinstance(key, str):
                errors.append(f"{path}: mapping keys must be strings")
            elif _normalized_name(key) in _FORBIDDEN_NAMES:
                errors.append(f"{child_path}: forbidden key")
            _scan_forbidden(child, child_path, errors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_forbidden(child, f"{path}[{index}]", errors)
    elif isinstance(value, str) and _normalized_name(value) in _FORBIDDEN_NAMES:
        errors.append(f"{path}: forbidden persisted value")


def _validate_identifier(value: object, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        errors.append(f"{path}: expected sanitized identifier")


def _validate_number(
    value: object,
    path: str,
    errors: list[str],
    *,
    nullable: bool = False,
    integer: bool = False,
    positive: bool = False,
    signed: bool = False,
) -> float | int | None:
    if value is None and nullable:
        return None
    valid_type = isinstance(value, int if integer else (int, float))
    if not valid_type or isinstance(value, bool):
        errors.append(f"{path}: expected {'integer' if integer else 'number'}")
        return None
    numeric = _bounded_float(value)
    if numeric is None:
        errors.append(f"{path}: expected finite bounded value")
        return None
    if (
        (not signed and numeric < 0)
        or (positive and numeric <= 0)
    ):
        expectation = "finite value" if signed else "finite non-negative value"
        errors.append(f"{path}: expected {expectation}")
        return None
    return value


def _validate_observation(
    value: object, path: str, errors: list[str]
) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{path}: expected observation object")
        return
    keys = set(value)
    for key in sorted(keys - _OBSERVATION_KEYS, key=str):
        errors.append(f"{path}.{key}: key is not allowlisted")
    for key in sorted(_OBSERVATION_KEYS - keys):
        errors.append(f"{path}.{key}: required key is missing")

    for key in ("route", "case_id", "candidate_id", "phase_id", "attempt_id"):
        if key in value:
            _validate_identifier(value[key], f"{path}.{key}", errors)
    repetition = _validate_number(
        value.get("repetition"),
        f"{path}.repetition",
        errors,
        integer=True,
        positive=True,
    )
    status = value.get("status")
    if status not in _ALLOWED_STATUSES:
        errors.append(f"{path}.status: invalid status")
    http_status = value.get("http_status")
    validated_http_status = _validate_number(
        http_status, f"{path}.http_status", errors, nullable=True, integer=True
    )
    if validated_http_status is not None and not 100 <= validated_http_status <= 599:
        errors.append(f"{path}.http_status: outside HTTP status range")

    token_values: dict[str, int | None] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        token = _validate_number(
            value.get(key), f"{path}.{key}", errors, nullable=True, integer=True
        )
        token_values[key] = token if isinstance(token, int) else None
    if all(token_values[key] is not None for key in token_values):
        if (
            token_values["prompt_tokens"] + token_values["completion_tokens"]
            != token_values["total_tokens"]
        ):
            errors.append(f"{path}.total_tokens: token accounting mismatch")

    finish_reason = value.get("finish_reason")
    if finish_reason is not None:
        _validate_identifier(finish_reason, f"{path}.finish_reason", errors)

    timing: dict[str, float | int | None] = {}
    for key in ("elapsed_seconds", "ttft_seconds", "last_content_seconds"):
        timing[key] = _validate_number(
            value.get(key),
            f"{path}.{key}",
            errors,
            nullable=key != "elapsed_seconds",
        )
    elapsed = timing["elapsed_seconds"]
    ttft = timing["ttft_seconds"]
    last_content = timing["last_content_seconds"]
    if ttft is not None and last_content is None:
        errors.append(f"{path}.last_content_seconds: required when TTFT is present")
    if last_content is not None and ttft is None:
        errors.append(f"{path}.ttft_seconds: required when last content is present")
    if ttft is not None and last_content is not None and last_content < ttft:
        errors.append(f"{path}: last content precedes TTFT")
    if elapsed is not None and ttft is not None and ttft > elapsed:
        errors.append(f"{path}: TTFT exceeds elapsed time")
    if elapsed is not None and last_content is not None and last_content > elapsed:
        errors.append(f"{path}: last content exceeds elapsed time")

    for key in (
        "client_decode_tokens_per_second",
        "end_to_end_output_tokens_per_second",
        "effective_prompt_tokens_per_second",
        "server_decode_tokens_per_second",
    ):
        _validate_number(value.get(key), f"{path}.{key}", errors, nullable=True)
    if value.get("server_decode_tokens_per_second") is not None:
        errors.append(
            f"{path}.server_decode_tokens_per_second: server rate is batch-scoped"
        )

    performance_rate_keys = (
        "client_decode_tokens_per_second",
        "end_to_end_output_tokens_per_second",
        "effective_prompt_tokens_per_second",
    )
    if status == "ok" and isinstance(elapsed, (int, float)):
        expected_rates = derive_rates(
            prompt_tokens=token_values["prompt_tokens"],
            completion_tokens=token_values["completion_tokens"],
            elapsed_seconds=float(elapsed),
            ttft_seconds=None if ttft is None else float(ttft),
            last_content_seconds=None
            if last_content is None
            else float(last_content),
        )
        for key in performance_rate_keys:
            expected = expected_rates[key]
            actual = value.get(key)
            if expected is None:
                if actual is not None:
                    errors.append(f"{path}.{key}: rate should be null")
            else:
                actual_number = _bounded_float(actual)
                if actual_number is None:
                    errors.append(f"{path}.{key}: rate is missing or invalid")
                elif not math.isclose(
                    actual_number, expected, rel_tol=1e-9, abs_tol=1e-12
                ):
                    errors.append(f"{path}.{key}: formula mismatch")
    elif status in _ALLOWED_STATUSES and any(
        value.get(key) is not None for key in performance_rate_keys
    ):
        errors.append(f"{path}: unsuccessful observation has performance rates")

    error_category = value.get("error_category")
    if error_category is not None and error_category not in _ALLOWED_ERROR_CATEGORIES:
        errors.append(f"{path}.error_category: invalid coarse category")
    if status == "ok":
        if (
            validated_http_status is None
            or not 200 <= validated_http_status < 300
        ):
            errors.append(f"{path}.http_status: success requires HTTP 2xx")
        if any(token_values[key] is None for key in token_values):
            errors.append(f"{path}: success requires complete usage counts")
        if ttft is None or last_content is None:
            errors.append(f"{path}: success requires content timing")
    if status == "ok" and error_category is not None:
        errors.append(f"{path}: successful observation has an error category")
    if status != "ok" and error_category is None:
        errors.append(f"{path}: unsuccessful observation lacks an error category")
    if repetition is None:
        return


def _validate_metric_name_allowlist(
    value: object, path: str, errors: list[str]
) -> set[str]:
    if not isinstance(value, list):
        errors.append(f"{path}: expected metric-name list")
        return set()
    retained: list[str] = []
    for index, name in enumerate(value):
        if not isinstance(name, str) or _METRIC_NAME_RE.fullmatch(name) is None:
            errors.append(f"{path}[{index}]: invalid metric name")
        else:
            retained.append(name)
    if len(retained) != len(set(retained)):
        errors.append(f"{path}: duplicate metric name")
    if retained != sorted(retained):
        errors.append(f"{path}: metric names must be sorted")
    return set(retained)


def _validate_metric_values(
    value: object,
    path: str,
    errors: list[str],
    metric_name_allowlist: set[str],
) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{path}: expected metric mapping")
        return
    for name, metric_value in value.items():
        if not isinstance(name, str) or _METRIC_NAME_RE.fullmatch(name) is None:
            errors.append(f"{path}: invalid metric name")
            continue
        if name not in metric_name_allowlist:
            errors.append(f"{path}.{name}: metric name is not in the allowlist")
        _validate_number(metric_value, f"{path}.{name}", errors, nullable=True)


def _validate_batch(
    value: object,
    path: str,
    errors: list[str],
    metric_name_allowlist: set[str],
) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{path}: expected batch object")
        return
    keys = set(value)
    for key in sorted(keys - _BATCH_KEYS, key=str):
        errors.append(f"{path}.{key}: key is not allowlisted")
    for key in sorted(_BATCH_KEYS - keys):
        errors.append(f"{path}.{key}: required key is missing")
    for key in ("route", "case_id", "candidate_id", "phase_id", "attempt_id"):
        _validate_identifier(value.get(key), f"{path}.{key}", errors)

    workload_numbers: dict[str, int | None] = {}
    for key in (
        "requested_concurrency",
        "request_count",
        "target_prompt_tokens",
        "max_output_tokens",
    ):
        item = _validate_number(
            value.get(key), f"{path}.{key}", errors, integer=True, positive=True
        )
        workload_numbers[key] = item if isinstance(item, int) else None
    if value.get("decode_mode") not in _ALLOWED_DECODE_MODES:
        errors.append(f"{path}.decode_mode: invalid decode mode")
    if value.get("counting_mode") not in _ALLOWED_COUNTING_MODES:
        errors.append(f"{path}.counting_mode: invalid counting mode")

    counts: dict[str, int | None] = {}
    for key in (
        "attempted_count",
        "successful_count",
        "failed_count",
        "timed_out_count",
        "cancelled_count",
        "total_prompt_tokens",
        "total_completion_tokens",
        "total_tokens",
    ):
        item = _validate_number(
            value.get(key), f"{path}.{key}", errors, integer=True
        )
        counts[key] = item if isinstance(item, int) else None
    if (
        workload_numbers["request_count"] is not None
        and counts["attempted_count"] is not None
        and workload_numbers["request_count"] != counts["attempted_count"]
    ):
        errors.append(f"{path}.request_count: does not match attempted count")
    if all(
        counts[key] is not None
        for key in (
            "attempted_count",
            "successful_count",
            "failed_count",
            "cancelled_count",
        )
    ) and (
        counts["successful_count"]
        + counts["failed_count"]
        + counts["cancelled_count"]
        != counts["attempted_count"]
    ):
        errors.append(f"{path}: batch outcome counts do not match attempts")
    if (
        counts["timed_out_count"] is not None
        and counts["failed_count"] is not None
        and counts["timed_out_count"] > counts["failed_count"]
    ):
        errors.append(f"{path}: timeout count exceeds failed count")
    if all(
        counts[key] is not None
        for key in ("total_prompt_tokens", "total_completion_tokens", "total_tokens")
    ) and (
        counts["total_prompt_tokens"] + counts["total_completion_tokens"]
        != counts["total_tokens"]
    ):
        errors.append(f"{path}.total_tokens: batch token accounting mismatch")

    wall = _validate_number(
        value.get("batch_wall_seconds"),
        f"{path}.batch_wall_seconds",
        errors,
        positive=True,
    )
    output_rate = _validate_number(
        value.get("aggregate_output_tokens_per_second"),
        f"{path}.aggregate_output_tokens_per_second",
        errors,
        nullable=True,
    )
    successful_count = counts["successful_count"]
    completion_total = counts["total_completion_tokens"]
    successful_token_totals = (
        counts["total_prompt_tokens"],
        counts["total_completion_tokens"],
        counts["total_tokens"],
    )
    if successful_count == 0:
        if any(value not in (0, None) for value in successful_token_totals):
            errors.append(f"{path}: successful token totals must be zero")
        if value.get("aggregate_output_tokens_per_second") is not None:
            errors.append(
                f"{path}.aggregate_output_tokens_per_second: successful output rate must be null"
            )
    elif wall is not None and completion_total is not None:
        expected = completion_total / float(wall)
        if not isinstance(output_rate, (int, float)) or not math.isclose(
            float(output_rate), expected, rel_tol=1e-9, abs_tol=1e-12
        ):
            errors.append(
                f"{path}.aggregate_output_tokens_per_second: formula mismatch"
            )
    counter_deltas = value.get("counter_deltas")
    _validate_metric_values(
        counter_deltas,
        f"{path}.counter_deltas",
        errors,
        metric_name_allowlist,
    )
    _validate_metric_values(
        value.get("gauge_peaks"),
        f"{path}.gauge_peaks",
        errors,
        metric_name_allowlist,
    )

    count_metric = value.get("server_decode_count_metric")
    sum_metric = value.get("server_decode_sum_metric")
    for metric_name, key in (
        (count_metric, "server_decode_count_metric"),
        (sum_metric, "server_decode_sum_metric"),
    ):
        if metric_name is not None and (
            not isinstance(metric_name, str)
            or _METRIC_NAME_RE.fullmatch(metric_name) is None
        ):
            errors.append(f"{path}.{key}: invalid metric name")
        elif isinstance(metric_name, str) and metric_name not in metric_name_allowlist:
            errors.append(f"{path}.{key}: metric name is not in the allowlist")
    count_delta = _validate_number(
        value.get("server_decode_count_delta"),
        f"{path}.server_decode_count_delta",
        errors,
        nullable=True,
    )
    sum_delta = _validate_number(
        value.get("server_decode_sum_delta"),
        f"{path}.server_decode_sum_delta",
        errors,
        nullable=True,
    )
    server_rate = _validate_number(
        value.get("server_decode_tokens_per_second"),
        f"{path}.server_decode_tokens_per_second",
        errors,
        nullable=True,
    )
    if (count_metric is None) != (sum_metric is None):
        errors.append(f"{path}: server decode metric names must be paired")
    if count_metric is None and sum_metric is None:
        if count_delta is not None or sum_delta is not None or server_rate is not None:
            errors.append(f"{path}: server decode values require metric names")
    elif isinstance(count_metric, str) and isinstance(sum_metric, str):
        if not isinstance(counter_deltas, Mapping):
            errors.append(f"{path}: server decode provenance is missing")
        else:
            if count_metric not in counter_deltas or sum_metric not in counter_deltas:
                errors.append(f"{path}: server decode counters are missing")
            for metric_name, dedicated, key in (
                (count_metric, count_delta, "server_decode_count_delta"),
                (sum_metric, sum_delta, "server_decode_sum_delta"),
            ):
                source = counter_deltas.get(metric_name)
                if source is None and dedicated is None:
                    continue
                source_number = _bounded_float(source)
                dedicated_number = _bounded_float(dedicated)
                if (
                    source_number is None
                    or dedicated_number is None
                    or not math.isclose(
                        source_number,
                        dedicated_number,
                        rel_tol=1e-9,
                        abs_tol=1e-12,
                    )
                ):
                    errors.append(f"{path}.{key}: counter provenance mismatch")
        expected_server_rate = derive_rates(
            None,
            None,
            0.0,
            None,
            None,
            None if count_delta is None else float(count_delta),
            None if sum_delta is None else float(sum_delta),
        )["server_decode_tokens_per_second"]
        if expected_server_rate is None:
            if server_rate is not None:
                errors.append(
                    f"{path}.server_decode_tokens_per_second: rate should be null"
                )
        elif server_rate is None or not math.isclose(
            float(server_rate),
            expected_server_rate,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            errors.append(
                f"{path}.server_decode_tokens_per_second: formula mismatch"
            )


def _candidate_observations(
    document: Mapping[str, object],
    candidate_id: str,
    *,
    phase_id: str | None = None,
    attempt_id: str | None = None,
) -> list[Mapping[str, object]]:
    observations = document.get("observations")
    if not isinstance(observations, list):
        return []
    return [
        observation
        for observation in observations
        if isinstance(observation, Mapping)
        and observation.get("candidate_id") == candidate_id
        and (phase_id is None or observation.get("phase_id") == phase_id)
        and (attempt_id is None or observation.get("attempt_id") == attempt_id)
    ]


def _candidate_batches(
    document: Mapping[str, object],
    candidate_id: str,
    *,
    phase_id: str | None = None,
    attempt_id: str | None = None,
) -> list[Mapping[str, object]]:
    batches = document.get("batches")
    if not isinstance(batches, list):
        return []
    return [
        batch
        for batch in batches
        if isinstance(batch, Mapping)
        and batch.get("candidate_id") == candidate_id
        and (phase_id is None or batch.get("phase_id") == phase_id)
        and (attempt_id is None or batch.get("attempt_id") == attempt_id)
    ]


def _integer_field(value: Mapping[str, object], key: str) -> int | None:
    return _bounded_nonnegative_integer(value.get(key))


def _finite_field(
    value: Mapping[str, object], key: str, *, positive: bool = False
) -> float | None:
    numeric = _bounded_float(value.get(key))
    if numeric is None or numeric < 0 or (positive and numeric <= 0):
        return None
    return numeric


def _validate_batch_observation_aggregates(
    document: Mapping[str, object], errors: list[str]
) -> None:
    observations = document.get("observations")
    batches = document.get("batches")
    if not isinstance(observations, list) or not isinstance(batches, list):
        return

    grouped_batches: dict[
        tuple[str, str, str, str, str], list[Mapping[str, object]]
    ] = {}
    for batch in batches:
        if not isinstance(batch, Mapping):
            continue
        key_values = (
            batch.get("route"),
            batch.get("case_id"),
            batch.get("candidate_id"),
            batch.get("phase_id"),
            batch.get("attempt_id"),
        )
        if not all(isinstance(item, str) for item in key_values):
            continue
        key = tuple(str(item) for item in key_values)
        grouped_batches.setdefault(key, []).append(batch)

    for key, scoped_batches in grouped_batches.items():
        scoped_observations = [
            observation
            for observation in observations
            if isinstance(observation, Mapping)
            and (
                observation.get("route"),
                observation.get("case_id"),
                observation.get("candidate_id"),
                observation.get("phase_id"),
                observation.get("attempt_id"),
            )
            == key
        ]
        expected = {
            "attempted_count": len(scoped_observations),
            "successful_count": sum(
                observation.get("status") == "ok"
                for observation in scoped_observations
            ),
            "failed_count": sum(
                observation.get("status") not in {"ok", "cancelled"}
                for observation in scoped_observations
            ),
            "timed_out_count": sum(
                observation.get("error_category") == "timeout"
                for observation in scoped_observations
            ),
            "cancelled_count": sum(
                observation.get("status") == "cancelled"
                for observation in scoped_observations
            ),
            "total_prompt_tokens": sum(
                _integer_field(observation, "prompt_tokens") or 0
                for observation in scoped_observations
                if observation.get("status") == "ok"
            ),
            "total_completion_tokens": sum(
                _integer_field(observation, "completion_tokens") or 0
                for observation in scoped_observations
                if observation.get("status") == "ok"
            ),
            "total_tokens": sum(
                _integer_field(observation, "total_tokens") or 0
                for observation in scoped_observations
                if observation.get("status") == "ok"
            ),
        }
        for field, expected_total in expected.items():
            actual_values = [
                _integer_field(batch, field) for batch in scoped_batches
            ]
            if any(actual is None for actual in actual_values):
                continue
            if sum(int(actual) for actual in actual_values if actual is not None) != expected_total:
                description = (
                    "successful output token totals"
                    if field.startswith("total_")
                    else "outcome counts"
                )
                errors.append(
                    f"$.batches: {description} do not match observations"
                )


def _candidate_hard_gate_status(
    observations: Sequence[Mapping[str, object]],
    batches: Sequence[Mapping[str, object]],
) -> bool | None:
    if any(observation.get("status") != "ok" for observation in observations):
        return False
    for batch in batches:
        attempted = _integer_field(batch, "attempted_count")
        successful = _integer_field(batch, "successful_count")
        failed = _integer_field(batch, "failed_count")
        timed_out = _integer_field(batch, "timed_out_count")
        cancelled = _integer_field(batch, "cancelled_count")
        if None in (attempted, successful, failed, timed_out, cancelled):
            return None
        if (
            attempted == 0
            or successful != attempted
            or failed != 0
            or timed_out != 0
            or cancelled != 0
        ):
            return False
    if not observations or not batches:
        return None

    attempted_total = sum(
        _integer_field(batch, "attempted_count") or 0 for batch in batches
    )
    successful_total = sum(
        _integer_field(batch, "successful_count") or 0 for batch in batches
    )
    if attempted_total != len(observations) or successful_total != len(observations):
        return False
    return True


def _batch_work_signature(
    batches: Sequence[Mapping[str, object]],
) -> list[tuple[str, str, int, int, int, int, str, str]] | None:
    signature: list[tuple[str, str, int, int, int, int, str, str]] = []
    for batch in batches:
        route = batch.get("route")
        case_id = batch.get("case_id")
        requested_concurrency = _integer_field(batch, "requested_concurrency")
        request_count = _integer_field(batch, "request_count")
        target_prompt_tokens = _integer_field(batch, "target_prompt_tokens")
        max_output_tokens = _integer_field(batch, "max_output_tokens")
        decode_mode = batch.get("decode_mode")
        counting_mode = batch.get("counting_mode")
        if (
            not isinstance(route, str)
            or not isinstance(case_id, str)
            or None
            in (
                requested_concurrency,
                request_count,
                target_prompt_tokens,
                max_output_tokens,
            )
            or decode_mode not in _ALLOWED_DECODE_MODES
            or counting_mode not in _ALLOWED_COUNTING_MODES
        ):
            return None
        signature.append(
            (
                route,
                case_id,
                int(requested_concurrency),
                int(request_count),
                int(target_prompt_tokens),
                int(max_output_tokens),
                str(decode_mode),
                str(counting_mode),
            )
        )
    return sorted(signature)


def _same_work_signature(
    observations: Sequence[Mapping[str, object]],
) -> list[tuple[str, str, int, int, int]] | None:
    signature: list[tuple[str, str, int, int, int]] = []
    for observation in observations:
        route = observation.get("route")
        case_id = observation.get("case_id")
        prompt_tokens = _integer_field(observation, "prompt_tokens")
        completion_tokens = _integer_field(observation, "completion_tokens")
        total_tokens = _integer_field(observation, "total_tokens")
        if (
            not isinstance(route, str)
            or not isinstance(case_id, str)
            or None in (prompt_tokens, completion_tokens, total_tokens)
        ):
            return None
        signature.append(
            (
                route,
                case_id,
                int(prompt_tokens),
                int(completion_tokens),
                int(total_tokens),
            )
        )
    return sorted(signature)


def _batch_performance_totals(
    batches: Sequence[Mapping[str, object]],
) -> tuple[int, int, int, int, float] | None:
    successful_count = 0
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    wall_seconds = 0.0
    for batch in batches:
        successful = _integer_field(batch, "successful_count")
        prompt = _integer_field(batch, "total_prompt_tokens")
        completion = _integer_field(batch, "total_completion_tokens")
        total = _integer_field(batch, "total_tokens")
        wall = _finite_field(batch, "batch_wall_seconds", positive=True)
        if None in (successful, prompt, completion, total, wall):
            return None
        successful_count += int(successful)
        prompt_tokens += int(prompt)
        completion_tokens += int(completion)
        total_tokens += int(total)
        wall_seconds += float(wall)
        if (
            successful_count > _MAX_SAFE_JSON_INTEGER
            or prompt_tokens > _MAX_SAFE_JSON_INTEGER
            or completion_tokens > _MAX_SAFE_JSON_INTEGER
            or total_tokens > _MAX_SAFE_JSON_INTEGER
            or _bounded_float(wall_seconds) is None
        ):
            return None
    return (
        successful_count,
        prompt_tokens,
        completion_tokens,
        total_tokens,
        wall_seconds,
    )


def _performance_values(
    observations: Sequence[Mapping[str, object]], key: str
) -> list[float] | None:
    values: list[float] = []
    for observation in observations:
        value = _finite_field(observation, key)
        if value is None:
            return None
        values.append(value)
    return values or None


def _derive_equal_work_performance(
    candidate_slices: Sequence[
        tuple[Sequence[Mapping[str, object]], Sequence[Mapping[str, object]]]
    ],
    comparison_observations: Sequence[Mapping[str, object]],
    comparison_batches: Sequence[Mapping[str, object]],
) -> dict[str, float] | None:
    comparison_signature = _same_work_signature(comparison_observations)
    comparison_work_signature = _batch_work_signature(comparison_batches)
    comparison_totals = _batch_performance_totals(comparison_batches)
    if (
        comparison_signature is None
        or comparison_work_signature is None
        or comparison_totals is None
    ):
        return None

    candidate_observations: list[Mapping[str, object]] = []
    candidate_batches: list[Mapping[str, object]] = []
    for observations, batches in candidate_slices:
        if (
            _candidate_hard_gate_status(observations, batches) is not True
            or _same_work_signature(observations) != comparison_signature
            or _batch_work_signature(batches) != comparison_work_signature
        ):
            return None
        totals = _batch_performance_totals(batches)
        if totals is None or totals[:4] != comparison_totals[:4]:
            return None
        candidate_observations.extend(observations)
        candidate_batches.extend(batches)

    candidate_totals = _batch_performance_totals(candidate_batches)
    if candidate_totals is None:
        return None
    multiplier = len(candidate_slices)
    expected_totals = tuple(value * multiplier for value in comparison_totals[:4])
    if (
        any(value > _MAX_SAFE_JSON_INTEGER for value in expected_totals)
        or candidate_totals[:4] != expected_totals
    ):
        return None

    comparison_rate = comparison_totals[2] / comparison_totals[4]
    candidate_rate = candidate_totals[2] / candidate_totals[4]
    if comparison_rate <= 0:
        return None
    candidate_decode = _performance_values(
        candidate_observations, "client_decode_tokens_per_second"
    )
    comparison_decode = _performance_values(
        comparison_observations, "client_decode_tokens_per_second"
    )
    candidate_ttft = _performance_values(candidate_observations, "ttft_seconds")
    comparison_ttft = _performance_values(comparison_observations, "ttft_seconds")
    if None in (
        candidate_decode,
        comparison_decode,
        candidate_ttft,
        comparison_ttft,
    ):
        return None

    comparison_decode_median = statistics.median(comparison_decode)
    candidate_decode_median = statistics.median(candidate_decode)
    comparison_ttft_p95 = nearest_rank_percentile(comparison_ttft, 95.0)
    candidate_ttft_p95 = nearest_rank_percentile(candidate_ttft, 95.0)
    if comparison_decode_median <= 0 or not comparison_ttft_p95:
        return None
    return {
        "same_work_throughput_gain_percent": (
            (candidate_rate / comparison_rate) - 1.0
        )
        * 100.0,
        "median_decode_regression_percent": (
            (comparison_decode_median - candidate_decode_median)
            / comparison_decode_median
        )
        * 100.0,
        "p95_ttft_ratio": candidate_ttft_p95 / comparison_ttft_p95,
    }


def _derive_candidate_aggregate_evidence(
    document: Mapping[str, object],
    *,
    candidate_id: str,
    phase_id: str,
    attempt_id: str,
    comparison_candidate_id: str | None,
    comparison_attempt_id: str | None,
    confirmation_attempt_ids: Sequence[str] = (),
) -> dict[str, bool | float | None]:
    current_observations = _candidate_observations(
        document,
        candidate_id,
        phase_id=phase_id,
        attempt_id=attempt_id,
    )
    current_batches = _candidate_batches(
        document,
        candidate_id,
        phase_id=phase_id,
        attempt_id=attempt_id,
    )
    hard_gates_passed = _candidate_hard_gate_status(
        current_observations, current_batches
    )
    evidence: dict[str, bool | float | None] = {
        "hard_gates_passed": hard_gates_passed,
        "same_work_throughput_gain_percent": None,
        "median_decode_regression_percent": None,
        "p95_ttft_ratio": None,
        "confirmation_completed": False,
    }
    if (
        hard_gates_passed is not True
        or comparison_candidate_id is None
        or comparison_attempt_id is None
    ):
        return evidence

    comparison_observations = _candidate_observations(
        document,
        comparison_candidate_id,
        phase_id=phase_id,
        attempt_id=comparison_attempt_id,
    )
    comparison_batches = _candidate_batches(
        document,
        comparison_candidate_id,
        phase_id=phase_id,
        attempt_id=comparison_attempt_id,
    )
    if (
        _candidate_hard_gate_status(
            comparison_observations, comparison_batches
        )
        is not True
    ):
        return evidence

    linked_attempts = list(confirmation_attempt_ids)
    if (
        any(not isinstance(item, str) for item in linked_attempts)
        or len(linked_attempts) != len(set(linked_attempts))
        or attempt_id in linked_attempts
    ):
        return evidence

    candidate_slices: list[
        tuple[list[Mapping[str, object]], list[Mapping[str, object]]]
    ] = []
    for linked_attempt_id in linked_attempts:
        linked_observations = _candidate_observations(
            document,
            candidate_id,
            phase_id=phase_id,
            attempt_id=linked_attempt_id,
        )
        linked_batches = _candidate_batches(
            document,
            candidate_id,
            phase_id=phase_id,
            attempt_id=linked_attempt_id,
        )
        linked_performance = _derive_equal_work_performance(
            [(linked_observations, linked_batches)],
            comparison_observations,
            comparison_batches,
        )
        if linked_performance is None:
            return evidence
        linked_gain = linked_performance[
            "same_work_throughput_gain_percent"
        ]
        if not (
            DECISION_THRESHOLDS["confirmation_gain_floor_percent"]
            <= linked_gain
            < DECISION_THRESHOLDS["promotion_gain_percent"]
            and linked_performance["median_decode_regression_percent"]
            <= DECISION_THRESHOLDS[
                "max_median_decode_regression_percent"
            ]
            and linked_performance["p95_ttft_ratio"]
            <= DECISION_THRESHOLDS["max_p95_ttft_ratio"]
        ):
            return evidence
        candidate_slices.append((linked_observations, linked_batches))

    candidate_slices.append((current_observations, current_batches))
    pooled = _derive_equal_work_performance(
        candidate_slices,
        comparison_observations,
        comparison_batches,
    )
    if pooled is None:
        return evidence
    evidence.update(pooled)
    evidence["confirmation_completed"] = bool(linked_attempts)
    return evidence


def _evidence_numbers_match(actual: object, expected: object) -> bool:
    if actual is None or expected is None:
        return actual is None and expected is None
    actual_number = _bounded_float(actual)
    expected_number = _bounded_float(expected)
    return (
        actual_number is not None
        and expected_number is not None
        and math.isclose(
            actual_number, expected_number, rel_tol=1e-9, abs_tol=1e-9
        )
    )


def _recompute_calculated_candidate_decisions(
    document: dict[str, object],
) -> None:
    decisions = document.get("candidate_decisions")
    if not isinstance(decisions, list):
        return
    evidence_keys = {
        "hard_gates_passed",
        "same_work_throughput_gain_percent",
        "median_decode_regression_percent",
        "p95_ttft_ratio",
        "confirmation_completed",
        "confirmation_attempt_ids",
    }
    for index, value in enumerate(decisions):
        if not isinstance(value, Mapping) or not evidence_keys.issubset(value):
            continue
        decision = dict(value)
        candidate_id = decision.get("candidate_id")
        phase_id = decision.get("phase_id")
        attempt_id = decision.get("attempt_id")
        comparison_candidate_id = decision.get("comparison_candidate_id")
        comparison_attempt_id = decision.get("comparison_attempt_id")
        confirmation_attempt_ids = decision.get("confirmation_attempt_ids")
        derived = _derive_candidate_aggregate_evidence(
            document,
            candidate_id=candidate_id if isinstance(candidate_id, str) else "",
            phase_id=phase_id if isinstance(phase_id, str) else "",
            attempt_id=attempt_id if isinstance(attempt_id, str) else "",
            comparison_candidate_id=comparison_candidate_id
            if isinstance(comparison_candidate_id, str)
            else None,
            comparison_attempt_id=comparison_attempt_id
            if isinstance(comparison_attempt_id, str)
            else None,
            confirmation_attempt_ids=confirmation_attempt_ids
            if isinstance(confirmation_attempt_ids, list)
            else (),
        )
        hard_gates_passed = derived["hard_gates_passed"]
        gain = derived["same_work_throughput_gain_percent"]
        regression = derived["median_decode_regression_percent"]
        ratio = derived["p95_ttft_ratio"]
        confirmation_completed = derived["confirmation_completed"] is True
        decision["hard_gates_passed"] = hard_gates_passed
        decision["same_work_throughput_gain_percent"] = gain
        decision["median_decode_regression_percent"] = regression
        decision["p95_ttft_ratio"] = ratio
        decision["confirmation_completed"] = confirmation_completed
        calculated = calculate_candidate_decision(
            hard_gates_passed=hard_gates_passed
            if isinstance(hard_gates_passed, bool)
            else None,
            same_work_throughput_gain_percent=gain
            if isinstance(gain, float)
            else None,
            median_decode_regression_percent=regression
            if isinstance(regression, float)
            else None,
            p95_ttft_ratio=ratio if isinstance(ratio, float) else None,
            confirmation_completed=confirmation_completed,
        )
        decision["decision"] = (
            "REJECT"
            if decision.get("operator_rejection_override") is True
            else calculated
        )
        decisions[index] = decision
    document["final_verdict"] = (
        decisions[-1].get("decision", "PENDING")
        if decisions and isinstance(decisions[-1], Mapping)
        else "PENDING"
    )


def _validate_candidate_decision(
    value: object,
    path: str,
    errors: list[str],
    *,
    document: Mapping[str, object],
) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{path}: expected candidate decision object")
        return
    core_keys = {"candidate_id", "phase_id", "attempt_id", "decision"}
    relationship_keys = {
        "comparison_candidate_id",
        "comparison_attempt_id",
    }
    evidence_keys = {
        "hard_gates_passed",
        "same_work_throughput_gain_percent",
        "median_decode_regression_percent",
        "p95_ttft_ratio",
        "confirmation_completed",
        "confirmation_attempt_ids",
    }
    override_key = "operator_rejection_override"
    allowed = core_keys | relationship_keys | evidence_keys | {override_key}
    keys = set(value)
    for key in sorted(keys - allowed, key=str):
        errors.append(f"{path}.{key}: key is not allowlisted")
    for key in (core_keys | relationship_keys) - keys:
        errors.append(f"{path}.{key}: required key is missing")
    candidate_id = value.get("candidate_id")
    phase_id = value.get("phase_id")
    attempt_id = value.get("attempt_id")
    for key, identifier in (
        ("candidate_id", candidate_id),
        ("phase_id", phase_id),
        ("attempt_id", attempt_id),
    ):
        _validate_identifier(identifier, f"{path}.{key}", errors)
    comparison_candidate_id = value.get("comparison_candidate_id")
    comparison_attempt_id = value.get("comparison_attempt_id")
    if (comparison_candidate_id is None) != (comparison_attempt_id is None):
        errors.append(f"{path}: comparison identifiers must be paired")
    for key, identifier in (
        ("comparison_candidate_id", comparison_candidate_id),
        ("comparison_attempt_id", comparison_attempt_id),
    ):
        if identifier is not None:
            _validate_identifier(identifier, f"{path}.{key}", errors)
    if (
        isinstance(candidate_id, str)
        and isinstance(phase_id, str)
        and isinstance(attempt_id, str)
        and comparison_candidate_id == candidate_id
        and comparison_attempt_id == attempt_id
    ):
        errors.append(f"{path}: decision cannot compare a slice with itself")
    decision = value.get("decision")
    if decision not in _ALLOWED_DECISIONS:
        errors.append(f"{path}.decision: invalid candidate decision")

    raw_confirmation_attempt_ids = value.get("confirmation_attempt_ids")
    confirmation_attempt_ids: list[str] = []
    if raw_confirmation_attempt_ids is not None:
        if not isinstance(raw_confirmation_attempt_ids, list):
            errors.append(f"{path}.confirmation_attempt_ids: expected list")
        else:
            for index, linked_attempt_id in enumerate(raw_confirmation_attempt_ids):
                if not isinstance(linked_attempt_id, str):
                    errors.append(
                        f"{path}.confirmation_attempt_ids[{index}]: invalid identifier"
                    )
                    continue
                _validate_identifier(
                    linked_attempt_id,
                    f"{path}.confirmation_attempt_ids[{index}]",
                    errors,
                )
                confirmation_attempt_ids.append(linked_attempt_id)
            if len(confirmation_attempt_ids) != len(set(confirmation_attempt_ids)):
                errors.append(f"{path}.confirmation_attempt_ids: duplicate attempt")
            if isinstance(attempt_id, str) and attempt_id in confirmation_attempt_ids:
                errors.append(
                    f"{path}.confirmation_attempt_ids: current attempt cannot confirm itself"
                )

    derived_evidence = _derive_candidate_aggregate_evidence(
        document,
        candidate_id=candidate_id if isinstance(candidate_id, str) else "",
        phase_id=phase_id if isinstance(phase_id, str) else "",
        attempt_id=attempt_id if isinstance(attempt_id, str) else "",
        comparison_candidate_id=comparison_candidate_id
        if isinstance(comparison_candidate_id, str)
        else None,
        comparison_attempt_id=comparison_attempt_id
        if isinstance(comparison_attempt_id, str)
        else None,
        confirmation_attempt_ids=confirmation_attempt_ids,
    )
    derived_hard_gates = derived_evidence["hard_gates_passed"]
    candidate_is_represented = (
        isinstance(candidate_id, str)
        and isinstance(phase_id, str)
        and isinstance(attempt_id, str)
        and bool(
            _candidate_observations(
                document,
                candidate_id,
                phase_id=phase_id,
                attempt_id=attempt_id,
            )
            or _candidate_batches(
                document,
                candidate_id,
                phase_id=phase_id,
                attempt_id=attempt_id,
            )
        )
    )
    if decision in {"PASS", "PROMOTE", "RETAIN"} and not candidate_is_represented:
        errors.append(
            f"{path}.decision: success decision requires represented aggregate evidence"
        )
    operator_rejection_override = value.get(override_key) is True
    if override_key in keys and value.get(override_key) is not True:
        errors.append(f"{path}.{override_key}: expected true")

    if confirmation_attempt_ids:
        decisions = document.get("candidate_decisions")
        current_index = None
        if isinstance(decisions, list):
            current_index = next(
                (index for index, item in enumerate(decisions) if item is value),
                None,
            )
        prior_relationships: set[tuple[str, str, str, object, object]] = set()
        if isinstance(decisions, list) and current_index is not None:
            for prior in decisions[:current_index]:
                if isinstance(prior, Mapping):
                    prior_scope = (
                        prior.get("candidate_id"),
                        prior.get("phase_id"),
                        prior.get("attempt_id"),
                        prior.get("comparison_candidate_id"),
                        prior.get("comparison_attempt_id"),
                    )
                    if all(isinstance(item, str) for item in prior_scope):
                        prior_relationships.add(prior_scope)
        for linked_attempt_id in confirmation_attempt_ids:
            relationship = (
                candidate_id,
                phase_id,
                linked_attempt_id,
                comparison_candidate_id,
                comparison_attempt_id,
            )
            if relationship not in prior_relationships:
                errors.append(
                    f"{path}.confirmation_attempt_ids: linked attempt is not a prior comparable candidate decision"
                )

    present_evidence = keys & evidence_keys
    if not present_evidence:
        if decision not in {"PENDING", "SKIP", "HOLD", "INVALID"}:
            errors.append(f"{path}.decision: decision evidence is required")
        if operator_rejection_override:
            errors.append(f"{path}: operator rejection requires derived evidence")
        if candidate_is_represented and (
            derived_hard_gates is not None or decision == "PASS"
        ):
            errors.append(
                f"{path}: candidate aggregate evidence requires calculated decision evidence"
            )
        return
    for key in evidence_keys - keys:
        errors.append(f"{path}.{key}: required evidence is missing")

    hard_gates_passed = value.get("hard_gates_passed")
    if hard_gates_passed is not None and not isinstance(hard_gates_passed, bool):
        errors.append(f"{path}.hard_gates_passed: expected boolean or null")
        hard_gates_passed = None
    gain = _validate_number(
        value.get("same_work_throughput_gain_percent"),
        f"{path}.same_work_throughput_gain_percent",
        errors,
        nullable=True,
        signed=True,
    )
    regression = _validate_number(
        value.get("median_decode_regression_percent"),
        f"{path}.median_decode_regression_percent",
        errors,
        nullable=True,
        signed=True,
    )
    ratio = _validate_number(
        value.get("p95_ttft_ratio"),
        f"{path}.p95_ttft_ratio",
        errors,
        nullable=True,
    )
    confirmation_completed = value.get("confirmation_completed")
    if not isinstance(confirmation_completed, bool):
        errors.append(f"{path}.confirmation_completed: expected boolean")
        confirmation_completed = False

    if hard_gates_passed is not derived_hard_gates:
        errors.append(
            f"{path}.hard_gates_passed: candidate aggregate evidence mismatch"
        )
    for key, actual in (
        ("same_work_throughput_gain_percent", gain),
        ("median_decode_regression_percent", regression),
        ("p95_ttft_ratio", ratio),
    ):
        if not _evidence_numbers_match(actual, derived_evidence[key]):
            errors.append(f"{path}.{key}: candidate aggregate evidence mismatch")
    if confirmation_completed is not (
        derived_evidence["confirmation_completed"] is True
    ):
        errors.append(
            f"{path}.confirmation_completed: candidate aggregate evidence mismatch"
        )

    expected = (
        "REJECT"
        if operator_rejection_override
        else calculate_candidate_decision(
            hard_gates_passed=hard_gates_passed,
            same_work_throughput_gain_percent=None
            if gain is None
            else float(gain),
            median_decode_regression_percent=None
            if regression is None
            else float(regression),
            p95_ttft_ratio=None if ratio is None else float(ratio),
            confirmation_completed=confirmation_completed,
        )
    )
    if decision in _ALLOWED_DECISIONS and decision != expected:
        errors.append(f"{path}.decision: formula mismatch")


def _encode_summary_identifier(components: Sequence[str]) -> str:
    return json.dumps(
        list(components), ensure_ascii=True, separators=(",", ":")
    )


def _decode_summary_identifier(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, str):
        return None
    try:
        components = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    if (
        not isinstance(components, list)
        or len(components) != 6
        or any(not isinstance(component, str) for component in components)
        or _encode_summary_identifier(components) != value
    ):
        return None
    if any(_IDENTIFIER_RE.fullmatch(component) is None for component in components[:5]):
        return None
    if components[5] not in _SUMMARY_METRICS:
        return None
    return tuple(components)


def _validate_summaries(value: object, path: str, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{path}: expected summaries object")
        return
    for summary_id, summary in value.items():
        if _decode_summary_identifier(summary_id) is None:
            errors.append(f"{path}: invalid summary identifier")
        summary_path = f"{path}.{summary_id}"
        if not isinstance(summary, Mapping):
            errors.append(f"{summary_path}: expected summary object")
            continue
        for key in sorted(set(summary) - _SUMMARY_KEYS, key=str):
            errors.append(f"{summary_path}.{key}: key is not allowlisted")
        for key, item in summary.items():
            if key == "count":
                _validate_number(
                    item, f"{summary_path}.{key}", errors, integer=True
                )
            elif key in _SUMMARY_KEYS:
                _validate_number(
                    item, f"{summary_path}.{key}", errors, nullable=True
                )


def validate_aggregate_document(document: Mapping[str, object]) -> list[str]:
    errors: list[str] = []
    if not isinstance(document, Mapping):
        return ["$: expected aggregate object"]
    _scan_forbidden(document, "$", errors)
    for key in sorted(set(document) - _ROOT_KEYS, key=str):
        errors.append(f"$.{key}: key is not allowlisted")
    for key in sorted(_ROOT_KEYS - set(document)):
        errors.append(f"$.{key}: required key is missing")
    schema_version = document.get("schema_version")
    if schema_version != SCHEMA_VERSION or isinstance(schema_version, bool):
        errors.append(f"$.schema_version: expected {SCHEMA_VERSION}")
    if "decision_policy" in document and document.get("decision_policy") != DECISION_POLICY:
        errors.append("$.decision_policy: pre-registered policy mismatch")
    if (
        "decision_thresholds" in document
        and document.get("decision_thresholds") != DECISION_THRESHOLDS
    ):
        errors.append("$.decision_thresholds: pre-registered thresholds mismatch")
    if "final_verdict" in document and document.get("final_verdict") not in _ALLOWED_DECISIONS:
        errors.append("$.final_verdict: invalid verdict")
    metric_name_allowlist = _validate_metric_name_allowlist(
        document.get("prometheus_metric_name_allowlist"),
        "$.prometheus_metric_name_allowlist",
        errors,
    )

    observations = document.get("observations", [])
    if not isinstance(observations, list):
        errors.append("$.observations: expected list")
    else:
        for index, observation in enumerate(observations):
            _validate_observation(observation, f"$.observations[{index}]", errors)

    batches = document.get("batches", [])
    if not isinstance(batches, list):
        errors.append("$.batches: expected list")
    else:
        for index, batch in enumerate(batches):
            _validate_batch(
                batch,
                f"$.batches[{index}]",
                errors,
                metric_name_allowlist,
            )
        _validate_batch_observation_aggregates(document, errors)

    represented_scopes: set[tuple[str, str, str]] = set()
    for records in (observations, batches):
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, Mapping):
                continue
            scope = (
                record.get("candidate_id"),
                record.get("phase_id"),
                record.get("attempt_id"),
            )
            if all(isinstance(item, str) for item in scope):
                represented_scopes.add(tuple(str(item) for item in scope))

    candidate_decisions = document.get("candidate_decisions", [])
    if not isinstance(candidate_decisions, list):
        errors.append("$.candidate_decisions: expected list")
    else:
        seen_scopes: set[tuple[str, str, str]] = set()
        for index, decision in enumerate(candidate_decisions):
            _validate_candidate_decision(
                decision,
                f"$.candidate_decisions[{index}]",
                errors,
                document=document,
            )
            if isinstance(decision, Mapping):
                scope = (
                    decision.get("candidate_id"),
                    decision.get("phase_id"),
                    decision.get("attempt_id"),
                )
                if all(isinstance(item, str) for item in scope):
                    normalized_scope = tuple(str(item) for item in scope)
                    if normalized_scope in seen_scopes:
                        errors.append(
                            f"$.candidate_decisions[{index}]: duplicate candidate scope"
                        )
                    seen_scopes.add(normalized_scope)
        for _scope in sorted(represented_scopes - seen_scopes):
            errors.append(
                "$.candidate_decisions: missing candidate decision for represented candidate scope"
            )
        expected_final_verdict = "PENDING"
        if candidate_decisions and isinstance(candidate_decisions[-1], Mapping):
            latest_decision = candidate_decisions[-1].get("decision")
            if latest_decision in _ALLOWED_DECISIONS:
                expected_final_verdict = latest_decision
        if document.get("final_verdict") != expected_final_verdict:
            errors.append("$.final_verdict: does not match latest candidate decision")

    summaries = document.get("summaries", {})
    _validate_summaries(summaries, "$.summaries", errors)
    if isinstance(summaries, Mapping) and summaries != _recompute_summaries(document):
        errors.append(_SUMMARY_STALE_ERROR)
    return errors


_SUMMARY_METRICS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "elapsed_seconds",
    "ttft_seconds",
    "last_content_seconds",
    "client_decode_tokens_per_second",
    "end_to_end_output_tokens_per_second",
    "effective_prompt_tokens_per_second",
    "server_decode_tokens_per_second",
)


class _AggregateFailure(Exception):
    pass


class _ArgumentParseFailure(Exception):
    pass


class _CoarseArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _ArgumentParseFailure


_SUMMARY_STALE_ERROR = "$.summaries: aggregate statistics are stale or incorrect"


def _validation_errors_except_stale_summaries(
    document: Mapping[str, object],
) -> list[str]:
    return [
        error
        for error in validate_aggregate_document(document)
        if error != _SUMMARY_STALE_ERROR
    ]


def _recompute_summaries(
    document: Mapping[str, object],
) -> dict[str, dict[str, float | int | None]]:
    grouped: dict[str, list[float]] = {}
    observations = document.get("observations", [])
    if not isinstance(observations, list):
        return {}
    for observation in observations:
        if not isinstance(observation, Mapping):
            continue
        if observation.get("status") != "ok":
            continue
        route = observation.get("route")
        case_id = observation.get("case_id")
        candidate_id = observation.get("candidate_id")
        phase_id = observation.get("phase_id")
        attempt_id = observation.get("attempt_id")
        if not all(
            isinstance(item, str)
            for item in (route, case_id, candidate_id, phase_id, attempt_id)
        ):
            continue
        for metric in _SUMMARY_METRICS:
            value = _bounded_float(observation.get(metric))
            if value is not None and value >= 0:
                summary_id = _encode_summary_identifier(
                    (
                        candidate_id,
                        phase_id,
                        attempt_id,
                        route,
                        case_id,
                        metric,
                    )
                )
                grouped.setdefault(summary_id, []).append(value)
    return {
        summary_id: summarize_values(values)
        for summary_id, values in sorted(grouped.items())
    }


def _format_report_number(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return f"{value:.6g}"
    return "null"


def build_report(document: Mapping[str, object]) -> str:
    if validate_aggregate_document(document):
        raise _AggregateFailure("aggregate validation failed")
    observations = document.get("observations", [])
    observation_list = observations if isinstance(observations, list) else []
    status_counts = {
        status: sum(
            1
            for observation in observation_list
            if isinstance(observation, Mapping)
            and observation.get("status") == status
        )
        for status in sorted(_ALLOWED_STATUSES)
    }
    lines = [
        "# Dragon Aggregate Benchmark",
        "",
        "This report contains aggregate numeric evidence only; response content is discarded during streaming.",
        "",
        "## Method",
        "",
        "Time to First Token (TTFT — delay from request start to the first content-bearing event) uses the client monotonic clock.",
        "",
        "- client decode = `(completion_tokens - 1) / (last_content - first_content)`",
        "- end-to-end output = `completion_tokens / elapsed`",
        "- effective prompt = `prompt_tokens / TTFT`",
        "- server decode = `metric_count_delta / metric_sum_delta` when available",
        "- repeated values use the arithmetic mean, population standard deviation, and nearest-rank p50/p95",
        "",
        "## Outcomes",
        "",
        f"- observations: {len(observation_list)}",
    ]
    for status, count in status_counts.items():
        lines.append(f"- {status}: {count}")

    lines.extend(
        [
            "",
            "## Aggregate statistics",
            "",
            "| Series | Count | Minimum | Maximum | Median | Mean | Population standard deviation | p50 | p95 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    summaries = document.get("summaries", {})
    if isinstance(summaries, Mapping) and summaries:
        for summary_id, summary in sorted(summaries.items(), key=lambda item: str(item[0])):
            if not isinstance(summary, Mapping):
                continue
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(summary_id),
                        _format_report_number(summary.get("count")),
                        _format_report_number(summary.get("minimum")),
                        _format_report_number(summary.get("maximum")),
                        _format_report_number(summary.get("median")),
                        _format_report_number(summary.get("mean")),
                        _format_report_number(
                            summary.get("population_standard_deviation")
                        ),
                        _format_report_number(summary.get("p50")),
                        _format_report_number(summary.get("p95")),
                    ]
                )
                + " |"
            )
    else:
        lines.append("| none | 0 | null | null | null | null | null | null | null |")

    lines.extend(["", "## Candidate decisions", ""])
    candidate_decisions = document.get("candidate_decisions", [])
    if isinstance(candidate_decisions, list) and candidate_decisions:
        for decision in candidate_decisions:
            if isinstance(decision, Mapping):
                comparison = decision.get("comparison_candidate_id")
                comparison_attempt = decision.get("comparison_attempt_id")
                comparison_text = (
                    ""
                    if comparison is None
                    else f" vs {comparison}/{comparison_attempt}"
                )
                lines.append(
                    f"- {decision.get('candidate_id')} "
                    f"[{decision.get('phase_id')}/{decision.get('attempt_id')}]"
                    f"{comparison_text}: {decision.get('decision')}"
                )
    else:
        lines.append("- none: PENDING")
    lines.extend(
        [
            "",
            f"Final verdict: {document.get('final_verdict', 'PENDING')}",
            "",
        ]
    )
    return "\n".join(lines)


def _initial_document() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "decision_policy": dict(DECISION_POLICY),
        "decision_thresholds": dict(DECISION_THRESHOLDS),
        "prometheus_metric_name_allowlist": [],
        "observations": [],
        "batches": [],
        "candidate_decisions": [],
        "summaries": {},
        "final_verdict": "PENDING",
    }


def _load_document(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    try:
        document = json.loads(payload)
    finally:
        payload = b""
    if not isinstance(document, dict):
        raise _AggregateFailure("aggregate root is invalid")
    return document


def _stage_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return temporary_path


def _remove_staged(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _atomic_write_text(path: Path, content: str) -> None:
    temporary_path = _stage_text(path, content)
    try:
        os.replace(temporary_path, path)
    except Exception:
        _remove_staged(temporary_path)
        raise


def _serialize_document(document: Mapping[str, object]) -> str:
    if validate_aggregate_document(document):
        raise _AggregateFailure("aggregate validation failed")
    try:
        return json.dumps(
            document, indent=2, sort_keys=True, allow_nan=False
        ) + "\n"
    except (OverflowError, TypeError, ValueError):
        raise _AggregateFailure("aggregate serialization failed") from None


def _write_document(path: Path, document: Mapping[str, object]) -> None:
    serialized = _serialize_document(document)
    _atomic_write_text(path, serialized)
    serialized = ""


def _metric_name_set(value: str | None) -> set[str]:
    if value is None or not value.strip():
        return set()
    names = {item.strip() for item in value.split(",") if item.strip()}
    if any(_METRIC_NAME_RE.fullmatch(name) is None for name in names):
        raise _AggregateFailure("invalid metric names")
    return names


def _validate_cli_run_inputs(
    arguments: argparse.Namespace,
    route: RouteSpec,
    requests: Sequence[RequestSpec],
    *,
    metric_names: set[str],
    counter_names: set[str],
    gauge_names: set[str],
) -> None:
    if arguments.repetitions <= 0 or arguments.concurrency <= 0:
        raise _AggregateFailure("phase sizes must be positive")
    if (
        arguments.timeout_seconds <= 0
        or not math.isfinite(arguments.timeout_seconds)
        or arguments.metric_sample_interval_seconds <= 0
        or not math.isfinite(arguments.metric_sample_interval_seconds)
    ):
        raise _AggregateFailure("intervals must be finite and positive")
    for request in requests:
        category = _request_validation_category(
            route,
            request,
            timeout_seconds=arguments.timeout_seconds,
            require_exact_count=True if arguments.require_exact_count else None,
            tokenize_url=arguments.tokenize_url,
            pinned_tokenizer=None,
        )
        if category is not None:
            raise _AggregateFailure(category)
    if route.api_key_env is not None and os.environ.get(route.api_key_env) is None:
        raise _AggregateFailure("credential unavailable")

    comparison_candidate_id = arguments.comparison_candidate_id
    comparison_attempt_id = arguments.comparison_attempt_id
    if (comparison_candidate_id is None) != (comparison_attempt_id is None):
        raise _AggregateFailure("comparison identifiers must be paired")
    for identifier in (comparison_candidate_id, comparison_attempt_id):
        if identifier is not None and _IDENTIFIER_RE.fullmatch(identifier) is None:
            raise _AggregateFailure("comparison identifiers are invalid")
    if (
        comparison_candidate_id == arguments.candidate_id
        and comparison_attempt_id == arguments.attempt_id
    ):
        raise _AggregateFailure("comparison slice must be distinct")

    if not counter_names.issubset(metric_names) or not gauge_names.issubset(
        metric_names
    ):
        raise _AggregateFailure("metric subsets are invalid")
    if arguments.metrics_url is None:
        if metric_names or counter_names or gauge_names:
            raise _AggregateFailure("metric names require an endpoint")
    elif not _valid_http_endpoint(arguments.metrics_url) or not metric_names:
        raise _AggregateFailure("metrics endpoint and allowlist are invalid")
    server_metric_names = {
        arguments.server_decode_count_metric,
        arguments.server_decode_sum_metric,
    }
    if (
        arguments.server_decode_count_metric is None
    ) != (arguments.server_decode_sum_metric is None):
        raise _AggregateFailure("server decode metrics must be paired")
    if arguments.server_decode_count_metric is not None and (
        None in server_metric_names
        or any(
            not isinstance(name, str)
            or _METRIC_NAME_RE.fullmatch(name) is None
            for name in server_metric_names
        )
        or not server_metric_names.issubset(counter_names)
        or not server_metric_names.issubset(metric_names)
    ):
        raise _AggregateFailure("server decode metrics are invalid")

    numeric_evidence = (
        arguments.same_work_throughput_gain_percent,
        arguments.median_decode_regression_percent,
        arguments.p95_ttft_ratio,
    )
    any_numeric_evidence = any(value is not None for value in numeric_evidence)
    complete_numeric_evidence = all(
        value is not None for value in numeric_evidence
    )
    if arguments.hard_gates_passed is True and not complete_numeric_evidence:
        raise _AggregateFailure("decision evidence is incomplete")
    if (
        (arguments.hard_gates_passed is True or any_numeric_evidence)
        and comparison_candidate_id is None
    ):
        raise _AggregateFailure("decision evidence requires an explicit comparison slice")
    if any_numeric_evidence and (
        arguments.hard_gates_passed is None or not complete_numeric_evidence
    ):
        raise _AggregateFailure("decision evidence is incomplete")
    confirmation_attempt_ids = arguments.confirmation_attempt_id
    if not isinstance(confirmation_attempt_ids, list) or any(
        not isinstance(item, str) or _IDENTIFIER_RE.fullmatch(item) is None
        for item in confirmation_attempt_ids
    ):
        raise _AggregateFailure("confirmation attempt identifiers are invalid")
    if (
        len(confirmation_attempt_ids) != len(set(confirmation_attempt_ids))
        or arguments.attempt_id in confirmation_attempt_ids
    ):
        raise _AggregateFailure("confirmation attempts must be distinct")
    if arguments.confirmation_completed and not confirmation_attempt_ids:
        raise _AggregateFailure("confirmation requires linked prior evidence")
    if confirmation_attempt_ids and comparison_candidate_id is None:
        raise _AggregateFailure("confirmation requires an explicit comparison slice")
    if any(
        value is not None and not math.isfinite(value)
        for value in numeric_evidence
    ):
        raise _AggregateFailure("decision evidence must be finite")
    if arguments.p95_ttft_ratio is not None and arguments.p95_ttft_ratio < 0:
        raise _AggregateFailure("TTFT ratio must be non-negative")


def _command_init(arguments: argparse.Namespace) -> int:
    result_path = Path(arguments.result)
    if result_path.exists():
        raise _AggregateFailure("result already exists")
    _write_document(result_path, _initial_document())
    print("aggregate result initialized")
    return 0


def _command_validate(arguments: argparse.Namespace) -> int:
    document = _load_document(Path(arguments.result))
    errors = validate_aggregate_document(document)
    if errors:
        print(f"invalid aggregate document ({len(errors)} errors)")
        return 1
    print("aggregate document valid")
    return 0


def _command_summarize(arguments: argparse.Namespace) -> int:
    result_path = Path(arguments.result)
    report_path = None if arguments.report is None else Path(arguments.report)
    if report_path is not None and result_path.resolve() == report_path.resolve():
        raise _AggregateFailure("result and report paths collide")
    document = _load_document(result_path)
    if _validation_errors_except_stale_summaries(document):
        raise _AggregateFailure("aggregate validation failed")
    document["summaries"] = _recompute_summaries(document)
    report = build_report(document)
    if report_path is None:
        _write_document(result_path, document)
    else:
        serialized = _serialize_document(document)
        staged_result: Path | None = None
        staged_report: Path | None = None
        try:
            staged_result = _stage_text(result_path, serialized)
            staged_report = _stage_text(report_path, report)
            os.replace(staged_result, result_path)
            staged_result = None
            os.replace(staged_report, report_path)
            staged_report = None
        finally:
            _remove_staged(staged_result)
            _remove_staged(staged_report)
        serialized = ""
    report = ""
    print("aggregate summaries updated")
    return 0


def _command_run(arguments: argparse.Namespace) -> int:
    result_path = Path(arguments.result)
    document = _load_document(result_path)
    if _validation_errors_except_stale_summaries(document):
        raise _AggregateFailure("aggregate validation failed")
    if arguments.repetitions <= 0:
        raise _AggregateFailure("repetitions must be positive")

    route = RouteSpec(
        name=arguments.route_name,
        protocol=arguments.protocol,
        url=arguments.url,
        model=arguments.model,
        api_key_env=arguments.api_key_env,
    )
    requests = [
        RequestSpec(
            case_id=arguments.case_id,
            target_prompt_tokens=arguments.target_prompt_tokens,
            max_output_tokens=arguments.max_output_tokens,
            repetition=repetition,
            candidate_id=arguments.candidate_id,
            phase_id=arguments.phase_id,
            attempt_id=arguments.attempt_id,
        )
        for repetition in range(1, arguments.repetitions + 1)
    ]
    metric_names = _metric_name_set(arguments.metric_names)
    counter_names = _metric_name_set(arguments.counter_names)
    gauge_names = _metric_name_set(arguments.gauge_names)
    _validate_cli_run_inputs(
        arguments,
        route,
        requests,
        metric_names=metric_names,
        counter_names=counter_names,
        gauge_names=gauge_names,
    )
    captured_metrics: list[dict[str, object]] = []
    captured_batch_timing: list[dict[str, object]] = []
    observations = run_batch(
        route,
        requests,
        concurrency=arguments.concurrency,
        timeout_seconds=arguments.timeout_seconds,
        require_exact_count=True if arguments.require_exact_count else None,
        tokenize_url=arguments.tokenize_url,
        metrics_url=arguments.metrics_url,
        metric_names=metric_names,
        counter_names=counter_names,
        gauge_names=gauge_names,
        server_decode_count_metric=arguments.server_decode_count_metric,
        server_decode_sum_metric=arguments.server_decode_sum_metric,
        metric_sample_interval_seconds=arguments.metric_sample_interval_seconds,
        metrics_callback=captured_metrics.append if arguments.metrics_url else None,
        batch_callback=captured_batch_timing.append,
    )
    batch_wall_seconds = (
        float(captured_batch_timing[-1]["batch_wall_seconds"])
        if captured_batch_timing
        else 0.0
    )
    if batch_wall_seconds <= 0:
        batch_wall_seconds = 1e-9

    persisted_metric_names = document.get("prometheus_metric_name_allowlist")
    if not isinstance(persisted_metric_names, list):
        raise _AggregateFailure("metric-name allowlist is invalid")
    document["prometheus_metric_name_allowlist"] = sorted(
        set(persisted_metric_names) | metric_names
    )

    observation_documents = document.get("observations")
    if not isinstance(observation_documents, list):
        raise _AggregateFailure("observations are invalid")
    observation_documents.extend(asdict(observation) for observation in observations)

    decisions = document.get("candidate_decisions")
    if not isinstance(decisions, list):
        raise _AggregateFailure("candidate decisions are invalid")
    comparison_candidate_id = arguments.comparison_candidate_id
    comparison_attempt_id = arguments.comparison_attempt_id
    confirmation_attempt_ids = list(arguments.confirmation_attempt_id)

    successful_count = sum(item.status == "ok" for item in observations)
    cancelled_count = sum(item.status == "cancelled" for item in observations)
    failed_count = len(observations) - successful_count - cancelled_count
    timed_out_count = sum(item.error_category == "timeout" for item in observations)
    successful_observations = [
        item for item in observations if item.status == "ok"
    ]
    total_prompt_tokens = sum(
        item.prompt_tokens or 0 for item in successful_observations
    )
    total_completion_tokens = sum(
        item.completion_tokens or 0 for item in successful_observations
    )
    metric_summary = captured_metrics[-1] if captured_metrics else {}
    counter_deltas = metric_summary.get("counter_deltas", {})
    gauge_peaks = metric_summary.get("gauge_peaks", {})
    server_decode_count_metric = metric_summary.get(
        "server_decode_count_metric", arguments.server_decode_count_metric
    )
    server_decode_sum_metric = metric_summary.get(
        "server_decode_sum_metric", arguments.server_decode_sum_metric
    )
    server_decode_count_delta = metric_summary.get("server_decode_count_delta")
    server_decode_sum_delta = metric_summary.get("server_decode_sum_delta")
    server_decode_rate = metric_summary.get("server_decode_tokens_per_second")
    batches = document.get("batches")
    if not isinstance(batches, list):
        raise _AggregateFailure("batches are invalid")
    batches.append(
        {
            "route": arguments.route_name,
            "case_id": arguments.case_id,
            "candidate_id": arguments.candidate_id,
            "phase_id": arguments.phase_id,
            "attempt_id": arguments.attempt_id,
            "requested_concurrency": arguments.concurrency,
            "request_count": len(requests),
            "target_prompt_tokens": arguments.target_prompt_tokens,
            "max_output_tokens": arguments.max_output_tokens,
            "decode_mode": f"{route.protocol}_sse",
            "counting_mode": (
                "tokenize_exact" if arguments.tokenize_url else "declared"
            ),
            "attempted_count": len(observations),
            "successful_count": successful_count,
            "failed_count": failed_count,
            "timed_out_count": timed_out_count,
            "cancelled_count": cancelled_count,
            "total_prompt_tokens": total_prompt_tokens,
            "total_completion_tokens": total_completion_tokens,
            "total_tokens": total_prompt_tokens + total_completion_tokens,
            "batch_wall_seconds": batch_wall_seconds,
            "aggregate_output_tokens_per_second": (
                None
                if successful_count == 0
                else total_completion_tokens / batch_wall_seconds
            ),
            "counter_deltas": counter_deltas,
            "gauge_peaks": gauge_peaks,
            "server_decode_count_metric": server_decode_count_metric,
            "server_decode_sum_metric": server_decode_sum_metric,
            "server_decode_count_delta": server_decode_count_delta,
            "server_decode_sum_delta": server_decode_sum_delta,
            "server_decode_tokens_per_second": server_decode_rate,
        }
    )
    document["summaries"] = _recompute_summaries(document)
    derived_evidence = _derive_candidate_aggregate_evidence(
        document,
        candidate_id=arguments.candidate_id,
        phase_id=arguments.phase_id,
        attempt_id=arguments.attempt_id,
        comparison_candidate_id=comparison_candidate_id,
        comparison_attempt_id=comparison_attempt_id,
        confirmation_attempt_ids=confirmation_attempt_ids,
    )
    derived_hard_gates = derived_evidence["hard_gates_passed"]
    derived_confirmation_completed = (
        derived_evidence["confirmation_completed"] is True
    )
    if confirmation_attempt_ids and not derived_confirmation_completed:
        raise _AggregateFailure("confirmation evidence does not match aggregates")
    if arguments.confirmation_completed and not derived_confirmation_completed:
        raise _AggregateFailure("claimed confirmation does not match aggregates")
    if arguments.hard_gates_passed is True and derived_hard_gates is not True:
        raise _AggregateFailure("claimed hard-gate evidence does not match aggregates")
    claimed_performance = {
        "same_work_throughput_gain_percent": (
            arguments.same_work_throughput_gain_percent
        ),
        "median_decode_regression_percent": (
            arguments.median_decode_regression_percent
        ),
        "p95_ttft_ratio": arguments.p95_ttft_ratio,
    }
    for key, claimed in claimed_performance.items():
        if claimed is not None and not _evidence_numbers_match(
            claimed, derived_evidence[key]
        ):
            raise _AggregateFailure("claimed performance evidence does not match aggregates")

    operator_rejection_override = arguments.hard_gates_passed is False
    effective_hard_gates = derived_hard_gates
    if effective_hard_gates is True:
        throughput_gain = derived_evidence[
            "same_work_throughput_gain_percent"
        ]
        decode_regression = derived_evidence[
            "median_decode_regression_percent"
        ]
        ttft_ratio = derived_evidence["p95_ttft_ratio"]
    else:
        throughput_gain = None
        decode_regression = None
        ttft_ratio = None
    calculated_decision = calculate_candidate_decision(
        hard_gates_passed=effective_hard_gates,
        same_work_throughput_gain_percent=throughput_gain
        if isinstance(throughput_gain, float)
        else None,
        median_decode_regression_percent=decode_regression
        if isinstance(decode_regression, float)
        else None,
        p95_ttft_ratio=ttft_ratio if isinstance(ttft_ratio, float) else None,
        confirmation_completed=derived_confirmation_completed,
    )
    candidate_decision = (
        "REJECT" if operator_rejection_override else calculated_decision
    )
    decision_record: dict[str, object] = {
        "candidate_id": arguments.candidate_id,
        "phase_id": arguments.phase_id,
        "attempt_id": arguments.attempt_id,
        "comparison_candidate_id": comparison_candidate_id,
        "comparison_attempt_id": comparison_attempt_id,
        "decision": candidate_decision,
        "hard_gates_passed": effective_hard_gates,
        "same_work_throughput_gain_percent": throughput_gain,
        "median_decode_regression_percent": decode_regression,
        "p95_ttft_ratio": ttft_ratio,
        "confirmation_completed": derived_confirmation_completed,
        "confirmation_attempt_ids": confirmation_attempt_ids,
    }
    if operator_rejection_override:
        decision_record["operator_rejection_override"] = True
    existing_index = next(
        (
            index
            for index, decision in enumerate(decisions)
            if isinstance(decision, Mapping)
            and decision.get("candidate_id") == arguments.candidate_id
            and decision.get("phase_id") == arguments.phase_id
            and decision.get("attempt_id") == arguments.attempt_id
        ),
        None,
    )
    if existing_index is None:
        decisions.append(decision_record)
    else:
        decisions[existing_index] = decision_record
    _recompute_calculated_candidate_decisions(document)
    _write_document(result_path, document)
    print(f"run recorded ({len(observations)} observations)")
    return 0


def _argument_parser() -> argparse.ArgumentParser:
    parser = _CoarseArgumentParser(
        description="Aggregate-only local Dragon benchmark harness"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--result", required=True)
    init_parser.set_defaults(handler=_command_init)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--result", required=True)
    run_parser.add_argument("--route-name", "--route", dest="route_name", required=True)
    run_parser.add_argument(
        "--protocol", choices=("openai", "anthropic"), required=True
    )
    run_parser.add_argument("--url", required=True)
    run_parser.add_argument("--model", required=True)
    run_parser.add_argument("--api-key-env")
    run_parser.add_argument("--case-id", "--case", dest="case_id", required=True)
    run_parser.add_argument(
        "--candidate-id", "--candidate", dest="candidate_id", required=True
    )
    run_parser.add_argument("--phase-id", default="default")
    run_parser.add_argument("--attempt-id", default="1")
    run_parser.add_argument("--comparison-candidate-id")
    run_parser.add_argument("--comparison-attempt-id")
    run_parser.add_argument("--target-prompt-tokens", type=int, required=True)
    run_parser.add_argument("--max-output-tokens", type=int, required=True)
    run_parser.add_argument("--repetitions", type=int, default=1)
    run_parser.add_argument("--concurrency", type=int, default=1)
    run_parser.add_argument("--timeout-seconds", type=float, default=600.0)
    run_parser.add_argument("--require-exact-count", action="store_true")
    hard_gate_group = run_parser.add_mutually_exclusive_group()
    hard_gate_group.add_argument(
        "--hard-gates-passed",
        dest="hard_gates_passed",
        action="store_true",
    )
    hard_gate_group.add_argument(
        "--hard-gates-failed",
        dest="hard_gates_passed",
        action="store_false",
    )
    run_parser.set_defaults(hard_gates_passed=None)
    run_parser.add_argument("--same-work-throughput-gain-percent", type=float)
    run_parser.add_argument("--median-decode-regression-percent", type=float)
    run_parser.add_argument("--p95-ttft-ratio", type=float)
    run_parser.add_argument("--confirmation-completed", action="store_true")
    run_parser.add_argument(
        "--confirmation-attempt-id", action="append", default=[]
    )
    run_parser.add_argument("--tokenize-url")
    run_parser.add_argument("--metrics-url")
    run_parser.add_argument("--metric-names")
    run_parser.add_argument("--counter-names")
    run_parser.add_argument("--gauge-names")
    run_parser.add_argument("--server-decode-count-metric")
    run_parser.add_argument("--server-decode-sum-metric")
    run_parser.add_argument(
        "--metric-sample-interval-seconds", type=float, default=0.1
    )
    run_parser.set_defaults(handler=_command_run)

    summarize_parser = subparsers.add_parser("summarize")
    summarize_parser.add_argument("--result", required=True)
    summarize_parser.add_argument("--report")
    summarize_parser.set_defaults(handler=_command_summarize)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--result", required=True)
    validate_parser.set_defaults(handler=_command_validate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        parser = _argument_parser()
        arguments = parser.parse_args(argv)
        return arguments.handler(arguments)
    except (
        _AggregateFailure,
        _ArgumentParseFailure,
        json.JSONDecodeError,
        OSError,
        OverflowError,
        RuntimeError,
        ValueError,
    ):
        print("benchmark operation failed", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
