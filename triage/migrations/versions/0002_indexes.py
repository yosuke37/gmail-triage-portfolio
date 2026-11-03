"""Indexes for review pagination, audit history, and worker accounting."""
from alembic import op
import sqlalchemy as sa

revision = "0002_indexes"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

INDEXES = [
    ("ix_emails_account_status_id", "emails", ["account", "status", "id"]),
    ("ix_emails_status_id", "emails", ["status", "id"]),
    ("ix_emails_account_created", "emails", ["account", "created"]),
    ("ix_audit_email_event_id", "audit", ["email_row", "event", "id"]),
    ("ix_automation_processed_created", "automation_processed", ["created"]),
    ("ix_runs_started", "automation_runs", ["started"]),
]


def upgrade():
    for name, table, columns in INDEXES:
        existing = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}
        if name not in existing:
            op.create_index(name, table, columns)


def downgrade():
    for name, table, columns in reversed(INDEXES):
        op.drop_index(name, table_name=table)
