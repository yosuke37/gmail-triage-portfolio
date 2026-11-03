"""Bounded attachment text snapshots supporting review evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0005_attachments"
down_revision = "0004_job_attempts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("email_attachments",
        sa.Column("email_row", sa.Integer, sa.ForeignKey("emails.id"), primary_key=True),
        sa.Column("attachment_id", sa.Text, primary_key=True),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("created", sa.Text, nullable=False))


def downgrade():
    raise RuntimeError("Attachment evidence downgrade is disabled; restore a backup rather than discard review history.")
