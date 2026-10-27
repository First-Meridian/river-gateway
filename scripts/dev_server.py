"""Try the /ui console without a River key or any cost.

Runs the gateway against the fake River from tests/fake_river.py (in echo
mode), with demo keys and two weeks of synthetic usage in a throwaway
database. Then open http://127.0.0.1:8090/ui/ and sign in with the admin
token printed at startup.

    python scripts/dev_server.py [--port 8090]
"""

from __future__ import annotations

import argparse
import random
import socket
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # make tests.fake_river importable

from river_gateway.accounts import CONFIG_ACCOUNT_ID, CONFIG_ACCOUNT_NAME  # noqa: E402
from river_gateway.app import create_app  # noqa: E402
from river_gateway.config import GatewayConfig, Price  # noqa: E402
from river_gateway.pricing import compute_cost  # noqa: E402
from river_gateway.store import Store, UsageRecord  # noqa: E402
from river_gateway.usage import Usage  # noqa: E402
from tests.fake_river import PRICING, FakeRiver  # noqa: E402

ADMIN_TOKEN = "river-dev-admin-token"
TEAM_KEY = "rv_demo_team_key_0123456789"
REVOKED_KEY = "rv_demo_revoked_key_000000"
HOUR_WEIGHTS = [1, 1, 1, 1, 1, 2, 3, 5, 8, 10, 10, 9, 7, 9, 10, 10, 9, 8, 6, 5, 4, 3, 2, 1]
# (requested model, model that served it, weight)
TRAFFIC = [
    ("fast", "deepseek-ai/DeepSeek-V4.1-Flash", 50),
    ("smart", "moonshotai/Kimi-K3", 12),
    ("zai-org/GLM-5.3-Flash", "zai-org/GLM-5.3-Flash", 20),
    ("deepseek-ai/DeepSeek-V4.1-Flash", "deepseek-ai/DeepSeek-V4.1-Flash", 18),
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def seed(store: Store, rng: random.Random) -> int:
    """Demo keys plus synthetic usage for the last 14 days and today so far."""
    now = time.time()
    web, _ = store.create_key("web-app", rpm=120, max_concurrency=8, budget_usd=5.0, note="官网聊天助手")
    data, _ = store.create_key("data-team", allowed_models=["deepseek-ai/*", "fast"], rpm=60, max_concurrency=4, budget_usd=0.05, note="数据标注")
    batch, _ = store.create_key("batch-jobs", note="夜间批处理")
    store.create_key("old-demo", expires_at=now - 3 * 86400, note="已过期的示例")
    # River account pool: the config key, a second account and one whose key was revoked.
    store.create_account(CONFIG_ACCOUNT_NAME, api_key=None, source="config", account_id=CONFIG_ACCOUNT_ID,
                         note="config.yaml / .env 里的 RIVER_API_KEY")
    store.update_account(CONFIG_ACCOUNT_ID, balance_usd=5.0, balance_at=now - 7 * 86400)
    team = store.create_account("团队 B", api_key=TEAM_KEY, note="演示：第二个 River 账号")
    store.update_account(team.id, balance_usd=20.0, balance_at=now - 3 * 86400)
    old = store.create_account("旧账号", api_key=REVOKED_KEY, note="演示：key 已在 Console 作废")
    store.update_account(old.id, check_at=now - 3600, check_ok=False, check_status=401,
                         check_error="Missing or invalid Authorization header. Expected: Bearer <api-key>")
    prices = {
        item["model"]: Price(input=item["input"], cached_input=item["cached_input"], output=item["output"])
        for item in PRICING["inference"]
    }
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    records: list[UsageRecord] = []
    for days_ago in range(14, -1, -1):
        day_start = midnight - days_ago * 86400
        for _ in range(rng.randint(70, 170)):
            hour = rng.choices(range(24), weights=HOUR_WEIGHTS)[0]
            ts = day_start + hour * 3600 + rng.random() * 3600
            if ts > now:
                continue
            key = rng.choices([web, data, batch], weights=[6, 3, 1])[0]
            requested, target, _ = rng.choices(TRAFFIC, weights=[t[2] for t in TRAFFIC])[0]
            if key is data:
                requested, target = "fast", "deepseek-ai/DeepSeek-V4.1-Flash"
            stream = rng.random() < 0.6
            roll = rng.random()
            status, error, attempts = 200, None, 1
            if roll > 0.985:
                status, error = 400, "Unsupported parameter: logit_bias"
            elif roll > 0.97:
                status, error = 499, "client disconnected"
            elif roll > 0.955:
                status, error, attempts = 502, "cannot reach upstream: ConnectError", 3
            elif roll > 0.935:
                status, error = 429, "Too many requests"
            elif roll > 0.9:
                attempts = 2  # recovered after a retry
            usage = None
            if status == 200:
                prompt = rng.randint(200, 6000)
                completion = rng.randint(20, 1200)
                usage = Usage(
                    prompt_tokens=prompt,
                    cached_tokens=int(prompt * rng.choice([0, 0, 0.3, 0.6])),
                    completion_tokens=completion,
                    reasoning_tokens=int(completion * rng.choice([0, 0, 0.2, 0.5])),
                )
            latency = int(rng.lognormvariate(7.2, 0.6)) if status != 429 else rng.randint(40, 200)
            records.append(
                UsageRecord(
                    request_id="gw-" + uuid.UUID(int=rng.getrandbits(128)).hex[:24],
                    endpoint="chat" if rng.random() < 0.85 else "responses",
                    status=status,
                    key_id=key.id,
                    model=requested,
                    target_model=target,
                    upstream="river",
                    stream=stream,
                    attempts=attempts,
                    prompt_tokens=usage.prompt_tokens if usage else None,
                    cached_tokens=usage.cached_tokens if usage else None,
                    completion_tokens=usage.completion_tokens if usage else None,
                    reasoning_tokens=usage.reasoning_tokens if usage else None,
                    cost_usd=compute_cost(usage, prices.get(target)),
                    latency_ms=latency,
                    ttft_ms=int(latency * rng.uniform(0.1, 0.4)) if stream else None,
                    upstream_request_id=("chatcmpl-" + uuid.uuid4().hex[:11]) if status == 200 else None,
                    error=error,
                    account_id=team.id if rng.random() < 0.3 else CONFIG_ACCOUNT_ID,
                    ts=ts,
                )
            )
    # Insert in time order, as real traffic arrives (the log pages by insertion id).
    for record in sorted(records, key=lambda r: r.ts):
        store.record_usage(record)
    store.update_key(batch.id, enabled=False)
    return len(records)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()

    fake = FakeRiver()
    fake.echo = True
    fake.api_keys.add(TEAM_KEY)
    river_port = free_port()
    river = uvicorn.Server(uvicorn.Config(fake.app, host="127.0.0.1", port=river_port, log_level="warning"))
    threading.Thread(target=river.run, daemon=True).start()
    while not river.started:
        time.sleep(0.05)

    database = Path(tempfile.mkdtemp(prefix="river-gateway-dev-")) / "dev.db"
    store = Store(str(database))
    rows = seed(store, random.Random(7))
    store.close()

    config = GatewayConfig.model_validate(
        {
            "server": {"host": args.host, "port": args.port},
            "database": str(database),
            "admin_token": ADMIN_TOKEN,
            "upstreams": {"river": {"base_url": f"http://127.0.0.1:{river_port}/v1", "api_key": "rv_test_key"}},
            "pricing": {"url": f"http://127.0.0.1:{river_port}/api/public/pricing"},
            "models": [
                {
                    "name": "fast",
                    "target": "deepseek-ai/DeepSeek-V4.1-Flash",
                    "chat_defaults": {"reasoning_effort": "none"},
                    "fallbacks": ["zai-org/GLM-5.3-Flash"],
                },
                {"name": "smart", "target": "moonshotai/Kimi-K3", "fallbacks": ["deepseek-ai/DeepSeek-V4.1-Flash"]},
                {"name": "flaky-demo", "target": "fail/500", "fallbacks": ["moonshotai/Kimi-K3"]},
            ],
        }
    )
    print(f"Demo database with {rows} synthetic requests: {database}")
    print(f"Console:      http://{args.host}:{args.port}/ui/")
    print(f"Admin token:  {ADMIN_TOKEN}")
    print("Upstream is the local fake River (echo mode); nothing is sent to api.river.ai.")
    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
