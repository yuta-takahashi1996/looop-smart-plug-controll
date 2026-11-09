"""add schedule devices（docs/17 §17.4.5・§17.4.6）

スケジュール・ルールの対象機器（schedule_devices・schedule_rule_devices）を追加し、control_schedules の
機器ごとの実行結果の列を schedule_devices へ移す。既存のスケジュール・ルールは制御対象の先頭の1台
（Phase 9 で制御していた機器）に関連付ける。制御対象がない場合は関連付けない（実行結果の列は失われる）。

Revision ID: f5cea316df10
Revises: 5b7c2e9d4a10
Create Date: 2026-11-07 15:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f5cea316df10'
down_revision: Union[str, Sequence[str], None] = '5b7c2e9d4a10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MOVED_COLUMNS = ('executed_start_at', 'executed_end_at', 'battery_start_percent', 'battery_end_percent', 'measured_power_w')


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('schedule_devices',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('schedule_id', sa.Integer(), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('PENDING', 'RUNNING', 'RETURNED', 'DONE', 'ABORTED', 'SKIPPED', name='scheduledevicestatus', native_enum=False, length=8), nullable=False),
    sa.Column('status_reason', sa.Text(), nullable=True),
    sa.Column('executed_start_at', sa.DateTime(), nullable=True),
    sa.Column('executed_end_at', sa.DateTime(), nullable=True),
    sa.Column('executed_saving', sa.Float(), nullable=True),
    sa.Column('battery_start_percent', sa.Integer(), nullable=True),
    sa.Column('battery_end_percent', sa.Integer(), nullable=True),
    sa.Column('measured_power_w', sa.Float(), nullable=True),
    sa.Column('power_source_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], ),
    sa.ForeignKeyConstraint(['power_source_id'], ['devices.id'], ),
    sa.ForeignKeyConstraint(['schedule_id'], ['control_schedules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('schedule_id', 'device_id')
    )
    with op.batch_alter_table('schedule_devices', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_schedule_devices_device_id'), ['device_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_schedule_devices_schedule_id'), ['schedule_id'], unique=False)

    op.create_table('schedule_rule_devices',
    sa.Column('rule_id', sa.Integer(), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], ),
    sa.ForeignKeyConstraint(['rule_id'], ['schedule_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('rule_id', 'device_id')
    )

    # 既存のスケジュール・ルールを、Phase 9 で制御していた機器（制御対象の先頭の1台）に関連付ける。
    # control_schedules の列の削除（batch モード）は表を作り直すため、先に値を読み出し、列を削除してから書き込む
    # （先に書き込むと、古い表の削除で schedule_devices の行が ON DELETE CASCADE で消える）
    bind = op.get_bind()
    primary = bind.execute(sa.text(
        "SELECT d.id, d.power_source_id FROM devices d JOIN device_types t ON t.id = d.device_type_id "
        "WHERE d.active = 1 AND t.category = 'POWER_SWITCH' ORDER BY d.sort_order, d.id LIMIT 1"
    )).first()
    rows = bind.execute(sa.text(
        "SELECT id, status, status_reason, executed_start_at, executed_end_at, executed_saving, "
        "battery_start_percent, battery_end_percent, measured_power_w FROM control_schedules"
    )).mappings().all()

    with op.batch_alter_table('control_schedules', schema=None) as batch_op:
        for column in MOVED_COLUMNS:
            batch_op.drop_column(column)

    if primary is not None:
        for row in rows:
            measured = row["battery_start_percent"] is not None or row["measured_power_w"] is not None
            bind.execute(
                sa.text(
                    "INSERT INTO schedule_devices (schedule_id, device_id, status, status_reason, executed_start_at, "
                    "executed_end_at, executed_saving, battery_start_percent, battery_end_percent, measured_power_w, "
                    "power_source_id) VALUES (:id, :device_id, :status, :status_reason, :executed_start_at, "
                    ":executed_end_at, :executed_saving, :battery_start_percent, :battery_end_percent, "
                    ":measured_power_w, :power_source_id)"
                ),
                {**row, "device_id": primary.id, "power_source_id": primary.power_source_id if measured else None},
            )
        bind.execute(
            sa.text("INSERT INTO schedule_rule_devices (rule_id, device_id) SELECT id, :device_id FROM schedule_rules"),
            {"device_id": primary.id},
        )

    with op.batch_alter_table('system_events', schema=None) as batch_op:
        batch_op.add_column(sa.Column('device_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_system_events_device_id'), ['device_id'], unique=False)
        batch_op.create_foreign_key('fk_system_events_device_id', 'devices', ['device_id'], ['id'])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('system_events', schema=None) as batch_op:
        batch_op.drop_constraint('fk_system_events_device_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_system_events_device_id'))
        batch_op.drop_column('device_id')

    # 機器ごとの実行結果は、スケジュールごとに最初の機器の値を戻す（列の追加で表を作り直す前に読み出す）
    bind = op.get_bind()
    first_rows = bind.execute(sa.text(
        "SELECT schedule_id, " + ", ".join(MOVED_COLUMNS) + " FROM schedule_devices sd "
        "WHERE sd.id = (SELECT MIN(id) FROM schedule_devices WHERE schedule_id = sd.schedule_id)"
    )).mappings().all()
    with op.batch_alter_table('control_schedules', schema=None) as batch_op:
        batch_op.add_column(sa.Column('executed_start_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('executed_end_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('battery_start_percent', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('battery_end_percent', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('measured_power_w', sa.Float(), nullable=True))
    for row in first_rows:
        bind.execute(
            sa.text("UPDATE control_schedules SET " + ", ".join(f"{c} = :{c}" for c in MOVED_COLUMNS) + " WHERE id = :schedule_id"),
            dict(row),
        )

    op.drop_table('schedule_rule_devices')
    with op.batch_alter_table('schedule_devices', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_schedule_devices_schedule_id'))
        batch_op.drop_index(batch_op.f('ix_schedule_devices_device_id'))
    op.drop_table('schedule_devices')
