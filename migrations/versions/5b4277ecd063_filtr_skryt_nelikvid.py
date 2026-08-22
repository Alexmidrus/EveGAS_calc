"""filtr skryt nelikvid

Revision ID: 5b4277ecd063
Revises: 6845cf1f3668
Create Date: 2026-08-20 22:44:05.049810

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5b4277ecd063'
down_revision: Union[str, Sequence[str], None] = '6845cf1f3668'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('user_settings', schema=None) as batch_op:
        # У всех, кто сохранял настройки раньше, фильтр «скрыть неликвид»
        # выключен: его до этой ревизии не было. server_default нужен строкам,
        # которые уже лежат в базе, — колонка объявлена NOT NULL.
        batch_op.add_column(
            sa.Column('hide_illiquid', sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('user_settings', schema=None) as batch_op:
        batch_op.drop_column('hide_illiquid')
