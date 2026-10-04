"""Structural checks for the three editable control-layer policy presets."""
import json
from pathlib import Path


PRESET_DIR = Path(__file__).resolve().parents[1] / "config" / "presets"
NAMES = ("lenient", "standard", "strict")


def test_presets_have_consistent_intercept_policy_shape_and_ordered_strictness():
    presets = {
        name: json.loads((PRESET_DIR / f"{name}.json").read_text(encoding="utf-8"))
        for name in NAMES
    }
    assert set(presets) == set(NAMES)
    assert all(preset["name"] == name for name, preset in presets.items())
    assert len({frozenset(preset) for preset in presets.values()}) == 1

    max_calls = []
    token_budgets = []
    tools = []
    for preset in presets.values():
        intercept = preset["intercept"]
        velocity = intercept["velocity_guard"]
        guard = intercept["semantic_guard"]
        assert velocity["window_s"] > 0
        max_calls.append(velocity["max_calls"])
        assert (0 < guard["alert_threshold"] <= guard["approve_threshold"]
                <= guard["block_threshold"] <= 1)
        assert guard["on_error"] == "BLOCK"
        assert "velocity-guard" not in intercept["feedback"]["allowed_actions"]
        assert intercept["pattern_match"]["enabled"]
        assert intercept["pattern_match"]["action"] == "BLOCK"
        assert preset["budget"]["tokens"] > 0
        assert preset["budget"]["tool_calls"] > 0
        token_budgets.append(preset["budget"]["tokens"])
        tools.append(set(preset["allowed_tools"]))
    assert max_calls[0] > max_calls[1] > max_calls[2]
    assert token_budgets[0] > token_budgets[1] > token_budgets[2]
    assert tools[2] <= tools[1] <= tools[0]
