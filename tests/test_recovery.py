from __future__ import annotations

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4, UUID
from datetime import datetime, timezone

from recover_sales_report import get_val, recover_row, parse_telegram_message
from models.order import Order
from models.payment import Payment
from models.subscription import Subscription

def test_get_val():
    row = {
        "Order ID": "123",
        "Date": "2026-06-20",
        "user_id": "456",
        "Amount Paid": "10.00"
    }
    assert get_val(row, ["Order ID"]) == "123"
    assert get_val(row, ["order_id"]) == "123"
    assert get_val(row, ["user_id"]) == "456"
    assert get_val(row, ["Amount Paid"]) == "10.00"
    assert get_val(row, ["amount_paid"]) == "10.00"
    assert get_val(row, ["nonexistent"]) is None


@pytest.mark.asyncio
async def test_recover_row_already_exists(mock_session):
    """If the order already exists, it should skip it and return False."""
    row = {
        "Order ID": str(uuid4()),
        "Date": "2026-06-20 12:00:00",
        "User ID": "12345",
        "Plan": "Test Plan",
        "Amount Paid": "10.00"
    }
    mock_session.get.return_value = MagicMock()  # Order exists
    
    res = await recover_row(mock_session, row)
    assert res is False
    mock_session.get.assert_called_once()


@pytest.mark.asyncio
async def test_recover_row_creates_new(mock_session):
    """If the order doesn't exist, it should recreate user, order, payment, and fallback subscription."""
    order_uuid = uuid4()
    row = {
        "Order ID": str(order_uuid),
        "Date": "2026-06-20 12:00:00",
        "User ID": "12345",
        "Name": "John Doe",
        "Username": "johndoe",
        "Plan": "Test Plan",
        "Config Name": "johndoe_vpn",
        "Amount Paid": "10.00",
        "Currency": "USD",
        "Payment Method": "Gateway"
    }
    
    # Mocks
    mock_session.get.return_value = None  # Order does not exist
    
    mock_user = MagicMock()
    mock_user.id = uuid4()
    
    mock_plan = MagicMock()
    mock_plan.id = uuid4()
    mock_plan.volume_bytes = 1000
    mock_plan.name = "Test Plan"
    
    mock_session.scalar = AsyncMock()
    # Mocking first scalar for UserRepository.get_by_telegram_id returning None,
    # then next scalar for Plan lookup
    mock_session.scalar.side_effect = [None, mock_plan, None]
    
    # Mock execute for scalars().all() in UserRepository and Plan fallback listing
    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_execute_result
    
    # Patch find_client_on_xui_servers to return no remote client
    with patch("recover_sales_report.find_client_on_xui_servers", return_value=(None, None, None)):
        res = await recover_row(mock_session, row)
        assert res is True
        
        # Verify order and payment were added
        added_objs = [call[0][0] for call in mock_session.add.call_args_list]
        
        # Check order addition
        order_adds = [o for o in added_objs if isinstance(o, Order)]
        assert len(order_adds) == 1
        assert order_adds[0].id == order_uuid
        assert order_adds[0].amount == Decimal("10.00")
        assert order_adds[0].source == "gateway"
        
        # Check payment addition
        payment_adds = [p for p in added_objs if isinstance(p, Payment)]
        assert len(payment_adds) == 1
        assert payment_adds[0].price_amount == Decimal("10.00")
        assert payment_adds[0].payment_status == "finished"
        assert payment_adds[0].callback_payload["config_name"] == "johndoe_vpn"
        
        # Check subscription addition (fallback)
        sub_adds = [s for s in added_objs if isinstance(s, Subscription)]
        assert len(sub_adds) == 1
        assert sub_adds[0].status == "pending_activation"


def test_parse_telegram_message_purchase():
    text = (
        "🛒 خرید جدید!\n\n"
        "👤 کاربر: Ali | @ali123 (ID: <code>987654321</code>)\n"
        "📦 پلن: 3 Months 50GB\n"
        "💰 مبلغ: 15.00 USD\n"
        "📛 کانفیگ: ali_vpn_config\n"
        "💳 روش: NowPayments"
    )
    parsed = parse_telegram_message(text)
    assert parsed is not None
    assert parsed["type"] == "purchase"
    assert parsed["telegram_id"] == 987654321
    assert parsed["name"] == "Ali"
    assert parsed["username"] == "ali123"
    assert parsed["plan_name"] == "3 Months 50GB"
    assert parsed["config_name"] == "ali_vpn_config"
    assert parsed["amount"] == Decimal("15.00")
    assert parsed["currency"] == "USD"
    assert parsed["payment_method"] == "NowPayments"


def test_parse_telegram_message_renewal():
    text = (
        "🔄 تمدید سرویس!\n\n"
        "👤 کاربر: Ali | @ali123 (ID: 987654321)\n"
        "📦 نوع: حجم\n"
        "📊 مقدار: 50\n"
        "💰 مبلغ: 10 USD"
    )
    parsed = parse_telegram_message(text)
    assert parsed is not None
    assert parsed["type"] == "renewal"
    assert parsed["telegram_id"] == 987654321
    assert parsed["renew_type"] == "volume"
    assert parsed["renew_amount"] == "50"
    assert parsed["price"] == Decimal("10")
    assert parsed["currency"] == "USD"

