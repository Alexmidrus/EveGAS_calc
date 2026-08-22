"""filtr po odnoy stroke na hab

Revision ID: dbd72c07311f
Revises: 5b4277ecd063
Create Date: 2026-08-20 23:03:41.132386

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dbd72c07311f'
down_revision: Union[str, Sequence[str], None] = '5b4277ecd063'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('user_settings', schema=None) as batch_op:
        # У всех, кто сохранял настройки раньше, свёртка выключена: её до этой
        # ревизии не было. server_default нужен строкам, которые уже лежат
        # в базе, — колонка объявлена NOT NULL.
        batch_op.add_column(
            sa.Column('best_per_hub', sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('user_settings', schema=None) as batch_op:
        batch_op.drop_column('best_per_hub')
