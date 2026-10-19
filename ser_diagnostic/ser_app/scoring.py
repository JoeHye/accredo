"""Content loading/validation and scoring for the SER Readiness Diagnostic.

All wording, points and band thresholds live in content.json; nothing here is
specific to a particular question, so text edits never require code changes.
"""
import json
import os


class ContentError(ValueError):
    """content.json is missing, malformed, or internally inconsistent."""


def load_content(path):
    try:
        with open(path, encoding="utf-8") as f:
            content = json.load(f)
    except FileNotFoundError as e:
        raise ContentError(f"Content file not found: {path}") from e
    except json.JSONDecodeError as e:
        raise ContentError(f"Content file is not valid JSON ({path}): {e}") from e
    validate_content(content)
    return content


def validate_content(content):
    for key in ("title", "intro", "answers", "questions", "bands", "gate_failed", "cta", "contact_step"):
        if key not in content:
            raise ContentError(f"content.json is missing required key '{key}'")

    answer_keys = [a.get("key") for a in content["answers"]]
    if not answer_keys or len(set(answer_keys)) != len(answer_keys):
        raise ContentError("content.json 'answers' must be non-empty with unique keys")
    for a in content["answers"]:
        if not isinstance(a.get("points"), int) or a["points"] < 0:
            raise ContentError(f"Answer '{a.get('key')}' must have a non-negative integer 'points'")

    ids = [q.get("id") for q in content["questions"]]
    if not ids or len(set(ids)) != len(ids):
        raise ContentError("content.json 'questions' must be non-empty with unique ids")
    for q in content["questions"]:
        for field in ("id", "text", "why"):
            if not q.get(field):
                raise ContentError(f"Question '{q.get('id')}' is missing '{field}'")

    band_keys = {b.get("key") for b in content["bands"]}
    if not any(b.get("min_percent") == 0 for b in content["bands"]):
        raise ContentError("content.json 'bands' must include a band with min_percent 0")
    for b in content["bands"]:
        for field in ("key", "label", "color", "message"):
            if not b.get(field):
                raise ContentError(f"Band '{b.get('key')}' is missing '{field}'")
        if not isinstance(b.get("min_percent"), (int, float)) or not 0 <= b["min_percent"] <= 100:
            raise ContentError(f"Band '{b.get('key')}' min_percent must be a number from 0 to 100")
    if content["gate_failed"].get("band") not in band_keys:
        raise ContentError("content.json 'gate_failed.band' must name one of the bands")


def max_points(content):
    return len(content["questions"]) * max(a["points"] for a in content["answers"])


def score(content, answers):
    """Score a dict of {question_id: answer_key}.

    Raises ValueError if any question is unanswered or has an unknown answer.
    Returns a dict with points, max, percent, band (the band dict), gated, and
    gaps (questions not answered with the top-scoring option, worst first).
    """
    points_by_key = {a["key"]: a["points"] for a in content["answers"]}
    top_points = max(points_by_key.values())

    total = 0
    gated = False
    gaps = []
    for q in content["questions"]:
        answer = answers.get(q["id"])
        if answer not in points_by_key:
            raise ValueError(f"Missing or invalid answer for {q['id']}")
        pts = points_by_key[answer]
        total += pts
        if q.get("gate") and pts == 0:
            gated = True
        if pts < top_points:
            gaps.append({"question": q, "answer": answer, "points": pts})

    maximum = max_points(content)
    percent = round(total * 100 / maximum, 1) if maximum else 0.0
    gaps.sort(key=lambda g: g["points"])  # stable: keeps content order within equal points

    bands = sorted(content["bands"], key=lambda b: b["min_percent"], reverse=True)
    if gated:
        base = next(b for b in bands if b["key"] == content["gate_failed"]["band"])
        band = dict(base, label=content["gate_failed"]["label"], message=content["gate_failed"]["message"])
    else:
        band = next(b for b in bands if percent >= b["min_percent"])

    return {"points": total, "max": maximum, "percent": percent, "band": band, "gated": gated, "gaps": gaps}


DEFAULT_CONTENT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "content.json")
