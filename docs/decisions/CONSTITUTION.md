# SignalForge Engineering Constitution

1. **Repo is the source of truth.** Code, migrations, and decisions live in the repository; undocumented state does not count.
2. **Toolchain.** Python 3.12, `uv` for environments, `ruff` for linting, `pytest` for tests.
3. **Data.** PostgreSQL 16, all times stored as UTC `TIMESTAMPTZ`, schema changes only via Alembic migrations (never `create_all()`).
4. **Ingestion.** Idempotent (deduplicated by hash); rejected records are tracked explicitly in `ingestion_failures`, never silently dropped.
5. **Real-DB testing.** Tests run against real PostgreSQL (`signalforge_test`); no SQLite.
6. **Evidence only.** No fabricated metrics, benchmarks, or citations; report only what was measured or verified.
7. **Small reviewable slices.** Each task is a small, focused, verifiable change with a clear commit.