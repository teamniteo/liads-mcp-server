import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import httpx
import pytest

from liads_mcp.client import LinkedInError
from liads_mcp.guards import GuardError

from .conftest import ACCOUNT, API, CAMPAIGN_ARGS, TOKEN, writes


def test_draft_campaign_writes_nothing_and_fixes_settings(service, api):
    plan = service.draft_campaign(**CAMPAIGN_ARGS)
    assert writes(api) == []
    stored = service.plans.get(plan["plan_id"])
    group, campaign = stored.ops[0]["body"], stored.ops[1]["body"]
    assert group["status"] == "DRAFT" and group["totalBudget"] == {"amount": "1350.00", "currencyCode": "GBP"}
    assert campaign["status"] == "DRAFT"
    assert campaign["audienceExpansionEnabled"] is False
    assert campaign["offsiteDeliveryEnabled"] is False
    assert campaign["costType"] == "CPC"
    assert campaign["dailyBudget"] == {"amount": "14.80", "currencyCode": "GBP"}
    assert campaign["campaignGroup"] == "$ref:0:urn:li:sponsoredCampaignGroup:"
    assert "DRAFT" in plan["preview"]["fixed"]


def test_draft_refuses_over_cap_and_unknown_account(service):
    with pytest.raises(GuardError, match="max_daily_budget"):
        service.draft_campaign(**{**CAMPAIGN_ARGS, "daily_budget": 50})
    with pytest.raises(GuardError, match="allowed_accounts"):
        service.draft_campaign(**{**CAMPAIGN_ARGS, "account_id": 999})


def test_dry_run_writes_nothing(service, api):
    plan = service.draft_campaign(**CAMPAIGN_ARGS)
    out = service.confirm_and_apply([plan["plan_id"]], dry_run=True)
    assert writes(api) == []
    assert out["results"][0]["would_apply"][0].startswith("campaign group")


def test_apply_creates_group_then_campaign_with_ref(service, api, cfg):
    api.post(f"{API}/adAccounts/{ACCOUNT}/adCampaignGroups").respond(201, headers={"x-restli-id": "6001"})
    api.post(f"{API}/adAccounts/{ACCOUNT}/adCampaigns").respond(201, headers={"x-restli-id": "7001"})
    api.get(f"{API}/adAccounts/{ACCOUNT}/adCampaigns/7001").respond(
        200, json={"id": 7001, "status": "DRAFT", "audienceExpansionEnabled": False, "offsiteDeliveryEnabled": False}
    )
    plan = service.draft_campaign(**CAMPAIGN_ARGS)
    out = service.confirm_and_apply([plan["plan_id"]], dry_run=False)
    posted = writes(api)
    assert [r.url.path for r in posted] == [f"/rest/adAccounts/{ACCOUNT}/adCampaignGroups", f"/rest/adAccounts/{ACCOUNT}/adCampaigns"]
    body = json.loads(posted[1].content)
    assert body["campaignGroup"] == "urn:li:sponsoredCampaignGroup:6001"
    assert posted[0].headers["LinkedIn-Version"] == "202610"
    assert posted[0].headers["X-Restli-Protocol-Version"] == "2.0.0"
    assert out["results"][0]["readback"][0]["status"] == "DRAFT"
    log = cfg.safety.log_file.read_text()
    assert "6001" in log and TOKEN not in log
    with pytest.raises(KeyError):
        service.plans.get(plan["plan_id"])  # applied plans are removed


def test_post_is_never_retried(service, api):
    route = api.post(f"{API}/adAccounts/{ACCOUNT}/adCampaignGroups").respond(503, json={"message": "busy"})
    plan = service.draft_campaign(**CAMPAIGN_ARGS)
    out = service.confirm_and_apply([plan["plan_id"]], dry_run=False)
    assert route.call_count == 1
    assert "503" in out["results"][0]["error"]
    assert TOKEN not in out["results"][0]["error"]


def test_get_is_retried(service, api, monkeypatch):
    monkeypatch.setattr("liads_mcp.client.time.sleep", lambda s: None)
    route = api.get(f"{API}/adAccounts/{ACCOUNT}/adCampaignGroups")
    route.side_effect = [httpx.Response(503), httpx.Response(200, json={"elements": [{"id": 1}]})]
    assert service.list_campaign_groups(ACCOUNT) == [{"id": 1}]
    assert route.call_count == 2


def test_errors_do_not_leak_token(service, api):
    api.get(f"{API}/adAccounts/{ACCOUNT}/adCampaigns/5").respond(403, json={"message": "Not enough permissions", "serviceErrorCode": 100})
    with pytest.raises(LinkedInError) as exc:
        service.get_campaign(ACCOUNT, 5)
    assert "Not enough permissions" in str(exc.value) and TOKEN not in str(exc.value)


def _live(**over):
    c = {"id": 7001, "name": "c", "status": "DRAFT", "audienceExpansionEnabled": False, "offsiteDeliveryEnabled": False,
         "dailyBudget": {"amount": "14.80", "currencyCode": "GBP"}, "totalBudget": {"amount": "1350", "currencyCode": "GBP"},
         "unitCost": {"amount": "6", "currencyCode": "GBP"}, "runSchedule": {"start": 1796688000000, "end": 1803945600000}}
    c.update(over)
    return c


def test_budget_rise_is_its_own_plan(service, api):
    api.get(f"{API}/adAccounts/{ACCOUNT}/adCampaigns/7001").respond(200, json=_live())
    rise = service.draft_campaign_update(ACCOUNT, 7001, daily_budget=18)
    cut = service.draft_campaign_update(ACCOUNT, 7001, daily_budget=10)
    assert rise["kind"] == "budget_rise" and cut["kind"] == "change"
    with pytest.raises(GuardError, match="one plan at a time"):
        service.confirm_and_apply([rise["plan_id"], cut["plan_id"]], dry_run=True)


def test_enable_rechecks_campaign(service, api):
    route = api.get(f"{API}/adAccounts/{ACCOUNT}/adCampaigns/7001")
    route.respond(200, json=_live())
    plan = service.enable_entity(ACCOUNT, "campaign", 7001)
    assert plan["kind"] == "enable"
    route.respond(200, json=_live(audienceExpansionEnabled=True))  # someone changed it in Campaign Manager
    out = service.confirm_and_apply([plan["plan_id"]], dry_run=False)
    assert "expansion" in out["results"][0]["error"]
    assert writes(api) == []


def test_enable_applies_active(service, api):
    api.get(f"{API}/adAccounts/{ACCOUNT}/adCampaigns/7001").respond(200, json=_live())
    patch = api.post(f"{API}/adAccounts/{ACCOUNT}/adCampaigns/7001").respond(204)
    plan = service.enable_entity(ACCOUNT, "campaign", 7001)
    service.confirm_and_apply([plan["plan_id"]], dry_run=False)
    req = patch.calls[0].request
    assert req.headers["X-RestLi-Method"] == "PARTIAL_UPDATE"
    assert json.loads(req.content) == {"patch": {"$set": {"status": "ACTIVE"}}}


def test_pause(service, api):
    patch = api.post(f"{API}/adAccounts/{ACCOUNT}/adCampaigns/7001").respond(204)
    api.get(f"{API}/adAccounts/{ACCOUNT}/adCampaigns/7001").respond(200, json=_live(status="PAUSED"))
    plan = service.pause_entity(ACCOUNT, "campaign", 7001)
    out = service.confirm_and_apply([plan["plan_id"]], dry_run=False)
    assert json.loads(patch.calls[0].request.content) == {"patch": {"$set": {"status": "PAUSED"}}}
    assert out["results"][0]["readback"] == [{"campaign": 7001, "status": "PAUSED"}]


def test_image_ad_flow(service, api, cfg):
    img = cfg.uploads.allowed_dir / "ad.png"
    img.write_bytes(b"\x89PNG data")
    api.post(f"{API}/images").respond(
        200, json={"value": {"uploadUrl": "https://www.linkedin.com/dms-uploads/abc", "image": "urn:li:image:9"}}
    )
    upload = api.put("https://www.linkedin.com/dms-uploads/abc").respond(201)
    api.post(f"{API}/posts").respond(201, headers={"x-restli-id": "urn:li:share:42"})
    creative = api.post(f"{API}/adAccounts/{ACCOUNT}/creatives").respond(201, headers={"x-restli-id": "urn:li:sponsoredCreative:3"})
    plan = service.draft_image_ad(ACCOUNT, 7001, str(img), "Assessment week, sorted.", "See what's due",
                                  "https://calibro.example/demo?utm_source=linkedin", "assessment-week")
    service.confirm_and_apply([plan["plan_id"]], dry_run=False)
    assert upload.calls[0].request.content == b"\x89PNG data"
    posts =[c.request for c in api.calls if c.request.url.path == "/rest/posts"]
    post = json.loads(posts[0].content)
    assert post["distribution"]["feedDistribution"] == "NONE"
    assert post["content"]["media"]["id"] == "urn:li:image:9"
    body = json.loads(creative.calls[0].request.content)
    assert body == {"campaign": "urn:li:sponsoredCampaign:7001", "content": {"reference": "urn:li:share:42"},
                    "intendedStatus": "DRAFT", "name": "assessment-week"}


def test_upload_host_is_checked(service, api, cfg):
    img = cfg.uploads.allowed_dir / "ad.png"
    img.write_bytes(b"x")
    api.post(f"{API}/images").respond(200, json={"value": {"uploadUrl": "https://evil.example/u", "image": "urn:li:image:9"}})
    plan = service.draft_image_ad(ACCOUNT, 7001, str(img), "t", "h", "https://calibro.example/demo", "a")
    out = service.confirm_and_apply([plan["plan_id"]], dry_run=False)
    assert "non-LinkedIn host" in out["results"][0]["error"]


def test_image_outside_allowed_dir(service, tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"x")
    with pytest.raises(GuardError, match="outside"):
        service.draft_image_ad(ACCOUNT, 7001, str(img), "t", "h", "https://a.example", "a")


def test_conversion_upload_hashes_and_skips(service, api, cfg):
    recent = (datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=120)).date().isoformat()
    csv_file = cfg.uploads.allowed_dir / "enquiries.csv"
    csv_file.write_text(
        "email,li_fat_id,happened_at,value,event_id\n"
        f" Jane@Lab.example ,fat-1,{recent},,e1\n"
        f"old@lab.example,,{old},,e2\n"
        f",,{recent},,e3\n"
    )
    batch = api.post(f"{API}/conversionEvents").respond(201, json={"elements": [{"status": 201}]})
    plan = service.draft_conversion_upload(ACCOUNT, 88, str(csv_file))
    assert plan["preview"]["events"] == 1
    assert plan["preview"]["skipped"] == {"no id": 1, "older than 90 days": 1, "bad date": 0}
    service.confirm_and_apply([plan["plan_id"]], dry_run=False)
    req = batch.calls[0].request
    assert req.headers["X-RestLi-Method"] == "BATCH_CREATE"
    sent = req.content.decode()
    assert "jane@lab.example" not in sent.lower()
    el = json.loads(sent)["elements"][0]
    assert el["user"]["userIds"][0]["idValue"] == hashlib.sha256(b"jane@lab.example").hexdigest()
    assert el["conversion"] == "urn:lla:llaPartnerConversion:88" and el["eventId"] == "e1"


def test_conversion_rule_and_attach(service, api):
    api.post(f"{API}/conversions").respond(201, headers={"x-restli-id": "88"})
    attach = api.put(url__startswith=f"{API}/campaignConversions/").respond(204)
    plan = service.draft_conversion(ACCOUNT, "Enquiry", campaign_ids=[7001])
    service.confirm_and_apply([plan["plan_id"]], dry_run=False)
    path = unquote(attach.calls[0].request.url.raw_path.decode())
    assert "campaign:urn:li:sponsoredCampaign:7001" in path and "conversion:urn:lla:llaPartnerConversion:88" in path


def test_analytics_query(service, api):
    route = api.get(url__startswith=f"{API}/adAnalytics").respond(200, json={"elements": [{"clicks": 3}]})
    assert service.get_analytics(ACCOUNT, "2026-12-01", "2026-12-21", pivot="MEMBER_JOB_TITLE", campaign_ids=[7001]) == [{"clicks": 3}]
    q = unquote(route.calls[0].request.url.query.decode())
    assert "pivot=MEMBER_JOB_TITLE" in q and "campaigns=List(urn:li:sponsoredCampaign:7001)" in q
    assert "dateRange=(start:(year:2026,month:12,day:1),end:(year:2026,month:12,day:21))" in q
    with pytest.raises(ValueError):
        service.get_analytics(ACCOUNT, "2026-12-01", "2026-12-21", pivot="EVERYTHING")
