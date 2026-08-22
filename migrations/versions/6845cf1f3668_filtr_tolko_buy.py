"""filtr tolko buy

Revision ID: 6845cf1f3668
Revises: 8010e076158d
Create Date: 2026-08-20 22:25:57.404303

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6845cf1f3668'
down_revision: Union[str, Sequence[str], None] = '8010e076158d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('user_settings', schema=None) as batch_op:
        # У всех, кто сохранял настройки раньше, фильтр «только buy» выключен:
        # его до этой ревизии просто не было. server_default нужен строкам,
        # которые уже лежат в базе, — колонка объявлена NOT NULL.
        batch_op.add_column(
            sa.Column('buy_only', sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('user_settings', schema=None) as batch_op:
        batch_op.drop_column('buy_only')
