from typing import Any

from sqlmodel import Session, create_engine, select

from app import crud
from app.core.config import settings
from app.models import Region, User, UserCreate

engine = create_engine(str(settings.DATABASE_URL), pool_pre_ping=True)

# Regions are seeded by hand from docs/design.md ("Region: North Alabama"): NOAA station Muscle
# Shoals 2 N, 1991-2020 normals at a 30% frost risk. The frost dates gain a data snapshot when the
# NOAA normals are loaded.
NORTH_ALABAMA: dict[str, Any] = {
    "code": "north-al",
    "name": "North Alabama",
    "noaa_station": "Muscle Shoals 2 N",
    "ams_region": "Southeast",
    "frost_risk_pct": 30,
    "last_spring_frost": "04-03",
    "first_fall_frost": "10-30",
}


# make sure all SQLModel models are imported (app.models) before initializing DB
# otherwise, SQLModel might fail to initialize relationships properly
# for more details: https://github.com/fastapi/full-stack-fastapi-template/issues/28


def init_db(session: Session) -> None:
    # Tables should be created with Alembic migrations
    # But if you don't want to use migrations, create
    # the tables un-commenting the next lines
    # from sqlmodel import SQLModel

    # This works because the models are already imported and registered from app.models
    # SQLModel.metadata.create_all(engine)

    user = session.exec(
        select(User).where(User.email == settings.FIRST_SUPERUSER)
    ).first()
    if not user:
        user_in = UserCreate(
            email=settings.FIRST_SUPERUSER,
            password=settings.FIRST_SUPERUSER_PASSWORD,
            is_superuser=True,
        )
        user = crud.create_user(session=session, user_create=user_in)

    seed_regions(session)


def seed_regions(session: Session) -> None:
    """Insert seeded regions that are missing; never changes an existing row."""
    for fields in (NORTH_ALABAMA,):
        exists = session.exec(
            select(Region).where(Region.code == fields["code"])
        ).first()
        if exists is None:
            session.add(Region(**fields))
    session.commit()
