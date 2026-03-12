FROM python:3.12.9-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml .
RUN uv pip install --system --no-cache .

COPY *.py ./

USER 65534:65534

CMD ["python", "compactor.py"]
