from pathlib import Path


CRON_TEMPLATE = Path(__file__).resolve().parents[1] / "deploy" / "power_briefing.cron"


def test_power_briefing_cron_keeps_precompute_and_scheduled_send_separate():
    assert CRON_TEMPLATE.is_file()
    lines = [
        line.strip()
        for line in CRON_TEMPLATE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert len(lines) == 2
    precompute = next(line for line in lines if "POWER_BRIEFING_MODE=precompute" in line)
    scheduled_send = next(line for line in lines if "POWER_BRIEFING_MODE=send" in line)

    assert precompute.startswith("50 0 * * * ")
    assert "POWER_BRIEFING_ALLOW_SEND=1" not in precompute
    assert scheduled_send.startswith("0 1 * * * ")
    assert "POWER_BRIEFING_ALLOW_SEND=1" not in scheduled_send
    assert "scripts/daily_power_briefing.py" in precompute
    assert "scripts/daily_power_briefing.py" in scheduled_send
