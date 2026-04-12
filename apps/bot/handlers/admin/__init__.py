from aiogram import Router

from apps.bot.handlers.admin.broadcast import router as broadcast_router
from apps.bot.handlers.admin.plans import router as plans_router
from apps.bot.handlers.admin.servers import router as servers_router
from apps.bot.handlers.admin.support import router as support_router
from apps.bot.handlers.admin.stats import router as stats_router
from apps.bot.handlers.admin.subs import router as subs_router
from apps.bot.handlers.admin.users import router as users_router


router = Router(name="admin")
router.include_router(servers_router)
router.include_router(plans_router)
router.include_router(users_router)
router.include_router(subs_router)
router.include_router(broadcast_router)
router.include_router(support_router)
router.include_router(stats_router)

__all__ = ["router"]
