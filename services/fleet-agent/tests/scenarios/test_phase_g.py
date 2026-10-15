"""Phase G: every scripted scenario passes all of its checks.

The scenarios themselves live in app/scenarios.py so the same code makes
the results report (python -m app.scenarios --report ...)."""
import pytest

from app.scenarios import SCENARIOS, report_markdown


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[fn.__name__ for fn in SCENARIOS])
def test_scenario(scenario):
    outcome = scenario()
    failed = [f"{c.text} ({c.detail})" for c in outcome.checks if not c.ok]
    assert outcome.checks and not failed, f"{outcome.key} {outcome.title}: " + "; ".join(failed)


def test_report_lists_every_scenario():
    from app.scenarios import g7_partition, g9_instruction

    text = report_markdown([g7_partition(), g9_instruction()])
    assert "## G7. Radio partition - PASS" in text and "## G9. Operator instruction - PASS" in text
    assert "| G7 Radio partition | PASS |" in text
