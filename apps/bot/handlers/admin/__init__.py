from aiogram import Router

from apps.bot.handlers.admin.broadcast import router as broadcast_router
from apps.bot.handlers.admin.plans import router as plans_router
from apps.bot.handlers.admin.ready_configs import router as ready_configs_router
from apps.bot.handlers.admin.retargeting import router as retargeting_router
from apps.bot.handlers.admin.servers import router as servers_router
from apps.bot.handlers.admin.support import router as support_router
from apps.bot.handlers.admin.stats import router as stats_router
from apps.bot.handlers.admin.subs import router as subs_router
from apps.bot.handlers.admin.users import router as users_router
from apps.bot.handlers.admin.settings import router as settings_router
from apps.bot.handlers.admin.discounts import router as discounts_router
from apps.bot.handlers.admin.gifts import router as gifts_router
from apps.bot.handlers.admin.recovery import router as recovery_router
from apps.bot.handlers.admin.manual_payments import router as manual_payments_router
from apps.bot.handlers.admin.customers import router as customers_router
from apps.bot.handlers.admin.config_search import router as config_search_router
from apps.bot.handlers.admin.legacy_import import router as legacy_import_router
from apps.bot.middlewares.menu_escape import MainMenuEscapeMiddleware


router = Router(name="admin")
# Same FSM-escape protection we added to the user router (see
# apps/bot/handlers/user/__init__.py and middlewares/menu_escape.py).
# Without it, an admin in a plan-edit flow who taps "⚙️ پنل مدیریت"
# has the button label silently consumed by the state-filtered handler
# as the new plan name — producing a confusing ProgrammingError on
# DB write.
router.message.middleware(MainMenuEscapeMiddleware())
router.include_router(servers_router)
router.include_router(plans_router)
router.include_router(ready_configs_router)
router.include_router(users_router)
router.include_router(subs_router)
router.include_router(broadcast_router)
router.include_router(retargeting_router)
router.include_router(support_router)
router.include_router(stats_router)
router.include_router(settings_router)
router.include_router(discounts_router)
router.include_router(gifts_router)
router.include_router(recovery_router)
router.include_router(manual_payments_router)
router.include_router(customers_router)
router.include_router(config_search_router)
router.include_router(legacy_import_router)

__all__ = ["router"]
