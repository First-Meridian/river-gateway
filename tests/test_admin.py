"""Admin API, model listing and ops endpoints."""

from __future__ import annotations

from tests.conftest import GatewayFactory
from tests.test_proxy import CHAT, DEEPSEEK, GLM, chat_body


def auth(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}"}


async def test_admin_requires_the_admin_token(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    assert (await h.client.get("/admin/keys")).status_code == 401
    assert (await h.client.get("/admin/keys", headers=auth("nope"))).status_code == 401
    gateway_key = h.create_key()
    assert (await h.client.get("/admin/keys", headers=auth(gateway_key))).status_code == 401


async def test_admin_is_disabled_without_a_token(make_gateway: GatewayFactory) -> None:
    h = await make_gateway(admin_token=None)
    resp = await h.client.get("/admin/keys", headers=auth("anything"))
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "admin_disabled"


async def test_key_lifecycle_and_usage_reports(make_gateway: GatewayFactory) -> None:
    h = await make_gateway(key_defaults={"rpm": 30, "max_concurrency": 2})
    created = await h.admin("POST", "/admin/keys", json={"name": "team-a", "allowed_models": ["deepseek-ai/*"], "budget_usd": 5})
    assert created.status_code == 201
    secret, key = created.json()["secret"], created.json()["key"]
    assert secret.startswith("rgw_") and "key_hash" not in key
    assert (key["rpm"], key["max_concurrency"], key["budget_usd"]) == (30, 2, 5)

    assert (await h.post(CHAT, chat_body(), secret)).status_code == 200
    [listed] = (await h.admin("GET", "/admin/keys")).json()["data"]
    assert listed["spent_usd"] > 0 and listed["last_used_at"] is not None

    patched = await h.admin("PATCH", f"/admin/keys/{key['id']}", json={"enabled": False, "rpm": 0})
    assert patched.json()["enabled"] is False and patched.json()["rpm"] is None
    assert (await h.post(CHAT, chat_body(), secret)).status_code == 401

    summary = (await h.admin("GET", "/admin/usage", params={"group_by": "key"})).json()
    assert summary["totals"]["requests"] == 1
    assert summary["data"][0]["key_name"] == "team-a" and summary["data"][0]["key"] == key["id"]
    by_day = (await h.admin("GET", "/admin/usage", params={"group_by": "day", "since": "2020-01-01"})).json()
    assert by_day["data"][0]["requests"] == 1
    [recent] = (await h.admin("GET", "/admin/usage/recent")).json()["data"]
    assert recent["target_model"] == DEEPSEEK

    assert (await h.admin("DELETE", f"/admin/keys/{key['id']}")).json()["deleted"] is True
    assert (await h.admin("GET", f"/admin/keys/{key['id']}")).status_code == 404


async def test_admin_rejects_bad_input_in_openai_error_format(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    resp = await h.admin("POST", "/admin/keys", json={"name": "x", "rpm": -1})
    assert resp.status_code == 400 and resp.json()["error"]["param"] == "rpm"
    resp = await h.admin("GET", "/admin/usage", params={"group_by": "color"})
    assert resp.status_code == 400
    resp = await h.admin("GET", "/admin/usage", params={"since": "last tuesday"})
    assert resp.status_code == 400


async def test_admin_status_and_pricing_hide_secrets(make_gateway: GatewayFactory) -> None:
    h = await make_gateway(models=[{"name": "fast", "target": DEEPSEEK, "fallbacks": [GLM]}])
    status = (await h.admin("GET", "/admin/status")).json()
    assert status["upstreams"]["river"] == {"base_url": "http://river.test/v1", "api_key_set": True, "max_concurrency": None}
    assert status["models"] == [{"name": "fast", "upstream": "river", "target": DEEPSEEK, "fallbacks": [GLM]}]
    pricing = (await h.admin("GET", "/admin/pricing")).json()
    assert pricing["models"][DEEPSEEK] == {"input": 0.12, "cached_input": 0.0024, "output": 0.48}


async def test_models_merge_routes_with_river_and_respect_the_allowlist(make_gateway: GatewayFactory) -> None:
    h = await make_gateway(
        models=[{"name": "fast", "target": DEEPSEEK}, {"name": "internal", "target": GLM, "hidden": True}]
    )
    key = h.create_key(allowed_models=["fast", "deepseek-ai/*", "internal"])

    resp = await h.client.get("/v1/models", headers=auth(key))
    assert [model["id"] for model in resp.json()["data"]] == ["fast", DEEPSEEK]
    one = await h.client.get(f"/v1/models/{DEEPSEEK}", headers=auth(key))
    assert one.status_code == 200 and one.json()["owned_by"] == "river"
    assert (await h.client.get(f"/v1/models/{GLM}", headers=auth(key))).status_code == 404
    assert (await h.client.get("/v1/models")).status_code == 401


async def test_health_metrics_and_unknown_endpoints(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    assert (await h.client.get("/healthz")).json()["status"] == "ok"
    missing = await h.client.post("/v1/embeddings", json={})
    assert missing.status_code == 404 and "Unknown endpoint" in missing.json()["error"]["message"]

    key = h.create_key()
    await h.post(CHAT, chat_body(), key)
    metrics = (await h.client.get("/metrics")).text
    assert f'river_gateway_requests_total{{endpoint="chat",model="{DEEPSEEK}",status="200"}} 1.0' in metrics
    assert f'river_gateway_tokens_total{{kind="completion",model="{DEEPSEEK}"}} 500.0' in metrics
    assert "river_gateway_cost_usd_total" in metrics
