from pathlib import Path


def test_architectural_invariants_are_present() -> None:
    text = Path("src/interface_cua/invariants.md").read_text(encoding="utf-8")
    assert "No LLM decision during replay" in text
    assert "No unique target" in text
    assert "Never blind-retry" in text
    assert "Exactly one controller" in text
    assert "Policy is evaluated below the model" in text

