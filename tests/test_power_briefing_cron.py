from pathlib import Path


CRON_TEMPLATE = Path(__file__).resolve().parents[1] / "deploy" / "power_briefing.cron"
DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


def test_power_briefing_cron_keeps_morning_and_conditional_afternoon_releases_separate():
    assert CRON_TEMPLATE.is_file()
    content = CRON_TEMPLATE.read_text(encoding="utf-8")
    assert "CRON_TZ=Asia/Shanghai" in content
    assert "WEATHER_AGENT_COMPOSE_DIR=" in content
    lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and line.lstrip()[0].isdigit()
    ]

    assert len(lines) == 4
    precompute = next(line for line in lines if "POWER_BRIEFING_MODE=precompute" in line)
    scheduled_send = next(line for line in lines if "POWER_BRIEFING_MODE=send" in line)
    afternoon_precompute = next(
        line for line in lines if "POWER_BRIEFING_MODE=afternoon_precompute" in line
    )
    afternoon_send = next(
        line for line in lines if "POWER_BRIEFING_MODE=afternoon_send" in line
    )

    assert precompute.startswith("50 8 * * * ")
    assert "POWER_BRIEFING_ALLOW_SEND=1" not in precompute
    assert scheduled_send.startswith("0 9 * * * ")
    assert afternoon_precompute.startswith("50 14 * * * ")
    assert afternoon_send.startswith("0 15 * * * ")
    assert "POWER_BRIEFING_ALLOW_SEND=1" not in scheduled_send
    assert "POWER_BRIEFING_AFTERNOON_ALLOW_SEND=1" not in afternoon_send
    for line in lines:
        assert "docker compose exec -T" in line
        assert "python -m scripts.daily_power_briefing" in line
        assert " < " not in line


def test_weather_image_contains_the_scheduled_briefing_module():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY scripts ./scripts" in dockerfile
