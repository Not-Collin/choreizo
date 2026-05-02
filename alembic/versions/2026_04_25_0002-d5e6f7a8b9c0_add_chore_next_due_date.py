"""add chore next_due_date

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-04-25 00:02:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("chores", schema=None) as batch_op:
        batch_op.add_column(sa.Column("next_due_date", sa.String(length=10), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("chores", schema=None) as batch_op:
        batch_op.drop_column("next_due_date")
