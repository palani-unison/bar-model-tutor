import json
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parent.parent


def _schema():
    return json.loads((ROOT / "specs" / "barspec.schema.json").read_text(encoding="utf-8"))


def test_marbles_example_validates():
    spec = json.loads((ROOT / "specs" / "examples" / "marbles.json").read_text(encoding="utf-8"))
    jsonschema.validate(instance=spec, schema=_schema())


def test_schema_requires_pause_checked_by_verify_not_schema():
    # Pause is a teaching rule enforced in agent/verify.py, not the JSON schema.
    spec = json.loads((ROOT / "specs" / "examples" / "marbles.json").read_text(encoding="utf-8"))
    assert any(s["do"] == "pause" for s in spec["timeline"])
