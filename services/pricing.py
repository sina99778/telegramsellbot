"""The same personal discount quote is used for display and payment."""
from decimal import Decimal

def discounted_price(price, percent):
    try:
        percent = max(0, min(100, int(percent or 0)))
    except (TypeError, ValueError):
        percent = 0
    return (Decimal(str(price)) * Decimal(100-percent) / Decimal(100)).quantize(Decimal("0.01"))

def purchase_price(plan, user):
    return discounted_price(plan.price, getattr(user, "personal_discount_percent", 0))
