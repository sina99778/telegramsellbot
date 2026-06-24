#!/usr/bin/env python
"""
recover_sales_report.py - Recover missing data from sales reports (CSV, Telegram JSON export, or raw copied text).
Usage:
  python recover_sales_report.py <input_file> --format <csv|telegram-json|txt>
"""
import sys
import os
import csv
import re
import json
import argparse
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

# Ensure the app directories are in Python path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from core.database import AsyncSessionFactory
from models.user import User
from models.order import Order
from models.payment import Payment
from models.plan import Plan
from models.subscription import Subscription
from models.xui import XUIClientRecord, XUIInboundRecord, XUIServerRecord
from models.wallet import Wallet, WalletTransaction
from repositories.user import UserRepository
from services.xui.runtime import build_sub_link, create_xui_client_for_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("recover_sales_report")


def get_val(row: dict, keys: list[str]) -> str | None:
    for k in keys:
        if k in row:
            return row[k]
        for rk in row.keys():
            cleaned_rk = rk.lower().strip().replace(" ", "").replace("_", "")
            cleaned_k = k.lower().strip().replace(" ", "").replace("_", "")
            if cleaned_rk == cleaned_k:
                return row[rk]
    return None


def parse_telegram_message(text: str) -> dict | None:
    """Extract purchase or renewal details from a Telegram message text."""
    # Check message type
    if "خرید جدید" in text or "🛒" in text:
        msg_type = "purchase"
    elif "تمدید سرویس" in text or "🔄" in text:
        msg_type = "renewal"
    else:
        return None

    # Extract Telegram ID
    tg_id_match = re.search(r"ID:\s*(?:<code>)?(\d+)(?:<\/code>)?", text)
    if not tg_id_match:
        tg_id_match = re.search(r"\(ID:\s*(\d+)\)", text)
    tg_id = int(tg_id_match.group(1)) if tg_id_match else None

    if not tg_id:
        return None

    # Extract User Info
    name_match = re.search(r"کاربر:\s*([^\|]+)", text)
    name = name_match.group(1).strip() if name_match else None

    username_match = re.search(r"@(\w+)", text)
    username = username_match.group(1).strip() if username_match else None

    if msg_type == "purchase":
        plan_match = re.search(r"📦\s*پلن:\s*(.*)", text)
        plan_name = plan_match.group(1).strip() if plan_match else None

        config_match = re.search(r"📛\s*کانفیگ:\s*(.*)", text)
        config_name = config_match.group(1).strip() if config_match else None

        amount_match = re.search(r"💰\s*مبلغ:\s*([\d\.,]+)", text)
        amount = Decimal(amount_match.group(1).replace(",", "").strip()) if amount_match else Decimal("0")

        currency_match = re.search(r"💰\s*مبلغ:\s*[\d\.,]+\s*(\w+)", text)
        currency = currency_match.group(1).strip() if currency_match else "USD"

        method_match = re.search(r"💳\s*روش:\s*(.*)", text)
        payment_method = method_match.group(1).strip() if method_match else "gateway"

        return {
            "type": "purchase",
            "telegram_id": tg_id,
            "name": name,
            "username": username,
            "plan_name": plan_name,
            "config_name": config_name,
            "amount": amount,
            "currency": currency,
            "payment_method": payment_method
        }
    elif msg_type == "renewal":
        renew_type_match = re.search(r"📦\s*نوع:\s*(.*)", text)
        renew_type = "volume" if (renew_type_match and "حجم" in renew_type_match.group(1)) else "time"

        amount_match = re.search(r"📊\s*مقدار:\s*(.*)", text)
        renew_amount = amount_match.group(1).strip() if amount_match else "0"

        price_match = re.search(r"💰\s*مبلغ:\s*([\d\.,]+)", text)
        price = Decimal(price_match.group(1).replace(",", "").strip()) if price_match else Decimal("0")

        currency_match = re.search(r"💰\s*مبلغ:\s*[\d\.,]+\s*(\w+)", text)
        currency = currency_match.group(1).strip() if currency_match else "USD"

        return {
            "type": "renewal",
            "telegram_id": tg_id,
            "name": name,
            "username": username,
            "renew_type": renew_type,
            "renew_amount": renew_amount,
            "price": price,
            "currency": currency
        }
    return None


async def find_client_on_xui_servers(session, config_name: str, order_id: UUID):
    from sqlalchemy import select
    servers = (await session.execute(
        select(XUIServerRecord).where(XUIServerRecord.is_active == True, XUIServerRecord.health_status != "deleted")
    )).scalars().all()
    
    logger.info(f"Searching for client '{config_name}' (Order ID: {order_id}) across {len(servers)} active X-UI server(s)...")
    
    for server in servers:
        try:
            async with create_xui_client_for_server(server) as xui_client:
                inbounds = await xui_client.get_inbounds()
                for inbound in inbounds:
                    if not inbound.settings or not isinstance(inbound.settings, dict):
                        continue
                    clients = inbound.settings.get("clients", [])
                    for client in clients:
                        client_email = client.get("email", "")
                        client_comment = client.get("comment", "")
                        
                        match = False
                        if f"order:{order_id}" in client_comment:
                            match = True
                        elif client_email == config_name or client_email.startswith(f"{config_name}_"):
                            match = True
                            
                        if match:
                            inbound_rec = await session.scalar(
                                select(XUIInboundRecord).where(
                                    XUIInboundRecord.server_id == server.id,
                                    XUIInboundRecord.xui_inbound_remote_id == inbound.id
                                )
                            )
                            if inbound_rec:
                                logger.info(f"Found client '{client_email}' on server '{server.name}' inbound {inbound.id}!")
                                return server, inbound_rec, client
        except Exception as exc:
            logger.warning(f"Error querying server '{server.name}': {exc}")
            
    return None, None, None


async def recover_telegram_purchase(session, msg_data: dict, order_date: datetime) -> bool:
    from sqlalchemy import select
    from sqlalchemy import func

    config_name = msg_data["config_name"]
    tg_id = msg_data["telegram_id"]
    plan_name = msg_data["plan_name"]
    amount = msg_data["amount"]
    currency = msg_data["currency"]
    payment_method = msg_data["payment_method"]

    # Prevent duplicate recovery by checking XUIClientRecord username
    existing_record = await session.scalar(
        select(XUIClientRecord).where(XUIClientRecord.username == config_name)
    )
    if existing_record:
        logger.info(f"Subscription for config '{config_name}' already exists in DB. Skipping.")
        return False

    order_id = uuid4()
    logger.info(f"Recovering Telegram Purchase (User: {tg_id}, Plan: {plan_name}, Config: {config_name})...")

    # 1. Get or create User
    user_repo = UserRepository(session)
    user, created_user = await user_repo.get_or_create_user(
        telegram_id=tg_id,
        username=msg_data["username"],
        first_name=msg_data["name"] or f"User {tg_id}",
    )
    if created_user:
        logger.info(f"  -> Created missing User {tg_id}")

    # 2. Find Plan
    plan = await session.scalar(
        select(Plan).where(func.lower(Plan.name) == plan_name.lower())
    )
    if not plan:
        plans = (await session.execute(select(Plan))).scalars().all()
        for p in plans:
            if plan_name.lower() in p.name.lower() or p.name.lower() in plan_name.lower():
                plan = p
                logger.info(f"  -> Matched plan '{plan_name}' to similar plan '{p.name}'")
                break
    if not plan:
        plan = await session.scalar(select(Plan).where(Plan.is_active == True).limit(1))
        if plan:
            logger.warning(f"  -> Plan '{plan_name}' not found. Falling back to '{plan.name}'")
        else:
            logger.error(f"  -> Cannot recover order: no active Plan found in database.")
            return False

    # 3. Create Order
    source = "gateway" if "gateway" in payment_method.lower() or "درگاه" in payment_method.lower() else "wallet"
    order = Order(
        id=order_id,
        user_id=user.id,
        plan_id=plan.id,
        status="provisioned",
        source=source,
        amount=amount,
        currency=currency,
        created_at=order_date,
        updated_at=order_date
    )
    session.add(order)
    await session.flush()

    # 4. Create Payment
    payment = Payment(
        id=uuid4(),
        user_id=user.id,
        provider=payment_method,
        kind="direct_purchase",
        order_id=str(order.id),
        payment_status="finished",
        price_currency=currency,
        price_amount=amount,
        actually_paid=amount,
        callback_payload={"provisioned": True, "plan_id": str(plan.id), "config_name": config_name},
        created_at=order_date,
        updated_at=order_date
    )
    session.add(payment)
    await session.flush()

    # 5. Deduct from Wallet if applicable
    if source == "wallet":
        wallet = await session.scalar(select(Wallet).where(Wallet.user_id == user.id))
        if not wallet:
            wallet = Wallet(user_id=user.id, balance=Decimal("0.00"), credit_limit=Decimal("0.00"), hold_balance=Decimal("0.00"))
            session.add(wallet)
            await session.flush()

        txn = await session.scalar(
            select(WalletTransaction).where(
                WalletTransaction.user_id == user.id,
                WalletTransaction.reference_id == order.id
            )
        )
        if not txn:
            txn = WalletTransaction(
                id=uuid4(),
                user_id=user.id,
                amount=amount,
                direction="debit",
                type="purchase",
                currency=currency,
                reference_type="order",
                reference_id=order.id,
                description=f"Purchase of plan: {plan.name}",
                created_at=order_date,
                updated_at=order_date
            )
            session.add(txn)
            wallet.balance -= amount
            logger.info(f"  -> Deducted {amount} from user wallet. New balance: {wallet.balance}")

    # 6. Recreate Subscription & XUIClientRecord
    server, inbound, client = await find_client_on_xui_servers(session, config_name, order_id)
    if server and inbound and client:
        client_uuid = client.get("id") or client.get("uuid")
        sub_id = client.get("subId")
        total_bytes = client.get("totalGB") or 0
        expiry_time = client.get("expiryTime") or 0
        client_email = client.get("email")

        ends_at = None
        status = "pending_activation"
        if expiry_time > 0:
            ends_at = datetime.fromtimestamp(expiry_time / 1000, tz=timezone.utc)
            if ends_at <= datetime.now(timezone.utc):
                status = "expired"
            else:
                status = "active"

        sub_link = build_sub_link(server, sub_id)

        subscription = Subscription(
            user_id=user.id,
            order_id=order.id,
            plan_id=plan.id,
            status=status,
            activation_mode="first_use",
            starts_at=order_date if status == "active" else None,
            ends_at=ends_at,
            activated_at=order_date if status == "active" else None,
            volume_bytes=total_bytes,
            used_bytes=0,
            sub_link=sub_link,
            created_at=order_date,
            updated_at=order_date
        )
        session.add(subscription)
        await session.flush()

        xui_record = XUIClientRecord(
            subscription_id=subscription.id,
            inbound_id=inbound.id,
            xui_client_remote_id=client_uuid,
            email=client_email,
            client_uuid=client_uuid,
            username=config_name,
            sub_link=sub_link,
            usage_bytes=0,
            is_active=True if status != "expired" else False,
            created_at=order_date,
            updated_at=order_date
        )
        session.add(xui_record)
        logger.info(f"  -> Linked subscription {subscription.id} for client UUID {client_uuid}")
    else:
        logger.warning(f"  -> Client {config_name} NOT found on any active X-UI panel. Creating local fallback subscription.")
        fallback_uuid = str(uuid4())
        fallback_sub_id = fallback_uuid[:8]
        sub_link = f"http://fallback.sub/{fallback_sub_id}"

        subscription = Subscription(
            user_id=user.id,
            order_id=order.id,
            plan_id=plan.id,
            status="pending_activation",
            activation_mode="first_use",
            starts_at=None,
            ends_at=None,
            activated_at=None,
            volume_bytes=plan.volume_bytes,
            used_bytes=0,
            sub_link=sub_link,
            created_at=order_date,
            updated_at=order_date
        )
        session.add(subscription)
        await session.flush()

        inbound = await session.scalar(
            select(XUIInboundRecord).where(XUIInboundRecord.is_active == True).limit(1)
        )
        if inbound:
            xui_record = XUIClientRecord(
                subscription_id=subscription.id,
                inbound_id=inbound.id,
                xui_client_remote_id=fallback_uuid,
                email=f"{config_name}_fallback",
                client_uuid=fallback_uuid,
                username=config_name,
                sub_link=sub_link,
                usage_bytes=0,
                is_active=True,
                created_at=order_date,
                updated_at=order_date
            )
            session.add(xui_record)
            logger.info(f"  -> Created local fallback subscription {subscription.id}")

    return True


async def recover_telegram_renewal(session, msg_data: dict, order_date: datetime) -> bool:
    from sqlalchemy import select

    tg_id = msg_data["telegram_id"]
    renew_type = msg_data["renew_type"]
    renew_amount = msg_data["renew_amount"]
    price = msg_data["price"]
    currency = msg_data["currency"]

    user_repo = UserRepository(session)
    user = await user_repo.get_by_telegram_id(tg_id)
    if not user:
        logger.warning(f"User {tg_id} not found for renewal. Cannot process.")
        return False

    # Find the active/recent subscription for the user
    subscription = await session.scalar(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    if not subscription:
        logger.warning(f"No subscription found for user {tg_id}. Cannot process renewal.")
        return False

    # Check if a renewal order already exists within a 5-minute window of order_date
    existing_order = await session.scalar(
        select(Order).where(
            Order.user_id == user.id,
            Order.plan_id == subscription.plan_id,
            Order.amount == price,
            Order.created_at >= order_date - timedelta(minutes=5),
            Order.created_at <= order_date + timedelta(minutes=5)
        )
    )
    if existing_order:
        logger.info(f"Renewal order for user {tg_id} around {order_date} already exists. Skipping.")
        return False

    logger.info(f"Recovering Telegram Renewal (User: {tg_id}, Sub: {subscription.id}, Price: {price})...")

    # Create renewal Order
    order = Order(
        id=uuid4(),
        user_id=user.id,
        plan_id=subscription.plan_id,
        amount=price,
        currency=currency,
        status="completed",
        source="bot",
        created_at=order_date,
        updated_at=order_date
    )
    session.add(order)
    await session.flush()

    # Create Wallet Transaction and deduct balance if price > 0
    if price > 0:
        wallet = await session.scalar(select(Wallet).where(Wallet.user_id == user.id))
        if not wallet:
            wallet = Wallet(user_id=user.id, balance=Decimal("0.00"), credit_limit=Decimal("0.00"), hold_balance=Decimal("0.00"))
            session.add(wallet)
            await session.flush()

        txn = WalletTransaction(
            id=uuid4(),
            user_id=user.id,
            amount=price,
            direction="debit",
            type="renewal",
            currency=currency,
            reference_type="order",
            reference_id=order.id,
            description=f"Renewal of subscription {subscription.id}",
            created_at=order_date,
            updated_at=order_date
        )
        session.add(txn)
        wallet.balance -= price
        logger.info(f"  -> Deducted renewal cost {price} from user wallet. New balance: {wallet.balance}")

    return True


async def recover_row(session, row: dict) -> bool:
    """Recover a single CSV row."""
    from sqlalchemy import select
    from sqlalchemy import func
    
    order_id_str = get_val(row, ["Order ID", "order_id", "OrderId"])
    date_str = get_val(row, ["Date", "date", "created_at"])
    user_id_str = get_val(row, ["User ID", "user_id", "UserId", "Telegram ID", "telegram_id"])
    first_name = get_val(row, ["Name", "first_name", "FirstName"])
    username = get_val(row, ["Username", "username", "UserName"])
    plan_name = get_val(row, ["Plan", "plan", "PlanName"])
    config_name = get_val(row, ["Config Name", "config_name", "ConfigName"])
    amount_str = get_val(row, ["Amount Paid", "amount_paid", "amount", "Amount"])
    currency = get_val(row, ["Currency", "currency"]) or "USD"
    payment_method = get_val(row, ["Payment Method", "payment_method"]) or "gateway"
    
    if not order_id_str or not user_id_str or not date_str:
        logger.warning(f"Skipping incomplete CSV row: {row}")
        return False
        
    try:
        order_id = UUID(order_id_str)
        tg_id = int(user_id_str)
        amount = Decimal(amount_str or "0")
    except ValueError as e:
        logger.warning(f"Failed to parse row UUID/numeric data: {e} | Row: {row}")
        return False
        
    order_date = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            order_date = datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
            
    if not order_date:
        logger.warning(f"Failed to parse date '{date_str}'. Defaulting to current time.")
        order_date = datetime.now(timezone.utc)
        
    order = await session.get(Order, order_id)
    if order:
        logger.info(f"Order {order_id} already exists in DB. Skipping.")
        return False
        
    logger.info(f"Recovering Order {order_id} (User: {tg_id}, Plan: {plan_name}, Amount: {amount} {currency})...")
    
    user_repo = UserRepository(session)
    user, created_user = await user_repo.get_or_create_user(
        telegram_id=tg_id,
        username=username.lstrip("@") if username else None,
        first_name=first_name or f"User {tg_id}",
    )
    if created_user:
        logger.info(f"  -> Created missing User {tg_id}")
        
    plan = await session.scalar(
        select(Plan).where(func.lower(Plan.name) == plan_name.lower())
    )
    if not plan:
        plans = (await session.execute(select(Plan))).scalars().all()
        for p in plans:
            if plan_name.lower() in p.name.lower() or p.name.lower() in plan_name.lower():
                plan = p
                logger.info(f"  -> Plan '{plan_name}' not found exactly. Matched with similar plan '{p.name}'")
                break
                
    if not plan:
        plan = await session.scalar(select(Plan).where(Plan.is_active == True).limit(1))
        if plan:
            logger.warning(f"  -> Plan '{plan_name}' not found. Falling back to plan '{plan.name}'")
        else:
            logger.error(f"  -> Cannot recover order: no Plan found in database.")
            return False
            
    source = "gateway" if "gateway" in payment_method.lower() else "wallet"
    order = Order(
        id=order_id,
        user_id=user.id,
        plan_id=plan.id,
        status="provisioned",
        source=source,
        amount=amount,
        currency=currency,
        created_at=order_date,
        updated_at=order_date
    )
    session.add(order)
    await session.flush()
    
    payment = Payment(
        id=uuid4(),
        user_id=user.id,
        provider=payment_method,
        kind="direct_purchase",
        order_id=str(order.id),
        payment_status="finished",
        price_currency=currency,
        price_amount=amount,
        actually_paid=amount,
        callback_payload={"provisioned": True, "plan_id": str(plan.id), "config_name": config_name or ""},
        created_at=order_date,
        updated_at=order_date
    )
    session.add(payment)
    await session.flush()
    
    if source == "wallet":
        wallet = await session.scalar(select(Wallet).where(Wallet.user_id == user.id))
        if not wallet:
            wallet = Wallet(user_id=user.id, balance=Decimal("0.00"), credit_limit=Decimal("0.00"), hold_balance=Decimal("0.00"))
            session.add(wallet)
            await session.flush()
            
        txn = await session.scalar(
            select(WalletTransaction).where(
                WalletTransaction.user_id == user.id,
                WalletTransaction.reference_id == order.id
            )
        )
        if not txn:
            txn = WalletTransaction(
                id=uuid4(),
                user_id=user.id,
                amount=amount,
                direction="debit",
                type="purchase",
                currency=currency,
                reference_type="order",
                reference_id=order.id,
                description=f"Purchase of plan: {plan.name}",
                created_at=order_date,
                updated_at=order_date
            )
            session.add(txn)
            wallet.balance -= amount
            logger.info(f"  -> Deducted {amount} from user's wallet. New balance: {wallet.balance}")
            
    server, inbound, client = await find_client_on_xui_servers(session, config_name, order_id)
    
    if server and inbound and client:
        client_uuid = client.get("id") or client.get("uuid")
        sub_id = client.get("subId")
        total_bytes = client.get("totalGB") or 0
        expiry_time = client.get("expiryTime") or 0
        client_email = client.get("email")
        
        ends_at = None
        status = "pending_activation"
        if expiry_time > 0:
            ends_at = datetime.fromtimestamp(expiry_time / 1000, tz=timezone.utc)
            if ends_at <= datetime.now(timezone.utc):
                status = "expired"
            else:
                status = "active"
                
        sub_link = build_sub_link(server, sub_id)
        
        subscription = Subscription(
            user_id=user.id,
            order_id=order.id,
            plan_id=plan.id,
            status=status,
            activation_mode="first_use",
            starts_at=order_date if status == "active" else None,
            ends_at=ends_at,
            activated_at=order_date if status == "active" else None,
            volume_bytes=total_bytes,
            used_bytes=0,
            sub_link=sub_link,
            created_at=order_date,
            updated_at=order_date
        )
        session.add(subscription)
        await session.flush()
        
        xui_record = XUIClientRecord(
            subscription_id=subscription.id,
            inbound_id=inbound.id,
            xui_client_remote_id=client_uuid,
            email=client_email,
            client_uuid=client_uuid,
            username=config_name,
            sub_link=sub_link,
            usage_bytes=0,
            is_active=True if status != "expired" else False,
            created_at=order_date,
            updated_at=order_date
        )
        session.add(xui_record)
        logger.info(f"  -> Successfully recreated remote-linked subscription {subscription.id} for client UUID {client_uuid}")
    else:
        logger.warning(f"  -> WARNING: Client {config_name} was NOT found on any active X-UI server. Creating fallback local subscription.")
        fallback_uuid = str(uuid4())
        fallback_sub_id = fallback_uuid[:8]
        sub_link = f"http://fallback.sub/{fallback_sub_id}"
        
        subscription = Subscription(
            user_id=user.id,
            order_id=order.id,
            plan_id=plan.id,
            status="pending_activation",
            activation_mode="first_use",
            starts_at=None,
            ends_at=None,
            activated_at=None,
            volume_bytes=plan.volume_bytes,
            used_bytes=0,
            sub_link=sub_link,
            created_at=order_date,
            updated_at=order_date
        )
        session.add(subscription)
        await session.flush()
        
        inbound = await session.scalar(
            select(XUIInboundRecord).where(XUIInboundRecord.is_active == True).limit(1)
        )
        if inbound:
            xui_record = XUIClientRecord(
                subscription_id=subscription.id,
                inbound_id=inbound.id,
                xui_client_remote_id=fallback_uuid,
                email=f"{config_name}_fallback",
                client_uuid=fallback_uuid,
                username=config_name,
                sub_link=sub_link,
                usage_bytes=0,
                is_active=True,
                created_at=order_date,
                updated_at=order_date
            )
            session.add(xui_record)
            logger.info(f"  -> Created local-only subscription {subscription.id}")
            
    return True


def get_plain_text(text_field) -> str:
    """Helper to join Telegram exported JSON text array/structure into a flat string."""
    if isinstance(text_field, str):
        return text_field
    if isinstance(text_field, list):
        out = []
        for part in text_field:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict) and "text" in part:
                out.append(part["text"])
        return "".join(out)
    return ""


def parse_telegram_json(filepath: str) -> list[tuple[dict, datetime]]:
    """Parse Telegram exported JSON chat history file."""
    logger.info(f"Parsing Telegram exported JSON file: {filepath}")
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    messages = data.get("messages", [])
    results = []

    for msg in messages:
        if msg.get("type") != "message":
            continue
        
        raw_text = get_plain_text(msg.get("text", ""))
        if not raw_text:
            continue

        msg_data = parse_telegram_message(raw_text)
        if msg_data:
            # Parse Telegram JSON ISO date: 2026-06-20T12:00:00
            date_str = msg.get("date")
            try:
                date_val = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
            except Exception:
                date_val = datetime.now(timezone.utc)
            results.append((msg_data, date_val))

    return results


def parse_telegram_txt(filepath: str) -> list[tuple[dict, datetime]]:
    """Parse a plain text file with copied Telegram messages."""
    logger.info(f"Parsing plain text copy-paste file: {filepath}")
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Split using re.split on purchase/renewal headers
    pattern = r"(\n|^)(🛒\s*خرید جدید!|🔄\s*تمدید سرویس!)"
    parts = re.split(pattern, content)
    
    raw_msgs = []
    # If split matched, we reconstruct the parts
    # parts[0] is junk header before first pattern
    i = 1
    while i < len(parts):
        header = parts[i+1] # re.split grouping returns (\n|^) in parts[i] and (emoji...) in parts[i+1]
        body = parts[i+2] if i+2 < len(parts) else ""
        raw_msgs.append(header + body)
        i += 3

    results = []
    for text in raw_msgs:
        msg_data = parse_telegram_message(text)
        if msg_data:
            # Look for copied date metadata in the message block
            # For example, Telegram Desktop copy-paste format: [6/20/26, 12:00:00 PM]
            date_val = None
            date_match = re.search(r"\[(\d{1,2})[-/](\d{1,2})[-/](\d{2,4}),?\s+(\d{1,2}):(\d{2}):(\d{2})\s*(AM|PM)?\]", text)
            if date_match:
                try:
                    month, day, year, hour, minute, second = (
                        int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)),
                        int(date_match.group(4)), int(date_match.group(5)), int(date_match.group(6))
                    )
                    ampm = date_match.group(7)
                    if ampm == "PM" and hour < 12:
                        hour += 12
                    elif ampm == "AM" and hour == 12:
                        hour = 0
                    if year < 100:
                        year += 2000
                    date_val = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
                except Exception:
                    pass

            if not date_val:
                # Fallback to general YYYY-MM-DD search
                dt_match = re.search(r"(\d{4})[-/](\d{2})[-/](\d{2})\s+(\d{2}):(\d{2})", text)
                if dt_match:
                    try:
                        date_val = datetime(
                            int(dt_match.group(1)), int(dt_match.group(2)), int(dt_match.group(3)),
                            int(dt_match.group(4)), int(dt_match.group(5)), 0, tzinfo=timezone.utc
                        )
                    except Exception:
                        pass

            if not date_val:
                date_val = datetime.now(timezone.utc)

            results.append((msg_data, date_val))

    return results


async def process_telegram_recovery(session, parsed_messages: list[tuple[dict, datetime]]) -> int:
    success_count = 0
    for msg_data, date_val in parsed_messages:
        try:
            if msg_data["type"] == "purchase":
                success = await recover_telegram_purchase(session, msg_data, date_val)
            elif msg_data["type"] == "renewal":
                success = await recover_telegram_renewal(session, msg_data, date_val)
            else:
                success = False

            if success:
                success_count += 1
        except Exception as e:
            logger.error(f"Error processing recovery: {e} | Data: {msg_data}", exc_info=True)
    return success_count


async def main():
    parser = argparse.ArgumentParser(description="Recover database records from sales report CSV, Telegram JSON, or copied text.")
    parser.add_argument("input_file", help="Path to CSV, Telegram JSON, or text report file")
    parser.add_argument("--format", choices=["csv", "telegram-json", "txt"], default="csv",
                        help="Format of the input file (default: csv)")
    args = parser.parse_args()

    if not os.path.exists(args.input_file):
        logger.error(f"Input file not found: {args.input_file}")
        sys.exit(1)

    logger.info(f"Starting recovery using file: {args.input_file} (format: {args.format})")

    async with AsyncSessionFactory() as session:
        try:
            if args.format == "csv":
                with open(args.input_file, mode="r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    success_count = 0
                    total_rows = 0
                    for row in reader:
                        total_rows += 1
                        try:
                            recovered = await recover_row(session, row)
                            if recovered:
                                success_count += 1
                        except Exception as e:
                            logger.error(f"Error recovering row: {e} | Row: {row}", exc_info=True)
                    logger.info(f"Recovery completed! Recovered {success_count} / {total_rows} orders.")
            else:
                if args.format == "telegram-json":
                    parsed_messages = parse_telegram_json(args.input_file)
                else:  # txt
                    parsed_messages = parse_telegram_txt(args.input_file)

                logger.info(f"Parsed {len(parsed_messages)} message(s) from report file.")
                success_count = await process_telegram_recovery(session, parsed_messages)
                logger.info(f"Recovery completed! Recovered {success_count} / {len(parsed_messages)} orders/renewals.")

            await session.commit()
        except Exception as exc:
            await session.rollback()
            logger.error(f"Transaction rolled back due to error: {exc}", exc_info=True)
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
