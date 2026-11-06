"""Add backtest_jobs table for async WhatsApp backtests."""
from alembic import op
from rslr.storage import Base

revision = "0002_backtest_jobs"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    op.drop_table("backtest_jobs")
