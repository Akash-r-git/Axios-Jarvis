"""
Gemini access, via the REST API over urllib. No SDK, no dependency.

What the LLM is allowed to do here, and nothing else:
  * suggest a canonical target for an ambiguous column  (validated, rejectable)
  * turn a plain-English description into an ESTIMATED factory JSON (validated
    against the Plant schema, every value tagged estimated)
  * narrate results that were already computed (number-verified in narrate.py)

It never performs arithmetic, simulation, statistics, bottleneck detection or
event interpretation, and nothing it returns reaches the user without passing a
deterministic validator first.

Configuration: GEMINI_API_KEY and, optionally, GEMINI_MODEL. The key is read
from the environment only. It is never logged, never returned by the API and
never sent to the browser.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_MODEL = "gemini-2.5-flash"
ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
            "{model}:generateContent")
TIMEOUT = 25


def model_name() -> str:
    return os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL


def available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def status() -> dict:
    """Safe to send to the browser: says whether a key exists, never what it is."""
    return {"provider": "gemini", "model": model_name(),
            "configured": available(),
            "note": ("Gemini is configured. It may suggest field mappings, "
                     "draft an ESTIMATED factory and phrase narration; it "
                     "never computes any number."
                     if available() else
                     "No GEMINI_API_KEY in the environment. Mapping falls back "
                     "to the deterministic alias tables, factory description "
                     "falls back to the archetype matcher, and narration uses "
                     "templates.")}


def generate(system: str, prompt: str, max_tokens: int = 800,
             temperature: float = 0.2) -> str:
    """One Gemini call. Raises on any failure; callers must handle it."""
    import urllib.request

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    body = json.dumps({
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature,
                             "maxOutputTokens": max_tokens},
    }).encode()
    req = urllib.request.Request(
        ENDPOINT.format(model=model_name()), data=body,
        headers={"content-type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read())
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts",
                                                                       [])
    return "".join(p.get("text", "") for p in parts)


def _json_from(text: str) -> Any:
    """Pull a JSON value out of a model response, fences and all."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.MULTILINE).strip()
    start = min([i for i in (t.find("{"), t.find("[")) if i >= 0] or [-1])
    if start < 0:
        raise ValueError("no JSON in the model response")
    depth, opener = 0, t[start]
    closer = "}" if opener == "{" else "]"
    for i in range(start, len(t)):
        if t[i] == opener:
            depth += 1
        elif t[i] == closer:
            depth -= 1
            if depth == 0:
                return json.loads(t[start:i + 1])
    raise ValueError("unterminated JSON in the model response")


# ==========================================================================
# 1. Mapping suggestions for ambiguous columns
# ==========================================================================
MAPPING_SYSTEM = (
    "You map column headers from manufacturing data files onto a fixed set of "
    "canonical field names. Reply with JSON only: a list of objects with keys "
    "field, target, confidence (0-1) and reason. `target` MUST be one of the "
    "allowed targets or the string 'ignore'. Never invent targets. Never "
    "compute or infer values from the data; you are naming columns, nothing "
    "more.")


def suggest_mapping(headers: List[str], allowed: List[str],
                    samples: Optional[Dict[str, List[Any]]] = None
                    ) -> Tuple[List[dict], List[dict]]:
    """Ask Gemini to name ambiguous columns.

    Returns (accepted, rejected). A suggestion is accepted only if the field
    exists, the target is in `allowed`, and the confidence parses as a number
    in [0, 1]. Everything else is rejected with a reason. The caller is still
    free to ignore the accepted list; the deterministic validator decides.
    """
    if not headers:
        return [], []
    prompt = json.dumps({
        "headers": headers[:60],
        "allowed_targets": sorted(set(allowed)) + ["ignore"],
        "example_values": {k: [str(x)[:40] for x in v[:4]]
                           for k, v in (samples or {}).items()},
    })
    raw = generate(MAPPING_SYSTEM, prompt, max_tokens=900)
    data = _json_from(raw)
    if isinstance(data, dict):
        data = data.get("mappings") or data.get("fields") or []
    accepted, rejected = [], []
    allowed_set = set(allowed) | {"ignore"}
    header_set = set(headers)
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            rejected.append({"item": str(item)[:80],
                             "why": "not an object"})
            continue
        field, target = str(item.get("field", "")), str(item.get("target", ""))
        if field not in header_set:
            rejected.append({"item": field, "why": "no such column in the file"})
            continue
        if target not in allowed_set:
            rejected.append({"item": field,
                             "why": f"target {target!r} is not allowed"})
            continue
        try:
            conf = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        accepted.append({"field": field, "target": target,
                         "confidence": max(0.0, min(1.0, conf)),
                         "reason": str(item.get("reason", ""))[:200],
                         "source": "gemini-suggestion"})
    return accepted, rejected


# ==========================================================================
# 2. Describe a factory in plain English -> ESTIMATED Plant JSON
# ==========================================================================
FACTORY_SYSTEM = (
    "You draft a rough starting model of a SERIAL production line from a plain "
    "English description. Reply with JSON only, in this exact shape:\n"
    '{"name": str, "stages": [{"id": str, "name": str, "machines": int, '
    '"proc": {"kind": "lognorm", "mean": <seconds per part>, "cv": <0-1>}, '
    '"in_buffer": <int or -1 for unlimited>, "mtbf": <seconds or -1>, '
    '"repair": {"kind": "lognorm", "mean": <seconds>, "cv": <0-1>}, '
    '"yield": <0-1>}]}\n'
    "Stages must be listed in process order. Every number is a rough industry "
    "estimate, not a measurement. Do not model branching, merging or rework "
    "loops: if the description implies them, pick the main serial path and say "
    "nothing about it. 2 to 12 stages.")


def describe_factory(description: str) -> Tuple[dict, Dict[str, str], List[str]]:
    """Return (plant_dict, provenance, notes). Raises if nothing usable came back.

    The result is validated against the Plant schema by the caller; every value
    is tagged ESTIMATED because none of it was measured.
    """
    if not (description or "").strip():
        raise ValueError("empty description")
    raw = generate(FACTORY_SYSTEM, description.strip()[:4000], max_tokens=1600)
    data = _json_from(raw)
    if not isinstance(data, dict) or not isinstance(data.get("stages"), list):
        raise ValueError("model did not return a stage list")
    notes: List[str] = []
    stages = []
    seen = set()
    for i, s in enumerate(data["stages"][:12]):
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or s.get("name") or f"S{i+1}")[:24]
        sid = re.sub(r"[^A-Za-z0-9_\-]", "_", sid) or f"S{i+1}"
        while sid in seen:
            sid += "_2"
        seen.add(sid)
        proc = s.get("proc") if isinstance(s.get("proc"), dict) else {}
        try:
            mean = float(proc.get("mean", s.get("cycle_time", 0)) or 0)
        except (TypeError, ValueError):
            mean = 0.0
        if mean <= 0 or mean > 86400:
            notes.append(f"{sid}: processing time was missing or absurd; "
                         f"defaulted to 60s")
            mean = 60.0
        try:
            cv = min(1.5, max(0.0, float(proc.get("cv", 0.2) or 0.0)))
        except (TypeError, ValueError):
            cv = 0.2
        try:
            machines = max(1, min(50, int(s.get("machines", 1) or 1)))
        except (TypeError, ValueError):
            machines = 1
        rep = s.get("repair") if isinstance(s.get("repair"), dict) else {}
        try:
            mttr = max(0.0, float(rep.get("mean", 0) or 0))
        except (TypeError, ValueError):
            mttr = 0.0
        try:
            mtbf = float(s.get("mtbf", -1) or -1)
        except (TypeError, ValueError):
            mtbf = -1.0
        if mtbf > 0 and mttr <= 0:
            mttr = max(60.0, mean * 5)
            notes.append(f"{sid}: MTBF without MTTR; MTTR defaulted")
        if mtbf <= 0:
            mttr = 0.0
        try:
            yld = float(s.get("yield", 1.0) or 1.0)
        except (TypeError, ValueError):
            yld = 1.0
        if not (0 < yld <= 1):
            notes.append(f"{sid}: yield {yld} out of range; set to 1.0")
            yld = 1.0
        try:
            buf = float(s.get("in_buffer", -1))
        except (TypeError, ValueError):
            buf = -1.0
        stages.append({
            "id": sid, "name": str(s.get("name") or sid)[:40],
            "machines": machines,
            "proc": {"kind": "lognorm" if cv else "det", "mean": mean, "cv": cv},
            "in_buffer": -1 if buf < 0 else buf,
            "mtbf": -1 if mtbf <= 0 else mtbf,
            "repair": {"kind": "lognorm", "mean": mttr, "cv": 0.4 if mttr else 0.0},
            "yield": yld,
        })
    if len(stages) < 2:
        raise ValueError("model returned fewer than two usable stages")
    plant = {"name": str(data.get("name") or "Described line")[:60],
             "horizon_s": 28800, "warmup_s": 3600, "stages": stages}
    prov = {}
    for s in stages:
        for f in ("proc", "mtbf", "repair", "yield", "machines"):
            prov[f"{s['id']}:{f}"] = "estimated"
    return plant, prov, notes
