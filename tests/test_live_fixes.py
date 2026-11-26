"""Fixes from the first live test against LinkedIn (2026-11-26)."""

import os
import time

import httpx
import pytest

from liads_mcp.client import LinkedInError
from liads_mcp.guards import GuardError

from .conftest import ACCOUNT, API, CAMPAIGN_ARGS


def test_validation_details_are_reported(service, api):
    api.post(f"{API}/adAccounts/{ACCOUNT}/adCampaignGroups").respond(201, headers={"x-restli-id": "6001"})
    api.post(f"{API}/adAccounts/{ACCOUNT}/adCampaigns").respond(400, json={
        "message": "Multiple errors occurred during the input validation.",
        "code": "MULTIPLE_VALIDATIONS_FAILED",
        "errorDetails": {"inputErrors": [
            {"description": "Invalid value", "input": {"inputPath": {"fieldPath": "/optimizationTargetType"}}, "code": "INVALID_VALUE"},
            {"description": "Field is required", "input": {"inputPath": {"fieldPath": "/format"}}, "code": "FIELD_REQUIRED"},
        ]},
    })
    plan = service.draft_campaign(**CAMPAIGN_ARGS)
    out = service.confirm_and_apply([plan["plan_id"]], dry_run=False)["results"][0]
    assert "/optimizationTargetType: Invalid value (INVALID_VALUE)" in out["error"]
    assert "/format: Field is required (FIELD_REQUIRED)" in out["error"]


@pytest.mark.parametrize("over, message", [
    ({"daily_budget": 5}, "minimum 10 a day"),
    ({"lifetime_budget": 50}, "minimum 100"),
])
def test_linkedin_minimums_checked_when_drafting(service, over, message):
    with pytest.raises(GuardError, match=message):
        service.draft_campaign(**{**CAMPAIGN_ARGS, **over})


def test_config_reloads_when_file_changes(tmp_path, monkeypatch, cfg):
    import yaml

    import liads_mcp.server as server

    path = tmp_path / "config.yaml"
    data = cfg.model_dump(mode="json")
    path.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("LIADS_CONFIG", str(path))
    monkeypatch.setattr(server, "_service", None)
    assert server.svc().cfg.ads.allowed_accounts == [ACCOUNT]
    data["ads"]["allowed_accounts"] = [ACCOUNT, 42]
    path.write_text(yaml.safe_dump(data))
    os.utime(path, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    assert server.svc().cfg.ads.allowed_accounts == [ACCOUNT, 42]


@pytest.mark.parametrize("language, country, message", [
    ("en", "GB", "use en_US"),
    ("es", "MX", "use es_ES"),
    ("sl", "SI", "doesn't support this language"),
])
def test_unsupported_ad_language_refused_with_hint(service, language, country, message):
    with pytest.raises(GuardError, match=message):
        service.draft_campaign(**{**CAMPAIGN_ARGS, "language": language, "country": country})


def test_supported_ad_language_accepted(service):
    plan = service.draft_campaign(**{**CAMPAIGN_ARGS, "language": "de", "country": "DE"})
    assert plan["preview"]["ad_language"] == "de_DE"
