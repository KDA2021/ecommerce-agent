"""Add nullable structured buyer preference facts without rewriting legacy data."""

import sqlalchemy as sa
from alembic import op

revision = "p17_0001"
down_revision = "p16_0001"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("attribute", 32),
    ("value", 64),
    ("currency", 8),
    ("source_session_id", 191),
    ("updated_at", 40),
)


def upgrade():
    """Safe after create_all and repeat invocation; old rows remain nullable."""
    existing = set()
    if not op.get_context().as_sql:
        existing = {
            column["name"] for column in sa.inspect(op.get_bind()).get_columns("buyer_preferences")
        }
    for name, length in _COLUMNS:
        if name not in existing:
            op.add_column("buyer_preferences", sa.Column(name, sa.String(length), nullable=True))


def downgrade():
    existing = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("buyer_preferences")
    }
    for name, _ in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("buyer_preferences", name)
