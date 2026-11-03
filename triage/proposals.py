"""Versioned plans and deterministic policy decisions. This module never executes tools."""
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from triage.core import Email, Label, Suggestion

PROMPT_VERSION = "triage-planner-v1"
WORKFLOW_VERSION = "action-proposals-v1"
Kind = Literal["set_label", "mark_read", "mark_unread", "draft_reply"]
Risk = Literal["prompt_injection", "sensitive_request", "ambiguous"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(StrictModel):
    field: Literal["sender", "subject", "body"]
    quote: str = Field(min_length=1, max_length=500)


class Action(StrictModel):
    kind: Kind
    label: Label | None
    reply_instruction: str | None = Field(max_length=2000)
    evidence: Evidence

    @model_validator(mode="after")
    def parameters_match_kind(self):
        if (self.kind == "set_label") != (self.label is not None):
            raise ValueError("Only set_label must have a label.")
        if self.kind == "draft_reply":
            if not self.reply_instruction or not self.reply_instruction.strip():
                raise ValueError("draft_reply requires an instruction.")
        elif self.reply_instruction is not None:
            raise ValueError("Only draft_reply can contain an instruction.")
        return self


class Plan(StrictModel):
    schema_version: Literal["1"]
    intent: Literal["request", "recruiting", "receipt", "newsletter", "other"]
    label: Label
    reason: str = Field(min_length=1, max_length=2000)
    needs_review: bool
    risk_flags: list[Risk] = Field(max_length=3)
    actions: list[Action] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def consistent_actions(self):
        labels = [a for a in self.actions if a.kind == "set_label"]
        kinds = [a.kind for a in self.actions]
        if len(labels) != 1 or labels[0].label != self.label:
            raise ValueError("A plan requires one set_label action matching its classification.")
        if len(kinds) != len(set(kinds)) or {"mark_read", "mark_unread"}.issubset(kinds):
            raise ValueError("Duplicate or conflicting actions.")
        return self


@dataclass(frozen=True)
class Policy:
    mode: str = "demo"
    source: str = "sample"
    account: str = "local-samples"
    authorized_account: str = "local-samples"
    auto_labels: tuple[str, ...] = ()
    auto_draft_labels: tuple[str, ...] = ()


def deterministic_risks(email):
    text = f"{email.sender}\n{email.subject}\n{email.body}".casefold()
    # Defense in depth for common patterns, not a comprehensive injection detector.
    patterns = (r"ignore\s+(?:all\s+)?(?:previous|prior|your)\s+(?:instructions|rules)",
                r"(?:system|developer)\s+(?:prompt|message)\s*:", r"(?:reveal|send|print)\s+(?:me\s+)?(?:your\s+)?(?:api key|secret|password)")
    return ["prompt_injection"] if any(re.search(pattern, text) for pattern in patterns) else []


def baseline_plan(email: Email, suggestion: Suggestion) -> Plan:
    field = next((f for f in ("subject", "body", "sender") if getattr(email, f)), "subject")
    quote = getattr(email, field)[:200] or "[no content]"
    action = Action(kind="set_label", label=suggestion.label, reply_instruction=None, evidence=Evidence(field=field, quote=quote))
    return Plan(schema_version="1", intent={"Action needed": "request", "Recruiting": "recruiting", "Receipts": "receipt", "Newsletters": "newsletter", "Other": "other"}[suggestion.label],
                label=suggestion.label, reason=suggestion.reason, needs_review=suggestion.needs_review,
                risk_flags=["ambiguous"] if suggestion.needs_review else [], actions=[action])


def decisions(bundle, policy: Policy):
    plan = Plan.model_validate(bundle["plan"])
    results = []
    valid = bundle["evidence_valid"]
    for index, action in enumerate(plan.actions):
        status, code = "needs_approval", "explicit_approval_required"
        if policy.account != policy.authorized_account:
            status, code = "blocked", "account_mismatch"
        elif index >= len(valid) or not valid[index]:
            status, code = "blocked", "ungrounded_evidence"
        elif bundle["risk_flags"] or plan.needs_review:
            code = "risk_or_uncertainty_requires_review"
        elif policy.mode != "ai" or policy.source != "gmail":
            code = "preview_only_context"
        elif action.kind == "set_label" and action.label in policy.auto_labels:
            status, code = "automatic", "enabled_label_rule"
        elif action.kind == "draft_reply" and plan.label in policy.auto_draft_labels:
            status, code = "automatic", "enabled_drafting_rule"
        # Read-state proposals always require explicit approval in this version.
        action_id = hashlib.sha256(json.dumps([bundle["email_fingerprint"], action.model_dump()], sort_keys=True).encode()).hexdigest()[:24]
        results.append({"action_id": action_id, "kind": action.kind, "status": status, "reason_code": code})
    return results


def validate_plan(email, plan: Plan, policy=Policy()):
    fields = {"sender": email.sender[:1000], "subject": email.subject[:1000], "body": email.body[:12000]}
    bundle = {
        "workflow_version": WORKFLOW_VERSION, "prompt_version": PROMPT_VERSION,
        "plan": plan.model_dump(), "evidence_valid": [a.evidence.quote in fields[a.evidence.field] for a in plan.actions],
        "risk_flags": sorted(set(plan.risk_flags + deterministic_risks(email))),
        "email_fingerprint": hashlib.sha256(json.dumps([email.id, fields], sort_keys=True).encode()).hexdigest(),
    }
    bundle["decisions"] = decisions(bundle, policy)
    return bundle


def can_automate(store, row_id, *, kind, config, account):
    bundle = store.proposal(row_id)
    if bundle is None:
        return False  # Legacy/unvalidated rows remain available for explicit manual review.
    row = store.get(row_id)
    if bundle["plan"]["label"] != row["label"]:
        return False
    policy = Policy(mode=row["mode"], source=row["source"], account=row["account"], authorized_account=account,
                    auto_labels=tuple(config.auto_labels), auto_draft_labels=tuple(config.suggested_reply_labels))
    return any(item["kind"] == kind and item["status"] == "automatic" for item in decisions(bundle, policy))
