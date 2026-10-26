"""Persist authenticated session ownership."""

import sqlalchemy as sa
from alembic import op

revision = "p16_0001"
down_revision = "p11_0001"
branch_labels = None
depends_on = None


def upgrade():
    # bootstrap_schema may already have created this additive table.
    if op.get_context().as_sql or not sa.inspect(op.get_bind()).has_table("shopping_sessions"):
        op.create_table(
            "shopping_sessions",
            sa.Column("session_id", sa.String(191, collation="utf8mb4_bin"), primary_key=True),
            sa.Column("buyer_id", sa.String(64, collation="utf8mb4_bin"), nullable=False),
        )
        op.create_index("ix_shopping_sessions_buyer_id", "shopping_sessions", ["buyer_id"])
    op.execute(
        "ALTER TABLE buyer_preferences MODIFY buyer_id VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL"
    )
    for table in ("user_feedback", "session_metrics"):
        op.execute(
            f"ALTER TABLE {table} MODIFY session_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL"
        )


def downgrade():
    op.drop_index("ix_shopping_sessions_buyer_id", table_name="shopping_sessions")
    op.drop_table("shopping_sessions")
