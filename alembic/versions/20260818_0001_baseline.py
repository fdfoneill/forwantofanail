"""Fresh PostgreSQL multiplayer baseline."""
from alembic import op

from forwantofanail.core.database import Base
import forwantofanail.core.models  # noqa: F401

revision = "20260818_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
