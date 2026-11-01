"""ORM モデル。Alembic の autogenerate が Base.metadata を参照できるよう、ここで全モデルを import する。"""

from app.models.settings import SystemSetting

__all__ = ["SystemSetting"]
