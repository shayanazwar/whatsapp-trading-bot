# Render deployment

1. Create a **new private GitHub repository** named `kronos-mexc-intelligence` and push this project. This package does not create cloud resources or access your Render account.
2. In Render, create a Blueprint from the repository and review `render.yaml` before applying it.
3. Set `APP_SECRET_KEY` to a random value at least 32 characters long, `ADMIN_USERNAME`, and `ADMIN_PASSWORD` (14+ characters). Set the database connection reference created by the Blueprint.
4. Confirm the web and both worker services use the same PostgreSQL database and commit.
5. All three containers run `python -m app.migration_lock` at startup. PostgreSQL advisory locking serializes concurrent Alembic upgrades if Render starts services together. Verify `/healthz`, `/readyz`, worker logs and a queued manual job.
6. The compute worker is CPU-first (the Blueprint currently requests 2 CPU / 4 GB RAM); the web and market services use 0.5 CPU / 512 MB RAM, and PostgreSQL uses 0.5 CPU / 1 GB RAM. These are current Render plan IDs as documented in 2026. The compute worker clones the pinned Kronos source during image build; Hugging Face weights download on first forecast to the worker's persistent disk. Benchmark real inference time and memory with 1,000 paths before increasing throughput or lowering the plan.

Render services are independent processes. A disk attached to one service is not a shared disk for other services. Do not use a local container path for cross-service forecasts; use PostgreSQL, which is the only implemented artifact backend in this release. Render does not publish a dedicated GPU service plan in its standard service plan list, so high-throughput inference may need an external GPU inference provider behind a private authenticated URL. Keep the web service lightweight.

The provided Blueprint is a deployable starting configuration but it cannot prove that the user's account, region, plan availability, billing or resource limits permit deployment. Validate it in Render before applying. WhatsApp remains blocked regardless of deployment.
