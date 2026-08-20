"""Состояние сервера ESI: таблица esi_status

Чип «ESI online» в шапке читает состояние Tranquility отсюда: спрашивает
его сборщик раз в цикл, пользователь к ESI не ходит (CLAUDE.md).

Revision ID: 8010e076158d
Revises: 2ce7ee3df765
Create Date: 2026-08-20 18:18:30.491806

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8010e076158d'
down_revision: Union[str, Sequence[str], None] = '2ce7ee3df765'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('esi_status',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('checked_at', sa.DateTime(), nullable=False),
    sa.Column('reachable', sa.Boolean(), nullable=False),
    sa.Column('players', sa.Integer(), nullable=True),
    sa.Column('server_version', sa.String(length=32), nullable=True),
    sa.Column('start_time', sa.DateTime(), nullable=True),
    sa.Column('vip', sa.Boolean(), nullable=False),
    sa.Column('error', sa.String(length=255), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_esi_status'))
    )
    with op.batch_alter_table('esi_status', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_esi_status_checked_at'), ['checked_at'], unique=False)



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('esi_status', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_esi_status_checked_at'))

    op.drop_table('esi_status')
