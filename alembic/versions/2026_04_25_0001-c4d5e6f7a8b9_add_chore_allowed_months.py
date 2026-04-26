"""add chore allowed_months

Revision ID: c4d5e6f7a8b9
Revises: b3c8d9e1f2a4
Create Date: 2026-04-25 00:01:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "b3c8d9e1f2a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("chores", schema=None) as batch_op:
        batch_op.add_column(sa.Column("allowed_months", sa.String(length=35), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("chores", schema=None) as batch_op:
        batch_op.drop_column("allowed_months")
