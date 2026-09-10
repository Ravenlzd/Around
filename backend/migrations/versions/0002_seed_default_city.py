"""seed the default signup city

Revision ID: 0002_seed_default_city
Revises: 0001_initial
Create Date: 2026-09-10
"""
from alembic import op


revision = "0002_seed_default_city"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO cities (id, name, country_code, center, timezone)
        SELECT
            '11111111-1111-1111-1111-111111111111',
            'Vilnius',
            'LT',
            ST_SetSRID(ST_MakePoint(25.2797, 54.6872), 4326),
            'Europe/Vilnius'
        WHERE NOT EXISTS (
            SELECT 1 FROM cities WHERE name = 'Vilnius' AND country_code = 'LT'
        )
    """)


def downgrade() -> None:
    pass
