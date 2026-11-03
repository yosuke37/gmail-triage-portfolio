"""Baseline schema and safe adoption of pre-migration SQLite databases."""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def schema():
    # This schema is frozen in the revision; do not import mutable app models.
    m = sa.MetaData()
    integer, string, real = sa.Integer, sa.Text, sa.Float

    def col(name, kind=string, nullable=True, default=None, primary=False):
        return sa.Column(name, kind, nullable=nullable if not primary else False, primary_key=primary, server_default=sa.text(default) if default is not None else None)

    def pk(name="id"):
        return col(name, integer, primary=True)

    def ref(name="email_row", primary=False, unique=False):
        return sa.Column(name, integer, sa.ForeignKey("emails.id", ondelete="RESTRICT"), nullable=False, primary_key=primary, unique=unique)

    sa.Table("emails", m, pk(), col("source", nullable=False), col("account", nullable=False), col("email_id", nullable=False),
             col("subject", nullable=False), col("sender", nullable=False), col("label", nullable=False), col("reason", nullable=False),
             col("needs_review", integer, False), col("mode", nullable=False), col("status", nullable=False, default="'pending'"), col("created", nullable=False),
             sa.UniqueConstraint("source", "account", "email_id", name="uq_emails_source_account_message"))
    sa.Table("audit", m, pk(), ref(), col("event", nullable=False), col("details", nullable=False), col("created", nullable=False))
    sa.Table("actions", m, pk(), ref(unique=True), col("label_id", nullable=False), col("label", nullable=False), col("preexisting", integer, False), col("state", nullable=False), col("created", nullable=False))
    sa.Table("replies", m, pk(), ref(unique=True), col("payload", nullable=False), col("state", nullable=False, default="'draft'"), col("sent_message_id"), col("created", nullable=False))
    sa.Table("proposals", m, ref(primary=True), col("payload", nullable=False), col("created", nullable=False))
    sa.Table("automation", m, pk(), col("config", nullable=False), col("enabled", integer, False, "0"), col("owner"), col("account"),
             col("pid", integer), col("heartbeat", real), col("state", nullable=False, default="'stopped'"), col("next_run", real), col("updated", nullable=False), sa.CheckConstraint("id=1", name="ck_automation_singleton"))
    sa.Table("automation_runs", m, pk(), col("started", nullable=False), col("finished"), col("state", nullable=False), col("account", nullable=False), col("mode", nullable=False),
             *[col(name, integer, False, "0") for name in ("processed", "applied", "requests", "input_tokens", "output_tokens", "total_tokens")], col("error"))
    sa.Table("automation_processed", m, sa.Column("run_id", integer, sa.ForeignKey("automation_runs.id", ondelete="RESTRICT"), nullable=False), col("email_id", nullable=False), col("account", nullable=False), col("created", nullable=False), sa.UniqueConstraint("run_id", "email_id", "account", name="uq_automation_processed_message"))
    sa.Table("reply_suggestions", m, ref(primary=True), col("state", nullable=False), col("body"), col("notes"), col("error"), col("created", nullable=False))
    sa.Table("daily_digests", m, pk(), col("account", nullable=False), col("day", nullable=False), col("state", nullable=False), col("content"), col("error"), col("created", nullable=False), col("requests", integer, False, "0"), col("total_tokens", integer, False, "0"), sa.UniqueConstraint("account", "day", name="uq_digest_account_day"))
    return m


def upgrade():
    connection = op.get_bind()
    metadata = schema()
    metadata.create_all(connection, checkfirst=True)
    inspector = sa.inspect(connection)
    digest_columns = {c["name"] for c in inspector.get_columns("daily_digests")}
    for name in ("requests", "total_tokens"):
        if name not in digest_columns:
            op.add_column("daily_digests", sa.Column(name, sa.Integer, nullable=False, server_default="0"))
    inspector = sa.inspect(connection)
    for table in metadata.sorted_tables:
        columns = {c["name"] for c in inspector.get_columns(table.name)}
        if not set(table.columns.keys()).issubset(columns):
            raise RuntimeError("Legacy schema is incompatible; restore the backup and review migration requirements.")


def downgrade():
    raise RuntimeError("Destructive baseline downgrade is disabled. Restore a verified backup instead.")
