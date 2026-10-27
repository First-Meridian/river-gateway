"""The /ui console's static serving and the admin endpoints it relies on."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from river_gateway.store import UsageRecord
from tests.conftest import GatewayFactory
from tests.test_proxy import CHAT, DEEPSEEK, GLM, chat_body


async def test_console_is_served_with_security_headers(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    root = await h.client.get("/")
    assert root.status_code in (302, 307) and root.headers["location"] == "/ui/"

    page = await h.client.get("/ui/")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert page.headers["x-content-type-options"] == "nosniff"
    assert 'src="js/app.js"' in page.text

    for asset in ("js/app.js", "js/api.js", "js/charts.js", "js/dom.js", "js/keys.js", "js/logs.js",
                  "js/overview.js", "js/playground.js", "js/status.js", "style.css"):
        response = await h.client.get(f"/ui/{asset}")
        assert response.status_code == 200, asset
        assert "content-security-policy" in response.headers


async def test_console_can_be_turned_off(make_gateway: GatewayFactory) -> None:
    h = await make_gateway(server={"ui": False})
    assert (await h.client.get("/ui/")).status_code == 404
    assert (await h.client.get("/")).status_code == 404


async def test_admin_models_include_routes_hidden_ones_and_upstream_models(make_gateway: GatewayFactory) -> None:
    h = await make_gateway(models=[
        {"name": "fast", "target": DEEPSEEK, "fallbacks": [GLM]},
        {"name": "internal", "target": GLM, "hidden": True},
    ])
    data = {m["id"]: m for m in (await h.admin("GET", "/admin/models")).json()["data"]}
    assert data["fast"]["route"] == {"upstream": "river", "target": DEEPSEEK, "fallbacks": [GLM], "hidden": False}
    assert data["internal"]["route"]["hidden"] is True
    assert data[DEEPSEEK]["route"] is None and data[DEEPSEEK]["owned_by"] == "river"


async def test_upstream_check_reports_reachability_and_key_problems(make_gateway: GatewayFactory) -> None:
    h = await make_gateway(upstreams={
        "river": {"base_url": "http://river.test/v1", "api_key": "rv_test_key"},
        "broken": {"base_url": "http://river.test/v1", "api_key": "rv_wrong"},
    })
    results = (await h.admin("GET", "/admin/upstreams/check")).json()["data"]
    assert results["river"]["ok"] is True and results["river"]["models"] == 3
    assert results["broken"]["ok"] is False and results["broken"]["status"] == 401
    assert "Authorization" in results["broken"]["error"]


async def test_status_lists_warnings_defaults_and_inflight(make_gateway: GatewayFactory) -> None:
    h = await make_gateway(key_defaults={"rpm": 30, "max_concurrency": 3},
                           upstreams={"river": {"base_url": "http://river.test/v1", "api_key": ""}})
    status = (await h.admin("GET", "/admin/status")).json()
    codes = {w["code"] for w in status["warnings"]}
    assert codes == {"weak_admin_token", "missing_upstream_key"}  # the test token is short
    assert status["key_defaults"] == {"rpm": 30, "max_concurrency": 3}
    assert status["inflight"] == 0


async def test_usage_buckets_follow_the_requested_time_zone(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    key, _ = h.store.create_key("tz")
    # 2026-10-25 23:30 UTC is 2026-10-26 07:30 in UTC+8.
    ts = datetime(2026, 10, 25, 23, 30, tzinfo=UTC).timestamp()
    h.store.record_usage(UsageRecord(request_id="r1", endpoint="chat", status=200, key_id=key.id, model="m", ts=ts))

    def buckets(group_by: str, tz_offset: int) -> list[str]:
        return [row[group_by] for row in h.store.usage_summary(group_by=group_by, utc_offset=tz_offset * 60)]

    assert buckets("day", 0) == ["2026-10-25"]
    assert buckets("day", 480) == ["2026-10-26"]
    assert buckets("hour", 480) == ["2026-10-26 07:00"]
    response = await h.admin("GET", "/admin/usage", params={"group_by": "hour", "tz_offset": 480})
    assert response.json()["data"][0]["hour"] == "2026-10-26 07:00"
    assert (await h.admin("GET", "/admin/usage", params={"group_by": "hour", "tz_offset": 9999})).status_code == 400


async def test_recent_usage_pages_filters_and_names_keys(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    key, secret = h.store.create_key("team-a")
    await h.post(CHAT, chat_body(), secret)
    await h.post(CHAT, chat_body("fail/400"), secret)
    for i in range(3):
        h.store.record_usage(UsageRecord(request_id=f"x{i}", endpoint="chat", status=200, key_id=key.id, ts=time.time()))

    first = (await h.admin("GET", "/admin/usage/recent", params={"limit": 2})).json()["data"]
    assert [row["request_id"] for row in first] == ["x2", "x1"] and first[0]["key_name"] == "team-a"
    rest = (await h.admin("GET", "/admin/usage/recent", params={"limit": 10, "before_id": first[-1]["id"]})).json()["data"]
    assert len(rest) == 3 and all(row["id"] < first[-1]["id"] for row in rest)
    errors = (await h.admin("GET", "/admin/usage/recent", params={"errors_only": "true"})).json()["data"]
    assert [row["status"] for row in errors] == [400]


async def test_swagger_offers_authorize_for_admin_routes(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    spec = (await h.client.get("/openapi.json")).json()
    assert spec["components"]["securitySchemes"]["HTTPBearer"]["type"] == "http"
    assert spec["paths"]["/admin/keys"]["get"]["security"] == [{"HTTPBearer": []}]
