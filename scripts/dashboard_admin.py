"""
CLI utility — create / update / list dashboard admin credentials.

Used by install.sh menu option "Setup dashboard admin" so the operator
doesn't have to hand-craft a SQL INSERT.

Modes
-----
  python scripts/dashboard_admin.py create   [--username U] [--password P]
  python scripts/dashboard_admin.py set-password [--username U] [--password P]
  python scripts/dashboard_admin.py list
  python scripts/dashboard_admin.py disable  [--username U]

When --password is omitted in `create` / `set-password`, the script
either reads from the env var DASHBOARD_ADMIN_PASSWORD or generates a
strong random one and prints it back to stdout.

Always reads --username from --username flag → env DASHBOARD_ADMIN_USERNAME
→ stdin prompt, in that order.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys

from sqlalchemy import func, select

from core.dashboard_auth import (
    generate_strong_password,
    hash_password,
)
from core.database import AsyncSessionFactory
from models.dashboard_admin import DashboardAdmin


logger = logging.getLogger("dashboard_admin")


def _resolve(value: str | None, env_var: str, prompt: str, hidden: bool = False) -> str:
    if value:
        return value
    env_val = os.environ.get(env_var)
    if env_val:
        return env_val
    if hidden:
        return getpass.getpass(prompt)
    return input(prompt).strip()


async def _cmd_create(args: argparse.Namespace) -> int:
    username = _resolve(args.username, "DASHBOARD_ADMIN_USERNAME", "نام کاربری: ").strip().lower()
    if not username:
        logger.error("نام کاربری خالی نمی‌تواند باشد.")
        return 2

    password = args.password or os.environ.get("DASHBOARD_ADMIN_PASSWORD")
    autogen = False
    if not password:
        if args.auto_password:
            password = generate_strong_password()
            autogen = True
        else:
            password = getpass.getpass("رمز عبور: ")

    try:
        async with AsyncSessionFactory() as session:
            existing = await session.scalar(
                select(DashboardAdmin).where(
                    func.lower(DashboardAdmin.username) == username
                )
            )
            if existing:
                logger.error("ادمین با نام کاربری «%s» قبلاً وجود دارد. "
                             "از `set-password` برای تغییر رمز استفاده کن.", username)
                return 3
            admin = DashboardAdmin(
                username=username,
                password_hash=hash_password(password),
                display_name=args.display_name,
                is_active=True,
            )
            session.add(admin)
            await session.commit()
    except ValueError as exc:
        logger.error("رمز عبور قابل قبول نیست: %s", exc)
        return 2
    except Exception as exc:
        logger.error("خطا در ساخت ادمین: %s", exc, exc_info=True)
        return 1

    logger.info("✅ ادمین داشبورد ساخته شد:")
    logger.info("    username:     %s", username)
    if autogen:
        logger.info("    password:     %s   ← این رو جای امن نگه دار!", password)
    else:
        logger.info("    password:     (مخفی)")
    logger.info("")
    logger.info("حالا برو به https://<your-domain>/dashboard/ و وارد شو.")
    return 0


async def _cmd_set_password(args: argparse.Namespace) -> int:
    username = _resolve(args.username, "DASHBOARD_ADMIN_USERNAME", "نام کاربری: ").strip().lower()
    password = args.password or os.environ.get("DASHBOARD_ADMIN_PASSWORD")
    autogen = False
    if not password:
        if args.auto_password:
            password = generate_strong_password()
            autogen = True
        else:
            password = getpass.getpass("رمز عبور جدید: ")
    try:
        async with AsyncSessionFactory() as session:
            admin = await session.scalar(
                select(DashboardAdmin).where(
                    func.lower(DashboardAdmin.username) == username
                )
            )
            if admin is None:
                logger.error("ادمین «%s» پیدا نشد. از `create` استفاده کن.", username)
                return 3
            admin.password_hash = hash_password(password)
            admin.is_active = True
            await session.commit()
    except ValueError as exc:
        logger.error("رمز عبور قابل قبول نیست: %s", exc)
        return 2
    except Exception as exc:
        logger.error("خطا در به‌روزرسانی رمز: %s", exc, exc_info=True)
        return 1
    logger.info("✅ رمز عبور «%s» به‌روزرسانی شد.", username)
    if autogen:
        logger.info("    new password: %s   ← این رو جای امن نگه دار!", password)
    return 0


async def _cmd_list(_: argparse.Namespace) -> int:
    async with AsyncSessionFactory() as session:
        rows = (await session.execute(
            select(DashboardAdmin).order_by(DashboardAdmin.created_at.asc())
        )).scalars().all()
    if not rows:
        logger.info("(هیچ ادمین داشبوردی ثبت نشده. از `create` استفاده کن.)")
        return 0
    logger.info("%-24s  %-6s  %s", "username", "active", "last_login")
    for a in rows:
        last = a.last_login_at.isoformat(timespec="seconds") if a.last_login_at else "—"
        logger.info("%-24s  %-6s  %s", a.username, "yes" if a.is_active else "no ", last)
    return 0


async def _cmd_disable(args: argparse.Namespace) -> int:
    username = _resolve(args.username, "DASHBOARD_ADMIN_USERNAME", "نام کاربری برای غیرفعال‌سازی: ").strip().lower()
    async with AsyncSessionFactory() as session:
        admin = await session.scalar(
            select(DashboardAdmin).where(
                func.lower(DashboardAdmin.username) == username
            )
        )
        if admin is None:
            logger.error("ادمین «%s» پیدا نشد.", username)
            return 3
        admin.is_active = False
        await session.commit()
    logger.info("✅ ادمین «%s» غیرفعال شد. (داده‌ها حفظ شده‌اند برای audit.)", username)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Dashboard admin credential management.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="یک ادمین داشبورد جدید بساز.")
    p_create.add_argument("--username")
    p_create.add_argument("--password")
    p_create.add_argument("--display-name", default=None)
    p_create.add_argument("--auto-password", action="store_true",
                          help="یک رمز قوی به‌صورت خودکار تولید و چاپ کن.")

    p_set = sub.add_parser("set-password", help="رمز یک ادمین موجود را تغییر بده.")
    p_set.add_argument("--username")
    p_set.add_argument("--password")
    p_set.add_argument("--auto-password", action="store_true")

    sub.add_parser("list", help="فهرست ادمین‌های داشبورد.")

    p_disable = sub.add_parser("disable", help="یک ادمین را غیرفعال کن (داده نگه داشته می‌شود).")
    p_disable.add_argument("--username")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    runner = {
        "create": _cmd_create,
        "set-password": _cmd_set_password,
        "list": _cmd_list,
        "disable": _cmd_disable,
    }[args.cmd]
    return asyncio.run(runner(args))


if __name__ == "__main__":
    sys.exit(main())
