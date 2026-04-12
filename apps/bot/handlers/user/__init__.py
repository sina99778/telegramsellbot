from aiogram import Router

from apps.bot.handlers.user.free_trial import router as free_trial_router
from apps.bot.handlers.user.purchase import router as purchase_router
from apps.bot.handlers.user.start import router as start_router
from apps.bot.handlers.user.support import router as support_router
from apps.bot.handlers.user.topup import router as topup_router
from apps.bot.middlewares.user import UserAccessMiddleware


router = Router(name="user")
router.message.middleware(UserAccessMiddleware())
router.callback_query.middleware(UserAccessMiddleware())
router.include_router(start_router)
router.include_router(topup_router)
router.include_router(purchase_router)
router.include_router(free_trial_router)
router.include_router(support_router)

__all__ = ["router"]
