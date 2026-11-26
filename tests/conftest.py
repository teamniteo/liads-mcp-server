import json
import time

import pytest
import respx

from liads_mcp.config import Config
from liads_mcp.service import Service

API = "https://api.linkedin.com/rest"
ACCOUNT = 512345678
TOKEN = "secret-access-token-xyz"


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "client.json").write_text(json.dumps({"client_id": "cid", "client_secret": "csecret"}))
    (tmp_path / "token.json").write_text(
        json.dumps({"access_token": TOKEN, "expires_at": time.time() + 86400 * 30, "refresh_token": "r", "scope": "rw_ads"})
    )
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    return Config.model_validate(
        {
            "linkedin": {"client_path": str(tmp_path / "client.json"), "token_path": str(tmp_path / "token.json")},
            "ads": {"allowed_accounts": [ACCOUNT]},
            "safety": {
                "max_daily_budget": 20,
                "max_lifetime_budget": 1500,
                "max_bid": 15,
                "log_file": str(tmp_path / "audit.log"),
            },
            "uploads": {"allowed_dir": str(uploads)},
        }
    )


@pytest.fixture
def api():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{API}/adAccounts/{ACCOUNT}").respond(
            200,
            json={"id": ACCOUNT, "name": "Calibro", "currency": "GBP", "status": "ACTIVE",
                  "reference": "urn:li:organization:777"},
        )
        yield mock


@pytest.fixture
def service(cfg, api):
    return Service(cfg)


def writes(mock):
    return [c.request for c in mock.calls if c.request.method != "GET"]


CAMPAIGN_ARGS = dict(
    account_id=ACCOUNT,
    name="2026q4-e2-uk-lab-quality-managers",
    objective="WEBSITE_VISIT",
    daily_budget=14.80,
    lifetime_budget=1350,
    bid=6,
    start="2026-12-08",
    end="2027-02-28",
    country="US",
    language="en",
    include=[
        {"facet": "locations", "values": ["urn:li:geo:101165590"]},
        {"facet": "staffCountRanges", "values": ["urn:li:staffCountRange:(11,50)", "urn:li:staffCountRange:(51,200)"]},
        {"facet": "titles", "values": ["urn:li:title:123"]},
    ],
    new_group_name="2026q4-e2-calibro",
)
