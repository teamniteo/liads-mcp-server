"""Plans: draft tools store a plan and return its preview; confirm_and_apply runs it.

A plan is a list of operations. Later operations can use the id an earlier one
returned: any string "$ref:<n>" or "$ref:<n>:<prefix>" is replaced by the id of
operation n (the prefix is added when the id isn't already a URN).
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Plans that always need their own approval, never batched with others.
SOLO_KINDS = {"enable", "budget_rise"}


@dataclass
class Plan:
    id: str
    kind: str  # "change" | "enable" | "budget_rise"
    account_id: int
    currency: str
    summary: str
    preview: dict
    ops: list[dict]
    created_at: float = field(default_factory=time.time)
    # ids returned by the operations applied so far; a failed apply resumes after them
    progress: list = field(default_factory=list)


class PlanStore:
    def __init__(self, path: Path, ttl_hours: int = 24):
        self.path = Path(path)
        self.ttl = ttl_hours * 3600

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        now = time.time()
        return {k: v for k, v in data.items() if now - v["created_at"] < self.ttl}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")

    def add(self, kind: str, account_id: int, currency: str, summary: str, preview: dict, ops: list[dict]) -> Plan:
        plan = Plan(f"plan_{secrets.token_hex(4)}", kind, int(account_id), currency, summary, preview, ops)
        data = self._load()
        data[plan.id] = asdict(plan)
        self._save(data)
        return plan

    def get(self, plan_id: str) -> Plan:
        data = self._load()
        if plan_id not in data:
            raise KeyError(f"unknown or expired plan {plan_id}")
        return Plan(**data[plan_id])

    def update(self, plan: Plan) -> None:
        data = self._load()
        data[plan.id] = asdict(plan)
        self._save(data)

    def remove(self, plan_id: str) -> None:
        data = self._load()
        data.pop(plan_id, None)
        self._save(data)

    def list(self) -> list[dict]:
        return [{"plan_id": k, "kind": v["kind"], "summary": v["summary"]} for k, v in self._load().items()]


def resolve_refs(value, results: list):
    if isinstance(value, str) and value.startswith("$ref:"):
        _, rest = value.split(":", 1)
        index, _, prefix = rest.partition(":")
        rid = results[int(index)]
        if rid is None:
            raise ValueError(f"operation {index} returned no id")
        rid = str(rid)
        return rid if rid.startswith("urn:") or not prefix else prefix + rid
    if isinstance(value, dict):
        return {k: resolve_refs(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_refs(v, results) for v in value]
    return value


def audit(log_file: Path, entry: dict) -> None:
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")
