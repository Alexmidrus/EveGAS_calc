"""vid sbora v collection_run

Revision ID: b773882d66e3
Revises: 594f2e77bc72
Create Date: 2026-08-19 01:03:27.473063

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b773882d66e3'
down_revision: Union[str, Sequence[str], None] = '594f2e77bc72'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Запуски, записанные до этапа 11, все были сбором стакана — server_default
    # проставляет им 'orders', иначе колонка NOT NULL не встала бы на живую базу.
    with op.batch_alter_table('collection_run', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('kind', sa.String(length=16), nullable=False, server_default='orders')
        )
        batch_op.create_check_constraint(
            batch_op.f('ck_collection_run_collection_run_kind'),
            "kind IN ('orders', 'history')",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('collection_run', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('ck_collection_run_collection_run_kind'), type_='check')
        batch_op.drop_column('kind')
