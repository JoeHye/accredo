# ser_app/content.py
# Loads and validates content.json, and scores a set of answers against it.
# Wording lives in content.json so content changes never need a code change.
import json
import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_CONTENT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "content.json")
ALLOWED_STATUSES = {"DRAFT", "APPROVED"}


class ContentError(Exception):
    """content.json is missing, unreadable, or structurally invalid."""


class AnswerError(ValueError):
    """Submitted answers do not match the questions in content.json."""


def load_content(path=None):
    path = path or os.environ.get("SER_CONTENT_PATH") or DEFAULT_CONTENT_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = json.load(fh)
    except FileNotFoundError:
        raise ContentError(f"Content file not found: {path}")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ContentError(f"Cannot read content file {path}: {e.__class__.__name__}: {e}")
    validate_content(content)
    logger.info(f"Loaded SER diagnostic content v{content['version']} ({content['status']}) from {path}")
    return content


def validate_content(content):
    """Fails at startup, not mid-request, if the content file is inconsistent."""
    problems = []
    if not isinstance(content, dict):
        raise ContentError("Content root must be a JSON object.")
    for key in ("status", "version", "intro", "answer_scale", "sections", "questions", "gate_result", "bands", "call_to_action"):
        if key not in content:
            problems.append(f"missing key '{key}'")
    if problems:
        raise ContentError("; ".join(problems))

    if content["status"] not in ALLOWED_STATUSES:
        problems.append(f"status must be one of {sorted(ALLOWED_STATUSES)}, got {content['status']!r}")

    scale_values = [a.get("value") for a in content["answer_scale"]]
    if len(set(scale_values)) != len(scale_values) or not scale_values:
        problems.append("answer_scale values must be non-empty and unique")
    for a in content["answer_scale"]:
        if not isinstance(a.get("points"), int) or a["points"] < 0:
            problems.append(f"answer_scale entry {a.get('value')!r} needs a non-negative integer 'points'")
    if "no" not in scale_values:
        problems.append("answer_scale must include 'no' (gate questions depend on it)")

    ids = [q.get("id") for q in content["questions"]]
    if len(set(ids)) != len(ids) or not ids:
        problems.append("question ids must be non-empty and unique")
    for q in content["questions"]:
        for key in ("id", "text", "why"):
            if not q.get(key):
                problems.append(f"question {q.get('id')!r} missing '{key}'")
    section_ids = [qid for s in content["sections"] for qid in s.get("question_ids", [])]
    if sorted(section_ids) != sorted(ids):
        problems.append("sections must list every question exactly once")

    bands = content["bands"]
    if not bands or min(b.get("min_score", -1) for b in bands) != 0:
        problems.append("bands must include one with min_score 0 so every score maps to a result")
    if len({b.get("min_score") for b in bands}) != len(bands):
        problems.append("band min_score values must be unique")

    if content["status"] == "APPROVED":
        if content.get("branding") in (None, "", "TBD"):
            problems.append("APPROVED content needs branding decided (not 'TBD')")
        if not content["call_to_action"].get("decided"):
            problems.append("APPROVED content needs call_to_action.decided = true")

    if problems:
        raise ContentError("; ".join(problems))


def max_score(content):
    top = max(a["points"] for a in content["answer_scale"])
    return top * len(content["questions"])


def score_answers(content, answers):
    """
    answers: {question_id: answer_value}. Every question must be answered with a scale value.
    Returns the result dict shown to the prospect. Raises AnswerError on invalid input.
    """
    if not isinstance(answers, dict):
        raise AnswerError("answers must be an object mapping question id to answer")
    points = {a["value"]: a["points"] for a in content["answer_scale"]}
    labels = {a["value"]: a["label"] for a in content["answer_scale"]}
    questions = content["questions"]
    known = {q["id"] for q in questions}

    unknown = sorted(set(answers) - known)
    missing = [q["id"] for q in questions if q["id"] not in answers]
    invalid = sorted(qid for qid, v in answers.items() if qid in known and v not in points)
    if unknown or missing or invalid:
        parts = []
        if missing:
            parts.append(f"unanswered: {', '.join(missing)}")
        if unknown:
            parts.append(f"unknown questions: {', '.join(unknown)}")
        if invalid:
            parts.append(f"invalid answers for: {', '.join(invalid)} (allowed: {', '.join(points)})")
        raise AnswerError("; ".join(parts))

    total = sum(points[answers[q["id"]]] for q in questions)
    # Per the draft: a "No" on a gate question caps the result, whatever the total.
    # "Partly or not sure" on a gate question does NOT trigger the gate (open review question).
    gate_failed = any(q.get("gate") and answers[q["id"]] == "no" for q in questions)

    if gate_failed:
        band = content["gate_result"]
    else:
        band = next(b for b in sorted(content["bands"], key=lambda b: b["min_score"], reverse=True)
                    if total >= b["min_score"])

    # Gaps: worst answers first, then question order.
    order = {q["id"]: i for i, q in enumerate(questions)}
    gaps = [q for q in questions if points[answers[q["id"]]] < max(points.values())]
    gaps.sort(key=lambda q: (points[answers[q["id"]]], order[q["id"]]))

    return {
        "score": total,
        "max_score": max_score(content),
        "result_key": band["key"],
        "result_title": band["title"],
        "result_message": band["message"],
        "gate_failed": gate_failed,
        "gaps": [{"id": q["id"], "question": q["text"], "answer": labels[answers[q["id"]]], "why": q["why"]} for q in gaps],
        "content_version": content["version"],
    }
