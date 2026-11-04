"""WhatsApp inbound message deduplication and audit trail."""
from alembic import op
from sqlalchemy import (
    Column, DateTime, Index, String, Table, Text, inspect,
)

revision = "0002_whatsapp_events"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if "whatsapp_inbound_events" not in inspect(bind).get_table_names():
        op.create_table(
            "whatsapp_inbound_events",
            Column("message_id", String(128), primary_key=True),
            Column("sender", String(32), nullable=False),
            Column("message_type", String(24), nullable=False, server_default="text"),
            Column("message_text", Text(), nullable=False, server_default=""),
            Column("status", String(24), nullable=False, server_default="RECEIVED"),
            Column("error", Text(), nullable=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("processed_at", DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_whatsapp_inbound_events_sender", "whatsapp_inbound_events", ["sender"])
        op.create_index("ix_whatsapp_inbound_events_status", "whatsapp_inbound_events", ["status"])
        op.create_index("ix_whatsapp_inbound_events_created_at", "whatsapp_inbound_events", ["created_at"])


def downgrade():
    bind = op.get_bind()
    if "whatsapp_inbound_events" in inspect(bind).get_table_names():
        op.drop_index("ix_whatsapp_inbound_events_created_at", table_name="whatsapp_inbound_events")
        op.drop_index("ix_whatsapp_inbound_events_status", table_name="whatsapp_inbound_events")
        op.drop_index("ix_whatsapp_inbound_events_sender", table_name="whatsapp_inbound_events")
        op.drop_table("whatsapp_inbound_events")
