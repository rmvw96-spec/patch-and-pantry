from fastapi import APIRouter

from app.api.routes import (
    grocery_analysis_runs,
    household,
    items,
    login,
    private,
    users,
    utils,
    vendors,
)
from app.core.config import settings

api_router = APIRouter()
api_router.include_router(login.router)
api_router.include_router(users.router)
api_router.include_router(utils.router)
api_router.include_router(items.router)
api_router.include_router(household.router)
api_router.include_router(vendors.router)
api_router.include_router(grocery_analysis_runs.router)


if settings.FASTAPI_ENV == "development":
    api_router.include_router(private.router)
