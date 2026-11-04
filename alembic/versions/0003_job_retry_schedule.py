"""Add persisted retry scheduling for durable jobs."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0003_job_retry_schedule"
down_revision = "0002_whatsapp_events"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("jobs")}
    if "next_attempt_at" not in columns:
        op.add_column("jobs", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    indexes = {index["name"] for index in inspect(bind).get_indexes("jobs")}
    if "ix_jobs_next_attempt_at" not in indexes:
        op.create_index("ix_jobs_next_attempt_at", "jobs", ["next_attempt_at"])


def downgrade():
    bind = op.get_bind()
    indexes = {index["name"] for index in inspect(bind).get_indexes("jobs")}
    if "ix_jobs_next_attempt_at" in indexes:
        op.drop_index("ix_jobs_next_attempt_at", table_name="jobs")
    columns = {column["name"] for column in inspect(bind).get_columns("jobs")}
    if "next_attempt_at" in columns:
        op.drop_column("jobs", "next_attempt_at")
