import os
from datetime import date
from decimal import Decimal

import pytest

from liads_mcp import guards, restli
from liads_mcp.plans import resolve_refs
from liads_mcp.service import targeting
from liads_mcp.versions import version_status


def test_restli_encodes_urns_and_structures():
    assert restli.encode("urn:li:sponsoredAccount:1") == "urn%3Ali%3AsponsoredAccount%3A1"
    assert restli.encode(["a", "b"]) == "List(a,b)"
    assert restli.encode({"x": {"values": ["urn:li:geo:1"]}}) == "(x:(values:List(urn%3Ali%3Ageo%3A1)))"
    assert restli.encode(True) == "true"
    assert restli.date_range("2026-11-01", "2026-11-30") == {
        "start": {"year": 2026, "month": 11, "day": 1},
        "end": {"year": 2026, "month": 11, "day": 30},
    }
    assert restli.query({"q": "search", "skip": None, "n": 5}) == "q=search&n=5"


def test_version_status():
    assert version_status("202610", date(2026, 11, 26))["state"] == "ok"
    assert version_status("202610", date(2027, 9, 1))["state"] == "ends soon"
    assert version_status("202510", date(2026, 11, 26))["state"] == "sunset"


def test_targeting_shape():
    t = targeting([{"facet": "locations", "values": ["urn:li:geo:1"]}], [{"facet": "titles", "values": ["urn:li:title:9"]}])
    assert t == {
        "include": {"and": [{"or": {"urn:li:adTargetingFacet:locations": ["urn:li:geo:1"]}}]},
        "exclude": {"or": {"urn:li:adTargetingFacet:titles": ["urn:li:title:9"]}},
    }
    with pytest.raises(guards.GuardError):
        targeting([])


def test_resolve_refs():
    assert resolve_refs({"g": "$ref:0:urn:li:sponsoredCampaignGroup:"}, ["55"]) == {"g": "urn:li:sponsoredCampaignGroup:55"}
    assert resolve_refs("$ref:0:urn:x:", ["urn:li:share:1"]) == "urn:li:share:1"
    assert resolve_refs(["$ref:1"], ["a", "b"]) == ["b"]


def _campaign(**over):
    body = {
        "status": "DRAFT", "audienceExpansionEnabled": False, "offsiteDeliveryEnabled": False, "costType": "CPC",
        "locale": {"country": "GB", "language": "en"}, "politicalIntent": "NOT_POLITICAL",
        "runSchedule": {"start": 1, "end": 2},
        "dailyBudget": {"amount": "10", "currencyCode": "GBP"},
        "totalBudget": {"amount": "500", "currencyCode": "GBP"},
        "unitCost": {"amount": "5", "currencyCode": "GBP"},
        "targetingCriteria": {"include": {"and": [1]}},
    }
    body.update(over)
    return body


def test_campaign_guard_accepts_good_body(cfg):
    guards.check_campaign(cfg, _campaign(), "GBP")


@pytest.mark.parametrize(
    "over, message",
    [
        ({"status": "ACTIVE"}, "DRAFT"),
        ({"audienceExpansionEnabled": True}, "expansion"),
        ({"offsiteDeliveryEnabled": True}, "Audience Network"),
        ({"costType": "CPM"}, "manual CPC"),
        ({"locale": {}}, "locale"),
        ({"runSchedule": {"start": 1}}, "end"),
        ({"dailyBudget": {"amount": "25", "currencyCode": "GBP"}}, "max_daily_budget"),
        ({"totalBudget": {"amount": "2000", "currencyCode": "GBP"}}, "max_lifetime_budget"),
        ({"totalBudget": None}, "lifetime"),
        ({"unitCost": {"amount": "30", "currencyCode": "GBP"}}, "max_bid"),
        ({"dailyBudget": {"amount": "10", "currencyCode": "USD"}}, "GBP"),
        ({"dailyBudget": {"amount": "600", "currencyCode": "GBP"}, "totalBudget": {"amount": "500", "currencyCode": "GBP"}},
         "max_daily_budget"),
    ],
)
def test_campaign_guard_refuses(cfg, over, message):
    with pytest.raises(guards.GuardError, match=message):
        guards.check_campaign(cfg, _campaign(**over), "GBP")


def test_account_allow_list(cfg):
    guards.check_account(cfg, 512345678)
    with pytest.raises(guards.GuardError):
        guards.check_account(cfg, 1)


def test_patch_cannot_go_live_or_widen(cfg):
    with pytest.raises(guards.GuardError, match="pause"):
        guards.check_patch(cfg, "campaign", {"status": "ACTIVE"}, "GBP")
    with pytest.raises(guards.GuardError, match="cannot change"):
        guards.check_patch(cfg, "campaign", {"audienceExpansionEnabled": True}, "GBP")
    with pytest.raises(guards.GuardError, match="cannot change"):
        guards.check_patch(cfg, "campaign", {"offsiteDeliveryEnabled": True}, "GBP")
    guards.check_patch(cfg, "campaign", {"status": "PAUSED"}, "GBP")
    guards.check_patch(cfg, "creative", {"intendedStatus": "PAUSED"}, "GBP")


def test_safe_file(cfg, tmp_path):
    root = cfg.uploads.allowed_dir
    good = root / "ad.png"
    good.write_bytes(b"\x89PNG")
    assert guards.safe_file(cfg, str(good), "image") == good.resolve()
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"x")
    with pytest.raises(guards.GuardError, match="outside"):
        guards.safe_file(cfg, str(outside), "image")
    with pytest.raises(guards.GuardError, match="outside"):
        guards.safe_file(cfg, str(root / ".." / "secret.png"), "image")
    link = root / "link.png"
    os.symlink(outside, link)
    with pytest.raises(guards.GuardError, match="outside"):
        guards.safe_file(cfg, str(link), "image")
    txt = root / "a.txt"
    txt.write_text("x")
    with pytest.raises(guards.GuardError, match="jpg, png or gif"):
        guards.safe_file(cfg, str(txt), "image")


def test_amount_must_be_positive():
    with pytest.raises(guards.GuardError):
        guards.amount({"amount": "0"}, "x")
    assert guards.amount({"amount": "1.5"}, "x") == Decimal("1.5")
