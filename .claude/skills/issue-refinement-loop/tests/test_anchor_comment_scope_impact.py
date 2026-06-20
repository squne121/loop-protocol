import json
from pathlib import Path


def test_anchor_comment_scope_impact_allows_json_null_only():
    schema = json.loads(
        (Path(__file__).resolve().parent.parent / "schemas" / "loop_state.schema.json").read_text(
            encoding="utf-8"
        )
    )
    scope_impact = schema["definitions"]["anchor_comment"]["properties"]["scope_impact"]
    assert scope_impact["type"] == ["string", "null"]
    assert None in scope_impact["enum"]
    assert "null" not in scope_impact["enum"]
