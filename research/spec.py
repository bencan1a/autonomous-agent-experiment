"""Load and validate the human-authored formal experiment specification.

The agreed baseline a research panel validates against lives in the existing
``experiments/<id>.md`` (consistency with how the repo already tracks
experiments) inside a fenced ```yaml block under ``## Specification (formal)``.
The prose around it stays as rationale + running observations.

The block is the *ground truth*: hypotheses (each with predicted AND falsifying
evidence), the variables, what is held constant, and success/stopping criteria.
The panel never authors these — it only operationalizes a coding scheme from
them (see ``research/prereg.py``).

``load_spec`` returns ``None`` (never raises) when no valid spec block is found,
so a panel run simply no-ops when there is no baseline to grade against.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Keys the loader normalizes onto every Spec (each defaults to an empty list).
_LIST_KEYS = (
    "independent_variables",
    "dependent_variables",
    "controls_held_constant",
    "success_criteria",
    "stopping_conditions",
    "candidate_behavior_codes",
)

_FENCE_RE = re.compile(r"```(?:ya?ml)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass
class Spec:
    """A parsed, validated formal experiment specification."""

    experiment_id: str
    spec_version: int
    hypotheses: list[dict[str, Any]]
    independent_variables: list[Any] = field(default_factory=list)
    dependent_variables: list[Any] = field(default_factory=list)
    controls_held_constant: list[Any] = field(default_factory=list)
    success_criteria: list[Any] = field(default_factory=list)
    stopping_conditions: list[Any] = field(default_factory=list)
    candidate_behavior_codes: list[dict[str, Any]] = field(default_factory=list)
    raw: str = ""          # the verbatim yaml block text
    spec_hash: str = ""     # sha256 of `raw` — bumps when the operator edits the spec

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "spec_version": self.spec_version,
            "hypotheses": self.hypotheses,
            "independent_variables": self.independent_variables,
            "dependent_variables": self.dependent_variables,
            "controls_held_constant": self.controls_held_constant,
            "success_criteria": self.success_criteria,
            "stopping_conditions": self.stopping_conditions,
            "candidate_behavior_codes": self.candidate_behavior_codes,
            "spec_hash": self.spec_hash,
        }


def spec_path(agent_root: Path | str, experiment_id: str) -> Path:
    return Path(agent_root) / "experiments" / f"{experiment_id}.md"


def _extract_spec_block(markdown: str) -> str | None:
    """Return the verbatim yaml of the first fenced block that looks like a spec.

    A spec block is a ```yaml fence whose parsed mapping carries a
    ``spec_version`` key. This is robust to heading wording / ordering.
    """
    import yaml

    for m in _FENCE_RE.finditer(markdown):
        body = m.group(1)
        try:
            parsed = yaml.safe_load(body)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict) and "spec_version" in parsed:
            return body
    return None


def _validate(parsed: dict[str, Any], *, experiment_id: str) -> list[str]:
    """Return a list of human-readable problems (empty == valid)."""
    problems: list[str] = []
    if "spec_version" not in parsed:
        problems.append("missing 'spec_version'")
    hyps = parsed.get("hypotheses")
    if not isinstance(hyps, list) or not hyps:
        problems.append("'hypotheses' must be a non-empty list")
    else:
        for i, h in enumerate(hyps):
            if not isinstance(h, dict):
                problems.append(f"hypothesis[{i}] is not a mapping")
                continue
            for key in ("id", "statement", "predicted_evidence", "falsifying_evidence"):
                if not h.get(key):
                    problems.append(f"hypothesis[{i}] missing '{key}'")
    declared = parsed.get("experiment_id")
    if declared and declared != experiment_id:
        problems.append(
            f"experiment_id mismatch: block says {declared!r}, file is {experiment_id!r}"
        )
    return problems


def load_spec(agent_root: Path | str, experiment_id: str) -> Spec | None:
    """Load + validate the formal spec for an experiment, or return None.

    Never raises: a missing file, a missing/garbled yaml block, or a validation
    failure are all logged and surface as ``None`` so the panel can skip cleanly.
    """
    path = spec_path(agent_root, experiment_id)
    if not path.exists():
        logger.info("No experiment doc for %s (%s); panel has no baseline.", experiment_id, path)
        return None
    try:
        markdown = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Cannot read experiment doc %s: %s", path, e)
        return None

    block = _extract_spec_block(markdown)
    if block is None:
        logger.info(
            "No '## Specification (formal)' yaml block in %s; panel has no baseline.", path
        )
        return None

    import yaml

    try:
        parsed = yaml.safe_load(block)
    except yaml.YAMLError as e:
        logger.warning("Spec yaml in %s failed to parse: %s", path, e)
        return None
    if not isinstance(parsed, dict):
        logger.warning("Spec block in %s is not a mapping.", path)
        return None

    problems = _validate(parsed, experiment_id=experiment_id)
    if problems:
        logger.warning("Formal spec for %s is invalid: %s", experiment_id, "; ".join(problems))
        return None

    spec_hash = hashlib.sha256(block.encode("utf-8")).hexdigest()
    return Spec(
        experiment_id=parsed.get("experiment_id") or experiment_id,
        spec_version=int(parsed.get("spec_version", 1)),
        hypotheses=list(parsed.get("hypotheses") or []),
        independent_variables=list(parsed.get("independent_variables") or []),
        dependent_variables=list(parsed.get("dependent_variables") or []),
        controls_held_constant=list(parsed.get("controls_held_constant") or []),
        success_criteria=list(parsed.get("success_criteria") or []),
        stopping_conditions=list(parsed.get("stopping_conditions") or []),
        candidate_behavior_codes=list(parsed.get("candidate_behavior_codes") or []),
        raw=block,
        spec_hash=spec_hash,
    )
