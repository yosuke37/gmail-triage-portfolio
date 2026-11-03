"""Durable queue, leased claims, and service heartbeats."""
from alembic import op
import sqlalchemy as sa

revision = "0003_jobs"
down_revision = "0002_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("jobs",
        sa.Column("id", sa.Text, primary_key=True), sa.Column("owner_id", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False), sa.Column("payload", sa.Text, nullable=False),
        sa.Column("payload_hash", sa.Text, nullable=False), sa.Column("idempotency_key", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False), sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False), sa.Column("available_at", sa.Float, nullable=False),
        sa.Column("lease_token", sa.Text), sa.Column("lease_until", sa.Float), sa.Column("worker_id", sa.Text),
        sa.Column("cancel_requested", sa.Integer, nullable=False, server_default="0"),
        sa.Column("progress", sa.Text), sa.Column("result", sa.Text), sa.Column("error", sa.Text),
        sa.Column("request_id", sa.Text), sa.Column("created", sa.Text, nullable=False),
        sa.Column("started", sa.Text), sa.Column("finished", sa.Text),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_jobs_owner_idempotency"))
    op.create_index("ix_jobs_ready", "jobs", ["state", "available_at", "created"])
    op.create_index("ix_jobs_owner_created", "jobs", ["owner_id", "created"])
    op.create_table("job_services", sa.Column("id", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False), sa.Column("heartbeat", sa.Float, nullable=False),
        sa.Column("state", sa.Text, nullable=False), sa.Column("job_id", sa.Text), sa.Column("started", sa.Text, nullable=False))


def downgrade():
    raise RuntimeError("Queue downgrade is disabled: drain/archive jobs and restore a backup instead of discarding queued work.")
