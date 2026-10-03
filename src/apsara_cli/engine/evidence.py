"""Interpret verifier and critic evidence without trusting a model's summary."""

from __future__ import annotations

import json


def verification_evidence(text: str) -> dict:
    try:
        payload = json.loads(text[text.index("{"):])
    except (ValueError, TypeError):
        return {"status": "failed", "reason": "No structured verification evidence."}
    if not isinstance(payload, dict) or payload.get("phase") not in {"baseline", "targeted", "full"}:
        return {"status": "failed", "reason": "Invalid verification evidence."}
    results = payload.get("results")
    status = payload.get("status")
    if status == "passed" and (not isinstance(results, list) or not results or any(
        not isinstance(item, dict) or item.get("status") != "passed" or item.get("returncode") != 0
        or not isinstance(item.get("command"), list) or not item["command"]
        for item in results
    )):
        return {"status": "failed", "reason": "Verification did not contain passing command evidence."}
    if status not in {"passed", "failed", "unavailable"}:
        return {"status": "failed", "reason": "Unknown verification status."}
    return payload


def critic_evidence(text: str) -> dict:
    # Exact legacy approval remains compatible with recorded tests and plugins.
    if text.strip() == "APPROVED":
        return {"verdict": "approved", "findings": []}
    try:
        payload = json.loads(text[text.index("{"):])
    except (ValueError, TypeError):
        return {"verdict": "unavailable", "findings": [], "reason": "Critic returned no structured verdict."}
    if not isinstance(payload, dict) or payload.get("verdict") not in {"approved", "changes_requested"}:
        return {"verdict": "unavailable", "findings": [], "reason": "Invalid critic verdict."}
    findings = payload.get("findings")
    if not isinstance(findings, list) or any(not isinstance(item, dict) or not item.get("description") for item in findings):
        return {"verdict": "unavailable", "findings": [], "reason": "Invalid critic findings."}
    if findings:
        return {**payload, "verdict": "changes_requested"}
    return payload
