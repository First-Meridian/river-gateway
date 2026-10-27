"""End-to-end smoke test with real processes and the official OpenAI SDK.

Starts the fake River server (tests/fake_river.py) and the gateway with
uvicorn on free local ports, then drives the gateway through the `openai`
package: chat, streaming, tools, aliases, fallbacks, the Responses API,
model listing, error mapping, client disconnects, the admin API and the CLI.

Run from the project root:  python scripts/smoke_test.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import openai

ROOT = Path(__file__).resolve().parent.parent
ADMIN = "smoke-admin-token"
failures: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_healthy(url: str, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"process for {url} exited with {proc.returncode}")
        try:
            if httpx.get(url, timeout=1.0).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"{url} did not become healthy")


def main() -> int:
    river_port, gateway_port = free_port(), free_port()
    workdir = Path(tempfile.mkdtemp(prefix="river-gateway-smoke-"))
    config_path = workdir / "config.yaml"
    config_path.write_text(
        f"""
server: {{host: 127.0.0.1, port: {gateway_port}}}
database: gateway.db
admin_token: {ADMIN}
upstreams:
  river:
    base_url: http://127.0.0.1:{river_port}/v1
    api_key: rv_test_key
pricing:
  url: http://127.0.0.1:{river_port}/api/public/pricing
retry: {{max_retries: 1, backoff_base: 0.05, backoff_max: 0.1}}
models:
  - name: fast
    target: deepseek-ai/DeepSeek-V4.1-Flash
    chat_defaults: {{reasoning_effort: none}}
  - name: smart
    target: fail/500
    fallbacks: [moonshotai/Kimi-K3]
""",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    base = f"http://127.0.0.1:{gateway_port}"
    with open(workdir / "gateway.log", "w", encoding="utf-8") as gateway_log:
        river = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "tests.fake_river:app", "--host", "127.0.0.1", "--port", str(river_port), "--log-level", "warning"],
            cwd=ROOT,
            env=env,
        )
        gateway = subprocess.Popen(
            [sys.executable, "-m", "river_gateway", "-c", str(config_path), "serve"],
            cwd=ROOT,
            env=env,
            stdout=gateway_log,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_healthy(f"http://127.0.0.1:{river_port}/_fake/stats", river)
            wait_healthy(f"{base}/healthz", gateway)
            admin = httpx.Client(base_url=base, headers={"authorization": f"Bearer {ADMIN}"}, timeout=30)
            run_checks(base, f"http://127.0.0.1:{river_port}", admin, config_path)
        finally:
            for proc in (gateway, river):
                proc.terminate()
                proc.wait(timeout=10)
    print("\n--- gateway log (last 15 lines) ---")
    print("\n".join((workdir / "gateway.log").read_text(encoding="utf-8").splitlines()[-15:]))
    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} CHECK(S) FAILED: {failures}'}")
    return 1 if failures else 0


def run_checks(base: str, river_url: str, admin: httpx.Client, config_path: Path) -> None:
    created = admin.post("/admin/keys", json={"name": "smoke", "budget_usd": 10}).json()
    key = created["secret"]
    client = openai.OpenAI(api_key=key, base_url=f"{base}/v1", max_retries=0, timeout=30)

    completion = client.chat.completions.create(
        model="deepseek-ai/DeepSeek-V4.1-Flash", messages=[{"role": "user", "content": "Say hello."}], max_tokens=64
    )
    check("chat completion", completion.choices[0].message.content == "Hello from fake River!", completion)
    check("chat usage passes through", completion.usage.prompt_tokens == 1000, completion.usage)

    with client.chat.completions.create(
        model="fast", messages=[{"role": "user", "content": "Say hello."}], stream=True
    ) as stream:
        text = "".join(chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices)
    check("streamed chat via alias", text == "Hello from fake River!", text)

    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the current weather in a city.",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
            },
        }
    ]
    tool_reply = client.chat.completions.create(
        model="deepseek-ai/DeepSeek-V4.1-Flash", messages=[{"role": "user", "content": "Weather in Paris?"}], tools=tools
    )
    call = tool_reply.choices[0].message.tool_calls[0]
    check("tool calls pass through", call.function.name == "get_weather" and "Paris" in call.function.arguments, call)

    fallback = client.chat.completions.with_raw_response.create(
        model="smart", messages=[{"role": "user", "content": "hi"}]
    )
    check(
        "500s fall back to the next model",
        fallback.parse().model == "moonshotai/Kimi-K3" and fallback.headers["x-gateway-attempts"] == "3",
        dict(fallback.headers),
    )

    response = client.responses.create(
        model="deepseek-ai/DeepSeek-V4.1-Flash", input="What is 2 + 2?", reasoning={"effort": "high"}, max_output_tokens=1024
    )
    check("responses API", response.output_text == "4", response)

    events = [event.type for event in client.responses.create(model="deepseek-ai/DeepSeek-V4.1-Flash", input="2+2?", stream=True)]
    check("streamed responses API", events[-1] == "response.completed", events)

    model_ids = {model.id for model in client.models.list()}
    check("models list merges aliases and River models", {"fast", "smart", "zai-org/GLM-5.3-Flash"} <= model_ids, model_ids)

    restricted = admin.post("/admin/keys", json={"name": "restricted", "allowed_models": ["fast"]}).json()["secret"]
    try:
        openai.OpenAI(api_key=restricted, base_url=f"{base}/v1", max_retries=0).chat.completions.create(
            model="moonshotai/Kimi-K3", messages=[{"role": "user", "content": "hi"}]
        )
        check("model allowlist -> PermissionDeniedError", False, "no error raised")
    except openai.PermissionDeniedError as exc:
        check("model allowlist -> PermissionDeniedError", exc.code == "model_not_allowed", exc)

    try:
        client.responses.create(model="fast", input="hi", previous_response_id="resp_123")
        check("unsupported Responses params -> BadRequestError", False, "no error raised")
    except openai.BadRequestError as exc:
        check("unsupported Responses params -> BadRequestError", exc.param == "previous_response_id", exc)

    try:
        openai.OpenAI(api_key="rgw_wrong", base_url=f"{base}/v1", max_retries=0).models.list()
        check("bad key -> AuthenticationError", False, "no error raised")
    except openai.AuthenticationError:
        check("bad key -> AuthenticationError", True)

    # Client disconnect mid-stream: the gateway must cancel the River stream and log a 499.
    with httpx.stream(
        "POST",
        f"{base}/v1/chat/completions",
        headers={"authorization": f"Bearer {key}"},
        json={"model": "slow/stream", "messages": [{"role": "user", "content": "count"}], "stream": True},
        timeout=30,
    ) as slow:
        lines = 0
        for line in slow.iter_lines():
            lines += bool(line.startswith("data: "))
            if lines >= 3:
                break
    recent: list[dict] = []
    for _ in range(50):
        recent = admin.get("/admin/usage/recent", params={"limit": 1}).json()["data"]
        if recent and recent[0]["target_model"] == "slow/stream":
            break
        time.sleep(0.1)
    check(
        "client disconnect is logged as 499",
        bool(recent) and recent[0]["target_model"] == "slow/stream" and recent[0]["status"] == 499,
        recent,
    )
    check("disconnect finished well before the 4 s stream", bool(recent) and recent[0]["latency_ms"] < 3000, recent)
    stats: dict = {}
    for _ in range(30):
        stats = httpx.get(f"{river_url}/_fake/stats").json()
        if stats["cancelled_streams"]:
            break
        time.sleep(0.1)
    check("River saw the upstream stream cancelled", stats.get("cancelled_streams") == 1, stats)

    summary = admin.get("/admin/usage", params={"group_by": "target"}).json()
    print("\nusage by target model:")
    for row in summary["data"]:
        print(f"  {row['target'] or '-':38} requests={row['requests']:<3} ok={row['succeeded']:<3} cost=${row['cost_usd']:.6f}")
    check("costs were computed", summary["totals"]["cost_usd"] > 0, summary["totals"])

    cli = subprocess.run(
        [sys.executable, "-m", "river_gateway", "-c", str(config_path), "usage", "--by", "key"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    check("CLI usage report", cli.returncode == 0 and "smoke" in cli.stdout, cli.stdout + cli.stderr)
    print(cli.stdout)
    metrics = httpx.get(f"{base}/metrics").text
    check("metrics count retries", 'river_gateway_upstream_retries_total{reason="500",upstream="river"}' in metrics)
    status = admin.get("/admin/status").json()
    check("admin status loads pricing and hides keys", status["pricing"]["models"] == 3 and "rv_test_key" not in json.dumps(status), status)


if __name__ == "__main__":
    sys.exit(main())
