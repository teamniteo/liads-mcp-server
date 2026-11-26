"""Rules every write must pass, checked when a plan is drafted and again before it is applied.

- Ad accounts must be on the allow-list.
- New campaign groups, campaigns and creatives are DRAFT; going live is only an enable plan.
- Campaigns keep audience expansion and the LinkedIn Audience Network off, bid manual CPC,
  and have daily and lifetime budgets under the caps.
- Files are read only from inside uploads.allowed_dir.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .config import Config

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_CSV_BYTES = 20 * 1024 * 1024

CAMPAIGN_PATCH_KEYS = {"name", "dailyBudget", "totalBudget", "unitCost", "runSchedule", "targetingCriteria", "status"}
GROUP_PATCH_KEYS = {"name", "totalBudget", "runSchedule", "status"}
CREATIVE_PATCH_KEYS = {"intendedStatus"}


class GuardError(ValueError):
    pass


def check_account(cfg: Config, account_id: int) -> None:
    if int(account_id) not in cfg.ads.allowed_accounts:
        raise GuardError(f"ad account {account_id} is not in ads.allowed_accounts")


def amount(money: dict | None, what: str) -> Decimal:
    if not money:
        raise GuardError(f"{what} is required")
    try:
        value = Decimal(str(money["amount"]))
    except (KeyError, InvalidOperation) as exc:
        raise GuardError(f"{what} needs a numeric amount") from exc
    if value <= 0:
        raise GuardError(f"{what} must be above zero")
    return value


def _check_currency(money: dict, currency: str, what: str) -> None:
    if money.get("currencyCode") != currency:
        raise GuardError(f"{what} is in {money.get('currencyCode')}, the ad account uses {currency}")


def _check_schedule(schedule: dict | None, need_end: bool) -> None:
    if not schedule or "start" not in schedule:
        raise GuardError("runSchedule.start is required")
    if need_end and "end" not in schedule:
        raise GuardError("runSchedule.end is required: every campaign has an end date")
    if "end" in schedule and schedule["end"] <= schedule["start"]:
        raise GuardError("runSchedule.end must be after start")


def _check_budgets(cfg: Config, daily: dict | None, total: dict | None, currency: str) -> None:
    s = cfg.safety
    if daily is not None:
        d = amount(daily, "dailyBudget")
        _check_currency(daily, currency, "dailyBudget")
        if d > s.max_daily_budget:
            raise GuardError(f"daily budget {d} is over safety.max_daily_budget {s.max_daily_budget}")
    if total is not None:
        t = amount(total, "totalBudget")
        _check_currency(total, currency, "totalBudget")
        if t > s.max_lifetime_budget:
            raise GuardError(f"lifetime budget {t} is over safety.max_lifetime_budget {s.max_lifetime_budget}")
        if daily is not None and amount(daily, "dailyBudget") > t:
            raise GuardError("daily budget is larger than the lifetime budget")


def _check_bid(cfg: Config, unit_cost: dict | None, currency: str) -> None:
    bid = amount(unit_cost, "unitCost (manual CPC bid)")
    _check_currency(unit_cost, currency, "unitCost")
    if bid > cfg.safety.max_bid:
        raise GuardError(f"bid {bid} is over safety.max_bid {cfg.safety.max_bid}")


def check_group(cfg: Config, body: dict, currency: str) -> None:
    if body.get("status") != "DRAFT":
        raise GuardError("new campaign groups are created as DRAFT")
    _check_schedule(body.get("runSchedule"), need_end=True)
    _check_budgets(cfg, None, body.get("totalBudget"), currency)


def check_campaign(cfg: Config, body: dict, currency: str) -> None:
    if body.get("status") != "DRAFT":
        raise GuardError("new campaigns are created as DRAFT")
    if body.get("audienceExpansionEnabled") is not False:
        raise GuardError("audience expansion must be off")
    if body.get("offsiteDeliveryEnabled") is not False:
        raise GuardError("LinkedIn Audience Network (offsite delivery) must be off")
    if body.get("costType") != "CPC":
        raise GuardError("bidding is manual CPC")
    if not body.get("locale", {}).get("country") or not body.get("locale", {}).get("language"):
        raise GuardError("locale country and language are required (no defaults)")
    if not body.get("politicalIntent"):
        raise GuardError("politicalIntent is required")
    _check_schedule(body.get("runSchedule"), need_end=True)
    _check_budgets(cfg, body.get("dailyBudget"), body.get("totalBudget"), currency)
    if body.get("totalBudget") is None:
        raise GuardError("totalBudget (lifetime budget) is required")
    _check_bid(cfg, body.get("unitCost"), currency)
    if not body.get("targetingCriteria", {}).get("include"):
        raise GuardError("targetingCriteria.include is required")


def check_creative(body: dict) -> None:
    if body.get("intendedStatus") != "DRAFT":
        raise GuardError("new creatives are created as DRAFT")


def check_post(body: dict) -> None:
    if body.get("distribution", {}).get("feedDistribution") != "NONE":
        raise GuardError("ad posts are hidden (feedDistribution NONE); this server never posts to the feed")
    if "adContext" not in body:
        raise GuardError("ad posts need adContext (direct sponsored content)")


def check_patch(cfg: Config, entity: str, fields: dict, currency: str, current: dict | None = None) -> None:
    """current: the entity's present dailyBudget/totalBudget, so a one-sided change is checked
    against the other budget."""
    allowed = {"campaign": CAMPAIGN_PATCH_KEYS, "group": GROUP_PATCH_KEYS, "creative": CREATIVE_PATCH_KEYS}[entity]
    extra = set(fields) - allowed
    if extra:
        raise GuardError(f"cannot change {', '.join(sorted(extra))} on a {entity}")
    status = fields.get("status") or fields.get("intendedStatus")
    if status and status != "PAUSED":
        raise GuardError("updates can only pause; going live is an enable plan")
    if entity == "campaign":
        current = current or {}
        daily = fields.get("dailyBudget", current.get("dailyBudget"))
        total = fields.get("totalBudget", current.get("totalBudget"))
        if "dailyBudget" in fields or "totalBudget" in fields:
            _check_budgets(cfg, daily, total, currency)
        if "unitCost" in fields:
            _check_bid(cfg, fields["unitCost"], currency)
        if "runSchedule" in fields:
            _check_schedule(fields["runSchedule"], need_end=True)
    if entity == "group":
        _check_budgets(cfg, None, fields.get("totalBudget"), currency)


def check_live_campaign(cfg: Config, campaign: dict, currency: str) -> None:
    """Before switching a campaign on: its current settings must still pass the rules."""
    if campaign.get("audienceExpansionEnabled"):
        raise GuardError("campaign has audience expansion on; turn it off before enabling")
    if campaign.get("offsiteDeliveryEnabled"):
        raise GuardError("campaign has the LinkedIn Audience Network on; turn it off before enabling")
    if campaign.get("costType") != "CPC":
        raise GuardError(f"campaign bids {campaign.get('costType')}, not manual CPC; change it before enabling")
    if not campaign.get("locale", {}).get("country") or not campaign.get("locale", {}).get("language"):
        raise GuardError("campaign has no locale")
    _check_budgets(cfg, campaign.get("dailyBudget"), campaign.get("totalBudget"), currency)
    if campaign.get("totalBudget") is None:
        raise GuardError("campaign has no lifetime budget")
    _check_bid(cfg, campaign.get("unitCost"), currency)
    end = campaign.get("runSchedule", {}).get("end")
    if not end:
        raise GuardError("campaign has no end date")


def check_live_group(cfg: Config, group: dict, currency: str) -> None:
    """Before switching a campaign group on."""
    if group.get("totalBudget") is not None:
        _check_budgets(cfg, None, group["totalBudget"], currency)
    if not group.get("runSchedule", {}).get("end"):
        raise GuardError("campaign group has no end date")


def safe_file(cfg: Config, path: str, kind: str) -> Path:
    root = Path(cfg.uploads.allowed_dir).expanduser().resolve()
    target = Path(path).expanduser().resolve()
    if not target.is_relative_to(root):
        raise GuardError(f"{path} is outside uploads.allowed_dir")
    if not target.is_file():
        raise GuardError(f"{path} is not a file")
    size = target.stat().st_size
    if kind == "image":
        if target.suffix.lower() not in IMAGE_TYPES:
            raise GuardError("images must be jpg, png or gif")
        if size > MAX_IMAGE_BYTES:
            raise GuardError("images must be 5 MB or smaller")
    elif kind == "csv":
        if target.suffix.lower() != ".csv":
            raise GuardError("conversion uploads must be .csv")
        if size > MAX_CSV_BYTES:
            raise GuardError("CSV is over 20 MB")
    return target


def ms(day: str) -> int:
    """'2026-12-08' -> epoch milliseconds at the start of that day (UTC)."""
    y, m, d = (int(x) for x in day.split("-"))
    from datetime import datetime, timezone

    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


def ms_end(day: str) -> int:
    """'2027-02-28' -> the last millisecond of that day (UTC), so the end day still runs."""
    return ms(day) + 86_400_000 - 1


def today() -> date:
    return date.today()
