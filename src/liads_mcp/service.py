"""Reads, drafts and apply, independent of the MCP layer (so tests can call them)."""

from __future__ import annotations

import csv
import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlparse

from . import guards, restli
from .auth import TokenStore
from .client import Client, LinkedInError
from .config import Config
from .plans import SOLO_KINDS, Plan, PlanStore, audit, resolve_refs
from .versions import version_status

ACCOUNT_URN = "urn:li:sponsoredAccount:"
GROUP_URN = "urn:li:sponsoredCampaignGroup:"
CAMPAIGN_URN = "urn:li:sponsoredCampaign:"
CREATIVE_URN = "urn:li:sponsoredCreative:"
CONVERSION_URN = "urn:lla:llaPartnerConversion:"
FACET_URN = "urn:li:adTargetingFacet:"

# Manual bidding: CPC with no automated optimisation target. Verify against a live account.
MANUAL_OPTIMIZATION = "NONE"
OBJECTIVES = {"WEBSITE_VISIT", "LEAD_GENERATION", "WEBSITE_CONVERSION", "ENGAGEMENT", "BRAND_AWARENESS"}
CTAS = {"LEARN_MORE", "SIGN_UP", "REGISTER", "REQUEST_DEMO", "DOWNLOAD", "SUBSCRIBE", "APPLY", "VIEW_QUOTE", "JOIN"}
PIVOTS = {
    "ACCOUNT", "CAMPAIGN_GROUP", "CAMPAIGN", "CREATIVE", "CONVERSION",
    "MEMBER_JOB_TITLE", "MEMBER_JOB_FUNCTION", "MEMBER_SENIORITY", "MEMBER_COMPANY_SIZE",
    "MEMBER_INDUSTRY", "MEMBER_COMPANY", "MEMBER_COUNTRY_V2", "MEMBER_REGION_V2",
}
ANALYTICS_FIELDS = [
    "pivotValues", "dateRange", "impressions", "clicks", "landingPageClicks", "costInLocalCurrency",
    "externalWebsiteConversions", "oneClickLeads", "approximateMemberReach",
]
INTRO_VISIBLE = 150
CONVERSION_BATCH = 5000
CONVERSION_MAX_AGE_DAYS = 90
LIST_LIMIT = 1000


def _num(entity_id) -> str:
    """'urn:li:sponsoredCampaign:123' or 123 -> '123'."""
    return str(entity_id).rsplit(":", 1)[-1]


def _money(value, currency: str) -> dict:
    return {"amount": str(Decimal(str(value)).quantize(Decimal("0.01"))), "currencyCode": currency}


def _facet(name: str) -> str:
    return name if name.startswith("urn:") else FACET_URN + name


def exclusion(exclude: list[dict]) -> dict:
    ors: dict = {}
    for f in exclude:
        ors.setdefault(_facet(f["facet"]), []).extend(f["values"])
    return {"or": ors}


def targeting(include: list[dict], exclude: list[dict] | None = None) -> dict:
    """include: [{"facet": "locations", "values": [urn, ...]}, ...] (facets AND-ed, values OR-ed);
    exclude: same shape, all OR-ed."""
    if not include:
        raise guards.GuardError("targeting needs at least one include facet")
    criteria = {"include": {"and": [{"or": {_facet(f["facet"]): list(f["values"])}} for f in include]}}
    if exclude:
        criteria["exclude"] = exclusion(exclude)
    return criteria


class Service:
    def __init__(self, cfg: Config, client: Client | None = None):
        self.cfg = cfg
        self.client = client or Client(cfg)
        self.plans = PlanStore(cfg.plans_file, cfg.safety.plan_ttl_hours)

    # ---------- reads ----------

    def health_check(self) -> dict:
        return {
            "token": TokenStore(self.cfg).status(),
            "api_version": version_status(self.cfg.linkedin.api_version),
            "allowed_accounts": self.cfg.ads.allowed_accounts,
            "caps": {
                "max_daily_budget": str(self.cfg.safety.max_daily_budget),
                "max_lifetime_budget": str(self.cfg.safety.max_lifetime_budget),
                "max_bid": str(self.cfg.safety.max_bid),
            },
            "pending_plans": self.plans.list(),
        }

    def _paged(self, path: str, params: dict) -> dict:
        """Follow nextPageToken up to LIST_LIMIT items; says when it stopped early."""
        items, token = [], None
        while True:
            q = dict(params, pageSize=100, **({"pageToken": token} if token else {}))
            data = self.client.get(path, restli.query(q))
            items.extend(data.get("elements", []))
            token = (data.get("metadata") or {}).get("nextPageToken")
            if not token or len(items) >= LIST_LIMIT:
                break
        return {"elements": items[:LIST_LIMIT], "count": min(len(items), LIST_LIMIT),
                "truncated": bool(token) or len(items) > LIST_LIMIT}

    def list_ad_accounts(self) -> dict:
        page = self._paged("/adAccounts", {"q": "search"})
        page["elements"] = [
            {
                "id": a.get("id"), "name": a.get("name"), "currency": a.get("currency"), "status": a.get("status"),
                "type": a.get("type"), "reference": a.get("reference"),
                "allowed": a.get("id") in self.cfg.ads.allowed_accounts,
            }
            for a in page["elements"]
        ]
        return page

    def get_ad_account(self, account_id: int) -> dict:
        return self.client.get(f"/adAccounts/{int(account_id)}")

    def list_campaign_groups(self, account_id: int) -> dict:
        return self._paged(f"/adAccounts/{int(account_id)}/adCampaignGroups", {"q": "search"})

    def list_campaigns(self, account_id: int, group_id: int | None = None) -> dict:
        params: dict = {"q": "search"}
        if group_id:
            params["search"] = {"campaignGroup": {"values": [GROUP_URN + _num(group_id)]}}
        return self._paged(f"/adAccounts/{int(account_id)}/adCampaigns", params)

    def get_campaign(self, account_id: int, campaign_id) -> dict:
        return self.client.get(f"/adAccounts/{int(account_id)}/adCampaigns/{_num(campaign_id)}")

    def list_creatives(self, account_id: int, campaign_id: int | None = None) -> dict:
        params: dict = {"q": "criteria"}
        if campaign_id:
            params["campaigns"] = [CAMPAIGN_URN + _num(campaign_id)]
        return self._paged(f"/adAccounts/{int(account_id)}/creatives", params)

    def get_targeting_facets(self) -> list[dict]:
        return self.client.get("/adTargetingFacets").get("elements", [])

    def search_targeting(self, facet: str, text: str) -> list[dict]:
        data = self.client.get(
            "/adTargetingEntities", restli.query({"q": "typeahead", "facet": _facet(facet), "query": text})
        )
        return [{"urn": e.get("urn"), "name": e.get("name"), "facet": e.get("facetUrn")} for e in data.get("elements", [])]

    def audience_count(self, include: list[dict], exclude: list[dict] | None = None) -> dict:
        data = self.client.get(
            "/audienceCounts", restli.query({"q": "targetingCriteriaV2", "targetingCriteria": targeting(include, exclude)})
        )
        elements = data.get("elements") or []
        if not elements:
            return {"total": None, "active": None, "note": "LinkedIn returned no estimate for this targeting"}
        return {"total": elements[0].get("total"), "active": elements[0].get("active")}

    def get_analytics(self, account_id: int, start: str, end: str, pivot: str = "CAMPAIGN",
                      campaign_ids: list | None = None, granularity: str = "ALL") -> list[dict]:
        if pivot not in PIVOTS:
            raise ValueError(f"pivot must be one of {sorted(PIVOTS)}")
        params: dict = {
            "q": "analytics", "pivot": pivot, "timeGranularity": granularity,
            "dateRange": restli.date_range(start, end),
        }
        if campaign_ids:
            params["campaigns"] = [CAMPAIGN_URN + _num(c) for c in campaign_ids]
        else:
            params["accounts"] = [ACCOUNT_URN + str(int(account_id))]
        params["fields"] = ",".join(ANALYTICS_FIELDS)
        query = restli.query({k: v for k, v in params.items() if k != "fields"}) + "&fields=" + params["fields"]
        return self.client.get("/adAnalytics", query).get("elements", [])

    def list_conversions(self, account_id: int) -> dict:
        return self._paged("/conversions", {"q": "account", "account": ACCOUNT_URN + str(int(account_id))})

    # ---------- drafts ----------

    def _account(self, account_id: int) -> dict:
        guards.check_account(self.cfg, account_id)
        acct = self.get_ad_account(account_id)
        if not acct.get("currency"):
            raise guards.GuardError("ad account has no currency")
        return acct

    def _plan(self, kind: str, account_id: int, currency: str, summary: str, preview: dict, ops: list[dict]) -> dict:
        plan = self.plans.add(kind, account_id, currency, summary, preview, ops)
        note = "Needs its own approval (not batched)." if kind in SOLO_KINDS else "Apply with confirm_and_apply."
        return {"plan_id": plan.id, "kind": kind, "summary": summary, "preview": preview, "note": note}

    def draft_campaign(self, account_id: int, name: str, objective: str, daily_budget, lifetime_budget, bid,
                       start: str, end: str, country: str, language: str, include: list[dict],
                       exclude: list[dict] | None = None, group_id: int | None = None,
                       new_group_name: str | None = None) -> dict:
        acct = self._account(account_id)
        cur = acct["currency"]
        if objective not in OBJECTIVES:
            raise guards.GuardError(f"objective must be one of {sorted(OBJECTIVES)}")
        if bool(group_id) == bool(new_group_name):
            raise guards.GuardError("give either group_id or new_group_name")
        schedule = {"start": guards.ms(start), "end": guards.ms_end(end)}
        ops: list[dict] = []
        if new_group_name:
            group = {
                "account": ACCOUNT_URN + str(int(account_id)), "name": new_group_name, "status": "DRAFT",
                "runSchedule": schedule, "totalBudget": _money(lifetime_budget, cur),
            }
            guards.check_group(self.cfg, group, cur)
            ops.append({"op": "create", "check": "group", "label": f"campaign group {new_group_name}",
                        "path": f"/adAccounts/{int(account_id)}/adCampaignGroups", "body": group})
            group_urn = "$ref:0:" + GROUP_URN
        else:
            group_urn = GROUP_URN + _num(group_id)
        body = {
            "account": ACCOUNT_URN + str(int(account_id)), "campaignGroup": group_urn, "name": name,
            "type": "SPONSORED_UPDATES", "objectiveType": objective, "costType": "CPC",
            "optimizationTargetType": MANUAL_OPTIMIZATION, "unitCost": _money(bid, cur),
            "dailyBudget": _money(daily_budget, cur), "totalBudget": _money(lifetime_budget, cur),
            "runSchedule": schedule, "locale": {"country": country.upper(), "language": language.lower()},
            "targetingCriteria": targeting(include, exclude), "status": "DRAFT",
            "audienceExpansionEnabled": False, "offsiteDeliveryEnabled": False, "politicalIntent": "NOT_POLITICAL",
        }
        guards.check_campaign(self.cfg, body, cur)
        ops.append({"op": "create", "check": "campaign", "label": f"campaign {name}",
                    "path": f"/adAccounts/{int(account_id)}/adCampaigns", "body": body})
        preview = {
            "account": f"{acct.get('name')} ({account_id}, {cur})",
            "group": new_group_name or group_id, "campaign": name, "objective": objective,
            "bidding": f"manual CPC, bid {body['unitCost']['amount']} {cur}",
            "budget": f"{body['dailyBudget']['amount']} {cur}/day, lifetime {body['totalBudget']['amount']} {cur}",
            "schedule": f"{start} to {end}", "ad_language": f"{language.lower()}_{country.upper()}",
            "targeting": {"include": include, "exclude": exclude or []},
            "fixed": "status DRAFT, audience expansion off, LinkedIn Audience Network off",
        }
        return self._plan("change", account_id, cur, f"Create campaign {name} (DRAFT)", preview, ops)

    def draft_campaign_update(self, account_id: int, campaign_id, name: str | None = None, daily_budget=None,
                              lifetime_budget=None, bid=None, end: str | None = None,
                              include: list[dict] | None = None, exclude: list[dict] | None = None) -> dict:
        acct = self._account(account_id)
        cur = acct["currency"]
        current = self.get_campaign(account_id, campaign_id)
        fields: dict = {}
        rise = False

        def higher(key: str, value) -> bool:
            old = current.get(key, {}).get("amount")
            return old is None or Decimal(str(value)) > Decimal(str(old))

        if name:
            fields["name"] = name
        if daily_budget is not None:
            fields["dailyBudget"] = _money(daily_budget, cur)
            rise |= higher("dailyBudget", daily_budget)
        if lifetime_budget is not None:
            fields["totalBudget"] = _money(lifetime_budget, cur)
            rise |= higher("totalBudget", lifetime_budget)
        if bid is not None:
            fields["unitCost"] = _money(bid, cur)
            rise |= higher("unitCost", bid)
        if end:
            fields["runSchedule"] = {"start": current.get("runSchedule", {}).get("start"), "end": guards.ms_end(end)}
        if include is not None or exclude is not None:
            # Keep the part of the targeting that isn't being changed, so exclusions aren't dropped.
            old = current.get("targetingCriteria") or {}
            if include is not None:
                new = targeting(include, exclude)
                if exclude is None and old.get("exclude"):
                    new["exclude"] = old["exclude"]
            else:
                if not old.get("include"):
                    raise guards.GuardError("campaign has no include targeting to keep")
                new = {"include": old["include"]}
                if exclude:
                    new["exclude"] = exclusion(exclude)
            fields["targetingCriteria"] = new
        if not fields:
            raise guards.GuardError("nothing to change")
        budgets = {k: current.get(k) for k in ("dailyBudget", "totalBudget") if current.get(k)}
        guards.check_patch(self.cfg, "campaign", fields, cur, budgets)
        ops = [{"op": "patch", "check": "campaign", "label": f"update campaign {_num(campaign_id)}",
                "path": f"/adAccounts/{int(account_id)}/adCampaigns/{_num(campaign_id)}", "fields": fields,
                "current": budgets}]
        preview = {"campaign": f"{current.get('name')} ({_num(campaign_id)})",
                   "changes": {k: {"from": current.get(k), "to": v} for k, v in fields.items()}}
        kind = "budget_rise" if rise else "change"
        return self._plan(kind, account_id, cur, f"Update campaign {current.get('name')}", preview, ops)

    def draft_image_ad(self, account_id: int, campaign_id, image_path: str, intro_text: str, headline: str,
                       landing_url: str, ad_name: str, call_to_action: str = "LEARN_MORE") -> dict:
        acct = self._account(account_id)
        cur = acct["currency"]
        org = acct.get("reference")
        if not org or not org.startswith("urn:li:organization:"):
            raise guards.GuardError("ad account isn't linked to a company page (reference urn:li:organization:…)")
        image = guards.safe_file(self.cfg, image_path, "image")
        if urlparse(landing_url).scheme != "https":
            raise guards.GuardError("landing_url must be https")
        if call_to_action not in CTAS:
            raise guards.GuardError(f"call_to_action must be one of {sorted(CTAS)}")
        if len(intro_text) > 3000 or len(headline) > 200:
            raise guards.GuardError("intro text max 3,000 characters, headline max 200")
        post = {
            "author": org, "commentary": intro_text, "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "NONE", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "content": {"media": {"title": headline, "id": "$ref:0"}},
            "contentLandingPage": landing_url, "contentCallToActionLabel": call_to_action,
            "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": True,
            "adContext": {"dscAdAccount": ACCOUNT_URN + str(int(account_id)), "dscStatus": "ACTIVE"},
        }
        guards.check_post(post)
        creative = {"campaign": CAMPAIGN_URN + _num(campaign_id), "content": {"reference": "$ref:1"},
                    "intendedStatus": "DRAFT", "name": ad_name}
        guards.check_creative(creative)
        ops = [
            {"op": "image_upload", "label": f"upload {image.name}", "owner": org, "file": str(image)},
            {"op": "create", "check": "post", "label": "hidden ad post", "path": "/posts", "body": post},
            {"op": "create", "check": "creative", "label": f"ad {ad_name}",
             "path": f"/adAccounts/{int(account_id)}/creatives", "body": creative},
        ]
        warnings = []
        if len(intro_text) > INTRO_VISIBLE:
            warnings.append(f"intro text is {len(intro_text)} characters; about {INTRO_VISIBLE} show before 'see more'")
        if len(headline) > 70:
            warnings.append(f"headline is {len(headline)} characters; about 70 show in the feed")
        preview = {"campaign": _num(campaign_id), "ad": ad_name, "image": image.name, "intro_text": intro_text,
                   "headline": headline, "landing_url": landing_url, "call_to_action": call_to_action,
                   "fixed": "hidden post (not on the company page feed), creative DRAFT", "warnings": warnings}
        return self._plan("change", account_id, cur, f"Create image ad {ad_name} (DRAFT)", preview, ops)

    def draft_thought_leader_ad(self, account_id: int, campaign_id, post_urn: str, ad_name: str) -> dict:
        acct = self._account(account_id)
        if not post_urn.startswith(("urn:li:share:", "urn:li:ugcPost:")):
            raise guards.GuardError("post_urn must be urn:li:share:… or urn:li:ugcPost:… of a published post")
        creative = {"campaign": CAMPAIGN_URN + _num(campaign_id), "content": {"reference": post_urn},
                    "intendedStatus": "DRAFT", "name": ad_name}
        guards.check_creative(creative)
        ops = [{"op": "create", "check": "creative", "label": f"Thought Leader Ad {ad_name}",
                "path": f"/adAccounts/{int(account_id)}/creatives", "body": creative}]
        preview = {"campaign": _num(campaign_id), "ad": ad_name, "post": post_urn,
                   "note": "The post's author must allow sponsoring it (Campaign Manager asks them); creative DRAFT"}
        return self._plan("change", account_id, acct["currency"], f"Create Thought Leader Ad {ad_name} (DRAFT)", preview, ops)

    def _entity_path(self, account_id: int, entity: str, entity_id) -> str:
        a = int(account_id)
        if entity == "campaign":
            return f"/adAccounts/{a}/adCampaigns/{_num(entity_id)}"
        if entity == "group":
            return f"/adAccounts/{a}/adCampaignGroups/{_num(entity_id)}"
        if entity == "creative":
            urn = entity_id if str(entity_id).startswith("urn:") else CREATIVE_URN + str(entity_id)
            return f"/adAccounts/{a}/creatives/{quote(urn, safe='')}"
        raise guards.GuardError("entity must be campaign, group or creative")

    def pause_entity(self, account_id: int, entity: str, entity_id) -> dict:
        acct = self._account(account_id)
        field = "intendedStatus" if entity == "creative" else "status"
        ops = [{"op": "patch", "check": entity, "label": f"pause {entity} {entity_id}",
                "path": self._entity_path(account_id, entity, entity_id), "fields": {field: "PAUSED"}}]
        return self._plan("change", account_id, acct["currency"], f"Pause {entity} {entity_id}",
                          {"entity": entity, "id": str(entity_id), "to": "PAUSED"}, ops)

    def enable_entity(self, account_id: int, entity: str, entity_id) -> dict:
        acct = self._account(account_id)
        cur = acct["currency"]
        preview: dict = {"entity": entity, "id": str(entity_id), "to": "ACTIVE"}
        op = {"op": "enable", "entity": entity, "id": str(entity_id), "label": f"enable {entity} {entity_id}",
              "path": self._entity_path(account_id, entity, entity_id)}
        live = self._live_check(int(account_id), cur, op)
        if entity == "campaign":
            current = live
            preview.update({
                "campaign": current.get("name"),
                "budget": f"{current.get('dailyBudget', {}).get('amount')} {cur}/day, "
                          f"lifetime {current.get('totalBudget', {}).get('amount')} {cur}",
                "ends": datetime.fromtimestamp(current["runSchedule"]["end"] / 1000, timezone.utc).date().isoformat(),
                "note": "Spends only once the ad account has a payment method and the campaign has an active ad",
            })
        elif entity == "creative":
            preview["campaign"] = live.get("name")
        return self._plan("enable", account_id, cur, f"Switch on {entity} {entity_id}", preview, [op])

    def _live_check(self, account_id: int, currency: str, op: dict) -> dict:
        """Read the entity as it is now in LinkedIn and check it may go live; returns the campaign
        (for campaigns and creatives) or the group."""
        entity = op["entity"]
        if entity == "campaign":
            campaign = self.get_campaign(account_id, op["id"])
            guards.check_live_campaign(self.cfg, campaign, currency)
            return campaign
        if entity == "group":
            group = self.client.get(op["path"])
            guards.check_live_group(self.cfg, group, currency)
            return group
        creative = self.client.get(op["path"])
        campaign_urn = creative.get("campaign")
        if not campaign_urn:
            raise guards.GuardError("creative has no campaign")
        campaign = self.get_campaign(account_id, campaign_urn)
        guards.check_live_campaign(self.cfg, campaign, currency)
        return campaign

    def draft_conversion(self, account_id: int, name: str, conversion_type: str = "LEAD",
                         campaign_ids: list | None = None, post_click_days: int = 30, view_days: int = 7) -> dict:
        acct = self._account(account_id)
        body = {
            "name": name, "account": ACCOUNT_URN + str(int(account_id)), "type": conversion_type,
            "attributionType": "LAST_TOUCH_BY_CAMPAIGN", "postClickAttributionWindowSize": post_click_days,
            "viewThroughAttributionWindowSize": view_days, "conversionMethod": "CONVERSIONS_API", "enabled": True,
        }
        ops = [{"op": "create", "check": None, "label": f"conversion {name}", "path": "/conversions", "body": body}]
        for cid in campaign_ids or []:
            ops.append({"op": "attach_conversion", "label": f"attach to campaign {_num(cid)}",
                        "campaign": CAMPAIGN_URN + _num(cid), "conversion": "$ref:0:" + CONVERSION_URN})
        preview = {"conversion": name, "type": conversion_type, "method": "Conversions API (offline upload)",
                   "windows": f"{post_click_days}-day click, {view_days}-day view", "campaigns": campaign_ids or []}
        return self._plan("change", account_id, acct["currency"], f"Create conversion {name}", preview, ops)

    def draft_conversion_upload(self, account_id: int, conversion_id, csv_path: str) -> dict:
        """CSV columns: email, li_fat_id (optional), happened_at (YYYY-MM-DD or ISO time),
        value (optional), event_id (optional). Emails are hashed here; raw emails never leave."""
        acct = self._account(account_id)
        cur = acct["currency"]
        path = guards.safe_file(self.cfg, csv_path, "csv")
        cutoff = datetime.now(timezone.utc) - timedelta(days=CONVERSION_MAX_AGE_DAYS)
        elements, skipped = [], {"no id": 0, "older than 90 days": 0, "bad date": 0, "bad value": 0}
        with path.open(newline="", encoding="utf-8-sig") as fh:  # -sig: Excel byte-order mark
            for row in csv.DictReader(fh):
                email = (row.get("email") or "").strip().lower()
                fat = (row.get("li_fat_id") or "").strip()
                if not email and not fat:
                    skipped["no id"] += 1
                    continue
                try:
                    raw = (row.get("happened_at") or "").strip()
                    when = datetime.fromisoformat(raw if "T" in raw else raw + "T12:00:00+00:00")
                    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
                except ValueError:
                    skipped["bad date"] += 1
                    continue
                if when < cutoff:
                    skipped["older than 90 days"] += 1
                    continue
                ids = []
                if email:
                    ids.append({"idType": "SHA256_EMAIL", "idValue": hashlib.sha256(email.encode()).hexdigest()})
                if fat:
                    ids.append({"idType": "LINKEDIN_FIRST_PARTY_ADS_TRACKING_UUID", "idValue": fat})
                raw_value = (row.get("value") or "").strip()
                value = None
                if raw_value:
                    try:
                        value = Decimal(raw_value)
                    except InvalidOperation:
                        value = None
                    if value is None or value < 0:
                        skipped["bad value"] += 1
                        continue
                el = {"conversion": CONVERSION_URN + _num(conversion_id),
                      "conversionHappenedAt": int(when.timestamp() * 1000), "user": {"userIds": ids}}
                if value is not None:
                    el["conversionValue"] = {"currencyCode": cur, "amount": str(value)}
                if row.get("event_id"):
                    el["eventId"] = row["event_id"].strip()
                elements.append(el)
        if not elements:
            raise guards.GuardError(f"no rows to upload (skipped: {skipped})")
        ops = [{"op": "batch_create", "label": f"conversion events {i + 1}", "path": "/conversionEvents",
                "elements": elements[i:i + CONVERSION_BATCH]} for i in range(0, len(elements), CONVERSION_BATCH)]
        preview = {"conversion": _num(conversion_id), "file": path.name, "events": len(elements), "skipped": skipped,
                   "matching": "SHA-256 email and/or li_fat_id; raw emails are not sent"}
        return self._plan("change", account_id, cur, f"Upload {len(elements)} conversion events", preview, ops)

    # ---------- apply ----------

    def _recheck(self, plan: Plan, op: dict) -> None:
        check, cur = op.get("check"), plan.currency
        if op["op"] == "create":
            body = op["body"]
            if check == "group":
                guards.check_group(self.cfg, body, cur)
            elif check == "campaign":
                guards.check_campaign(self.cfg, body, cur)
            elif check == "creative":
                guards.check_creative(body)
            elif check == "post":
                guards.check_post(body)
        elif op["op"] == "patch":
            guards.check_patch(self.cfg, check, op["fields"], cur, op.get("current"))
        elif op["op"] == "image_upload":
            guards.safe_file(self.cfg, op["file"], "image")
        elif op["op"] == "enable":
            self._live_check(plan.account_id, cur, op)

    def _run(self, plan: Plan, op: dict, results: list) -> dict:
        kind = op["op"]
        if kind == "create":
            body = resolve_refs(op["body"], results)
            out = self.client.create(op["path"], body)
            return {"id": out["id"]}
        if kind == "patch":
            self.client.partial_update(op["path"], op["fields"])
            return {"id": None}
        if kind == "image_upload":
            init = self.client.request(
                "POST", "/images", "action=initializeUpload",
                json={"initializeUploadRequest": {"owner": op["owner"]}},
            ).json()["value"]
            with open(op["file"], "rb") as fh:
                self.client.upload_bytes(init["uploadUrl"], fh.read())
            return {"id": init["image"]}
        if kind == "attach_conversion":
            conv = resolve_refs(op["conversion"], results)
            key = restli.encode({"campaign": op["campaign"], "conversion": conv})
            self.client.put(f"/campaignConversions/{key}", {"campaign": op["campaign"], "conversion": conv})
            return {"id": None}
        if kind == "batch_create":
            out = self.client.batch_create(op["path"], op["elements"])
            statuses = [e.get("status", 201) for e in out.get("elements", [])]
            failed = sum(1 for st in statuses if st >= 400)
            if statuses and failed == len(statuses):
                raise LinkedInError(statuses[0], f"LinkedIn rejected all {failed} events in this batch")
            return {"id": None, "sent": len(op["elements"]), "failed": failed}
        if kind == "enable":
            self._live_check(plan.account_id, plan.currency, op)  # as it is right now, just before the write
            field = "intendedStatus" if op["entity"] == "creative" else "status"
            self.client.partial_update(op["path"], {field: "ACTIVE"})
            return {"id": None}
        raise ValueError(f"unknown operation {kind}")

    def confirm_and_apply(self, plan_ids: list[str], dry_run: bool = True) -> dict:
        if len(set(plan_ids)) != len(plan_ids):
            raise guards.GuardError("a plan_id is listed more than once")
        plans = [self.plans.get(pid) for pid in plan_ids]
        if len(plans) > 1 and any(p.kind in SOLO_KINDS for p in plans):
            raise guards.GuardError("switching on and budget rises are applied one plan at a time")
        report = []
        for plan in plans:
            try:
                report.append(self._apply_one(plan, dry_run))
            except Exception as exc:  # report per plan; never lose what earlier plans did
                report.append({"plan_id": plan.id, "summary": plan.summary, "error": str(exc),
                               "applied": [], "note": "Nothing in this plan was applied"})
        return {"results": report}

    def _apply_one(self, plan: Plan, dry_run: bool) -> dict:
        guards.check_account(self.cfg, plan.account_id)
        acct = self.get_ad_account(plan.account_id)
        if acct.get("currency") != plan.currency:
            raise guards.GuardError(f"account currency is {acct.get('currency')}, the plan used {plan.currency}")
        start = len(plan.progress)  # steps a failed earlier apply already did
        for op in plan.ops[start:]:
            self._recheck(plan, op)
        if dry_run:
            return {"plan_id": plan.id, "dry_run": True, "summary": plan.summary,
                    "already_applied": [op["label"] for op in plan.ops[:start]],
                    "would_apply": [op["label"] for op in plan.ops[start:]]}
        results, done = list(plan.progress), []
        for op in plan.ops[start:]:
            try:
                out = self._run(plan, op, results)
            except Exception as exc:
                audit(self.cfg.safety.log_file, {"plan": plan.id, "account": plan.account_id, "step": op["label"],
                                                 "error": str(exc)})
                return {"plan_id": plan.id, "summary": plan.summary, "applied": done, "error": str(exc),
                        "note": "Steps before the error were applied and kept (new objects are DRAFT). "
                                "Applying this plan again continues from the failed step."}
            results.append(out.get("id"))
            plan.progress = results
            self.plans.update(plan)
            done.append({"step": op["label"], **out})
            audit(self.cfg.safety.log_file, {"plan": plan.id, "account": plan.account_id, "step": op["label"],
                                             "op": op["op"], "path": op.get("path"), "result": out})
        self.plans.remove(plan.id)
        result = {"plan_id": plan.id, "summary": plan.summary, "applied": done}
        try:
            result["readback"] = self._readback(plan, results)
        except Exception as exc:
            result["readback_error"] = f"applied, but reading it back failed: {exc}"
        return result

    def _readback(self, plan: Plan, results: list) -> list[dict]:
        out = []
        for op, rid in zip(plan.ops, results):
            if op["op"] == "create" and op.get("check") == "campaign" and rid:
                c = self.get_campaign(plan.account_id, rid)
                out.append({"campaign": rid, "status": c.get("status"),
                            "audienceExpansionEnabled": c.get("audienceExpansionEnabled"),
                            "offsiteDeliveryEnabled": c.get("offsiteDeliveryEnabled"),
                            "dailyBudget": c.get("dailyBudget"), "totalBudget": c.get("totalBudget")})
            if op["op"] in {"enable", "patch"} and op.get("entity", op.get("check")) == "campaign":
                c = self.client.get(op["path"])
                out.append({"campaign": c.get("id"), "status": c.get("status")})
        return out
