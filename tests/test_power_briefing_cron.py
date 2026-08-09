from pathlib import Path


CRON_TEMPLATE = Path(__file__).resolve().parents[1] / "deploy" / "power_briefing.cron"
DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


def test_power_briefing_cron_keeps_precompute_and_scheduled_send_separate():
    assert CRON_TEMPLATE.is_file()
    content = CRON_TEMPLATE.read_text(encoding="utf-8")
    assert "CRON_TZ=Asia/Shanghai" in content
    assert "WEATHER_AGENT_COMPOSE_DIR=" in content
    lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and line.lstrip()[0].isdigit()
    ]

    assert len(lines) == 2
    precompute = next(line for line in lines if "POWER_BRIEFING_MODE=precompute" in line)
    scheduled_send = next(line for line in lines if "POWER_BRIEFING_MODE=send" in line)

    assert precompute.startswith("50 8 * * * ")
    assert "POWER_BRIEFING_ALLOW_SEND=1" not in precompute
    assert scheduled_send.startswith("0 9 * * * ")
    assert "POWER_BRIEFING_ALLOW_SEND=1" not in scheduled_send
    assert "docker compose exec -T" in precompute
    assert "docker compose exec -T" in scheduled_send
    assert "python -m scripts.daily_power_briefing" in precompute
    assert "python -m scripts.daily_power_briefing" in scheduled_send
    assert " < " not in precompute
    assert " < " not in scheduled_send


def test_weather_image_contains_the_scheduled_briefing_module():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY scripts ./scripts" in dockerfile
