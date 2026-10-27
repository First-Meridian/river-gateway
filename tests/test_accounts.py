"""The River account pool: switching keys, balance estimates and 402 handling."""

from __future__ import annotations

import pytest

from river_gateway.accounts import CONFIG_ACCOUNT_ID, key_hint
from river_gateway.store import UsageRecord
from tests.conftest import GatewayFactory, Harness
from tests.fake_river import API_KEY
from tests.test_proxy import CHAT, chat_body

TEAM_KEY = "rv_team_account_key_0123456789"


async def add_team(h: Harness, **extra: object) -> dict:
    h.fake.api_keys.add(TEAM_KEY)
    response = await h.admin("POST", "/admin/accounts", json={"name": "team", "api_key": TEAM_KEY, **extra})
    assert response.status_code == 201, response.text
    return response.json()


async def test_config_key_is_the_default_active_account(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    listing = (await h.admin("GET", "/admin/accounts")).json()
    [config_account] = listing["data"]
    assert listing["active"] == CONFIG_ACCOUNT_ID and listing["upstream"] == "river"
    assert config_account["source"] == "config" and config_account["active"] is True
    assert config_account["key_hint"] == key_hint(API_KEY) and "api_key" not in config_account

    await h.post(CHAT, chat_body(), h.create_key())
    assert h.fake.requests[-1]["headers"]["authorization"] == f"Bearer {API_KEY}"
    assert h.usage_rows()[0]["account_id"] == CONFIG_ACCOUNT_ID


async def test_adding_an_account_verifies_the_key(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    bad = await h.admin("POST", "/admin/accounts", json={"name": "x", "api_key": "rv_not_a_real_key_123"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_river_key"

    team = await add_team(h, note="billing: team B")
    assert team["check"]["ok"] is True and team["check"]["models"] == 3
    assert team["active"] is False and team["key_hint"] == key_hint(TEAM_KEY)
    duplicate = await h.admin("POST", "/admin/accounts", json={"name": "again", "api_key": TEAM_KEY})
    assert duplicate.status_code == 409


async def test_switching_accounts_changes_the_key_on_the_next_request(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    team = await add_team(h)
    key = h.create_key()

    activated = (await h.admin("POST", f"/admin/accounts/{team['id']}/activate")).json()
    assert activated["active"] == team["id"]
    await h.post(CHAT, chat_body(), key)
    assert h.fake.requests[-1]["headers"]["authorization"] == f"Bearer {TEAM_KEY}"
    assert h.usage_rows()[0]["account_id"] == team["id"]
    assert h.usage_rows()[0]["account_name"] == "team"
    models = await h.client.get("/v1/models", headers={"authorization": f"Bearer {key}"})
    assert models.status_code == 200 and len(models.json()["data"]) == 3
    status = (await h.admin("GET", "/admin/status")).json()
    assert status["account"] == {"id": team["id"], "name": "team", "low_balance": False}

    await h.admin("POST", f"/admin/accounts/{CONFIG_ACCOUNT_ID}/activate")
    await h.post(CHAT, chat_body(), key)
    assert h.fake.requests[-1]["headers"]["authorization"] == f"Bearer {API_KEY}"


async def test_balance_estimate_subtracts_gateway_spend_since_recording(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    team = await add_team(h, balance_usd=1.0)
    await h.admin("POST", f"/admin/accounts/{team['id']}/activate")
    key = h.create_key()
    await h.post(CHAT, chat_body(), key)
    await h.post(CHAT, chat_body(), key)

    [account] = [a for a in (await h.admin("GET", "/admin/accounts")).json()["data"] if a["id"] == team["id"]]
    cost = 2 * (800 * 0.12 + 200 * 0.0024 + 500 * 0.48) / 1e6
    assert account["balance"]["requests_since"] == 2
    assert account["balance"]["spent_since_usd"] == pytest.approx(cost)
    assert account["balance"]["estimated_remaining_usd"] == pytest.approx(1.0 - cost)
    assert account["usage"]["requests"] == 2

    # Recording a new balance restarts the count.
    updated = (await h.admin("PATCH", f"/admin/accounts/{team['id']}", json={"balance_usd": 5})).json()
    assert updated["balance"]["recorded_usd"] == 5 and updated["balance"]["requests_since"] == 0


async def test_a_402_marks_the_account_until_a_request_succeeds(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    team = await add_team(h)
    await h.admin("POST", f"/admin/accounts/{team['id']}/activate")
    key = h.create_key()

    h.fake.insufficient_keys.add(TEAM_KEY)
    response = await h.post(CHAT, chat_body(), key)
    assert response.status_code == 402
    [account] = [a for a in (await h.admin("GET", "/admin/accounts")).json()["data"] if a["id"] == team["id"]]
    assert account["low_balance"]["message"].startswith("Insufficient balance")
    status = (await h.admin("GET", "/admin/status")).json()
    assert status["account"]["low_balance"] is True
    assert "account_low_balance" in {w["code"] for w in status["warnings"]}

    h.fake.insufficient_keys.clear()
    assert (await h.post(CHAT, chat_body(), key)).status_code == 200
    [account] = [a for a in (await h.admin("GET", "/admin/accounts")).json()["data"] if a["id"] == team["id"]]
    assert account["low_balance"] is None


async def test_account_rules(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    team = await add_team(h)
    assert (await h.admin("DELETE", f"/admin/accounts/{CONFIG_ACCOUNT_ID}")).status_code == 400
    change_config_key = await h.admin("PATCH", f"/admin/accounts/{CONFIG_ACCOUNT_ID}", json={"api_key": "rv_whatever_123"})
    assert change_config_key.json()["error"]["code"] == "config_account_key"

    await h.admin("POST", f"/admin/accounts/{team['id']}/activate")
    assert (await h.admin("DELETE", f"/admin/accounts/{team['id']}")).status_code == 409
    await h.admin("POST", f"/admin/accounts/{CONFIG_ACCOUNT_ID}/activate")
    assert (await h.admin("DELETE", f"/admin/accounts/{team['id']}")).json()["deleted"] is True
    assert (await h.admin("POST", "/admin/accounts/acc_missing/activate")).status_code == 404


async def test_check_records_the_result(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    team = await add_team(h)
    h.fake.api_keys.discard(TEAM_KEY)  # the key was revoked in River Console
    result = (await h.admin("POST", f"/admin/accounts/{team['id']}/check")).json()
    assert result["result"]["ok"] is False and result["result"]["status"] == 401
    assert result["account"]["check"]["ok"] is False


async def test_usage_can_be_grouped_by_account(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    h.store.record_usage(UsageRecord(request_id="r", endpoint="chat", status=200, account_id=CONFIG_ACCOUNT_ID, cost_usd=0.5))
    rows = (await h.admin("GET", "/admin/usage", params={"group_by": "account"})).json()["data"]
    assert rows == [{**rows[0], "account": CONFIG_ACCOUNT_ID, "account_name": "配置文件中的 key", "cost_usd": 0.5}]
