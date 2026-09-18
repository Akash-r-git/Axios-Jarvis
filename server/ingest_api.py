"""
HTTP surface for ingestion: discover -> map -> review -> launch.

No analytics here either. Uploads are size-capped, decoded defensively, and
never evaluated. Nothing from a file or from Gemini reaches a Plant without
passing dtwin.ingest.mapper.validate_proposal and Plant.validate.
"""
from __future__ import annotations

import base64
import binascii
from typing import Any, Dict, List, Tuple

from dtwin import llm
from dtwin.ingest.mapper import apply_edits, propose, to_plant, validate_proposal
from dtwin.ingest.normalize import ALIASES, MAX_BYTES, normalize

MAX_UPLOAD = MAX_BYTES          # 8 MB, same cap as the normalizer


def _payload_bytes(body: Dict[str, Any]) -> Tuple[bytes, str]:
    name = str(body.get("filename", "uploaded data"))[:120]
    if body.get("content_b64"):
        raw = str(body["content_b64"])
        if len(raw) > MAX_UPLOAD * 4 // 3 + 1024:
            raise ValueError(f"upload exceeds the {MAX_UPLOAD // (1024*1024)} "
                             f"MB cap")
        try:
            data = base64.b64decode(raw, validate=False)
        except (binascii.Error, ValueError):
            raise ValueError("upload is not valid base64")
    elif body.get("text") is not None:
        data = str(body["text"]).encode("utf-8", "replace")
    else:
        raise ValueError("no file content was sent")
    if len(data) > MAX_UPLOAD:
        raise ValueError(f"upload exceeds the {MAX_UPLOAD // (1024*1024)} MB cap")
    if not data.strip():
        raise ValueError("the uploaded file is empty")
    return data, name


def discover(body: Dict[str, Any]) -> dict:
    """Parse, normalize and propose a mapping. Nothing is committed yet."""
    data, name = _payload_bytes(body)
    overrides = body.get("overrides") if isinstance(body.get("overrides"),
                                                    dict) else None
    tbl = normalize(data, name, overrides=overrides)
    if not tbl.rows:
        raise ValueError("no rows could be parsed from that file")
    proposal = propose(tbl, body.get("name") or name)
    return {"table": tbl.to_dict(), "proposal": proposal,
            "targets": sorted(ALIASES.keys()),
            "llm": llm.status()}


def suggest(body: Dict[str, Any]) -> dict:
    """Ask Gemini to name columns the alias tables could not.

    The response is a SUGGESTION. It is validated here and the user (and then
    the deterministic validator) still decides.
    """
    headers = [str(h) for h in (body.get("headers") or [])][:60]
    if not headers:
        raise ValueError("no columns were sent")
    if not llm.available():
        return {"available": False, "accepted": [], "rejected": [],
                "note": llm.status()["note"]}
    try:
        accepted, rejected = llm.suggest_mapping(
            headers, sorted(ALIASES.keys()), body.get("samples") or {})
    except Exception as exc:
        return {"available": True, "accepted": [], "rejected": [],
                "note": f"Gemini was unavailable ({type(exc).__name__}); the "
                        f"deterministic mapping stands."}
    return {"available": True, "accepted": accepted, "rejected": rejected,
            "note": "Suggestions only. The deterministic validator has the "
                    "final say and every value stays editable."}


def build(body: Dict[str, Any]) -> Tuple[Any, Dict[str, str], List[dict], dict]:
    """Validate an (edited) proposal and turn it into a Plant."""
    proposal = body.get("proposal")
    if not isinstance(proposal, dict):
        raise ValueError("no mapping proposal was sent")
    proposal = apply_edits(proposal, body.get("edits") or [])
    errs = validate_proposal(proposal)
    if errs:
        raise ValueError("; ".join(errs))
    plant, prov, notes = to_plant(proposal)
    return plant, prov, notes, proposal


def describe(body: Dict[str, Any]) -> dict:
    """Plain English -> ESTIMATED factory JSON, or an honest refusal."""
    text = str(body.get("description", "")).strip()
    if not text:
        raise ValueError("describe what the line makes and how it flows")
    if not llm.available():
        return {"available": False, "note": llm.status()["note"],
                "fallback": "archetype"}
    try:
        plant_d, prov, notes = llm.describe_factory(text[:4000])
    except Exception as exc:
        return {"available": True, "error": f"{type(exc).__name__}: {exc}",
                "note": "Gemini did not return a usable factory. Use the "
                        "archetype matcher or upload data instead.",
                "fallback": "archetype"}
    return {"available": True, "plant": plant_d, "provenance": prov,
            "notes": notes, "estimated": True,
            "label": "ESTIMATED",
            "note": "Every value below was generated from your description by "
                    "Gemini and validated against the model schema. Nothing "
                    "here was measured. Calibrate it against real data before "
                    "trusting absolute numbers."}
