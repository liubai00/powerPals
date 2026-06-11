from services.weather_bot.search import _parse_search_results


def test_parse_tavily_search_results():
    results = _parse_search_results(
        {
            "results": [
                {"title": "Open-Meteo", "url": "https://open-meteo.com", "content": "Weather forecast API"},
                {"title": "", "url": "", "content": ""},
            ]
        }
    )

    assert len(results) == 1
    assert results[0].title == "Open-Meteo"
    assert results[0].url == "https://open-meteo.com"
