from pathlib import Path


def test_management_http_surface_is_bound_to_loopback_by_default() -> None:
    compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    compose = compose_path.read_text(encoding="utf-8")

    assert '"127.0.0.1:8001:8000"' in compose
    assert '\n      - "8001:8000"' not in compose
