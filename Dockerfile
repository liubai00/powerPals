FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY services ./services
COPY schemas ./schemas
COPY examples ./examples
COPY docs ./docs
COPY scripts ./scripts

RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ --timeout 120 --retries 5 .

EXPOSE 8000

CMD ["uvicorn", "services.weather_bot.main:app", "--host", "0.0.0.0", "--port", "8000"]
