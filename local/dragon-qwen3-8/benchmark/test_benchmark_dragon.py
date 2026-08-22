from __future__ import annotations

import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from benchmark_dragon import (
    Observation,
    RequestSpec,
    RouteSpec,
    aggregate_prometheus_samples,
    build_report,
    calculate_candidate_decision,
    derive_rates,
    main,
    nearest_rank_percentile,
    parse_anthropic_sse,
    parse_openai_sse,
    parse_prometheus_allowlist,
    run_batch,
    run_request,
    summarize_values,
    validate_aggregate_document,
)


def direct_child_process_ids() -> set[int]:
    children: set[int] = set()
    proc = Path("/proc")
    if not proc.is_dir():
        return children
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status_lines = (entry / "status").read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for line in status_lines:
            if line.startswith("PPid:"):
                if int(line.split()[1]) == os.getpid():
                    children.add(int(entry.name))
                break
    return children


class SequenceClock:
    def __init__(self, *values: int) -> None:
        self._values = iter(values)

    def __call__(self) -> int:
        return next(self._values)


def sse_data(document: object, event: str | None = None) -> bytes:
    prefix = b"" if event is None else f"event: {event}\n".encode("ascii")
    payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
    return prefix + b"data: " + payload + b"\n\n"


def openai_usage_events(
    prompt_tokens: int, completion_tokens: int = 2
) -> list[bytes]:
    return [
        sse_data(
            {
                "choices": [
                    {"delta": {"content": "x"}, "finish_reason": None}
                ]
            }
        ),
        sse_data(
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
        ),
        b"data: [DONE]\n\n",
    ]


class StaticStreamResponse:
    status = 200

    def __init__(self, events: list[bytes]) -> None:
        self._events = events
        self.closed = False

    def __enter__(self) -> "StaticStreamResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __iter__(self):
        return iter(self._events)

    def close(self) -> None:
        self.closed = True


class LocalFixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        if not self.path.startswith("/tokenize"):
            with self.server.state_lock:
                self.server.request_start_times.append(time.perf_counter())
                self.server.request_events.append("inference")
        content_length = int(self.headers.get("Content-Length", "0"))
        request_payload = self.rfile.read(content_length) if content_length else b""
        if not self.path.startswith("/tokenize") and request_payload:
            request_shape = json.loads(request_payload)
            with self.server.state_lock:
                self.server.saw_tools = self.server.saw_tools or "tools" in request_shape
                self.server.openai_thinking_disabled = (
                    self.server.openai_thinking_disabled
                    or request_shape.get("chat_template_kwargs")
                    == {"enable_thinking": False}
                )
            request_shape = {}
        request_payload = b""
        with self.server.state_lock:
            self.server.post_count += 1

        if self.path == "/error":
            self._send(503, b"body-not-for-observation", "text/plain")
            return
        if self.path == "/invalid":
            self._send(200, b"data: not-json\n\n", "text/event-stream")
            return
        if self.path == "/missing-usage":
            payload = b"".join(
                [
                    sse_data(
                        {
                            "choices": [
                                {
                                    "delta": {"content": "discard-local"},
                                    "finish_reason": None,
                                }
                            ]
                        }
                    ),
                    sse_data(
                        {
                            "choices": [
                                {"delta": {}, "finish_reason": "stop"}
                            ]
                        }
                    ),
                    b"data: [DONE]\n\n",
                ]
            )
            self._send(200, payload, "text/event-stream")
            return
        if self.path.startswith("/tokenize"):
            with self.server.state_lock:
                tokenize_call = self.server.tokenize_calls
                self.server.tokenize_calls += 1
                self.server.request_events.append("tokenize")
            if self.path == "/tokenize-asymmetric" and tokenize_call == 0:
                time.sleep(0.15)
            if self.path == "/tokenize-drip":
                self._start_body("application/json")
                for byte in b'{"count":8}':
                    try:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    time.sleep(0.03)
                return
            if self.path == "/tokenize-stall":
                self._start_body("application/json")
                time.sleep(1.0)
                return
            self._send(200, b'{"count":8}', "application/json")
            return
        if self.path == "/drip":
            self._start_stream()
            for _ in range(12):
                try:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                time.sleep(0.03)
            try:
                self.wfile.write(self._openai_success_payload())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if self.path == "/stall":
            self._start_stream()
            time.sleep(1.0)
            return
        if self.path in {"/slow-ok", "/slow-timeout"}:
            time.sleep(0.12)
        if self.path in {"/ok-openai", "/slow-ok", "/slow-timeout"}:
            self._send(200, self._openai_success_payload(), "text/event-stream")
            return
        self._send(404, b"not-found-body", "text/plain")

    def do_GET(self) -> None:
        if self.path not in {"/metrics", "/metrics-drip", "/metrics-stall"}:
            self._send(404, b"not-found-metrics", "text/plain")
            return
        with self.server.state_lock:
            call = self.server.metric_calls
            self.server.metric_calls += 1
        if self.path == "/metrics-drip":
            self._start_body("text/plain")
            for byte in b"approved_metric 1\n":
                try:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                time.sleep(0.03)
            return
        if self.path == "/metrics-stall":
            self._start_body("text/plain")
            time.sleep(1.0)
            return
        if call == 0:
            counter, running, waiting = 10, 0, 0
            decode_count, decode_sum = 100, 20.0
        elif call == 1:
            counter, running, waiting = 11, 2, 3
            decode_count, decode_sum = 102, 20.5
        else:
            counter, running, waiting = 14, 1, 1
            decode_count, decode_sum = 106, 22.0
        payload = (
            "# HELP private_marker body-not-for-results\n"
            f'vllm:request_success_total{{model_name="fixture"}} {counter}\n'
            f"vllm:num_requests_running {running}\n"
            f"vllm:num_requests_waiting {waiting}\n"
            f"vllm:time_per_output_token_seconds_count {decode_count}\n"
            f"vllm:time_per_output_token_seconds_sum {decode_sum}\n"
            "unlisted_metric 999\n"
        ).encode("ascii")
        self._send(200, payload, "text/plain")

    def _start_body(self, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True

    def _start_stream(self) -> None:
        self._start_body("text/event-stream")

    @staticmethod
    def _openai_success_payload() -> bytes:
        return b"".join(
            [
                sse_data(
                    {
                        "choices": [
                            {
                                "delta": {"content": "discard-success"},
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
                sse_data(
                    {
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {
                            "prompt_tokens": 8,
                            "completion_tokens": 2,
                            "total_tokens": 10,
                        },
                    }
                ),
                b"data: [DONE]\n\n",
            ]
        )

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


class LocalHTTPFixture:
    def __enter__(self) -> "LocalHTTPFixture":
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), LocalFixtureHandler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.server.state_lock = threading.Lock()
        self.server.post_count = 0
        self.server.metric_calls = 0
        self.server.tokenize_calls = 0
        self.server.request_start_times = []
        self.server.request_events = []
        self.server.saw_tools = False
        self.server.openai_thinking_disabled = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def url(self, path: str) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}{path}"


class StreamParsingTests(unittest.TestCase):
    def test_openai_uses_only_content_events_for_first_and_last_content(self) -> None:
        events = [
            sse_data(
                {
                    "choices": [
                        {"delta": {"role": "assistant"}, "finish_reason": None}
                    ]
                }
            ),
            sse_data(
                {
                    "choices": [
                        {
                            "delta": {"content": "discard-me-one"},
                            "finish_reason": None,
                        }
                    ]
                }
            ),
            sse_data(
                {
                    "choices": [
                        {
                            "delta": {"content": "discard-me-two"},
                            "finish_reason": None,
                        }
                    ]
                }
            ),
            sse_data(
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 2,
                        "total_tokens": 13,
                    },
                }
            ),
        ]

        result = parse_openai_sse(events, SequenceClock(100, 200, 300, 400))

        self.assertEqual(result.first_content_ns, 200)
        self.assertEqual(result.last_content_ns, 300)
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.prompt_tokens, 11)
        self.assertEqual(result.completion_tokens, 2)
        self.assertEqual(result.total_tokens, 13)
        serialized = json.dumps(asdict(result), sort_keys=True)
        self.assertNotIn("discard-me-one", serialized)
        self.assertNotIn("discard-me-two", serialized)

    def test_openai_parser_handles_crlf_boundaries_split_across_chunks(self) -> None:
        wire = b"".join(
            [
                sse_data(
                    {
                        "choices": [
                            {
                                "delta": {"content": "discard-fragment"},
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
                sse_data(
                    {
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {
                            "prompt_tokens": 3,
                            "completion_tokens": 1,
                            "total_tokens": 4,
                        },
                    }
                ),
            ]
        ).replace(b"\n", b"\r\n")

        result = parse_openai_sse(
            [bytes([value]) for value in wire], SequenceClock(10, 20)
        )

        self.assertEqual(result.first_content_ns, 10)
        self.assertEqual(result.last_content_ns, 10)
        self.assertEqual(result.total_tokens, 4)
        self.assertNotIn("discard-fragment", json.dumps(asdict(result)))

    def test_openai_parser_joins_fragmented_multiple_data_lines(self) -> None:
        wire = b"".join(
            [
                b'data: {"choices":\n',
                b'data: [{"delta":{"content":"discard-multiline"},"finish_reason":null}]}\n\n',
                b'data: {"choices":[],\n',
                b'data: "usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}\n\n',
            ]
        )

        result = parse_openai_sse(
            [bytes([value]) for value in wire], SequenceClock(10, 20)
        )

        self.assertEqual(result.first_content_ns, 10)
        self.assertEqual(result.last_content_ns, 10)
        self.assertEqual(result.total_tokens, 4)
        self.assertNotIn("discard-multiline", json.dumps(asdict(result)))

    def test_anthropic_parser_supports_carriage_return_only_event_lines(self) -> None:
        wire = b"".join(
            [
                b'event: message_start\rdata: {"type":"message_start","message":{"usage":{"input_tokens":3}}}\r\r',
                b'event: content_block_delta\rdata: {"type":"content_block_delta",\r',
                b'data: "delta":{"type":"text_delta","text":"discard-carriage"}}\r\r',
                b'event: message_delta\rdata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\r\r',
            ]
        )

        result = parse_anthropic_sse(
            [bytes([value]) for value in wire], SequenceClock(10, 20, 30)
        )

        self.assertEqual(result.first_content_ns, 20)
        self.assertEqual(result.last_content_ns, 20)
        self.assertEqual(result.total_tokens, 4)
        self.assertNotIn("discard-carriage", json.dumps(asdict(result)))

    def test_anthropic_metadata_does_not_start_or_extend_content_timing(self) -> None:
        events = [
            sse_data(
                {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": 17}},
                },
                event="message_start",
            ),
            sse_data(
                {
                    "type": "message_delta",
                    "delta": {},
                    "usage": {"output_tokens": 0},
                },
                event="message_delta",
            ),
            sse_data(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "discard-alpha"},
                },
                event="content_block_delta",
            ),
            sse_data(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "discard-beta"},
                },
                event="content_block_delta",
            ),
            sse_data(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 2},
                },
                event="message_delta",
            ),
        ]

        result = parse_anthropic_sse(
            events, SequenceClock(1_000, 2_000, 3_000, 4_000, 5_000)
        )

        self.assertEqual(result.first_content_ns, 3_000)
        self.assertEqual(result.last_content_ns, 4_000)
        self.assertEqual(result.finish_reason, "end_turn")
        self.assertEqual(result.prompt_tokens, 17)
        self.assertEqual(result.completion_tokens, 2)
        self.assertEqual(result.total_tokens, 19)
        serialized = json.dumps(asdict(result), sort_keys=True)
        self.assertNotIn("discard-alpha", serialized)
        self.assertNotIn("discard-beta", serialized)


class FormulaAndStatisticsTests(unittest.TestCase):
    def test_derive_rates_uses_the_registered_formulas(self) -> None:
        rates = derive_rates(
            prompt_tokens=120,
            completion_tokens=5,
            elapsed_seconds=4.0,
            ttft_seconds=0.5,
            last_content_seconds=2.5,
            server_metric_count_delta=9.0,
            server_metric_sum_delta=3.0,
        )

        self.assertEqual(rates["client_decode_tokens_per_second"], 2.0)
        self.assertEqual(rates["end_to_end_output_tokens_per_second"], 1.25)
        self.assertEqual(rates["effective_prompt_tokens_per_second"], 240.0)
        self.assertEqual(rates["server_decode_tokens_per_second"], 3.0)

    def test_derive_rates_accepts_the_stable_positional_interface(self) -> None:
        rates = derive_rates(120, 5, 4.0, 0.5, 2.5, 9.0, 3.0)

        self.assertEqual(rates["client_decode_tokens_per_second"], 2.0)
        self.assertEqual(rates["server_decode_tokens_per_second"], 3.0)

    def test_derive_rates_returns_none_for_undefined_rates(self) -> None:
        rates = derive_rates(
            prompt_tokens=120,
            completion_tokens=1,
            elapsed_seconds=0.0,
            ttft_seconds=0.0,
            last_content_seconds=0.0,
            server_metric_count_delta=None,
            server_metric_sum_delta=None,
        )

        self.assertIsNone(rates["client_decode_tokens_per_second"])
        self.assertIsNone(rates["end_to_end_output_tokens_per_second"])
        self.assertIsNone(rates["effective_prompt_tokens_per_second"])
        self.assertIsNone(rates["server_decode_tokens_per_second"])

    def test_nearest_rank_and_summary_statistics_are_population_values(self) -> None:
        values = [1.0, 2.0, 3.0, 10.0]

        self.assertEqual(nearest_rank_percentile(values, 50.0), 2.0)
        self.assertEqual(nearest_rank_percentile(values, 95.0), 10.0)
        summary = summarize_values(values)
        self.assertEqual(summary["count"], 4)
        self.assertEqual(summary["minimum"], 1.0)
        self.assertEqual(summary["maximum"], 10.0)
        self.assertEqual(summary["median"], 2.5)
        self.assertEqual(summary["mean"], 4.0)
        self.assertAlmostEqual(summary["population_standard_deviation"], 12.5**0.5)
        self.assertEqual(summary["p50"], 2.0)
        self.assertEqual(summary["p95"], 10.0)

    def test_empty_summary_has_zero_count_and_null_statistics(self) -> None:
        summary = summarize_values([])

        self.assertEqual(summary["count"], 0)
        for key in (
            "minimum",
            "maximum",
            "median",
            "mean",
            "population_standard_deviation",
            "p50",
            "p95",
        ):
            self.assertIsNone(summary[key])

    def test_candidate_decisions_follow_the_pre_registered_thresholds(self) -> None:
        self.assertEqual(
            calculate_candidate_decision(
                hard_gates_passed=False,
                same_work_throughput_gain_percent=20.0,
                median_decode_regression_percent=0.0,
                p95_ttft_ratio=1.0,
            ),
            "REJECT",
        )
        self.assertEqual(
            calculate_candidate_decision(
                hard_gates_passed=True,
                same_work_throughput_gain_percent=10.0,
                median_decode_regression_percent=5.0,
                p95_ttft_ratio=1.5,
            ),
            "PROMOTE",
        )
        self.assertEqual(
            calculate_candidate_decision(
                hard_gates_passed=True,
                same_work_throughput_gain_percent=7.0,
                median_decode_regression_percent=0.0,
                p95_ttft_ratio=1.0,
            ),
            "PENDING",
        )
        self.assertEqual(
            calculate_candidate_decision(
                hard_gates_passed=True,
                same_work_throughput_gain_percent=4.9,
                median_decode_regression_percent=0.0,
                p95_ttft_ratio=1.0,
            ),
            "RETAIN",
        )

    def test_candidate_decisions_accept_legitimate_signed_deltas(self) -> None:
        self.assertEqual(
            calculate_candidate_decision(
                hard_gates_passed=True,
                same_work_throughput_gain_percent=12.0,
                median_decode_regression_percent=-2.0,
                p95_ttft_ratio=1.0,
            ),
            "PROMOTE",
        )
        self.assertEqual(
            calculate_candidate_decision(
                hard_gates_passed=True,
                same_work_throughput_gain_percent=-1.0,
                median_decode_regression_percent=-2.0,
                p95_ttft_ratio=1.0,
            ),
            "RETAIN",
        )


def decision_policy_literal() -> dict[str, str]:
    return {
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


def decision_thresholds_literal() -> dict[str, float | int]:
    return {
        "max_context_tokens": 262_144,
        "near_ceiling_combined_tokens": 262_143,
        "promotion_gain_percent": 10.0,
        "confirmation_gain_floor_percent": 5.0,
        "max_median_decode_regression_percent": 5.0,
        "max_p95_ttft_ratio": 1.5,
    }


def single_value_summary(value: float) -> dict[str, float | int]:
    return {
        "count": 1,
        "minimum": value,
        "maximum": value,
        "median": value,
        "mean": value,
        "population_standard_deviation": 0.0,
        "p50": value,
        "p95": value,
    }


def summary_identifier(*components: str) -> str:
    return json.dumps(
        list(components), ensure_ascii=True, separators=(",", ":")
    )


def valid_observation_summaries() -> dict[str, dict[str, float | int]]:
    scope = ("baseline", "default", "1", "local-openai", "decode-short")
    return {
        summary_identifier(*scope, "prompt_tokens"): single_value_summary(11.0),
        summary_identifier(*scope, "completion_tokens"): single_value_summary(2.0),
        summary_identifier(*scope, "total_tokens"): single_value_summary(13.0),
        summary_identifier(*scope, "elapsed_seconds"): single_value_summary(1.0),
        summary_identifier(*scope, "ttft_seconds"): single_value_summary(0.2),
        summary_identifier(*scope, "last_content_seconds"): single_value_summary(0.8),
        summary_identifier(
            *scope, "client_decode_tokens_per_second"
        ): single_value_summary(1.6666666666666667),
        summary_identifier(
            *scope, "end_to_end_output_tokens_per_second"
        ): single_value_summary(2.0),
        summary_identifier(
            *scope, "effective_prompt_tokens_per_second"
        ): single_value_summary(55.0),
    }


def summaries_for_observations(
    observations: list[dict[str, object]],
) -> dict[str, dict[str, float | int | None]]:
    metrics = (
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
    grouped: dict[str, list[float]] = {}
    for observation in observations:
        if observation.get("status") != "ok":
            continue
        for metric in metrics:
            value = observation.get(metric)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                summary_id = summary_identifier(
                    *(
                        str(observation[key])
                        for key in (
                            "candidate_id",
                            "phase_id",
                            "attempt_id",
                            "route",
                            "case_id",
                        )
                    ),
                    metric,
                )
                grouped.setdefault(summary_id, []).append(float(value))
    return {
        summary_id: summarize_values(values)
        for summary_id, values in sorted(grouped.items())
    }


def aggregate_batch(
    candidate_id: str,
    *,
    route: str,
    case_id: str,
    wall_seconds: float,
    prompt_tokens: int,
    completion_tokens: int,
    phase_id: str = "default",
    attempt_id: str = "1",
    requested_concurrency: int = 1,
    request_count: int = 1,
    target_prompt_tokens: int | None = None,
    max_output_tokens: int = 4,
    decode_mode: str = "openai_sse",
    counting_mode: str = "declared",
) -> dict[str, object]:
    return {
        "route": route,
        "case_id": case_id,
        "candidate_id": candidate_id,
        "phase_id": phase_id,
        "attempt_id": attempt_id,
        "requested_concurrency": requested_concurrency,
        "request_count": request_count,
        "target_prompt_tokens": (
            prompt_tokens if target_prompt_tokens is None else target_prompt_tokens
        ),
        "max_output_tokens": max_output_tokens,
        "decode_mode": decode_mode,
        "counting_mode": counting_mode,
        "attempted_count": 1,
        "successful_count": 1,
        "failed_count": 0,
        "timed_out_count": 0,
        "cancelled_count": 0,
        "total_prompt_tokens": prompt_tokens,
        "total_completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "batch_wall_seconds": wall_seconds,
        "aggregate_output_tokens_per_second": completion_tokens / wall_seconds,
        "counter_deltas": {},
        "gauge_peaks": {},
        "server_decode_count_metric": None,
        "server_decode_sum_metric": None,
        "server_decode_count_delta": None,
        "server_decode_sum_delta": None,
        "server_decode_tokens_per_second": None,
    }


def valid_aggregate_document() -> dict[str, object]:
    return {
        "schema_version": 3,
        "decision_policy": decision_policy_literal(),
        "decision_thresholds": decision_thresholds_literal(),
        "prometheus_metric_name_allowlist": [],
        "observations": [
            {
                "route": "local-openai",
                "case_id": "decode-short",
                "candidate_id": "baseline",
                "phase_id": "default",
                "attempt_id": "1",
                "repetition": 1,
                "status": "ok",
                "http_status": 200,
                "prompt_tokens": 11,
                "completion_tokens": 2,
                "total_tokens": 13,
                "finish_reason": "stop",
                "elapsed_seconds": 1.0,
                "ttft_seconds": 0.2,
                "last_content_seconds": 0.8,
                "client_decode_tokens_per_second": 1.6666666666666667,
                "end_to_end_output_tokens_per_second": 2.0,
                "effective_prompt_tokens_per_second": 55.0,
                "server_decode_tokens_per_second": None,
                "error_category": None,
            }
        ],
        "batches": [],
        "candidate_decisions": [
            {
                "candidate_id": "baseline",
                "phase_id": "default",
                "attempt_id": "1",
                "comparison_candidate_id": None,
                "comparison_attempt_id": None,
                "decision": "PENDING",
            }
        ],
        "summaries": valid_observation_summaries(),
        "final_verdict": "PENDING",
    }


def manual_decision(
    candidate_id: str,
    decision: str,
    *,
    phase_id: str = "default",
    attempt_id: str = "1",
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "phase_id": phase_id,
        "attempt_id": attempt_id,
        "comparison_candidate_id": None,
        "comparison_attempt_id": None,
        "decision": decision,
    }


def calculated_decision(
    candidate_id: str,
    decision: str,
    *,
    hard_gates_passed: bool | None,
    gain: float | None = None,
    regression: float | None = None,
    ratio: float | None = None,
    confirmation_completed: bool = False,
    confirmation_attempt_ids: tuple[str, ...] = (),
    phase_id: str = "default",
    attempt_id: str = "1",
    comparison_candidate_id: str | None = None,
    comparison_attempt_id: str | None = None,
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "phase_id": phase_id,
        "attempt_id": attempt_id,
        "comparison_candidate_id": comparison_candidate_id,
        "comparison_attempt_id": comparison_attempt_id,
        "decision": decision,
        "hard_gates_passed": hard_gates_passed,
        "same_work_throughput_gain_percent": gain,
        "median_decode_regression_percent": regression,
        "p95_ttft_ratio": ratio,
        "confirmation_completed": confirmation_completed,
        "confirmation_attempt_ids": list(confirmation_attempt_ids),
    }


def empty_aggregate_document() -> dict[str, object]:
    document = valid_aggregate_document()
    document["observations"] = []
    document["batches"] = []
    document["candidate_decisions"] = []
    document["summaries"] = {}
    document["final_verdict"] = "PENDING"
    return document


def successful_candidate_with_batch_document() -> dict[str, object]:
    document = valid_aggregate_document()
    observation = document["observations"][0]
    document["batches"] = [
        aggregate_batch(
            "baseline",
            route=str(observation["route"]),
            case_id=str(observation["case_id"]),
            wall_seconds=1.0,
            prompt_tokens=int(observation["prompt_tokens"]),
            completion_tokens=int(observation["completion_tokens"]),
        )
    ]
    return document


def failed_candidate_with_evidence_free_pass_document() -> dict[str, object]:
    document = valid_aggregate_document()
    observation = document["observations"][0]
    observation.update(
        {
            "status": "failed",
            "http_status": 503,
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "finish_reason": None,
            "elapsed_seconds": 0.1,
            "ttft_seconds": None,
            "last_content_seconds": None,
            "client_decode_tokens_per_second": None,
            "end_to_end_output_tokens_per_second": None,
            "effective_prompt_tokens_per_second": None,
            "server_decode_tokens_per_second": None,
            "error_category": "http_5xx",
        }
    )
    document["batches"] = [
        {
            "route": observation["route"],
            "case_id": observation["case_id"],
            "candidate_id": observation["candidate_id"],
            "phase_id": observation["phase_id"],
            "attempt_id": observation["attempt_id"],
            "requested_concurrency": 1,
            "request_count": 1,
            "target_prompt_tokens": 11,
            "max_output_tokens": 4,
            "decode_mode": "openai_sse",
            "counting_mode": "declared",
            "attempted_count": 1,
            "successful_count": 0,
            "failed_count": 1,
            "timed_out_count": 0,
            "cancelled_count": 0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_tokens": 0,
            "batch_wall_seconds": 0.1,
            "aggregate_output_tokens_per_second": None,
            "counter_deltas": {},
            "gauge_peaks": {},
            "server_decode_count_metric": None,
            "server_decode_sum_metric": None,
            "server_decode_count_delta": None,
            "server_decode_sum_delta": None,
            "server_decode_tokens_per_second": None,
        }
    ]
    document["candidate_decisions"] = [manual_decision("baseline", "PASS")]
    document["summaries"] = {}
    document["final_verdict"] = "PASS"
    return document


class PrivacyValidationAndErrorTests(unittest.TestCase):
    def test_safe_numeric_token_keys_are_allowed(self) -> None:
        errors = validate_aggregate_document(valid_aggregate_document())

        self.assertEqual(errors, [])

    def test_empty_document_rejects_an_evidence_free_pass(self) -> None:
        document = empty_aggregate_document()
        document["candidate_decisions"] = [
            manual_decision("ghost", "PASS")
        ]
        document["final_verdict"] = "PASS"

        errors = validate_aggregate_document(document)

        self.assertTrue(errors)
        self.assertTrue(
            any("represented" in error or "evidence" in error for error in errors)
        )

    def test_unrepresented_success_decisions_cannot_validate(self) -> None:
        for decision in ("PASS", "PROMOTE", "RETAIN"):
            with self.subTest(decision=decision):
                document = empty_aggregate_document()
                document["candidate_decisions"] = [
                    manual_decision("ghost", decision)
                ]
                document["final_verdict"] = decision

                errors = validate_aggregate_document(document)

                self.assertTrue(errors)
                self.assertTrue(
                    any(
                        "represented" in error or "evidence" in error
                        for error in errors
                    )
                )

    def test_unrepresented_manual_records_are_limited_to_safe_non_success_states(self) -> None:
        for decision in ("PENDING", "SKIP", "HOLD", "INVALID"):
            with self.subTest(decision=decision):
                document = empty_aggregate_document()
                document["candidate_decisions"] = [
                    manual_decision("manual", decision)
                ]
                document["final_verdict"] = decision

                self.assertEqual(validate_aggregate_document(document), [])

    def test_huge_json_integer_is_rejected_without_validator_exception(self) -> None:
        document = valid_aggregate_document()
        document["observations"][0]["repetition"] = 10**400

        errors = validate_aggregate_document(document)

        self.assertTrue(errors)
        self.assertTrue(any("repetition" in error for error in errors))

    def test_failed_candidate_rejects_an_evidence_free_pass_decision(self) -> None:
        errors = validate_aggregate_document(
            failed_candidate_with_evidence_free_pass_document()
        )

        self.assertTrue(errors)
        self.assertTrue(
            any("candidate" in error and "evidence" in error for error in errors)
        )

    def test_candidate_with_observations_and_batches_requires_a_decision(self) -> None:
        document = successful_candidate_with_batch_document()
        document["candidate_decisions"] = []

        errors = validate_aggregate_document(document)

        self.assertTrue(errors)
        self.assertTrue(
            any("candidate decision" in error and "missing" in error for error in errors)
        )

    def test_successful_candidate_rejects_null_hard_gate_evidence(self) -> None:
        document = successful_candidate_with_batch_document()
        document["candidate_decisions"] = [
            calculated_decision(
                "baseline", "PENDING", hard_gates_passed=None
            )
        ]

        errors = validate_aggregate_document(document)

        self.assertTrue(errors)
        self.assertTrue(
            any(
                "hard_gates_passed" in error and "aggregate evidence" in error
                for error in errors
            )
        )

    def test_required_aggregate_sections_cannot_be_omitted(self) -> None:
        document = valid_aggregate_document()
        del document["decision_policy"]

        errors = validate_aggregate_document(document)

        self.assertTrue(any("decision_policy" in error for error in errors))

    def test_nonempty_observations_cannot_bypass_summaries_with_an_empty_mapping(self) -> None:
        document = valid_aggregate_document()
        document["summaries"] = {}

        errors = validate_aggregate_document(document)

        self.assertTrue(any("summaries" in error and "stale" in error for error in errors))

    def test_dot_containing_summary_scopes_remain_distinct_and_validate(self) -> None:
        template = valid_aggregate_document()["observations"][0]
        first = dict(template)
        first.update({"candidate_id": "candidate.alpha", "phase_id": "phase"})
        second = dict(template)
        second.update({"candidate_id": "candidate", "phase_id": "alpha.phase"})
        document = empty_aggregate_document()
        document["observations"] = [first, second]
        document["candidate_decisions"] = [
            manual_decision("candidate.alpha", "PENDING", phase_id="phase"),
            manual_decision("candidate", "PENDING", phase_id="alpha.phase"),
        ]
        document["summaries"] = summaries_for_observations([first, second])

        self.assertEqual(len(document["summaries"]), 18)
        self.assertTrue(
            all(summary["count"] == 1 for summary in document["summaries"].values())
        )
        self.assertEqual(validate_aggregate_document(document), [])

        merged = dict(document)
        merged["summaries"] = {
            "candidate.alpha.phase.1.local-openai.decode-short.prompt_tokens": (
                summarize_values([11.0, 11.0])
            )
        }
        errors = validate_aggregate_document(merged)
        self.assertTrue(any("invalid summary identifier" in error for error in errors))

    def test_failed_observations_remain_evidence_but_not_performance_samples(self) -> None:
        document = valid_aggregate_document()
        failed = dict(document["observations"][0])
        failed.update(
            {
                "repetition": 2,
                "status": "failed",
                "http_status": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "finish_reason": None,
                "elapsed_seconds": 99.0,
                "ttft_seconds": None,
                "last_content_seconds": None,
                "client_decode_tokens_per_second": None,
                "end_to_end_output_tokens_per_second": None,
                "effective_prompt_tokens_per_second": None,
                "server_decode_tokens_per_second": None,
                "error_category": "timeout",
            }
        )
        document["observations"].append(failed)
        document["candidate_decisions"] = [
            calculated_decision(
                "baseline", "REJECT", hard_gates_passed=False
            )
        ]
        document["final_verdict"] = "REJECT"

        self.assertEqual(validate_aggregate_document(document), [])
        self.assertEqual(len(document["observations"]), 2)
        elapsed_summary = document["summaries"][
            "baseline.default.1.local-openai.decode-short.elapsed_seconds"
        ]
        self.assertEqual(elapsed_summary["count"], 1)
        self.assertEqual(elapsed_summary["maximum"], 1.0)

    def test_failed_partial_usage_cannot_validate_as_successful_batch_output(self) -> None:
        document = valid_aggregate_document()
        failed = dict(document["observations"][0])
        failed.update(
            {
                "candidate_id": "partial-failure",
                "status": "failed",
                "http_status": 200,
                "prompt_tokens": 8,
                "completion_tokens": 3,
                "total_tokens": 11,
                "finish_reason": None,
                "elapsed_seconds": 1.0,
                "ttft_seconds": None,
                "last_content_seconds": None,
                "client_decode_tokens_per_second": None,
                "end_to_end_output_tokens_per_second": None,
                "effective_prompt_tokens_per_second": None,
                "server_decode_tokens_per_second": None,
                "error_category": "invalid_stream",
            }
        )
        document["observations"].append(failed)
        document["batches"] = [
            {
                "route": failed["route"],
                "case_id": failed["case_id"],
                "candidate_id": failed["candidate_id"],
                "phase_id": failed["phase_id"],
                "attempt_id": failed["attempt_id"],
                "requested_concurrency": 1,
                "request_count": 1,
                "target_prompt_tokens": 8,
                "max_output_tokens": 4,
                "decode_mode": "openai_sse",
                "counting_mode": "declared",
                "attempted_count": 1,
                "successful_count": 0,
                "failed_count": 1,
                "timed_out_count": 0,
                "cancelled_count": 0,
                "total_prompt_tokens": 8,
                "total_completion_tokens": 3,
                "total_tokens": 11,
                "batch_wall_seconds": 1.0,
                "aggregate_output_tokens_per_second": 3.0,
                "counter_deltas": {},
                "gauge_peaks": {},
                "server_decode_count_metric": None,
                "server_decode_sum_metric": None,
                "server_decode_count_delta": None,
                "server_decode_sum_delta": None,
                "server_decode_tokens_per_second": None,
            }
        ]

        errors = validate_aggregate_document(document)

        self.assertTrue(
            any("successful" in error and "output" in error for error in errors)
        )

    def test_forbidden_keys_are_rejected_recursively(self) -> None:
        forbidden = (
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
        )
        for key in forbidden:
            with self.subTest(key=key):
                document = valid_aggregate_document()
                document["summaries"] = {"decode-short": {key: "must-not-persist"}}
                errors = validate_aggregate_document(document)
                self.assertTrue(errors)
                self.assertTrue(any(key in error for error in errors))

    def test_success_timing_decisions_and_summaries_are_cross_validated(self) -> None:
        missing_usage = valid_aggregate_document()
        observation = missing_usage["observations"][0]
        observation.update(
            {
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "ttft_seconds": None,
                "client_decode_tokens_per_second": None,
                "end_to_end_output_tokens_per_second": None,
                "effective_prompt_tokens_per_second": None,
            }
        )
        self.assertTrue(validate_aggregate_document(missing_usage))

        duplicate_decision = valid_aggregate_document()
        duplicate_decision["candidate_decisions"].append(
            manual_decision("baseline", "PASS")
        )
        self.assertTrue(validate_aggregate_document(duplicate_decision))

        stale_summary = valid_aggregate_document()
        stale_summary["summaries"] = {
            "baseline.default.1.local-openai.decode-short.elapsed_seconds": {
                "count": 1,
                "minimum": 99.0,
                "maximum": 99.0,
                "median": 99.0,
                "mean": 99.0,
                "population_standard_deviation": 0.0,
                "p50": 99.0,
                "p95": 99.0,
            }
        }
        self.assertTrue(validate_aggregate_document(stale_summary))

    def test_candidate_decision_evidence_is_recomputed_and_signed(self) -> None:
        document = valid_aggregate_document()
        baseline = document["observations"][0]
        document["batches"] = [
            aggregate_batch(
                "baseline",
                route=str(baseline["route"]),
                case_id=str(baseline["case_id"]),
                wall_seconds=1.12,
                prompt_tokens=int(baseline["prompt_tokens"]),
                completion_tokens=int(baseline["completion_tokens"]),
            )
        ]
        current = dict(baseline)
        current.update(
            {
                "candidate_id": "current",
                "last_content_seconds": 0.2 + (1.0 / 1.7),
                "client_decode_tokens_per_second": 1.7,
            }
        )
        document["observations"].append(current)
        document["batches"].append(
            aggregate_batch(
                "current",
                route=str(current["route"]),
                case_id=str(current["case_id"]),
                wall_seconds=1.0,
                prompt_tokens=int(current["prompt_tokens"]),
                completion_tokens=int(current["completion_tokens"]),
            )
        )
        document["summaries"] = summaries_for_observations(document["observations"])
        evidence = calculated_decision(
            "current",
            "PROMOTE",
            hard_gates_passed=True,
            gain=12.0,
            regression=-2.0,
            ratio=1.0,
            comparison_candidate_id="baseline",
            comparison_attempt_id="1",
        )
        document["candidate_decisions"] = [
            calculated_decision(
                "baseline", "PENDING", hard_gates_passed=True
            ),
            evidence,
        ]
        document["final_verdict"] = "PROMOTE"

        self.assertEqual(validate_aggregate_document(document), [])

        evidence["decision"] = "RETAIN"
        document["final_verdict"] = "RETAIN"
        errors = validate_aggregate_document(document)
        self.assertTrue(any("formula mismatch" in error for error in errors))

    def test_failed_run_cannot_validate_claimed_promotion_evidence(self) -> None:
        document = valid_aggregate_document()
        failed = dict(document["observations"][0])
        failed.update(
            {
                "candidate_id": "current",
                "status": "failed",
                "http_status": 503,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "finish_reason": None,
                "elapsed_seconds": 0.1,
                "ttft_seconds": None,
                "last_content_seconds": None,
                "client_decode_tokens_per_second": None,
                "end_to_end_output_tokens_per_second": None,
                "effective_prompt_tokens_per_second": None,
                "server_decode_tokens_per_second": None,
                "error_category": "http_5xx",
            }
        )
        document["observations"].append(failed)
        document["candidate_decisions"] = [
            manual_decision("baseline", "PENDING"),
            calculated_decision(
                "current",
                "PROMOTE",
                hard_gates_passed=True,
                gain=12.0,
                regression=0.0,
                ratio=1.0,
                comparison_candidate_id="baseline",
                comparison_attempt_id="1",
            ),
        ]
        document["final_verdict"] = "PROMOTE"

        errors = validate_aggregate_document(document)

        self.assertTrue(
            any("candidate" in error and "aggregate evidence" in error for error in errors)
        )

    def test_final_verdict_must_match_the_latest_candidate_decision(self) -> None:
        document = valid_aggregate_document()
        document["final_verdict"] = "PROMOTE"

        errors = validate_aggregate_document(document)

        self.assertTrue(any("final_verdict" in error for error in errors))

    def test_server_decode_batch_provenance_and_scope_are_validated(self) -> None:
        document = valid_aggregate_document()
        count_metric = "vllm:time_per_output_token_seconds_count"
        sum_metric = "vllm:time_per_output_token_seconds_sum"
        document["prometheus_metric_name_allowlist"] = [count_metric, sum_metric]
        document["batches"] = [
            {
                "route": "local-openai",
                "case_id": "decode-short",
                "candidate_id": "baseline",
                "phase_id": "default",
                "attempt_id": "1",
                "requested_concurrency": 1,
                "request_count": 1,
                "target_prompt_tokens": 11,
                "max_output_tokens": 4,
                "decode_mode": "openai_sse",
                "counting_mode": "declared",
                "attempted_count": 1,
                "successful_count": 1,
                "failed_count": 0,
                "timed_out_count": 0,
                "cancelled_count": 0,
                "total_prompt_tokens": 11,
                "total_completion_tokens": 2,
                "total_tokens": 13,
                "batch_wall_seconds": 1.0,
                "aggregate_output_tokens_per_second": 2.0,
                "counter_deltas": {count_metric: 6.0, sum_metric: 2.0},
                "gauge_peaks": {},
                "server_decode_count_metric": count_metric,
                "server_decode_sum_metric": sum_metric,
                "server_decode_count_delta": 6.0,
                "server_decode_sum_delta": 2.0,
                "server_decode_tokens_per_second": 3.0,
            }
        ]
        document["candidate_decisions"] = [
            calculated_decision(
                "baseline", "PENDING", hard_gates_passed=True
            )
        ]

        self.assertEqual(validate_aggregate_document(document), [])

        document["batches"][0]["server_decode_tokens_per_second"] = 99.0
        errors = validate_aggregate_document(document)
        self.assertTrue(any("server_decode" in error and "formula" in error for error in errors))

        document = valid_aggregate_document()
        document["observations"][0]["server_decode_tokens_per_second"] = 3.0
        errors = validate_aggregate_document(document)
        self.assertTrue(any("server_decode" in error for error in errors))

    def test_persisted_metrics_must_be_in_the_exact_document_allowlist(self) -> None:
        document = valid_aggregate_document()
        document["prometheus_metric_name_allowlist"] = ["approved_metric"]
        document["batches"] = [
            {
                "route": "local-openai",
                "case_id": "decode-short",
                "candidate_id": "baseline",
                "phase_id": "default",
                "attempt_id": "1",
                "requested_concurrency": 1,
                "request_count": 1,
                "target_prompt_tokens": 11,
                "max_output_tokens": 4,
                "decode_mode": "openai_sse",
                "counting_mode": "declared",
                "attempted_count": 1,
                "successful_count": 1,
                "failed_count": 0,
                "timed_out_count": 0,
                "cancelled_count": 0,
                "total_prompt_tokens": 11,
                "total_completion_tokens": 2,
                "total_tokens": 13,
                "batch_wall_seconds": 1.0,
                "aggregate_output_tokens_per_second": 2.0,
                "counter_deltas": {"unapproved_metric": 1.0},
                "gauge_peaks": {},
                "server_decode_count_metric": None,
                "server_decode_sum_metric": None,
                "server_decode_count_delta": None,
                "server_decode_sum_delta": None,
                "server_decode_tokens_per_second": None,
            }
        ]

        errors = validate_aggregate_document(document)

        self.assertTrue(any("unapproved_metric" in error and "allowlist" in error for error in errors))

    def test_forbidden_values_and_aggregate_invariants_are_rejected(self) -> None:
        forbidden_value = valid_aggregate_document()
        forbidden_value["candidate_decisions"] = [
            manual_decision("raw_metrics", "PENDING")
        ]
        self.assertTrue(validate_aggregate_document(forbidden_value))

        invalid = valid_aggregate_document()
        observation = invalid["observations"][0]
        observation["total_tokens"] = 99
        observation["elapsed_seconds"] = float("nan")
        observation["ttft_seconds"] = 0.9
        observation["last_content_seconds"] = 0.2
        invalid["candidate_decisions"] = [
            manual_decision("baseline", "MAYBE")
        ]
        errors = validate_aggregate_document(invalid)
        self.assertGreaterEqual(len(errors), 4)

    def test_unsafe_aggregate_identifiers_are_rejected_before_network_access(self) -> None:
        with LocalHTTPFixture() as fixture:
            observation = run_request(
                RouteSpec(
                    "unsafe route",
                    "openai",
                    fixture.url("/ok-openai"),
                    "model",
                    None,
                ),
                RequestSpec("short", 8, 4, 1, "baseline"),
                timeout_seconds=1.0,
            )
            post_count = fixture.server.post_count

        self.assertEqual(observation.error_category, "invalid_request")
        self.assertEqual(post_count, 0)

    def test_malformed_route_and_tokenizer_urls_are_coarse_without_network_calls(self) -> None:
        calls: list[object] = []

        def unexpected_urlopen(request: object, **kwargs: object) -> object:
            calls.append(request)
            raise AssertionError("network must not be attempted")

        malformed_route = run_request(
            RouteSpec("fixture", "openai", "http://[", "model", None),
            RequestSpec("short", 8, 4, 1, "baseline"),
            timeout_seconds=1.0,
            urlopen=unexpected_urlopen,
        )
        malformed_tokenizer = run_request(
            RouteSpec("fixture", "openai", "http://127.0.0.1/unused", "model", None),
            RequestSpec("near-ceiling", 262_079, 64, 1, "baseline"),
            timeout_seconds=1.0,
            tokenize_url="file:///not-http",
            urlopen=unexpected_urlopen,
        )

        self.assertEqual(malformed_route.error_category, "invalid_request")
        self.assertEqual(malformed_tokenizer.error_category, "invalid_request")
        self.assertEqual(calls, [])

    def test_openai_requests_offer_no_tools_and_disable_thinking(self) -> None:
        with LocalHTTPFixture() as fixture:
            observation = run_request(
                RouteSpec(
                    "fixture", "openai", fixture.url("/ok-openai"), "model", None
                ),
                RequestSpec("short", 8, 4, 1, "baseline"),
                timeout_seconds=1.0,
            )
            saw_tools = fixture.server.saw_tools
            thinking_disabled = fixture.server.openai_thinking_disabled

        self.assertEqual(observation.status, "ok")
        self.assertFalse(saw_tools)
        self.assertTrue(thinking_disabled)

    def test_http_failure_keeps_only_coarse_category(self) -> None:
        with LocalHTTPFixture() as fixture:
            observation = run_request(
                RouteSpec("fixture", "openai", fixture.url("/error"), "model", None),
                RequestSpec("short", 8, 4, 1, "baseline"),
                timeout_seconds=1.0,
            )

        self.assertIsInstance(observation, Observation)
        self.assertEqual(observation.status, "failed")
        self.assertEqual(observation.http_status, 503)
        self.assertEqual(observation.error_category, "http_5xx")
        serialized = json.dumps(asdict(observation), sort_keys=True)
        self.assertNotIn("body-not-for-observation", serialized)
        self.assertNotIn(fixture.url("/error"), serialized)

    def test_invalid_stream_and_missing_usage_are_coarse(self) -> None:
        with LocalHTTPFixture() as fixture:
            invalid = run_request(
                RouteSpec(
                    "fixture", "openai", fixture.url("/invalid"), "model", None
                ),
                RequestSpec("short", 8, 4, 1, "baseline"),
                timeout_seconds=1.0,
            )
            missing_usage = run_request(
                RouteSpec(
                    "fixture",
                    "openai",
                    fixture.url("/missing-usage"),
                    "model",
                    None,
                ),
                RequestSpec("short", 8, 4, 2, "baseline"),
                timeout_seconds=1.0,
            )

        self.assertEqual(invalid.error_category, "invalid_stream")
        self.assertEqual(missing_usage.error_category, "usage_missing")
        serialized = json.dumps(
            [asdict(invalid), asdict(missing_usage)], sort_keys=True
        )
        self.assertNotIn("not-json", serialized)
        self.assertNotIn("discard-local", serialized)


def successful_observation(route: RouteSpec, request: RequestSpec) -> Observation:
    return Observation(
        route=route.name,
        case_id=request.case_id,
        candidate_id=request.candidate_id,
        repetition=request.repetition,
        status="ok",
        http_status=200,
        prompt_tokens=8,
        completion_tokens=2,
        total_tokens=10,
        finish_reason="stop",
        elapsed_seconds=1.0,
        ttft_seconds=0.2,
        last_content_seconds=0.8,
        client_decode_tokens_per_second=1.6666666666666667,
        end_to_end_output_tokens_per_second=2.0,
        effective_prompt_tokens_per_second=40.0,
        server_decode_tokens_per_second=None,
        error_category=None,
    )


class PrometheusConcurrencyAndCountingTests(unittest.TestCase):
    def test_prometheus_parser_and_aggregates_keep_only_allowlisted_numbers(self) -> None:
        payload = b"".join(
            [
                b"# HELP private_marker body-not-for-results\n",
                b'vllm:request_success_total{model_name="fixture"} 14\n',
                b"vllm:num_requests_running 2\n",
                b"vllm:num_requests_waiting 3\n",
                b"unlisted_metric 999\n",
            ]
        )
        names = {
            "vllm:request_success_total",
            "vllm:num_requests_running",
            "vllm:num_requests_waiting",
        }

        parsed = parse_prometheus_allowlist(payload, names)
        self.assertEqual(
            parsed,
            {
                "vllm:request_success_total": 14.0,
                "vllm:num_requests_running": 2.0,
                "vllm:num_requests_waiting": 3.0,
            },
        )
        aggregates = aggregate_prometheus_samples(
            before={"vllm:request_success_total": 10.0},
            after={"vllm:request_success_total": 14.0},
            samples=[
                {
                    "vllm:num_requests_running": 1.0,
                    "vllm:num_requests_waiting": 3.0,
                },
                {
                    "vllm:num_requests_running": 2.0,
                    "vllm:num_requests_waiting": 1.0,
                },
            ],
            counter_names={"vllm:request_success_total"},
            gauge_names={
                "vllm:num_requests_running",
                "vllm:num_requests_waiting",
            },
        )
        self.assertEqual(
            aggregates["counter_deltas"], {"vllm:request_success_total": 4.0}
        )
        self.assertEqual(
            aggregates["gauge_peaks"],
            {
                "vllm:num_requests_running": 2.0,
                "vllm:num_requests_waiting": 3.0,
            },
        )
        serialized = json.dumps([parsed, aggregates], sort_keys=True)
        self.assertNotIn("body-not-for-results", serialized)
        self.assertNotIn("unlisted_metric", serialized)

    def test_run_batch_starts_each_wave_together_and_preserves_order(self) -> None:
        route = RouteSpec("fixture", "openai", "http://127.0.0.1/unused", "model", None)
        requests = [
            RequestSpec("batch", 8, 4, repetition, "baseline")
            for repetition in range(1, 5)
        ]
        starts: list[float] = []
        starts_lock = threading.Lock()
        all_started = threading.Event()

        def runner(
            selected_route: RouteSpec,
            selected_request: RequestSpec,
            **kwargs: object,
        ) -> Observation:
            with starts_lock:
                starts.append(time.perf_counter())
                if len(starts) == len(requests):
                    all_started.set()
            self.assertTrue(all_started.wait(timeout=1.0))
            return successful_observation(selected_route, selected_request)

        observations = run_batch(
            route,
            requests,
            concurrency=4,
            timeout_seconds=1.0,
            request_runner=runner,
        )

        self.assertEqual([item.repetition for item in observations], [1, 2, 3, 4])
        self.assertEqual(len(starts), 4)
        self.assertLess(max(starts) - min(starts), 0.1)

    def test_batch_preflights_exact_counts_before_releasing_actual_http_requests(self) -> None:
        captured_batch: list[dict[str, object]] = []
        with LocalHTTPFixture() as fixture:
            observations = run_batch(
                RouteSpec(
                    "fixture", "openai", fixture.url("/ok-openai"), "model", None
                ),
                [
                    RequestSpec("batch", 8, 4, 1, "baseline"),
                    RequestSpec("batch", 8, 4, 2, "baseline"),
                ],
                concurrency=2,
                timeout_seconds=1.0,
                require_exact_count=True,
                tokenize_url=fixture.url("/tokenize-asymmetric"),
                batch_callback=captured_batch.append,
            )
            request_start_times = list(fixture.server.request_start_times)
            request_events = list(fixture.server.request_events)

        self.assertTrue(all(item.status == "ok" for item in observations))
        self.assertEqual(request_events[:2], ["tokenize", "tokenize"])
        self.assertEqual(len(request_start_times), 2)
        self.assertLess(max(request_start_times) - min(request_start_times), 0.05)
        self.assertEqual(len(captured_batch), 1)
        self.assertLess(captured_batch[0]["batch_wall_seconds"], 0.1)

    def test_batch_timeout_and_cancellation_are_coarse_and_do_not_send_payloads(self) -> None:
        with LocalHTTPFixture() as fixture:
            cancelled_event = threading.Event()
            cancelled_event.set()
            cancelled = run_batch(
                RouteSpec(
                    "fixture", "openai", fixture.url("/ok-openai"), "model", None
                ),
                [RequestSpec("batch", 8, 4, 1, "baseline")],
                concurrency=1,
                timeout_seconds=1.0,
                cancel_event=cancelled_event,
            )
            posts_after_cancel = fixture.server.post_count
            timed_out = run_batch(
                RouteSpec(
                    "fixture",
                    "openai",
                    fixture.url("/slow-timeout"),
                    "model",
                    None,
                ),
                [RequestSpec("batch", 8, 4, 2, "baseline")],
                concurrency=1,
                timeout_seconds=0.02,
            )

        self.assertEqual(cancelled[0].error_category, "cancelled")
        self.assertEqual(posts_after_cancel, 0)
        self.assertEqual(timed_out[0].error_category, "timeout")
        serialized = json.dumps(
            [asdict(cancelled[0]), asdict(timed_out[0])], sort_keys=True
        )
        self.assertNotIn("slow-timeout", serialized)
        self.assertNotIn("discard-success", serialized)

    def test_absolute_deadline_stops_a_drip_stream(self) -> None:
        with LocalHTTPFixture() as fixture:
            started = time.perf_counter()
            observation = run_request(
                RouteSpec("fixture", "openai", fixture.url("/drip"), "model", None),
                RequestSpec("batch", 8, 4, 1, "baseline"),
                timeout_seconds=0.1,
            )
            wall_seconds = time.perf_counter() - started

        self.assertEqual(observation.error_category, "timeout")
        self.assertLess(wall_seconds, 0.35)
        self.assertLess(observation.elapsed_seconds, 0.35)

    def test_cancellation_interrupts_a_stalled_read(self) -> None:
        cancel_event = threading.Event()
        timer = threading.Timer(0.05, cancel_event.set)
        with LocalHTTPFixture() as fixture:
            timer.start()
            started = time.perf_counter()
            observation = run_request(
                RouteSpec("fixture", "openai", fixture.url("/stall"), "model", None),
                RequestSpec("batch", 8, 4, 1, "baseline"),
                timeout_seconds=1.0,
                cancel_event=cancel_event,
            )
            wall_seconds = time.perf_counter() - started
        timer.join(timeout=1.0)

        self.assertEqual(observation.status, "cancelled")
        self.assertEqual(observation.error_category, "cancelled")
        self.assertLess(wall_seconds, 0.35)

    def test_cancellation_between_preflight_and_barrier_does_not_deadlock(self) -> None:
        class FlipCancel:
            def __init__(self) -> None:
                self.calls = 0

            def is_set(self) -> bool:
                self.calls += 1
                return self.calls >= 3

        result: list[list[Observation]] = []
        cancel_event = FlipCancel()

        def execute_batch() -> None:
            result.append(
                run_batch(
                    RouteSpec(
                        "fixture",
                        "openai",
                        "http://127.0.0.1/unused",
                        "model",
                        None,
                    ),
                    [RequestSpec("batch", 8, 4, 1, "baseline")],
                    concurrency=1,
                    timeout_seconds=0.1,
                    cancel_event=cancel_event,
                    urlopen=lambda request, **kwargs: self.fail(
                        "cancelled request must not open HTTP"
                    ),
                )
            )

        thread = threading.Thread(target=execute_batch, daemon=True)
        thread.start()
        thread.join(timeout=0.5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0][0].status, "cancelled")
        self.assertEqual(result[0][0].error_category, "cancelled")

    def test_cancellation_wins_when_an_interrupted_read_raises_socket_timeout(self) -> None:
        cancel_event = threading.Event()

        class TimeoutResponse:
            status = 200

            def __enter__(self) -> "TimeoutResponse":
                return self

            def __exit__(self, *exc_info: object) -> None:
                return None

            def __iter__(self):
                cancel_event.wait(timeout=1.0)
                raise socket.timeout
                yield b""

            def close(self) -> None:
                return None

        timer = threading.Timer(0.02, cancel_event.set)
        timer.start()
        observation = run_request(
            RouteSpec("fixture", "openai", "http://127.0.0.1/unused", "model", None),
            RequestSpec("batch", 8, 4, 1, "baseline"),
            timeout_seconds=1.0,
            cancel_event=cancel_event,
            urlopen=lambda request, **kwargs: TimeoutResponse(),
        )
        timer.join(timeout=1.0)

        self.assertEqual(observation.status, "cancelled")
        self.assertEqual(observation.error_category, "cancelled")

    def test_run_batch_bounds_joins_for_a_stalled_runner(self) -> None:
        release = threading.Event()
        route = RouteSpec("fixture", "openai", "http://127.0.0.1/unused", "model", None)
        request = RequestSpec("batch", 8, 4, 1, "baseline")

        def stalled_runner(
            selected_route: RouteSpec,
            selected_request: RequestSpec,
            **kwargs: object,
        ) -> Observation:
            release.wait(timeout=1.0)
            return successful_observation(selected_route, selected_request)

        started = time.perf_counter()
        observations = run_batch(
            route,
            [request],
            concurrency=1,
            timeout_seconds=0.05,
            request_runner=stalled_runner,
        )
        wall_seconds = time.perf_counter() - started
        release.set()

        self.assertLess(wall_seconds, 0.35)
        self.assertEqual(observations[0].error_category, "timeout")

    def test_late_runner_cannot_overwrite_a_sealed_timeout_result(self) -> None:
        release = threading.Event()
        finished = threading.Event()
        route = RouteSpec(
            "fixture", "openai", "http://127.0.0.1/unused", "model", None
        )
        request = RequestSpec("batch", 8, 4, 1, "baseline")

        def late_runner(
            selected_route: RouteSpec,
            selected_request: RequestSpec,
            **kwargs: object,
        ) -> Observation:
            release.wait(timeout=1.0)
            result = successful_observation(selected_route, selected_request)
            finished.set()
            return result

        def after_join(batch: dict[str, object]) -> None:
            release.set()
            self.assertTrue(finished.wait(timeout=1.0))

        observations = run_batch(
            route,
            [request],
            concurrency=1,
            timeout_seconds=0.01,
            request_runner=late_runner,
            batch_callback=after_join,
        )

        self.assertEqual(observations[0].status, "failed")
        self.assertEqual(observations[0].error_category, "timeout")
        time.sleep(0.01)
        self.assertEqual(observations[0].error_category, "timeout")

    def test_run_batch_join_deadline_uses_perf_counter_ns(self) -> None:
        route = RouteSpec(
            "fixture", "openai", "http://127.0.0.1/unused", "model", None
        )
        request = RequestSpec("batch", 8, 4, 1, "baseline")

        def runner(
            selected_route: RouteSpec,
            selected_request: RequestSpec,
            **kwargs: object,
        ) -> Observation:
            return successful_observation(selected_route, selected_request)

        with patch(
            "benchmark_dragon.time.perf_counter",
            side_effect=AssertionError("join deadline used second-resolution clock"),
        ):
            observations = run_batch(
                route,
                [request],
                concurrency=1,
                timeout_seconds=0.1,
                request_runner=runner,
            )

        self.assertEqual(observations[0].status, "ok")

    def test_invalid_metric_interval_is_rejected_before_network_access(self) -> None:
        with LocalHTTPFixture() as fixture:
            with self.assertRaises(ValueError):
                run_batch(
                    RouteSpec(
                        "fixture",
                        "openai",
                        fixture.url("/ok-openai"),
                        "model",
                        None,
                    ),
                    [RequestSpec("batch", 8, 4, 1, "baseline")],
                    concurrency=1,
                    timeout_seconds=1.0,
                    metrics_url=fixture.url("/metrics"),
                    metric_names={"vllm:num_requests_running"},
                    gauge_names={"vllm:num_requests_running"},
                    metric_sample_interval_seconds=0.0,
                )
            metric_calls = fixture.server.metric_calls
            post_count = fixture.server.post_count

        self.assertEqual(metric_calls, 0)
        self.assertEqual(post_count, 0)

    def test_nonfinite_metric_interval_is_rejected_before_exact_count_preflight(self) -> None:
        for interval in (float("nan"), float("inf")):
            with self.subTest(interval=interval), LocalHTTPFixture() as fixture:
                with self.assertRaises(ValueError):
                    run_batch(
                        RouteSpec(
                            "fixture",
                            "openai",
                            fixture.url("/ok-openai"),
                            "model",
                            None,
                        ),
                        [RequestSpec("exact", 8, 4, 1, "baseline")],
                        concurrency=1,
                        timeout_seconds=1.0,
                        require_exact_count=True,
                        tokenize_url=fixture.url("/tokenize"),
                        metrics_url=fixture.url("/metrics"),
                        metric_names={"vllm:num_requests_running"},
                        gauge_names={"vllm:num_requests_running"},
                        metric_sample_interval_seconds=interval,
                    )
                metric_calls = fixture.server.metric_calls
                post_count = fixture.server.post_count

            self.assertEqual(metric_calls, 0)
            self.assertEqual(post_count, 0)

    def test_run_batch_samples_counter_deltas_and_gauge_peaks(self) -> None:
        captured: list[dict[str, object]] = []
        metric_names = {
            "vllm:request_success_total",
            "vllm:num_requests_running",
            "vllm:num_requests_waiting",
        }
        with LocalHTTPFixture() as fixture:
            observations = run_batch(
                RouteSpec(
                    "fixture", "openai", fixture.url("/slow-ok"), "model", None
                ),
                [
                    RequestSpec("batch", 8, 4, 1, "baseline"),
                    RequestSpec("batch", 8, 4, 2, "baseline"),
                ],
                concurrency=2,
                timeout_seconds=1.0,
                metrics_url=fixture.url("/metrics"),
                metric_names=metric_names,
                counter_names={"vllm:request_success_total"},
                gauge_names={
                    "vllm:num_requests_running",
                    "vllm:num_requests_waiting",
                },
                metric_sample_interval_seconds=0.01,
                metrics_callback=captured.append,
            )

        self.assertTrue(all(item.status == "ok" for item in observations))
        self.assertEqual(len(captured), 1)
        self.assertEqual(
            captured[0]["counter_deltas"],
            {"vllm:request_success_total": 4.0},
        )
        self.assertEqual(
            captured[0]["gauge_peaks"],
            {
                "vllm:num_requests_running": 2.0,
                "vllm:num_requests_waiting": 3.0,
            },
        )
        self.assertNotIn("body-not-for-results", json.dumps(captured))

    def test_server_decode_rate_is_derived_once_at_batch_scope(self) -> None:
        captured: list[dict[str, object]] = []
        count_metric = "vllm:time_per_output_token_seconds_count"
        sum_metric = "vllm:time_per_output_token_seconds_sum"
        metric_names = {
            count_metric,
            sum_metric,
            "vllm:num_requests_running",
        }
        with LocalHTTPFixture() as fixture:
            observations = run_batch(
                RouteSpec(
                    "fixture", "openai", fixture.url("/slow-ok"), "model", None
                ),
                [
                    RequestSpec("batch", 8, 4, 1, "baseline"),
                    RequestSpec("batch", 8, 4, 2, "baseline"),
                ],
                concurrency=2,
                timeout_seconds=1.0,
                metrics_url=fixture.url("/metrics"),
                metric_names=metric_names,
                counter_names={count_metric, sum_metric},
                gauge_names={"vllm:num_requests_running"},
                server_decode_count_metric=count_metric,
                server_decode_sum_metric=sum_metric,
                metric_sample_interval_seconds=0.01,
                metrics_callback=captured.append,
            )

        self.assertTrue(
            all(item.server_decode_tokens_per_second is None for item in observations)
        )
        self.assertEqual(captured[0]["server_decode_count_delta"], 6.0)
        self.assertEqual(captured[0]["server_decode_sum_delta"], 2.0)
        self.assertEqual(captured[0]["server_decode_tokens_per_second"], 3.0)

    def test_tokenizer_auxiliary_reads_have_absolute_deadlines(self) -> None:
        for endpoint in ("/tokenize-drip", "/tokenize-stall"):
            with self.subTest(endpoint=endpoint), LocalHTTPFixture() as fixture:
                started = time.perf_counter()
                observation = run_request(
                    RouteSpec(
                        "fixture",
                        "openai",
                        fixture.url("/ok-openai"),
                        "model",
                        None,
                    ),
                    RequestSpec("exact", 8, 4, 1, "baseline"),
                    timeout_seconds=0.1,
                    require_exact_count=True,
                    tokenize_url=fixture.url(endpoint),
                )
                wall_seconds = time.perf_counter() - started
                inference_count = len(fixture.server.request_start_times)
                tokenize_calls = fixture.server.tokenize_calls

            self.assertEqual(observation.error_category, "exact_count_unavailable")
            self.assertLess(wall_seconds, 0.35)
            self.assertEqual(inference_count, 0)
            self.assertEqual(tokenize_calls, 1)

    def test_tokenizer_preflight_observes_cancellation_and_stops_calibration(self) -> None:
        cancel_event = threading.Event()
        timer = threading.Timer(0.05, cancel_event.set)
        with LocalHTTPFixture() as fixture:
            timer.start()
            started = time.perf_counter()
            observation = run_request(
                RouteSpec(
                    "fixture", "openai", fixture.url("/ok-openai"), "model", None
                ),
                RequestSpec("exact", 8, 4, 1, "baseline"),
                timeout_seconds=1.0,
                require_exact_count=True,
                tokenize_url=fixture.url("/tokenize-stall"),
                cancel_event=cancel_event,
            )
            wall_seconds = time.perf_counter() - started
            inference_count = len(fixture.server.request_start_times)
            tokenize_calls = fixture.server.tokenize_calls
        timer.join(timeout=1.0)

        self.assertEqual(observation.status, "cancelled")
        self.assertEqual(observation.error_category, "cancelled")
        self.assertLess(wall_seconds, 0.35)
        self.assertEqual(inference_count, 0)
        self.assertEqual(tokenize_calls, 1)

    def test_optional_metric_reads_timeout_to_null_and_sampler_stops(self) -> None:
        for endpoint in ("/metrics-drip", "/metrics-stall"):
            with self.subTest(endpoint=endpoint), LocalHTTPFixture() as fixture:
                captured: list[dict[str, object]] = []
                started = time.perf_counter()
                observations = run_batch(
                    RouteSpec(
                        "fixture",
                        "openai",
                        fixture.url("/ok-openai"),
                        "model",
                        None,
                    ),
                    [RequestSpec("batch", 8, 4, 1, "baseline")],
                    concurrency=1,
                    timeout_seconds=0.5,
                    metrics_url=fixture.url(endpoint),
                    metric_names={"approved_metric"},
                    counter_names={"approved_metric"},
                    metric_sample_interval_seconds=0.01,
                    metrics_callback=captured.append,
                )
                wall_seconds = time.perf_counter() - started

            self.assertEqual(observations[0].status, "ok")
            self.assertLess(wall_seconds, 0.45)
            self.assertEqual(captured[0]["counter_deltas"], {"approved_metric": None})
            self.assertFalse(
                any(
                    thread.name == "benchmark-metric-sampler" and thread.is_alive()
                    for thread in threading.enumerate()
                )
            )

    def test_optional_metric_read_observes_cancellation_and_sampler_stops(self) -> None:
        cancel_event = threading.Event()
        timer = threading.Timer(0.05, cancel_event.set)
        with LocalHTTPFixture() as fixture:
            timer.start()
            started = time.perf_counter()
            observations = run_batch(
                RouteSpec(
                    "fixture", "openai", fixture.url("/ok-openai"), "model", None
                ),
                [RequestSpec("batch", 8, 4, 1, "baseline")],
                concurrency=1,
                timeout_seconds=1.0,
                cancel_event=cancel_event,
                metrics_url=fixture.url("/metrics-stall"),
                metric_names={"approved_metric"},
                counter_names={"approved_metric"},
                metric_sample_interval_seconds=0.01,
            )
            wall_seconds = time.perf_counter() - started
            inference_count = len(fixture.server.request_start_times)
        timer.join(timeout=1.0)

        self.assertEqual(observations[0].status, "cancelled")
        self.assertEqual(observations[0].error_category, "cancelled")
        self.assertLess(wall_seconds, 0.35)
        self.assertEqual(inference_count, 0)
        self.assertFalse(
            any(
                thread.name == "benchmark-metric-sampler" and thread.is_alive()
                for thread in threading.enumerate()
            )
        )

    def test_stalled_pinned_tokenizer_is_terminated_at_deadline(self) -> None:
        stall = threading.Event()
        children_before = direct_child_process_ids()

        def stalled_tokenizer(route: RouteSpec, prompt: str) -> int:
            stall.wait(timeout=1.0)
            return 8

        started = time.perf_counter()
        observation = run_request(
            RouteSpec(
                "fixture", "openai", "http://127.0.0.1/unused", "model", None
            ),
            RequestSpec("exact", 8, 4, 1, "baseline"),
            timeout_seconds=0.05,
            require_exact_count=True,
            pinned_tokenizer=stalled_tokenizer,
            urlopen=lambda request, **kwargs: self.fail(
                "timed-out exact preflight must not open HTTP"
            ),
        )
        wall_seconds = time.perf_counter() - started

        self.assertEqual(observation.error_category, "exact_count_unavailable")
        self.assertLess(wall_seconds, 0.35)
        self.assertEqual(direct_child_process_ids(), children_before)

    def test_stalled_pinned_tokenizer_is_terminated_on_cancellation(self) -> None:
        stall = threading.Event()
        cancel_event = threading.Event()
        timer = threading.Timer(0.05, cancel_event.set)
        children_before = direct_child_process_ids()

        def stalled_tokenizer(route: RouteSpec, prompt: str) -> int:
            stall.wait(timeout=1.0)
            return 8

        timer.start()
        started = time.perf_counter()
        observation = run_request(
            RouteSpec(
                "fixture", "openai", "http://127.0.0.1/unused", "model", None
            ),
            RequestSpec("exact", 8, 4, 1, "baseline"),
            timeout_seconds=1.0,
            require_exact_count=True,
            pinned_tokenizer=stalled_tokenizer,
            cancel_event=cancel_event,
            urlopen=lambda request, **kwargs: self.fail(
                "cancelled exact preflight must not open HTTP"
            ),
        )
        wall_seconds = time.perf_counter() - started
        timer.join(timeout=1.0)

        self.assertEqual(observation.status, "cancelled")
        self.assertEqual(observation.error_category, "cancelled")
        self.assertLess(wall_seconds, 0.35)
        self.assertEqual(direct_child_process_ids(), children_before)

    def test_exact_count_uses_tokenize_then_pinned_fallback(self) -> None:
        with LocalHTTPFixture() as fixture:
            via_tokenize = run_request(
                RouteSpec(
                    "fixture", "openai", fixture.url("/ok-openai"), "model", None
                ),
                RequestSpec("exact", 8, 4, 1, "baseline"),
                timeout_seconds=1.0,
                require_exact_count=True,
                tokenize_url=fixture.url("/tokenize"),
            )
            via_fallback = run_request(
                RouteSpec(
                    "fixture", "openai", fixture.url("/ok-openai"), "model", None
                ),
                RequestSpec("exact", 8, 4, 2, "baseline"),
                timeout_seconds=1.0,
                require_exact_count=True,
                tokenize_url=fixture.url("/error"),
                pinned_tokenizer=lambda route, prompt: 8,
            )

        self.assertEqual(via_tokenize.status, "ok")
        self.assertEqual(via_fallback.status, "ok")

    def test_exact_count_calibrates_the_benign_prompt_for_template_overhead(self) -> None:
        observed_repeat_counts: list[int] = []

        def tokenizer(route: RouteSpec, prompt: str) -> int:
            repeat_count = prompt.count("benchmark ")
            observed_repeat_counts.append(repeat_count)
            return repeat_count + 2

        with LocalHTTPFixture() as fixture:
            observation = run_request(
                RouteSpec(
                    "fixture", "openai", fixture.url("/ok-openai"), "model", None
                ),
                RequestSpec("exact", 8, 4, 1, "baseline"),
                timeout_seconds=1.0,
                require_exact_count=True,
                pinned_tokenizer=tokenizer,
            )

        self.assertEqual(observation.status, "ok")
        self.assertEqual(observed_repeat_counts, [8, 6])

    def test_exact_count_must_match_streamed_prompt_usage(self) -> None:
        observation = run_request(
            RouteSpec(
                "fixture", "openai", "http://127.0.0.1/unused", "model", None
            ),
            RequestSpec("exact", 8, 4, 1, "baseline"),
            timeout_seconds=1.0,
            require_exact_count=True,
            pinned_tokenizer=lambda route, prompt: 8,
            urlopen=lambda request, **kwargs: StaticStreamResponse(
                openai_usage_events(7)
            ),
        )

        self.assertEqual(observation.status, "failed")
        self.assertEqual(observation.error_category, "usage_mismatch")
        self.assertEqual(observation.prompt_tokens, 7)

    def test_exact_count_usage_recheck_honors_registered_and_hard_boundaries(self) -> None:
        cases = (
            ("exact", 8, 4, 8),
            ("near-ceiling", 262_079, 64, 262_079),
            ("boundary", 262_080, 64, 262_080),
        )
        for case_id, prompt_tokens, output_tokens, reported_prompt_tokens in cases:
            with self.subTest(case_id=case_id):
                observation = run_request(
                    RouteSpec(
                        "fixture",
                        "openai",
                        "http://127.0.0.1/unused",
                        "model",
                        None,
                    ),
                    RequestSpec(
                        case_id,
                        prompt_tokens,
                        output_tokens,
                        1,
                        "baseline",
                    ),
                    timeout_seconds=1.0,
                    require_exact_count=True,
                    pinned_tokenizer=lambda route, prompt, count=prompt_tokens: count,
                    urlopen=lambda request, count=reported_prompt_tokens, **kwargs: StaticStreamResponse(
                        openai_usage_events(count)
                    ),
                )

                self.assertEqual(observation.status, "ok")
                self.assertIsNone(observation.error_category)

    def test_request_timing_begins_after_exact_count_preflight(self) -> None:
        clock = SequenceClock(
            0,
            5_000_000_000,
            6_000_000_000,
            7_000_000_000,
            8_000_000_000,
            9_000_000_000,
        )

        def tokenizer(route: RouteSpec, prompt: str) -> int:
            clock()
            return 8

        with LocalHTTPFixture() as fixture:
            observation = run_request(
                RouteSpec(
                    "fixture", "openai", fixture.url("/ok-openai"), "model", None
                ),
                RequestSpec("exact", 8, 4, 1, "baseline"),
                timeout_seconds=1.0,
                clock_ns=clock,
                require_exact_count=True,
                pinned_tokenizer=tokenizer,
            )

        self.assertEqual(observation.ttft_seconds, 1.0)
        self.assertEqual(observation.elapsed_seconds, 4.0)

    def test_near_ceiling_requires_an_exact_full_request_count(self) -> None:
        with LocalHTTPFixture() as fixture:
            observation = run_request(
                RouteSpec(
                    "fixture", "openai", fixture.url("/ok-openai"), "model", None
                ),
                RequestSpec("near-ceiling", 262_079, 64, 1, "baseline"),
                timeout_seconds=1.0,
            )
            post_count = fixture.server.post_count

        self.assertEqual(observation.status, "failed")
        self.assertEqual(observation.error_category, "exact_count_unavailable")
        self.assertEqual(post_count, 0)

    def test_near_ceiling_boundary_counts_fail_closed_without_an_exact_counter(self) -> None:
        for combined_tokens in (262_142, 262_143, 262_144):
            with self.subTest(combined_tokens=combined_tokens), LocalHTTPFixture() as fixture:
                observation = run_request(
                    RouteSpec(
                        "fixture", "openai", fixture.url("/ok-openai"), "model", None
                    ),
                    RequestSpec(
                        "boundary",
                        combined_tokens - 64,
                        64,
                        1,
                        "baseline",
                    ),
                    timeout_seconds=1.0,
                )
                post_count = fixture.server.post_count

            self.assertEqual(observation.error_category, "exact_count_unavailable")
            self.assertEqual(post_count, 0)

    def test_registered_near_ceiling_case_requires_the_exact_registered_target(self) -> None:
        for combined_tokens in (262_142, 262_144):
            with self.subTest(combined_tokens=combined_tokens), LocalHTTPFixture() as fixture:
                observation = run_request(
                    RouteSpec(
                        "fixture", "openai", fixture.url("/ok-openai"), "model", None
                    ),
                    RequestSpec(
                        "near-ceiling",
                        combined_tokens - 64,
                        64,
                        1,
                        "baseline",
                    ),
                    timeout_seconds=1.0,
                    pinned_tokenizer=lambda route, prompt: prompt.count("benchmark "),
                )
                post_count = fixture.server.post_count

            self.assertEqual(observation.error_category, "invalid_request")
            self.assertEqual(post_count, 0)

    def test_near_ceiling_exact_count_mismatch_fails_before_inference(self) -> None:
        with LocalHTTPFixture() as fixture:
            observation = run_request(
                RouteSpec(
                    "fixture", "openai", fixture.url("/ok-openai"), "model", None
                ),
                RequestSpec("boundary", 262_079, 64, 1, "baseline"),
                timeout_seconds=1.0,
                pinned_tokenizer=lambda route, prompt: 262_080,
            )
            post_count = fixture.server.post_count

        self.assertEqual(observation.error_category, "invalid_request")
        self.assertEqual(post_count, 0)


class CLIAndReportTests(unittest.TestCase):
    def call_main(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def run_aggregate_only_phase(
        self,
        result_path: Path,
        *,
        candidate_id: str,
        phase_id: str,
        attempt_id: str,
        wall_seconds: float,
        comparison_candidate_id: str | None = None,
        comparison_attempt_id: str | None = None,
        confirmation_completed: bool = False,
        confirmation_attempt_ids: tuple[str, ...] = (),
        concurrency: int = 1,
    ) -> tuple[int, str, str]:
        def aggregate_only_run(
            route: RouteSpec,
            requests: list[RequestSpec],
            **kwargs: object,
        ) -> list[Observation]:
            batch_callback = kwargs.get("batch_callback")
            if callable(batch_callback):
                batch_callback({"batch_wall_seconds": wall_seconds})
            return [
                Observation(
                    route=route.name,
                    case_id=request.case_id,
                    candidate_id=request.candidate_id,
                    repetition=request.repetition,
                    status="ok",
                    http_status=200,
                    prompt_tokens=8,
                    completion_tokens=2,
                    total_tokens=10,
                    finish_reason="stop",
                    elapsed_seconds=1.0,
                    ttft_seconds=0.2,
                    last_content_seconds=0.8,
                    client_decode_tokens_per_second=1.6666666666666667,
                    end_to_end_output_tokens_per_second=2.0,
                    effective_prompt_tokens_per_second=40.0,
                    server_decode_tokens_per_second=None,
                    error_category=None,
                    phase_id=request.phase_id,
                    attempt_id=request.attempt_id,
                )
                for request in requests
            ]

        arguments = [
            "run",
            "--result",
            str(result_path),
            "--route",
            "fixture",
            "--protocol",
            "openai",
            "--url",
            "http://127.0.0.1/unused",
            "--model",
            "model",
            "--case",
            "short",
            "--candidate",
            candidate_id,
            "--phase-id",
            phase_id,
            "--attempt-id",
            attempt_id,
            "--target-prompt-tokens",
            "8",
            "--max-output-tokens",
            "4",
            "--concurrency",
            str(concurrency),
        ]
        if comparison_candidate_id is not None:
            arguments.extend(
                ["--comparison-candidate-id", comparison_candidate_id]
            )
        if comparison_attempt_id is not None:
            arguments.extend(["--comparison-attempt-id", comparison_attempt_id])
        if confirmation_completed:
            arguments.append("--confirmation-completed")
        for confirmation_attempt_id in confirmation_attempt_ids:
            arguments.extend(
                ["--confirmation-attempt-id", confirmation_attempt_id]
            )
        with patch("benchmark_dragon.run_batch", side_effect=aggregate_only_run):
            return self.call_main(arguments)

    def test_cli_validate_rejects_contradictory_candidate_records(self) -> None:
        omitted = successful_candidate_with_batch_document()
        omitted["candidate_decisions"] = []
        null_hard_gate = successful_candidate_with_batch_document()
        null_hard_gate["candidate_decisions"] = [
            calculated_decision(
                "baseline", "PENDING", hard_gates_passed=None
            )
        ]
        cases = {
            "evidence-free pass": failed_candidate_with_evidence_free_pass_document(),
            "omitted decision": omitted,
            "null hard gate": null_hard_gate,
        }

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            for name, document in cases.items():
                with self.subTest(name=name):
                    result_path = Path(temp_dir, f"{name.replace(' ', '-')}.json")
                    result_path.write_text(
                        json.dumps(document, indent=2) + "\n", encoding="utf-8"
                    )

                    code, _, _ = self.call_main(
                        ["validate", "--result", str(result_path)]
                    )

                    self.assertEqual(code, 1)

    def test_cli_parser_failures_use_only_the_coarse_error(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            result_path = Path(temp_dir, "aggregate.json")
            run_arguments = [
                "run",
                "--result",
                str(result_path),
                "--route",
                "fixture",
                "--protocol",
                "openai",
                "--url",
                "http://127.0.0.1/unused",
                "--model",
                "model",
                "--case",
                "short",
                "--candidate",
                "current",
                "--target-prompt-tokens",
                "8",
                "--max-output-tokens",
                "4",
            ]
            cases = {
                "invalid numeric type": [
                    *run_arguments[:-3],
                    "not-an-integer",
                    *run_arguments[-2:],
                ],
                "missing required argument": ["init"],
                "invalid choice": [
                    *run_arguments[:6],
                    "unsupported-protocol",
                    *run_arguments[7:],
                ],
                "unknown option": [
                    "init",
                    "--result",
                    str(result_path),
                    "--unknown-option",
                ],
            }
            for name, arguments in cases.items():
                with self.subTest(name=name):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    escaped_system_exit = None
                    code = None
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        try:
                            code = main(arguments)
                        except SystemExit as exc:
                            escaped_system_exit = exc.code

                    self.assertIsNone(escaped_system_exit)
                    self.assertEqual(code, 1)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertEqual(stderr.getvalue(), "benchmark operation failed\n")

    def test_confirmation_attempts_use_pooled_equal_work_for_retain_or_promote(self) -> None:
        confirmation_cases = {
            "pooled-below-ten": (
                1.07 / 1.12,
                "RETAIN",
                9.44292237442923,
            ),
            "pooled-at-or-above-ten": (0.94, "PROMOTE", 10.30927835051547),
        }
        for name, (
            confirmation_wall,
            expected_decision,
            expected_pooled_gain,
        ) in confirmation_cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory(
                dir=Path(__file__).parent
            ) as temp_dir:
                result_path = Path(temp_dir, "aggregate.json")
                code, _, _ = self.call_main(["init", "--result", str(result_path)])
                self.assertEqual(code, 0)

                code, _, stderr = self.run_aggregate_only_phase(
                    result_path,
                    candidate_id="baseline",
                    phase_id="staircase",
                    attempt_id="baseline-1",
                    wall_seconds=1.07,
                )
                self.assertEqual(code, 0, stderr)
                code, _, stderr = self.run_aggregate_only_phase(
                    result_path,
                    candidate_id="current",
                    phase_id="staircase",
                    attempt_id="first",
                    wall_seconds=1.0,
                    comparison_candidate_id="baseline",
                    comparison_attempt_id="baseline-1",
                )
                self.assertEqual(code, 0, stderr)
                code, _, stderr = self.run_aggregate_only_phase(
                    result_path,
                    candidate_id="current",
                    phase_id="staircase",
                    attempt_id="confirmation",
                    wall_seconds=confirmation_wall,
                    comparison_candidate_id="baseline",
                    comparison_attempt_id="baseline-1",
                    confirmation_completed=True,
                    confirmation_attempt_ids=("first",),
                )
                self.assertEqual(code, 0, stderr)

                document = json.loads(result_path.read_text(encoding="utf-8"))
                current_decisions = [
                    decision
                    for decision in document["candidate_decisions"]
                    if decision["candidate_id"] == "current"
                ]
                self.assertEqual(
                    [decision["decision"] for decision in current_decisions],
                    ["PENDING", expected_decision],
                )
                confirmation = current_decisions[-1]
                self.assertEqual(confirmation["confirmation_attempt_ids"], ["first"])
                self.assertIs(confirmation["confirmation_completed"], True)
                self.assertAlmostEqual(
                    confirmation["same_work_throughput_gain_percent"],
                    expected_pooled_gain,
                )
                self.assertEqual(document["final_verdict"], expected_decision)
                self.assertEqual(validate_aggregate_document(document), [])

    def test_lone_claimed_confirmation_cannot_promote(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            result_path = Path(temp_dir, "aggregate.json")
            code, _, _ = self.call_main(["init", "--result", str(result_path)])
            self.assertEqual(code, 0)
            code, _, stderr = self.run_aggregate_only_phase(
                result_path,
                candidate_id="baseline",
                phase_id="staircase",
                attempt_id="baseline-1",
                wall_seconds=1.12,
            )
            self.assertEqual(code, 0, stderr)

            code, _, stderr = self.run_aggregate_only_phase(
                result_path,
                candidate_id="current",
                phase_id="staircase",
                attempt_id="only",
                wall_seconds=1.0,
                comparison_candidate_id="baseline",
                comparison_attempt_id="baseline-1",
                confirmation_completed=True,
            )

            self.assertEqual(code, 1)
            self.assertEqual(stderr, "benchmark operation failed\n")
            document = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(document["final_verdict"], "PENDING")
            self.assertFalse(
                any(
                    decision["candidate_id"] == "current"
                    for decision in document["candidate_decisions"]
                )
            )

    def test_mismatched_requested_concurrency_cannot_promote(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            result_path = Path(temp_dir, "aggregate.json")
            code, _, _ = self.call_main(["init", "--result", str(result_path)])
            self.assertEqual(code, 0)
            code, _, stderr = self.run_aggregate_only_phase(
                result_path,
                candidate_id="baseline",
                phase_id="staircase",
                attempt_id="baseline-1",
                wall_seconds=1.12,
                concurrency=1,
            )
            self.assertEqual(code, 0, stderr)
            code, _, stderr = self.run_aggregate_only_phase(
                result_path,
                candidate_id="current",
                phase_id="staircase",
                attempt_id="first",
                wall_seconds=1.0,
                comparison_candidate_id="baseline",
                comparison_attempt_id="baseline-1",
                concurrency=4,
            )
            self.assertEqual(code, 0, stderr)

            document = json.loads(result_path.read_text(encoding="utf-8"))
            decision = document["candidate_decisions"][-1]
            self.assertEqual(decision["decision"], "PENDING")
            self.assertIsNone(decision["same_work_throughput_gain_percent"])
            self.assertEqual(document["batches"][0]["requested_concurrency"], 1)
            self.assertEqual(document["batches"][1]["requested_concurrency"], 4)
            self.assertEqual(validate_aggregate_document(document), [])

    def test_promoted_last_good_candidate_can_collect_next_staircase_target(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            result_path = Path(temp_dir, "aggregate.json")
            code, _, _ = self.call_main(["init", "--result", str(result_path)])
            self.assertEqual(code, 0)
            phases = (
                {
                    "candidate_id": "baseline",
                    "phase_id": "staircase",
                    "attempt_id": "baseline-1",
                    "wall_seconds": 1.07,
                },
                {
                    "candidate_id": "current",
                    "phase_id": "staircase",
                    "attempt_id": "first",
                    "wall_seconds": 1.0,
                    "comparison_candidate_id": "baseline",
                    "comparison_attempt_id": "baseline-1",
                },
                {
                    "candidate_id": "current",
                    "phase_id": "staircase",
                    "attempt_id": "confirmation",
                    "wall_seconds": 1.07 / 1.12,
                    "comparison_candidate_id": "baseline",
                    "comparison_attempt_id": "baseline-1",
                    "confirmation_completed": True,
                },
                {
                    "candidate_id": "current",
                    "phase_id": "next-target",
                    "attempt_id": "baseline-next",
                    "wall_seconds": 1.1,
                },
                {
                    "candidate_id": "next",
                    "phase_id": "next-target",
                    "attempt_id": "first",
                    "wall_seconds": 1.0,
                    "comparison_candidate_id": "current",
                    "comparison_attempt_id": "baseline-next",
                },
            )
            for phase in phases:
                code, _, stderr = self.run_aggregate_only_phase(
                    result_path, **phase
                )
                self.assertEqual(code, 0, stderr)

            document = json.loads(result_path.read_text(encoding="utf-8"))
            promoted = [
                decision
                for decision in document["candidate_decisions"]
                if decision["candidate_id"] == "current"
                and decision["phase_id"] == "staircase"
                and decision["attempt_id"] == "confirmation"
            ][0]
            next_decision = document["candidate_decisions"][-1]
            self.assertEqual(promoted["decision"], "PROMOTE")
            self.assertEqual(next_decision["candidate_id"], "next")
            self.assertEqual(next_decision["comparison_candidate_id"], "current")
            self.assertEqual(
                next_decision["comparison_attempt_id"], "baseline-next"
            )
            self.assertEqual(next_decision["decision"], "PROMOTE")
            self.assertEqual(document["final_verdict"], "PROMOTE")
            self.assertEqual(validate_aggregate_document(document), [])

    def test_cli_init_validate_summarize_and_report_use_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            result_path = Path(temp_dir, "aggregate.json")
            report_path = Path(temp_dir, "aggregate.md")

            code, _, _ = self.call_main(["init", "--result", str(result_path)])
            self.assertEqual(code, 0)
            document = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(document["observations"], [])
            self.assertEqual(document["prometheus_metric_name_allowlist"], [])
            self.assertEqual(document["decision_policy"], decision_policy_literal())
            self.assertEqual(
                document["decision_thresholds"], decision_thresholds_literal()
            )

            document["observations"] = valid_aggregate_document()["observations"]
            document["candidate_decisions"] = valid_aggregate_document()[
                "candidate_decisions"
            ]
            result_path.write_text(
                json.dumps(document, indent=2) + "\n", encoding="utf-8"
            )
            code, _, _ = self.call_main(
                ["summarize", "--result", str(result_path), "--report", str(report_path)]
            )
            self.assertEqual(code, 0)
            summarized = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertTrue(summarized["summaries"])
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("Time to First Token (TTFT", report)
            self.assertIn("client decode", report.lower())
            self.assertNotIn("discard-", report)
            self.assertEqual(build_report(summarized), report)

            repeated = dict(summarized["observations"][0])
            repeated["repetition"] = 2
            summarized["observations"].append(repeated)
            result_path.write_text(
                json.dumps(summarized, indent=2) + "\n", encoding="utf-8"
            )
            code, _, _ = self.call_main(
                ["summarize", "--result", str(result_path), "--report", str(report_path)]
            )
            self.assertEqual(code, 0)
            summarized = json.loads(result_path.read_text(encoding="utf-8"))
            elapsed_summary = summarized["summaries"][
                "baseline.default.1.local-openai.decode-short.elapsed_seconds"
            ]
            self.assertEqual(elapsed_summary["count"], 2)

            code, stdout, stderr = self.call_main(
                ["validate", "--result", str(result_path)]
            )
            self.assertEqual(code, 0)
            self.assertIn("valid", stdout.lower())
            self.assertEqual(stderr, "")

    def test_summarize_rejects_same_and_normalized_alias_paths_before_writes(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            result_path = Path(temp_dir, "aggregate.json")
            code, _, _ = self.call_main(["init", "--result", str(result_path)])
            self.assertEqual(code, 0)
            before = result_path.read_bytes()
            alias_parent = Path(temp_dir, "alias")
            alias_parent.mkdir()
            aliases = (
                result_path,
                alias_parent / ".." / result_path.name,
            )

            for report_path in aliases:
                with self.subTest(report_path=str(report_path)):
                    code, stdout, stderr = self.call_main(
                        [
                            "summarize",
                            "--result",
                            str(result_path),
                            "--report",
                            str(report_path),
                        ]
                    )

                    self.assertEqual(code, 1)
                    self.assertEqual(stdout, "")
                    self.assertEqual(stderr, "benchmark operation failed\n")
                    self.assertEqual(result_path.read_bytes(), before)

    def test_summarize_stages_both_outputs_and_reports_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            result_path = Path(temp_dir, "aggregate.json")
            report_path = Path(temp_dir, "aggregate.md")
            code, _, _ = self.call_main(["init", "--result", str(result_path)])
            self.assertEqual(code, 0)
            real_replace = os.replace
            staged_counts: list[int] = []
            replace_calls = 0

            def replace_with_report_failure(source: object, destination: object) -> None:
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 1:
                    staged_counts.append(
                        len(list(Path(temp_dir).glob(".*.tmp")))
                    )
                    real_replace(source, destination)
                    return
                raise OSError("coarse report replacement failure")

            with patch(
                "benchmark_dragon.os.replace",
                side_effect=replace_with_report_failure,
            ):
                code, stdout, stderr = self.call_main(
                    [
                        "summarize",
                        "--result",
                        str(result_path),
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "benchmark operation failed\n")
            self.assertEqual(staged_counts, [2])

    def test_cli_huge_json_integer_is_coarse_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            result_path = Path(temp_dir, "aggregate.json")
            document = valid_aggregate_document()
            document["observations"][0]["repetition"] = 10**400
            result_path.write_text(
                json.dumps(document, indent=2) + "\n", encoding="utf-8"
            )
            before = result_path.read_bytes()

            code, stdout, stderr = self.call_main(
                ["validate", "--result", str(result_path)]
            )

            self.assertEqual(code, 1)
            self.assertIn("invalid aggregate document", stdout)
            self.assertNotIn("Traceback", stdout + stderr)

            code, stdout, stderr = self.call_main(
                ["summarize", "--result", str(result_path)]
            )
            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "benchmark operation failed\n")
            self.assertEqual(result_path.read_bytes(), before)

    def test_cli_run_atomically_appends_and_preserves_prior_candidate_evidence(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            result_path = Path(temp_dir, "aggregate.json")
            code, _, _ = self.call_main(["init", "--result", str(result_path)])
            self.assertEqual(code, 0)
            document = json.loads(result_path.read_text(encoding="utf-8"))
            failed = valid_aggregate_document()["observations"][0]
            failed.update(
                {
                    "candidate_id": "previous",
                    "status": "failed",
                    "http_status": None,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "total_tokens": None,
                    "finish_reason": None,
                    "elapsed_seconds": 0.02,
                    "ttft_seconds": None,
                    "last_content_seconds": None,
                    "client_decode_tokens_per_second": None,
                    "end_to_end_output_tokens_per_second": None,
                    "effective_prompt_tokens_per_second": None,
                    "server_decode_tokens_per_second": None,
                    "error_category": "timeout",
                }
            )
            document["observations"].append(failed)
            previous_decision = calculated_decision(
                "previous", "REJECT", hard_gates_passed=False
            )
            document["candidate_decisions"].append(previous_decision)
            document["final_verdict"] = "REJECT"
            result_path.write_text(
                json.dumps(document, indent=2) + "\n", encoding="utf-8"
            )
            inode_before = result_path.stat().st_ino

            with LocalHTTPFixture() as fixture:
                code, _, stderr = self.call_main(
                    [
                        "run",
                        "--result",
                        str(result_path),
                        "--route",
                        "fixture",
                        "--protocol",
                        "openai",
                        "--url",
                        fixture.url("/ok-openai"),
                        "--model",
                        "model",
                        "--case",
                        "short",
                        "--candidate",
                        "current",
                        "--target-prompt-tokens",
                        "8",
                        "--max-output-tokens",
                        "4",
                        "--repetitions",
                        "1",
                        "--concurrency",
                        "1",
                        "--timeout-seconds",
                        "1",
                    ]
                )
            self.assertEqual(code, 0, stderr)
            updated = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertNotEqual(result_path.stat().st_ino, inode_before)
            self.assertEqual(len(updated["observations"]), 2)
            self.assertEqual(updated["observations"][0], failed)
            self.assertIn(previous_decision, updated["candidate_decisions"])
            self.assertIn(
                calculated_decision(
                    "current", "PENDING", hard_gates_passed=True
                ),
                updated["candidate_decisions"],
            )
            serialized = json.dumps(updated, sort_keys=True)
            self.assertNotIn(fixture.url("/ok-openai"), serialized)
            self.assertNotIn("discard-success", serialized)
            code, _, _ = self.call_main(
                ["validate", "--result", str(result_path)]
            )
            self.assertEqual(code, 0)

    def test_cli_run_persists_evidence_and_derives_the_candidate_decision(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            result_path = Path(temp_dir, "aggregate.json")
            document = valid_aggregate_document()
            baseline = dict(document["observations"][0])
            baseline.update(
                {
                    "route": "fixture",
                    "case_id": "short",
                    "prompt_tokens": 8,
                    "completion_tokens": 2,
                    "total_tokens": 10,
                    "client_decode_tokens_per_second": 1.6666666666666667,
                    "end_to_end_output_tokens_per_second": 2.0,
                    "effective_prompt_tokens_per_second": 40.0,
                }
            )
            document["observations"] = [baseline]
            document["batches"] = [
                aggregate_batch(
                    "baseline",
                    route="fixture",
                    case_id="short",
                    wall_seconds=1.12,
                    prompt_tokens=8,
                    completion_tokens=2,
                )
            ]
            document["candidate_decisions"] = [
                calculated_decision(
                    "baseline", "PENDING", hard_gates_passed=True
                )
            ]
            document["summaries"] = summaries_for_observations([baseline])
            result_path.write_text(
                json.dumps(document, indent=2) + "\n", encoding="utf-8"
            )

            def aggregate_only_run(
                route: RouteSpec,
                requests: list[RequestSpec],
                **kwargs: object,
            ) -> list[Observation]:
                batch_callback = kwargs.get("batch_callback")
                if callable(batch_callback):
                    batch_callback({"batch_wall_seconds": 1.0})
                return [
                    Observation(
                        route=route.name,
                        case_id=requests[0].case_id,
                        candidate_id=requests[0].candidate_id,
                        repetition=requests[0].repetition,
                        status="ok",
                        http_status=200,
                        prompt_tokens=8,
                        completion_tokens=2,
                        total_tokens=10,
                        finish_reason="stop",
                        elapsed_seconds=1.0,
                        ttft_seconds=0.2,
                        last_content_seconds=0.2 + (1.0 / 1.7),
                        client_decode_tokens_per_second=1.7,
                        end_to_end_output_tokens_per_second=2.0,
                        effective_prompt_tokens_per_second=40.0,
                        server_decode_tokens_per_second=None,
                        error_category=None,
                    )
                ]

            with patch("benchmark_dragon.run_batch", side_effect=aggregate_only_run):
                code, _, stderr = self.call_main(
                    [
                        "run",
                        "--result",
                        str(result_path),
                        "--route",
                        "fixture",
                        "--protocol",
                        "openai",
                        "--url",
                        "http://127.0.0.1/unused",
                        "--model",
                        "model",
                        "--case",
                        "short",
                        "--candidate",
                        "current",
                        "--target-prompt-tokens",
                        "8",
                        "--max-output-tokens",
                        "4",
                        "--comparison-candidate-id",
                        "baseline",
                        "--comparison-attempt-id",
                        "1",
                        "--hard-gates-passed",
                        "--same-work-throughput-gain-percent",
                        "12",
                        "--median-decode-regression-percent",
                        "-2",
                        "--p95-ttft-ratio",
                        "1.0",
                    ]
                )

            self.assertEqual(code, 0, stderr)
            document = json.loads(result_path.read_text(encoding="utf-8"))
            decision = document["candidate_decisions"][-1]
            self.assertEqual(decision["candidate_id"], "current")
            self.assertEqual(decision["decision"], "PROMOTE")
            self.assertIs(decision["hard_gates_passed"], True)
            self.assertAlmostEqual(
                decision["same_work_throughput_gain_percent"], 12.0
            )
            self.assertAlmostEqual(
                decision["median_decode_regression_percent"], -2.0
            )
            self.assertAlmostEqual(decision["p95_ttft_ratio"], 1.0)
            self.assertEqual(document["final_verdict"], "PROMOTE")
            code, _, _ = self.call_main(
                ["validate", "--result", str(result_path)]
            )
            self.assertEqual(code, 0)

    def test_cli_failed_partial_usage_is_excluded_from_performance_totals(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            result_path = Path(temp_dir, "aggregate.json")
            code, _, _ = self.call_main(["init", "--result", str(result_path)])
            self.assertEqual(code, 0)

            def aggregate_only_run(
                route: RouteSpec,
                requests: list[RequestSpec],
                **kwargs: object,
            ) -> list[Observation]:
                batch_callback = kwargs.get("batch_callback")
                if callable(batch_callback):
                    batch_callback({"batch_wall_seconds": 1.0})
                return [
                    Observation(
                        route=route.name,
                        case_id=requests[0].case_id,
                        candidate_id=requests[0].candidate_id,
                        repetition=requests[0].repetition,
                        status="failed",
                        http_status=200,
                        prompt_tokens=8,
                        completion_tokens=3,
                        total_tokens=11,
                        finish_reason=None,
                        elapsed_seconds=1.0,
                        ttft_seconds=None,
                        last_content_seconds=None,
                        client_decode_tokens_per_second=None,
                        end_to_end_output_tokens_per_second=None,
                        effective_prompt_tokens_per_second=None,
                        server_decode_tokens_per_second=None,
                        error_category="invalid_stream",
                    )
                ]

            with patch("benchmark_dragon.run_batch", side_effect=aggregate_only_run):
                code, _, stderr = self.call_main(
                    [
                        "run",
                        "--result",
                        str(result_path),
                        "--route",
                        "fixture",
                        "--protocol",
                        "openai",
                        "--url",
                        "http://127.0.0.1/unused",
                        "--model",
                        "model",
                        "--case",
                        "short",
                        "--candidate",
                        "partial-failure",
                        "--target-prompt-tokens",
                        "8",
                        "--max-output-tokens",
                        "4",
                    ]
                )

            self.assertEqual(code, 0, stderr)
            document = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(document["observations"][0]["completion_tokens"], 3)
            batch = document["batches"][0]
            self.assertEqual(batch["successful_count"], 0)
            self.assertEqual(batch["total_prompt_tokens"], 0)
            self.assertEqual(batch["total_completion_tokens"], 0)
            self.assertEqual(batch["total_tokens"], 0)
            self.assertIsNone(batch["aggregate_output_tokens_per_second"])
            self.assertEqual(validate_aggregate_document(document), [])

    def test_cli_failed_run_rejects_claimed_promotion_evidence(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            result_path = Path(temp_dir, "aggregate.json")
            code, _, _ = self.call_main(["init", "--result", str(result_path)])
            self.assertEqual(code, 0)

            with LocalHTTPFixture() as fixture:
                code, _, stderr = self.call_main(
                    [
                        "run",
                        "--result",
                        str(result_path),
                        "--route",
                        "fixture",
                        "--protocol",
                        "openai",
                        "--url",
                        fixture.url("/error"),
                        "--model",
                        "model",
                        "--case",
                        "short",
                        "--candidate",
                        "current",
                        "--target-prompt-tokens",
                        "8",
                        "--max-output-tokens",
                        "4",
                        "--comparison-candidate-id",
                        "baseline",
                        "--comparison-attempt-id",
                        "1",
                        "--hard-gates-passed",
                        "--same-work-throughput-gain-percent",
                        "12",
                        "--median-decode-regression-percent",
                        "0",
                        "--p95-ttft-ratio",
                        "1.0",
                    ]
                )

            self.assertEqual(code, 1)
            self.assertEqual(stderr, "benchmark operation failed\n")
            document = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(document["observations"], [])
            self.assertEqual(document["batches"], [])
            self.assertEqual(document["candidate_decisions"], [])
            self.assertEqual(document["final_verdict"], "PENDING")

    def test_cli_operator_rejection_preserves_derived_evidence_explicitly(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            result_path = Path(temp_dir, "aggregate.json")
            code, _, _ = self.call_main(["init", "--result", str(result_path)])
            self.assertEqual(code, 0)

            with LocalHTTPFixture() as fixture:
                code, _, stderr = self.call_main(
                    [
                        "run",
                        "--result",
                        str(result_path),
                        "--route",
                        "fixture",
                        "--protocol",
                        "openai",
                        "--url",
                        fixture.url("/ok-openai"),
                        "--model",
                        "model",
                        "--case",
                        "short",
                        "--candidate",
                        "rejected",
                        "--target-prompt-tokens",
                        "8",
                        "--max-output-tokens",
                        "4",
                        "--hard-gates-failed",
                    ]
                )

            self.assertEqual(code, 0, stderr)
            document = json.loads(result_path.read_text(encoding="utf-8"))
            decision = document["candidate_decisions"][0]
            self.assertEqual(decision["decision"], "REJECT")
            self.assertIs(decision["hard_gates_passed"], True)
            self.assertIs(decision["operator_rejection_override"], True)
            self.assertIsNone(decision["same_work_throughput_gain_percent"])
            self.assertIsNone(decision["median_decode_regression_percent"])
            self.assertIsNone(decision["p95_ttft_ratio"])
            self.assertEqual(document["final_verdict"], "REJECT")
            self.assertEqual(validate_aggregate_document(document), [])

    def test_cli_run_persists_reproducible_batch_server_decode_rate(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            result_path = Path(temp_dir, "aggregate.json")
            code, _, _ = self.call_main(["init", "--result", str(result_path)])
            self.assertEqual(code, 0)
            count_metric = "vllm:time_per_output_token_seconds_count"
            sum_metric = "vllm:time_per_output_token_seconds_sum"

            with LocalHTTPFixture() as fixture:
                code, _, stderr = self.call_main(
                    [
                        "run",
                        "--result",
                        str(result_path),
                        "--route",
                        "fixture",
                        "--protocol",
                        "openai",
                        "--url",
                        fixture.url("/slow-ok"),
                        "--model",
                        "model",
                        "--case",
                        "short",
                        "--candidate",
                        "current",
                        "--target-prompt-tokens",
                        "8",
                        "--max-output-tokens",
                        "4",
                        "--repetitions",
                        "2",
                        "--concurrency",
                        "2",
                        "--timeout-seconds",
                        "1",
                        "--metrics-url",
                        fixture.url("/metrics"),
                        "--metric-names",
                        f"{count_metric},{sum_metric}",
                        "--counter-names",
                        f"{count_metric},{sum_metric}",
                        "--server-decode-count-metric",
                        count_metric,
                        "--server-decode-sum-metric",
                        sum_metric,
                        "--metric-sample-interval-seconds",
                        "0.01",
                    ]
                )

            self.assertEqual(code, 0, stderr)
            document = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(
                document["prometheus_metric_name_allowlist"],
                [count_metric, sum_metric],
            )
            batch = document["batches"][0]
            self.assertEqual(batch["server_decode_count_delta"], 6.0)
            self.assertEqual(batch["server_decode_sum_delta"], 2.0)
            self.assertEqual(batch["server_decode_tokens_per_second"], 3.0)
            self.assertTrue(
                all(
                    observation["server_decode_tokens_per_second"] is None
                    for observation in document["observations"]
                )
            )

    def test_cli_rejects_an_empty_phase_before_local_network_access(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            result_path = Path(temp_dir, "aggregate.json")
            code, _, _ = self.call_main(["init", "--result", str(result_path)])
            self.assertEqual(code, 0)
            before = result_path.read_bytes()

            with LocalHTTPFixture() as fixture:
                code, _, _ = self.call_main(
                    [
                        "run",
                        "--result",
                        str(result_path),
                        "--route",
                        "fixture",
                        "--protocol",
                        "openai",
                        "--url",
                        fixture.url("/ok-openai"),
                        "--model",
                        "model",
                        "--case",
                        "short",
                        "--candidate",
                        "current",
                        "--target-prompt-tokens",
                        "8",
                        "--max-output-tokens",
                        "4",
                        "--repetitions",
                        "0",
                    ]
                )
                post_count = fixture.server.post_count

            self.assertNotEqual(code, 0)
            self.assertEqual(post_count, 0)
            self.assertEqual(result_path.read_bytes(), before)

    def test_cli_validates_all_inputs_before_metrics_requests_or_workers(self) -> None:
        missing_key = "BENCHMARK_DRAGON_TEST_MISSING_KEY"
        previous_value = os.environ.pop(missing_key, None)
        try:
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
                result_path = Path(temp_dir, "aggregate.json")
                code, _, _ = self.call_main(["init", "--result", str(result_path)])
                self.assertEqual(code, 0)
                before = result_path.read_bytes()

                with LocalHTTPFixture() as fixture:
                    base = [
                        "run",
                        "--result",
                        str(result_path),
                        "--route",
                        "fixture",
                        "--protocol",
                        "openai",
                        "--url",
                        fixture.url("/ok-openai"),
                        "--model",
                        "model",
                        "--case",
                        "short",
                        "--candidate",
                        "current",
                        "--target-prompt-tokens",
                        "8",
                        "--max-output-tokens",
                        "4",
                        "--repetitions",
                        "1",
                        "--concurrency",
                        "1",
                        "--timeout-seconds",
                        "1",
                        "--metrics-url",
                        fixture.url("/metrics"),
                        "--metric-names",
                        "vllm:num_requests_running",
                        "--gauge-names",
                        "vllm:num_requests_running",
                    ]
                    invalid_cases = {
                        "unsafe identifier": ["--route", "unsafe route"],
                        "invalid token count": ["--target-prompt-tokens", "0"],
                        "context overflow": [
                            "--target-prompt-tokens",
                            "262100",
                            "--max-output-tokens",
                            "100",
                        ],
                        "invalid concurrency": ["--concurrency", "0"],
                        "nan timeout": ["--timeout-seconds", "nan"],
                        "infinite timeout": ["--timeout-seconds", "inf"],
                        "nan metric interval": [
                            "--metric-sample-interval-seconds",
                            "nan",
                        ],
                        "infinite metric interval": [
                            "--metric-sample-interval-seconds",
                            "inf",
                        ],
                        "bad credential variable": ["--api-key-env", "bad-name"],
                        "missing credential": ["--api-key-env", missing_key],
                        "malformed route URL": ["--url", "http://["],
                        "malformed tokenizer URL": [
                            "--require-exact-count",
                            "--tokenize-url",
                            "file:///not-http",
                        ],
                        "malformed metrics URL": ["--metrics-url", "not-a-url"],
                        "invalid metric subset": [
                            "--counter-names",
                            "vllm:not_registered",
                        ],
                        "partial decision evidence": ["--hard-gates-passed"],
                        "nonfinite decision evidence": [
                            "--hard-gates-passed",
                            "--same-work-throughput-gain-percent",
                            "nan",
                            "--median-decode-regression-percent",
                            "0",
                            "--p95-ttft-ratio",
                            "1",
                        ],
                    }
                    for name, overrides in invalid_cases.items():
                        with self.subTest(name=name):
                            code, stdout, stderr = self.call_main([*base, *overrides])
                            self.assertNotEqual(code, 0)
                            self.assertEqual(stdout, "")
                            self.assertEqual(stderr, "benchmark operation failed\n")
                            self.assertEqual(fixture.server.metric_calls, 0)
                            self.assertEqual(fixture.server.post_count, 0)
                            self.assertEqual(result_path.read_bytes(), before)
        finally:
            if previous_value is not None:
                os.environ[missing_key] = previous_value

    def test_cli_refuses_to_rewrite_a_privacy_invalid_document(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
            result_path = Path(temp_dir, "aggregate.json")
            report_path = Path(temp_dir, "aggregate.md")
            code, _, _ = self.call_main(["init", "--result", str(result_path)])
            self.assertEqual(code, 0)
            document = json.loads(result_path.read_text(encoding="utf-8"))
            document["response_body"] = "private-marker-not-for-output"
            result_path.write_text(
                json.dumps(document, indent=2) + "\n", encoding="utf-8"
            )
            before = result_path.read_bytes()

            code, stdout, stderr = self.call_main(
                ["summarize", "--result", str(result_path), "--report", str(report_path)]
            )

            self.assertNotEqual(code, 0)
            self.assertEqual(result_path.read_bytes(), before)
            self.assertFalse(report_path.exists())
            self.assertNotIn("private-marker-not-for-output", stdout)
            self.assertNotIn("private-marker-not-for-output", stderr)


if __name__ == "__main__":
    unittest.main()
