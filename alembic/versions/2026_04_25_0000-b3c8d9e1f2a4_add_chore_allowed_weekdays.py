"""add chore allowed_weekdays

Revision ID: b3c8d9e1f2a4
Revises: ef171a701513
Create Date: 2026-04-25 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3c8d9e1f2a4"
down_revision: Union[str, None] = "ef171a701513"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("chores", schema=None) as batch_op:
        batch_op.add_column(sa.Column("allowed_weekdays", sa.String(length=13), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("chores", schema=None) as batch_op:
        batch_op.drop_column("allowed_weekdays")
