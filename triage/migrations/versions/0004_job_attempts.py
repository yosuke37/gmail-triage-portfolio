"""Durable operational evidence without message text or prompt content."""
from alembic import op
import sqlalchemy as sa

revision = "0004_job_attempts"
down_revision = "0003_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("job_attempts",
        sa.Column("job_id", sa.Text, sa.ForeignKey("jobs.id"), primary_key=True),
        sa.Column("attempt", sa.Integer, primary_key=True),
        sa.Column("worker_id", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("started_at", sa.Float, nullable=False),
        sa.Column("finished_at", sa.Float), sa.Column("duration_seconds", sa.Float),
        *[sa.Column(name, sa.Integer, nullable=False, server_default="0") for name in
          ("requests", "input_tokens", "output_tokens", "total_tokens")])
    op.create_index("ix_job_attempts_started", "job_attempts", ["started_at"])


def downgrade():
    raise RuntimeError("Restore a backup rather than discard operational history.")
