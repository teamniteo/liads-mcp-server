"""Tests for the fixes from the first code review."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from liads_mcp.guards import GuardError

from .conftest import ACCOUNT, API, CAMPAIGN_ARGS, writes
from .test_service import _live


def test_failed_apply_resumes_without_duplicates(service, api):
    group = api.post(f"{API}/adAccounts/{ACCOUNT}/adCampaignGroups").respond(201, headers={"x-restli-id": "6001"})
    campaign = api.post(f"{API}/adAccounts/{ACCOUNT}/adCampaigns")
    campaign.side_effect = [httpx.Response(503, json={"message": "busy"}),
                            httpx.Response(201, headers={"x-restli-id": "7001"})]
    api.get(f"{API}/adAccounts/{ACCOUNT}/adCampaigns/7001").respond(200, json=_live())
    plan = service.draft_campaign(**CAMPAIGN_ARGS)
    first = service.confirm_and_apply([plan["plan_id"]], dry_run=False)["results"][0]
    assert "503" in first["error"] and first["applied"][0]["id"] == "6001"
    dry = service.confirm_and_apply([plan["plan_id"]], dry_run=True)["results"][0]
    assert dry["already_applied"] == ["campaign group 2026q4-e2-calibro"] and len(dry["would_apply"]) == 1
    second = service.confirm_and_apply([plan["plan_id"]], dry_run=False)["results"][0]
    assert "error" not in second
    assert group.call_count == 1 and campaign.call_count == 2
    assert json.loads(campaign.calls[1].request.content)["campaignGroup"] == "urn:li:sponsoredCampaignGroup:6001"


def test_duplicate_plan_ids_refused(service, api):
    plan = service.draft_campaign(**CAMPAIGN_ARGS)
    with pytest.raises(GuardError, match="more than once"):
        service.confirm_and_apply([plan["plan_id"], plan["plan_id"]], dry_run=False)
    assert writes(api) == []


def test_batch_keeps_report_of_applied_plan(service, api, cfg):
    patch = api.post(f"{API}/adAccounts/{ACCOUNT}/adCampaigns/7001").respond(204)
    api.get(f"{API}/adAccounts/{ACCOUNT}/adCampaigns/7001").respond(200, json=_live(status="PAUSED"))
    pause = service.pause_entity(ACCOUNT, "campaign", 7001)
    img = cfg.uploads.allowed_dir / "ad.png"
    img.write_bytes(b"x")
    ad = service.draft_image_ad(ACCOUNT, 7001, str(img), "t", "h", "https://calibro.example/demo", "a")
    img.unlink()  # the second plan's recheck now fails
    out = service.confirm_and_apply([pause["plan_id"], ad["plan_id"]], dry_run=False)["results"]
    assert out[0]["applied"][0]["step"].startswith("pause") and patch.call_count == 1
    assert "not a file" in out[1]["error"]


def test_readback_failure_still_reports_applied(service, api):
    api.post(f"{API}/adAccounts/{ACCOUNT}/adCampaigns/7001").respond(204)
    api.get(f"{API}/adAccounts/{ACCOUNT}/adCampaigns/7001").respond(500, json={"message": "down"})
    plan = service.pause_entity(ACCOUNT, "campaign", 7001)
    out = service.confirm_and_apply([plan["plan_id"]], dry_run=False)["results"][0]
    assert out["applied"] and "readback_error" in out


@pytest.mark.parametrize("over, message", [
    ({"unitCost": {"amount": "40", "currencyCode": "GBP"}}, "max_bid"),
    ({"costType": "CPM"}, "manual CPC"),
    ({"locale": {}}, "locale"),
])
def test_enable_checks_bid_bidding_and_locale(service, api, over, message):
    api.get(f"{API}/adAccounts/{ACCOUNT}/adCampaigns/7001").respond(200, json=_live(**over))
    with pytest.raises(GuardError, match=message):
        service.enable_entity(ACCOUNT, "campaign", 7001)


def test_dry_run_of_enable_rechecks_live(service, api):
    route = api.get(f"{API}/adAccounts/{ACCOUNT}/adCampaigns/7001")
    route.respond(200, json=_live())
    plan = service.enable_entity(ACCOUNT, "campaign", 7001)
    route.respond(200, json=_live(offsiteDeliveryEnabled=True))
    out = service.confirm_and_apply([plan["plan_id"]], dry_run=True)["results"][0]
    assert "Audience Network" in out["error"]


def test_enable_creative_checks_its_campaign(service, api):
    api.get(url__regex=rf"{API}/adAccounts/{ACCOUNT}/creatives/.*").respond(
        200, json={"campaign": "urn:li:sponsoredCampaign:7001"})
    api.get(f"{API}/adAccounts/{ACCOUNT}/adCampaigns/7001").respond(200, json=_live(audienceExpansionEnabled=True))
    with pytest.raises(GuardError, match="expansion"):
        service.enable_entity(ACCOUNT, "creative", "urn:li:sponsoredCreative:3")


def test_enable_group_needs_end_date(service, api):
    api.get(f"{API}/adAccounts/{ACCOUNT}/adCampaignGroups/6001").respond(200, json={"runSchedule": {"start": 1}})
    with pytest.raises(GuardError, match="end date"):
        service.enable_entity(ACCOUNT, "group", 6001)


def test_one_sided_budget_change_checked_against_current(service, api):
    api.get(f"{API}/adAccounts/{ACCOUNT}/adCampaigns/7001").respond(
        200, json=_live(dailyBudget={"amount": "10", "currencyCode": "GBP"},
                        totalBudget={"amount": "100", "currencyCode": "GBP"}))
    with pytest.raises(GuardError, match="larger than the lifetime"):
        service.draft_campaign_update(ACCOUNT, 7001, lifetime_budget=5)
    service.draft_campaign_update(ACCOUNT, 7001, daily_budget=19)  # 19 <= 100: fine


def test_end_date_covers_the_whole_day_and_one_day_campaigns(service):
    plan = service.draft_campaign(**{**CAMPAIGN_ARGS, "start": "2026-12-08", "end": "2026-12-08"})
    body = service.plans.get(plan["plan_id"]).ops[1]["body"]
    end = datetime.fromtimestamp(body["runSchedule"]["end"] / 1000, timezone.utc)
    assert (end.date().isoformat(), end.hour, end.minute) == ("2026-12-08", 23, 59)


def test_csv_with_bom_and_bad_value(service, cfg):
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    f = cfg.uploads.allowed_dir / "excel.csv"
    f.write_bytes(("﻿email,happened_at,value\n"
                   f"a@lab.example,{recent},12.5\n"
                   f'b@lab.example,{recent},"1,5"\n'
                   f"c@lab.example,{recent},\n").encode("utf-8"))
    plan = service.draft_conversion_upload(ACCOUNT, 88, str(f))
    assert plan["preview"]["events"] == 2 and plan["preview"]["skipped"]["bad value"] == 1


def test_all_failed_batch_is_an_error_and_plan_kept(service, api, cfg):
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    f = cfg.uploads.allowed_dir / "e.csv"
    f.write_text(f"email,happened_at\na@lab.example,{recent}\n")
    api.post(f"{API}/conversionEvents").respond(200, json={"elements": [{"status": 400}]})
    plan = service.draft_conversion_upload(ACCOUNT, 88, str(f))
    out = service.confirm_and_apply([plan["plan_id"]], dry_run=False)["results"][0]
    assert "rejected all" in out["error"]
    assert service.plans.get(plan["plan_id"])  # still there to retry


def test_targeting_updates_keep_the_other_side(service, api):
    current = {"include": {"and": [{"or": {"urn:li:adTargetingFacet:locations": ["urn:li:geo:1"]}}]},
               "exclude": {"or": {"urn:li:adTargetingFacet:titles": ["urn:li:title:9"]}}}
    api.get(f"{API}/adAccounts/{ACCOUNT}/adCampaigns/7001").respond(200, json=_live(targetingCriteria=current))
    only_exclude = service.draft_campaign_update(ACCOUNT, 7001, exclude=[{"facet": "titles", "values": ["urn:li:title:5"]}])
    t = service.plans.get(only_exclude["plan_id"]).ops[0]["fields"]["targetingCriteria"]
    assert t["include"] == current["include"]
    assert t["exclude"] == {"or": {"urn:li:adTargetingFacet:titles": ["urn:li:title:5"]}}
    only_include = service.draft_campaign_update(ACCOUNT, 7001, include=[{"facet": "locations", "values": ["urn:li:geo:2"]}])
    t = service.plans.get(only_include["plan_id"]).ops[0]["fields"]["targetingCriteria"]
    assert t["exclude"] == current["exclude"]


def test_audience_count_without_estimate(service, api):
    api.get(url__startswith=f"{API}/audienceCounts").respond(200, json={"elements": []})
    out = service.audience_count([{"facet": "locations", "values": ["urn:li:geo:1"]}])
    assert out["total"] is None and "no estimate" in out["note"]


def test_lists_follow_pages(service, api):
    route = api.get(url__startswith=f"{API}/adAccounts/{ACCOUNT}/adCampaigns")
    route.side_effect = [
        httpx.Response(200, json={"elements": [{"id": 1}], "metadata": {"nextPageToken": "p2"}}),
        httpx.Response(200, json={"elements": [{"id": 2}], "metadata": {}}),
    ]
    out = service.list_campaigns(ACCOUNT)
    assert [e["id"] for e in out["elements"]] == [1, 2] and out["truncated"] is False
    assert "pageToken=p2" in route.calls[1].request.url.query.decode()
