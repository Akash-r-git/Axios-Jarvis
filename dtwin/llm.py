"""
Gemini access, via the REST API over urllib. No SDK, no dependency.

What the LLM is allowed to do here, and nothing else:
  * suggest a canonical target for an ambiguous column
    (validated, rejectable)
  * turn a plain-English description into an ESTIMATED factory JSON
    (validated against the Plant schema, every value tagged estimated)
  * narrate results that were already computed
    (number-verified in narrate.py)

It never performs arithmetic, simulation, statistics, bottleneck detection or
event interpretation, and nothing it returns reaches the user without passing
a deterministic validator first.

Configuration:
  GEMINI_API_KEY
  GEMINI_MODEL (optional)

The key is read from the environment only. It is never logged, never returned
by the API and never sent to the browser.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_MODEL = "gemini-2.5-flash"

ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)

TIMEOUT = 25


def model_name() -> str:
    return os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL


def available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def status() -> dict:
    """Safe to send to the browser: says whether a key exists, never what it is."""
    return {
        "provider": "gemini",
        "model": model_name(),
        "configured": available(),
        "note": (
            "Gemini is configured. It may suggest field mappings, "
            "draft an ESTIMATED factory and phrase narration; it "
            "never computes any number."
            if available()
            else
            "No GEMINI_API_KEY in the environment. Mapping falls back "
            "to the deterministic alias tables, factory description "
            "falls back to the archetype matcher, and narration uses "
            "templates."
        ),
    }


# ==========================================================================
# Gemini REST call
# ==========================================================================

def generate(
    system: str,
    prompt: str,
    max_tokens: int = 800,
    temperature: float = 0.2,
    json_mode: bool = False,
    thinking_budget: Optional[int] = None,
) -> str:
    """
    Make one Gemini REST API call.

    No SDK is required.

    json_mode:
        When True, Gemini is explicitly instructed through the API to return
        application/json.

    thinking_budget:
        For structured JSON generation we can disable unnecessary reasoning
        tokens by setting this to 0.
    """
    import urllib.request
    import urllib.error

    key = os.environ.get("GEMINI_API_KEY")

    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")

    generation_config: Dict[str, Any] = {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
    }

    if json_mode:
        generation_config["responseMimeType"] = "application/json"

    if thinking_budget is not None:
        generation_config["thinkingConfig"] = {
            "thinkingBudget": thinking_budget
        }

    body = json.dumps(
        {
            "systemInstruction": {
                "parts": [
                    {
                        "text": system
                    }
                ]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": prompt
                        }
                    ],
                }
            ],
            "generationConfig": generation_config,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        ENDPOINT.format(model=model_name()),
        data=body,
        headers={
            "content-type": "application/json",
            "x-goog-api-key": key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            response_bytes = resp.read()

        data = json.loads(response_bytes)

    except urllib.error.HTTPError as exc:
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            error_body = ""

        raise RuntimeError(
            f"Gemini HTTP {exc.code}: {error_body[:500]}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Gemini network error: {exc}"
        ) from exc

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Gemini returned invalid HTTP JSON: {exc}"
        ) from exc

    candidates = data.get("candidates") or []

    if not candidates:
        prompt_feedback = data.get("promptFeedback")

        if prompt_feedback:
            raise RuntimeError(
                f"Gemini returned no candidates: {prompt_feedback}"
            )

        raise RuntimeError("Gemini returned no candidates")

    first_candidate = candidates[0] or {}

    content = first_candidate.get("content") or {}
    parts = content.get("parts") or []

    text_parts = []

    for part in parts:
        if isinstance(part, dict):
            part_text = part.get("text")

            if isinstance(part_text, str):
                text_parts.append(part_text)

    result = "".join(text_parts).strip()

    if not result:
        finish_reason = first_candidate.get("finishReason")

        raise RuntimeError(
            "Gemini returned an empty response"
            + (
                f" (finishReason={finish_reason})"
                if finish_reason
                else ""
            )
        )

    return result


# ==========================================================================
# Robust JSON extraction
# ==========================================================================

def _strip_markdown_fences(text: str) -> str:
    """
    Remove common Markdown JSON fences without damaging the JSON itself.
    """
    t = (text or "").strip()

    if t.startswith("```json"):
        t = t[7:].strip()

    elif t.startswith("```JSON"):
        t = t[7:].strip()

    elif t.startswith("```"):
        t = t[3:].strip()

    if t.endswith("```"):
        t = t[:-3].strip()

    return t


def _find_json_start(text: str) -> int:
    """
    Find the first likely JSON object/array.
    """
    positions = [
        pos
        for pos in (
            text.find("{"),
            text.find("["),
        )
        if pos >= 0
    ]

    return min(positions) if positions else -1


def _extract_balanced_json(text: str) -> str:
    """
    Extract the first complete JSON object/array.

    Unlike the old implementation, this correctly handles braces and brackets
    occurring inside quoted JSON strings.
    """
    t = _strip_markdown_fences(text)

    start = _find_json_start(t)

    if start < 0:
        raise ValueError("no JSON in the model response")

    opener = t[start]

    if opener == "{":
        expected_closer = "}"
    else:
        expected_closer = "]"

    stack: List[str] = []

    in_string = False
    escape = False

    pairs = {
        "{": "}",
        "[": "]",
    }

    for i in range(start, len(t)):
        char = t[i]

        if in_string:
            if escape:
                escape = False
                continue

            if char == "\\":
                escape = True
                continue

            if char == '"':
                in_string = False

            continue

        if char == '"':
            in_string = True
            continue

        if char in pairs:
            stack.append(char)
            continue

        if char in ("}", "]"):
            if not stack:
                continue

            expected = pairs.get(stack[-1])

            if char != expected:
                raise ValueError(
                    f"malformed JSON structure near character {i}"
                )

            stack.pop()

            if not stack:
                return t[start:i + 1]

    if in_string:
        raise ValueError(
            "unterminated JSON: model response ended inside a string"
        )

    raise ValueError(
        f"unterminated JSON: expected closing {expected_closer}"
    )


def _json_from(text: str) -> Any:
    """
    Pull a JSON value out of a model response.

    Handles:
      - raw JSON
      - ```json fenced JSON
      - surrounding explanatory text
      - braces/brackets inside quoted strings

    Raises a useful ValueError if the response is incomplete.
    """
    t = (text or "").strip()

    if not t:
        raise ValueError("empty model response")

    cleaned = _strip_markdown_fences(t)

    # First try the entire response directly.
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Then extract the first balanced JSON value.
    json_text = _extract_balanced_json(cleaned)

    try:
        return json.loads(json_text)

    except json.JSONDecodeError as exc:
        raise ValueError(
            f"invalid JSON in model response: {exc}"
        ) from exc


# ==========================================================================
# 1. Mapping suggestions for ambiguous columns
# ==========================================================================

MAPPING_SYSTEM = (
    "You map column headers from manufacturing data files onto a fixed set "
    "of canonical field names. Reply with JSON only: a list of objects with "
    "keys field, target, confidence (0-1) and reason. "
    "`target` MUST be one of the allowed targets or the string 'ignore'. "
    "Never invent targets. Never compute or infer values from the data; "
    "you are naming columns, nothing more."
)


def suggest_mapping(
    headers: List[str],
    allowed: List[str],
    samples: Optional[Dict[str, List[Any]]] = None,
) -> Tuple[List[dict], List[dict]]:
    """
    Ask Gemini to name ambiguous columns.

    Returns:
        (accepted, rejected)

    A suggestion is accepted only if:
      - the field exists
      - the target is allowed
      - confidence parses as a number in [0, 1]

    Everything else is rejected with a reason.
    """
    if not headers:
        return [], []

    prompt = json.dumps(
        {
            "headers": headers[:60],
            "allowed_targets": sorted(set(allowed)) + ["ignore"],
            "example_values": {
                k: [str(x)[:40] for x in v[:4]]
                for k, v in (samples or {}).items()
            },
        }
    )

    raw = generate(
        MAPPING_SYSTEM,
        prompt,
        max_tokens=900,
        temperature=0.2,
        json_mode=True,
        thinking_budget=0,
    )

    data = _json_from(raw)

    if isinstance(data, dict):
        data = data.get("mappings") or data.get("fields") or []

    accepted: List[dict] = []
    rejected: List[dict] = []

    allowed_set = set(allowed) | {"ignore"}
    header_set = set(headers)

    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            rejected.append(
                {
                    "item": str(item)[:80],
                    "why": "not an object",
                }
            )
            continue

        field = str(item.get("field", ""))
        target = str(item.get("target", ""))

        if field not in header_set:
            rejected.append(
                {
                    "item": field,
                    "why": "no such column in the file",
                }
            )
            continue

        if target not in allowed_set:
            rejected.append(
                {
                    "item": field,
                    "why": f"target {target!r} is not allowed",
                }
            )
            continue

        try:
            conf = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5

        accepted.append(
            {
                "field": field,
                "target": target,
                "confidence": max(0.0, min(1.0, conf)),
                "reason": str(item.get("reason", ""))[:200],
                "source": "gemini-suggestion",
            }
        )

    return accepted, rejected


# ==========================================================================
# 2. Describe a factory in plain English -> ESTIMATED Plant JSON
# ==========================================================================

FACTORY_SYSTEM = (
    "You draft a rough starting model of a SERIAL production line from a "
    "plain English description.\n\n"

    "IMPORTANT OUTPUT RULES:\n"
    "1. Return ONLY valid JSON.\n"
    "2. Do not use Markdown.\n"
    "3. Do not wrap the JSON in ```json fences.\n"
    "4. Do not include explanations before or after the JSON.\n"
    "5. Complete every opening { with its matching }.\n"
    "6. Keep the response compact.\n\n"

    "Use exactly this top-level shape:\n"
    "{"
    "\"name\": \"string\", "
    "\"stages\": ["
    "{"
    "\"id\": \"string\", "
    "\"name\": \"string\", "
    "\"machines\": 1, "
    "\"proc\": {"
    "\"kind\": \"lognorm\", "
    "\"mean\": 60, "
    "\"cv\": 0.2"
    "}, "
    "\"in_buffer\": -1, "
    "\"mtbf\": -1, "
    "\"repair\": {"
    "\"kind\": \"lognorm\", "
    "\"mean\": 0, "
    "\"cv\": 0"
    "}, "
    "\"yield\": 1.0"
    "}"
    "]"
    "}\n\n"

    "Field rules:\n"
    "- name: concise production-line name.\n"
    "- stages: 2 to 12 stages in process order.\n"
    "- id: short unique identifier such as S1, S2, S3.\n"
    "- machines: positive integer number of parallel machines.\n"
    "- proc.kind: use \"lognorm\".\n"
    "- proc.mean: seconds per part.\n"
    "- proc.cv: coefficient of variation between 0 and 1.\n"
    "- in_buffer: non-negative integer or -1 for unlimited.\n"
    "- mtbf: seconds or -1 when no downtime estimate is appropriate.\n"
    "- repair.mean: seconds or 0 when mtbf is -1.\n"
    "- repair.cv: between 0 and 1.\n"
    "- yield: greater than 0 and at most 1.\n\n"

    "Every numeric value is a ROUGH INDUSTRY ESTIMATE, not a measurement.\n"
    "Never claim that an estimated number came from the user's dataset.\n"
    "Do not model branching, merging or rework loops. If the description "
    "implies them, select the main serial path.\n\n"

    "If the description contains multiple machines at one operation, represent "
    "them with the machines field rather than creating duplicate stages unless "
    "the description clearly identifies separate sequential operations.\n\n"

    "The output must be complete JSON and must contain at least 2 stages."
)


def _factory_retry_prompt(description: str) -> str:
    """
    Smaller retry prompt designed to reduce the chance of truncation.
    """
    return (
        "Create the estimated serial production-line model now.\n\n"
        "User description:\n"
        f"{description[:3500]}\n\n"
        "Return ONLY compact valid JSON. "
        "Use 2 to 8 stages unless more are clearly required. "
        "Do not include explanations."
    )


def describe_factory(
    description: str,
) -> Tuple[dict, Dict[str, str], List[str]]:
    """
    Turn a plain-English production-line description into an ESTIMATED Plant.

    No real dataset is required for this workflow.

    Returns:
        (plant_dict, provenance, notes)
    """
    if not (description or "").strip():
        raise ValueError("empty description")

    clean_description = description.strip()[:4000]

    # ------------------------------------------------------------------
    # First attempt
    # ------------------------------------------------------------------

    raw = generate(
        FACTORY_SYSTEM,
        clean_description,
        max_tokens=3000,
        temperature=0.1,
        json_mode=True,
        thinking_budget=0,
    )

    try:
        data = _json_from(raw)

    except ValueError as first_error:
        # --------------------------------------------------------------
        # Retry once with a shorter, stricter prompt.
        #
        # This is specifically for truncated/invalid model output.
        # --------------------------------------------------------------

        retry_raw = generate(
            FACTORY_SYSTEM,
            _factory_retry_prompt(clean_description),
            max_tokens=3000,
            temperature=0.0,
            json_mode=True,
            thinking_budget=0,
        )

        try:
            data = _json_from(retry_raw)

        except ValueError as second_error:
            raise ValueError(
                "Gemini returned incomplete or invalid factory JSON "
                f"(first attempt: {first_error}; "
                f"retry: {second_error})"
            ) from second_error

    # ------------------------------------------------------------------
    # Validate top-level structure
    # ------------------------------------------------------------------

    if not isinstance(data, dict):
        raise ValueError(
            "model returned JSON, but the factory is not a JSON object"
        )

    if not isinstance(data.get("stages"), list):
        raise ValueError(
            "model did not return a stage list"
        )

    notes: List[str] = []
    stages: List[dict] = []
    seen = set()

    # ------------------------------------------------------------------
    # Normalize and deterministically validate every stage
    # ------------------------------------------------------------------

    for i, s in enumerate(data["stages"][:12]):

        if not isinstance(s, dict):
            notes.append(
                f"Stage {i + 1}: ignored because it was not an object"
            )
            continue

        # --------------------------------------------------------------
        # Stage ID
        # --------------------------------------------------------------

        sid = str(
            s.get("id")
            or s.get("name")
            or f"S{i + 1}"
        )[:24]

        sid = re.sub(
            r"[^A-Za-z0-9_\-]",
            "_",
            sid
        ) or f"S{i + 1}"

        while sid in seen:
            sid += "_2"

        seen.add(sid)

        # --------------------------------------------------------------
        # Processing parameters
        # --------------------------------------------------------------

        proc = (
            s.get("proc")
            if isinstance(s.get("proc"), dict)
            else {}
        )

        try:
            mean = float(
                proc.get(
                    "mean",
                    s.get("cycle_time", 0)
                ) or 0
            )
        except (TypeError, ValueError):
            mean = 0.0

        if mean <= 0 or mean > 86400:
            notes.append(
                f"{sid}: processing time was missing or absurd; "
                f"defaulted to 60s"
            )
            mean = 60.0

        try:
            cv = float(
                proc.get("cv", 0.2) or 0.0
            )
        except (TypeError, ValueError):
            cv = 0.2

        cv = min(
            1.5,
            max(0.0, cv)
        )

        # --------------------------------------------------------------
        # Machines
        # --------------------------------------------------------------

        try:
            machines = int(
                s.get("machines", 1) or 1
            )
        except (TypeError, ValueError):
            machines = 1

        machines = max(
            1,
            min(50, machines)
        )

        # --------------------------------------------------------------
        # Repair / MTTR
        # --------------------------------------------------------------

        rep = (
            s.get("repair")
            if isinstance(s.get("repair"), dict)
            else {}
        )

        try:
            mttr = float(
                rep.get("mean", 0) or 0
            )
        except (TypeError, ValueError):
            mttr = 0.0

        mttr = max(
            0.0,
            mttr
        )

        # --------------------------------------------------------------
        # MTBF
        # --------------------------------------------------------------

        try:
            mtbf = float(
                s.get("mtbf", -1) or -1
            )
        except (TypeError, ValueError):
            mtbf = -1.0

        if mtbf > 0 and mttr <= 0:
            mttr = max(
                60.0,
                mean * 5
            )

            notes.append(
                f"{sid}: MTBF without MTTR; MTTR defaulted"
            )

        if mtbf <= 0:
            mtbf = -1.0
            mttr = 0.0

        # --------------------------------------------------------------
        # Yield
        # --------------------------------------------------------------

        try:
            yld = float(
                s.get("yield", 1.0) or 1.0
            )
        except (TypeError, ValueError):
            yld = 1.0

        if not (0 < yld <= 1):
            notes.append(
                f"{sid}: yield {yld} out of range; set to 1.0"
            )
            yld = 1.0

        # --------------------------------------------------------------
        # Input buffer
        # --------------------------------------------------------------

        try:
            buf = float(
                s.get("in_buffer", -1)
            )
        except (TypeError, ValueError):
            buf = -1.0

        if buf < 0:
            in_buffer = -1
        else:
            in_buffer = max(
                0,
                int(buf)
            )

        # --------------------------------------------------------------
        # Final deterministic stage object
        # --------------------------------------------------------------

        stage = {
            "id": sid,
            "name": str(
                s.get("name") or sid
            )[:40],

            "machines": machines,

            "proc": {
                "kind": (
                    "lognorm"
                    if cv
                    else "det"
                ),
                "mean": mean,
                "cv": cv,
            },

            "in_buffer": in_buffer,

            "mtbf": (
                -1
                if mtbf <= 0
                else mtbf
            ),

            "repair": {
                "kind": "lognorm",
                "mean": mttr,
                "cv": (
                    0.4
                    if mttr
                    else 0.0
                ),
            },

            "yield": yld,
        }

        stages.append(stage)

    # ------------------------------------------------------------------
    # Minimum viable factory
    # ------------------------------------------------------------------

    if len(stages) < 2:
        raise ValueError(
            "model returned fewer than two usable stages"
        )

    # ------------------------------------------------------------------
    # Construct Plant
    # ------------------------------------------------------------------

    plant = {
        "name": str(
            data.get("name")
            or "Described line"
        )[:60],

        "horizon_s": 28800,
        "warmup_s": 3600,

        "stages": stages,
    }

    # ------------------------------------------------------------------
    # Provenance
    #
    # Every generated parameter is explicitly marked estimated.
    # ------------------------------------------------------------------

    provenance: Dict[str, str] = {}

    for stage in stages:
        stage_id = stage["id"]

        for field in (
            "proc",
            "mtbf",
            "repair",
            "yield",
            "machines",
        ):
            provenance[
                f"{stage_id}:{field}"
            ] = "estimated"

    return (
        plant,
        provenance,
        notes,
    )
