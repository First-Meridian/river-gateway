"""Live check: real River calls through a local gateway (costs a few hundred tokens).

Starts the gateway inside this process on a free local port, with your config
and River key but a throwaway database, creates a temporary gateway key, and
makes non-streaming and streaming calls to Chat Completions and Responses with
the official OpenAI SDK. For each call it prints the answer, the token usage
and estimated cost the gateway recorded, River's x-request-id and the latency.

    python scripts/live_check.py [--model fast] [--config config.yaml]
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import openai
import uvicorn

from river_gateway.app import create_app
from river_gateway.config import load_config

CHAT_PROMPT = "用一句话介绍一下你自己。"
STREAM_PROMPT = "从 1 数到 10，用逗号分隔，不要说别的。"
RESPONSES_PROMPT = "What is 2 + 2? Answer with just the number."


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LiveCheck:
    def __init__(self, app: Any, client: openai.OpenAI, model: str, max_tokens: int) -> None:
        self.app = app
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.failures: list[str] = []

    def usage_row(self, request_id: str) -> dict[str, Any] | None:
        # Streams are recorded when the gateway closes them, a moment after [DONE].
        for _ in range(40):
            for row in self.app.state.store.recent_usage(limit=20):
                if row["request_id"] == request_id:
                    return row
            time.sleep(0.05)
        return None

    def report(self, name: str, headers: Any, elapsed: float, extra: str = "") -> None:
        row = self.usage_row(headers.get("x-gateway-request-id", ""))
        river_id = headers.get("x-request-id") or "none sent by River"
        print(f"  River x-request-id : {river_id}; usage log id: {row['upstream_request_id'] if row else '-'}")
        print(f"  served by          : {headers.get('x-gateway-model')} @ {headers.get('x-gateway-upstream')}, attempts {headers.get('x-gateway-attempts')}")
        print(f"  client latency     : {elapsed * 1000:.0f} ms{extra}")
        if row is None:
            print("  gateway usage row  : (not found)")
            self.failures.append(f"{name}: no usage row")
            return
        if row["prompt_tokens"] is None:
            print(f"  gateway usage      : no token counts recorded (error: {row['error']})")
            self.failures.append(f"{name}: no usage recorded")
            return
        cost = f"${row['cost_usd']:.6f}" if row["cost_usd"] is not None else "unknown (no price)"
        print(
            f"  gateway usage      : prompt {row['prompt_tokens']} (cached {row['cached_tokens']}), "
            f"completion {row['completion_tokens']} (reasoning {row['reasoning_tokens']}), cost {cost}"
        )
        if row["error"]:
            print(f"  gateway note       : {row['error']}")

    def run(self, name: str, func: Any) -> None:
        print(f"\n=== {name} ===")
        try:
            func()
        except openai.APIError as exc:
            status = getattr(exc, "status_code", None)
            print(f"  FAILED: {type(exc).__name__} {status or ''}: {exc}")
            self.failures.append(name)

    def chat(self) -> None:
        started = time.perf_counter()
        raw = self.client.chat.completions.with_raw_response.create(
            model=self.model, messages=[{"role": "user", "content": CHAT_PROMPT}], max_tokens=self.max_tokens
        )
        elapsed = time.perf_counter() - started
        completion = raw.parse()
        choice = completion.choices[0]
        print(f"  answer             : {choice.message.content!r}  (finish_reason={choice.finish_reason})")
        self.report("chat", raw.headers, elapsed)

    def chat_stream(self) -> None:
        started = time.perf_counter()
        raw = self.client.chat.completions.with_raw_response.create(
            model=self.model,
            messages=[{"role": "user", "content": STREAM_PROMPT}],
            max_tokens=self.max_tokens,
            stream=True,
        )
        first_token = None
        chunks = empty_choice_chunks = 0
        finish_reason = None
        print("  streamed text      : ", end="", flush=True)
        for chunk in raw.parse():
            chunks += 1
            if not chunk.choices:
                empty_choice_chunks += 1
                continue
            delta = chunk.choices[0].delta.content or ""
            if delta and first_token is None:
                first_token = time.perf_counter() - started
            print(delta, end="", flush=True)
            finish_reason = chunk.choices[0].finish_reason or finish_reason
        elapsed = time.perf_counter() - started
        print(f"\n  stream             : {chunks} chunks, finish_reason={finish_reason}, usage-only chunks seen by client: {empty_choice_chunks}")
        ttft = f", first token {first_token * 1000:.0f} ms" if first_token is not None else ""
        self.report("chat stream", raw.headers, elapsed, ttft)
        if empty_choice_chunks:
            self.failures.append("chat stream: client saw a usage-only chunk it did not ask for")

    def responses(self) -> None:
        started = time.perf_counter()
        raw = self.client.responses.with_raw_response.create(
            model=self.model, input=RESPONSES_PROMPT, max_output_tokens=self.max_tokens
        )
        elapsed = time.perf_counter() - started
        response = raw.parse()
        print(f"  answer             : {response.output_text!r}  (status={response.status})")
        self.report("responses", raw.headers, elapsed)

    def responses_stream(self) -> None:
        started = time.perf_counter()
        raw = self.client.responses.with_raw_response.create(
            model=self.model, input=RESPONSES_PROMPT, max_output_tokens=self.max_tokens, stream=True
        )
        types: list[str] = []
        text = ""
        for event in raw.parse():
            types.append(event.type)
            if event.type == "response.output_text.delta":
                text += event.delta
        elapsed = time.perf_counter() - started
        distinct = list(dict.fromkeys(types))
        print(f"  streamed text      : {text!r}")
        print(f"  events             : {len(types)} ({', '.join(distinct)})")
        self.report("responses stream", raw.headers, elapsed)
        if not types or types[-1] != "response.completed":
            self.failures.append("responses stream: did not end with response.completed")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-c", "--config", help="gateway config (default: $RIVER_GATEWAY_CONFIG or ./config.yaml)")
    parser.add_argument("--model", default="fast", help="model or alias to call (default: fast)")
    parser.add_argument("--max-tokens", type=int, default=512)
    args = parser.parse_args()

    # Warnings (fallbacks, rejected parameters) still show; the per-request
    # details the access log would print are reported below instead.
    logging.basicConfig(level=logging.WARNING, format="  [gateway] %(levelname)s %(name)s: %(message)s")

    config = load_config(args.config)
    config.database = str(Path(tempfile.mkdtemp(prefix="river-gateway-live-")) / "live.db")
    app = create_app(config)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started or app.state.pricing.last_refresh is None:
        if time.time() > deadline or not thread.is_alive():
            print("gateway did not start (or pricing did not load) within 30 s")
            return 1
        time.sleep(0.05)

    _, secret = app.state.store.create_key("live-check")
    client = openai.OpenAI(api_key=secret, base_url=f"http://127.0.0.1:{port}/v1", timeout=600.0, max_retries=0)
    print(f"Gateway on http://127.0.0.1:{port}, model {args.model!r}, throwaway database {config.database}")
    check = LiveCheck(app, client, args.model, args.max_tokens)
    try:
        check.run("Chat Completions (non-streaming)", check.chat)
        check.run("Chat Completions (streaming)", check.chat_stream)
        check.run("Responses (non-streaming)", check.responses)
        check.run("Responses (streaming)", check.responses_stream)
        upstream = config.default_upstream or "river"
        injected_ok = app.state.gateway.pool.stream_usage_supported(upstream)
        print(f"\nRiver accepted stream_options.include_usage: {'yes' if injected_ok else 'no (gateway stopped adding it)'}")
        total = sum(row["cost_usd"] or 0 for row in app.state.store.recent_usage(limit=100))
        print(f"Total estimated cost of this check: ${total:.6f}")
    finally:
        server.should_exit = True
        thread.join(timeout=15)
    print("\nALL LIVE CHECKS PASSED" if not check.failures else f"\nFAILED: {check.failures}")
    return 1 if check.failures else 0


if __name__ == "__main__":
    sys.exit(main())
