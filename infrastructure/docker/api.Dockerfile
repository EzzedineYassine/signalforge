FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml alembic.ini ./

# Install CPU-only PyTorch first to avoid pulling multi-GB CUDA runtime wheels.
RUN pip install --upgrade pip && \
    pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.3,<3.0" && \
    pip install \
      "fastapi" \
      "uvicorn[standard]" \
      "pydantic>=2.7" \
      "pydantic-settings" \
      "sqlalchemy>=2.0" \
      "psycopg[binary]>=3.1" \
      "alembic" \
      "numpy>=2.0" \
      "pandas>=2.2" \
      "scikit-learn>=1.5" \
      "qdrant-client>=1.11" \
      "sentence-transformers>=3.0" \
      "rank-bm25>=0.2.2" \
      "networkx>=3.3" \
      "httpx>=0.27" \
      "langgraph>=0.2"

COPY apps/api ./apps/api
COPY services ./services
COPY evaluation ./evaluation
COPY database ./database
COPY data/synthetic/knowledge_base ./data/synthetic/knowledge_base
COPY experiments/benchmarks/anomaly_seed42 ./experiments/benchmarks/anomaly_seed42

RUN pip install --no-deps .

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn apps.api.src.main:app --host 0.0.0.0 --port 8000"]
