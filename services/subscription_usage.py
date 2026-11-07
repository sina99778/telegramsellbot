"""Present one renewal period without resetting cumulative panel accounting."""

from dataclasses import dataclass

from models.subscription import Subscription


@dataclass(frozen=True, slots=True)
class DisplayUsage:
    total_bytes: int
    used_bytes: int
    remaining_bytes: int


def display_usage(subscription: Subscription) -> DisplayUsage:
    total = int(subscription.volume_bytes or 0)
    used = int(subscription.used_bytes or 0)
    baseline = getattr(subscription, "cycle_start_used_bytes", None)
    # Historical rows have no reliable period boundary; preserve their display.
    if isinstance(baseline, int) and not isinstance(baseline, bool):
        lifetime = int(subscription.lifetime_used_bytes or 0)
        used = max(lifetime + used - baseline, 0)
        if total > 0:
            total = max(total + lifetime - baseline, 1)
    return DisplayUsage(total, used, max(total - used, 0) if total > 0 else 0)
