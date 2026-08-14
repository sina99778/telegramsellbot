"""
Regression tests for worker-job findings #51, #52, #53 (+ the leftover
substring "client gone" classifier in the sync job).

#51 — card/crypto autoconfirm must NOT hold FOR UPDATE on every pending
      payment across Telegram sends / blockchain polls: candidates are
      listed lock-free, then each payment is locked, processed and
      COMMITTED in its own short transaction (Telegram I/O after commit).
#52 — reconciliation must not count a swallowed provisioning/renewal
      failure as "retry SUCCESS": only the provisioned/renewal_applied
      flag written by process_successful_payment is a success signal,
      otherwise retry_count grows so MAX_RETRY_COUNT stays reachable.
#53 — falsely-expired pending_activation subs (ends_at NULL) must stay
      inside the sync job's recovery filter and be restored to
      pending_activation (not active) so first-use activation still runs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from apps.worker.jobs.card_autoconfirm import run_card_autoconfirm
from apps.worker.jobs.crypto_autoconfirm import run_crypto_autoconfirm
from apps.worker.jobs.reconciliation import run_reconciliation
from apps.worker.jobs.subscriptions import (
    sync_all_subscription_states,
    sync_pasarguard_usage_and_status,
    sync_xui_usage_and_status,
)
from services.xui.client import XUIRequestError


def _session_cm(session):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _result_with_rows(rows):
    result = MagicMock()
    result.all.return_value = rows
    result.scalars.return_value.all.return_value = rows
    return result


def _scalars_result(objs):
    result = MagicMock()
    result.scalars.return_value.all.return_value = objs
    return result


# ════════════════════════════════════════════════════════════════════════
# 51 — card autoconfirm: lock/process/commit per payment
# ════════════════════════════════════════════════════════════════════════


def _card_cfg(exception_ids=()):
    return MagicMock(
        enabled=True,
        delay_minutes=30,
        exception_telegram_ids=list(exception_ids),
    )


def _card_repo(cfg):
    repo = MagicMock()
    repo.get_card_autoconfirm_settings = AsyncMock(return_value=cfg)
    return repo


def _card_payment(pid, telegram_id=555):
    payment = MagicMock()
    payment.id = pid
    payment.user_id = uuid4()
    payment.price_amount = Decimal("5.00")
    payment.callback_payload = {}
    user = MagicMock()
    user.telegram_id = telegram_id
    payment.user = user
    return payment


async def test_card_autoconfirm_lists_without_lock_then_locks_and_commits_before_send():
    pid = uuid4()
    payment = _card_payment(pid)

    events: list[str] = []
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_result_with_rows([(pid,)]))
    # 1st scalar = the per-row locked load; 2nd = the sales-notify user
    # re-fetch (None → notify skipped).
    session.scalar = AsyncMock(side_effect=[payment, None])
    session.commit = AsyncMock(side_effect=lambda: events.append("commit"))
    session.flush = AsyncMock()

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=lambda *a, **k: events.append("send"))

    process = AsyncMock(side_effect=lambda **k: events.append("process") or True)
    with patch("apps.worker.jobs.card_autoconfirm.AppSettingsRepository", return_value=_card_repo(_card_cfg())), \
         patch("apps.worker.jobs.card_autoconfirm.process_successful_payment", process):
        result = await run_card_autoconfirm(session, bot)

    assert result["confirmed"] == 1

    # The candidate listing must NOT take row locks (it used to FOR UPDATE
    # the whole batch and keep it across Telegram sends).
    list_sql = str(session.execute.await_args_list[0].args[0])
    assert "FOR UPDATE" not in list_sql

    # The per-payment load takes the lock AND re-checks pending_approval.
    locked_sql = str(session.scalar.await_args_list[0].args[0])
    assert "FOR UPDATE" in locked_sql
    assert "payments.payment_status =" in locked_sql

    # Telegram I/O strictly AFTER the row's commit released the lock.
    assert events == ["process", "commit", "send"]


async def test_card_autoconfirm_skips_payment_resolved_between_listing_and_locking():
    pid = uuid4()
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_result_with_rows([(pid,)]))
    session.scalar = AsyncMock(return_value=None)  # skip_locked / already resolved

    process = AsyncMock()
    with patch("apps.worker.jobs.card_autoconfirm.AppSettingsRepository", return_value=_card_repo(_card_cfg())), \
         patch("apps.worker.jobs.card_autoconfirm.process_successful_payment", process):
        result = await run_card_autoconfirm(session, None)

    process.assert_not_awaited()
    assert result["confirmed"] == 0


async def test_card_autoconfirm_releases_lock_on_exempt_user():
    pid = uuid4()
    payment = _card_payment(pid, telegram_id=999)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_result_with_rows([(pid,)]))
    session.scalar = AsyncMock(return_value=payment)

    process = AsyncMock()
    with patch("apps.worker.jobs.card_autoconfirm.AppSettingsRepository",
               return_value=_card_repo(_card_cfg(exception_ids=[999]))), \
         patch("apps.worker.jobs.card_autoconfirm.process_successful_payment", process):
        result = await run_card_autoconfirm(session, None)

    process.assert_not_awaited()
    assert result["skipped_exempt"] == 1
    # The exempt row's transaction is ended so its lock is not dragged
    # across the rest of the sweep.
    session.commit.assert_awaited()


async def test_card_autoconfirm_failure_rolls_back_and_continues():
    pid_a, pid_b = uuid4(), uuid4()
    payment_a = _card_payment(pid_a)
    payment_b = _card_payment(pid_b)

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_result_with_rows([(pid_a,), (pid_b,)]))
    session.scalar = AsyncMock(side_effect=[payment_a, payment_b, None])

    process = AsyncMock(side_effect=[RuntimeError("boom"), True])
    with patch("apps.worker.jobs.card_autoconfirm.AppSettingsRepository", return_value=_card_repo(_card_cfg())), \
         patch("apps.worker.jobs.card_autoconfirm.process_successful_payment", process):
        result = await run_card_autoconfirm(session, None)

    assert result["failed"] == 1
    assert result["confirmed"] == 1
    session.rollback.assert_awaited_once()


async def test_card_autoconfirm_reports_false_fulfillment_as_failed():
    pid = uuid4()
    payment = _card_payment(pid)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_result_with_rows([(pid,)]))
    session.scalar = AsyncMock(return_value=payment)

    with patch("apps.worker.jobs.card_autoconfirm.AppSettingsRepository", return_value=_card_repo(_card_cfg())), \
         patch("apps.worker.jobs.card_autoconfirm.process_successful_payment", AsyncMock(return_value=False)):
        result = await run_card_autoconfirm(session, None)

    assert result["confirmed"] == 0
    assert result["failed"] == 1
    session.commit.assert_awaited_once()


# ════════════════════════════════════════════════════════════════════════
# 51 — crypto autoconfirm: snapshot lock-free, lock only the matched row
# ════════════════════════════════════════════════════════════════════════


def _crypto_payment(created_at):
    payment = MagicMock()
    payment.id = uuid4()
    payment.user_id = uuid4()
    payment.provider = "manual_crypto"
    payment.payment_status = "waiting_hash"
    payment.pay_currency = "trx"
    payment.pay_amount = Decimal("5")
    payment.price_amount = Decimal("5.00")
    payment.created_at = created_at
    payment.callback_payload = {"address": "Taddr1"}
    return payment


async def test_crypto_autoconfirm_no_lock_across_poll_and_commit_before_sends():
    now = datetime.now(timezone.utc)
    payment = _crypto_payment(created_at=now - timedelta(hours=1))

    events: list[str] = []
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_scalars_result([payment]))
    session.commit = AsyncMock(side_effect=lambda: events.append("commit"))
    session.flush = AsyncMock()

    user = MagicMock()
    user.telegram_id = 777
    # 1st scalar = locked matched row; 2nd = DM user; 3rd = sales-notify
    # user (None → skipped).
    session.scalar = AsyncMock(side_effect=[payment, user, None])

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=lambda *a, **k: events.append("send"))

    txs = [{"hash": "deadbeef", "amount": "5", "timestamp": now}]

    async def _fetch(**kwargs):
        events.append("fetch")
        return txs

    process = AsyncMock(side_effect=lambda **k: events.append("process"))
    with patch("apps.worker.jobs.crypto_autoconfirm.fetch_incoming", AsyncMock(side_effect=_fetch)), \
         patch("apps.worker.jobs.crypto_autoconfirm.is_autoconfirmable", return_value=True), \
         patch("apps.worker.jobs.crypto_autoconfirm.amount_matches", return_value=True), \
         patch("apps.worker.jobs.crypto_autoconfirm.process_successful_payment", process):
        result = await run_crypto_autoconfirm(session, bot)

    assert result["confirmed"] == 1

    # Snapshot select takes no lock; the read txn is closed BEFORE the
    # blockchain HTTP poll; the matched row's commit lands BEFORE Telegram.
    snapshot_sql = str(session.execute.await_args_list[0].args[0])
    assert "FOR UPDATE" not in snapshot_sql
    assert events == ["commit", "fetch", "process", "commit", "send"]

    # Only the matched row was locked, with a status re-check.
    locked_sql = str(session.scalar.await_args_list[0].args[0])
    assert "FOR UPDATE" in locked_sql
    assert "payments.payment_status IN" in locked_sql


async def test_crypto_autoconfirm_skips_invoice_resolved_after_snapshot():
    now = datetime.now(timezone.utc)
    payment = _crypto_payment(created_at=now - timedelta(hours=1))

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_scalars_result([payment]))
    session.scalar = AsyncMock(return_value=None)  # admin resolved it meanwhile

    process = AsyncMock()
    with patch("apps.worker.jobs.crypto_autoconfirm.fetch_incoming",
               AsyncMock(return_value=[{"hash": "h1", "amount": "5", "timestamp": now}])), \
         patch("apps.worker.jobs.crypto_autoconfirm.is_autoconfirmable", return_value=True), \
         patch("apps.worker.jobs.crypto_autoconfirm.amount_matches", return_value=True), \
         patch("apps.worker.jobs.crypto_autoconfirm.process_successful_payment", process):
        result = await run_crypto_autoconfirm(session, None)

    process.assert_not_awaited()
    assert result["confirmed"] == 0


async def test_crypto_autoconfirm_replay_guard_checks_fresh_locked_row():
    now = datetime.now(timezone.utc)
    payment = _crypto_payment(created_at=now - timedelta(hours=1))

    # The snapshot has NOT seen the hash, but the FRESH locked row has it
    # (e.g. another path processed it between snapshot and lock).
    fresh = _crypto_payment(created_at=now - timedelta(hours=1))
    fresh.id = payment.id
    fresh.callback_payload = {
        "address": "Taddr1",
        "autoconfirm_processed_hashes": ["h1"],
    }

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_scalars_result([payment]))
    session.scalar = AsyncMock(return_value=fresh)

    process = AsyncMock()
    with patch("apps.worker.jobs.crypto_autoconfirm.fetch_incoming",
               AsyncMock(return_value=[{"hash": "h1", "amount": "5", "timestamp": now}])), \
         patch("apps.worker.jobs.crypto_autoconfirm.is_autoconfirmable", return_value=True), \
         patch("apps.worker.jobs.crypto_autoconfirm.amount_matches", return_value=True), \
         patch("apps.worker.jobs.crypto_autoconfirm.process_successful_payment", process):
        result = await run_crypto_autoconfirm(session, None)

    process.assert_not_awaited()
    assert result["confirmed"] == 0


# ════════════════════════════════════════════════════════════════════════
# 52 — reconciliation success signal
# ════════════════════════════════════════════════════════════════════════


def _recon_payment(kind="direct_purchase", payload=None):
    payment = MagicMock()
    payment.id = uuid4()
    payment.kind = kind
    payment.payment_status = "finished"
    payment.price_amount = Decimal("9.00")
    payment.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
    payment.callback_payload = dict(payload or {})
    return payment


def _recon_factory(purchases, renewals):
    """Build a fake AsyncSessionFactory matching run_reconciliation's per-row
    session sequence: housekeeping → read-purchase-ids → one session per
    purchase → read-renewal-ids → one session per renewal.
    """
    sessions = []

    # 1. housekeeping (bulk expire) — needs .execute(...).rowcount
    hk = AsyncMock()
    hk_res = MagicMock()
    hk_res.rowcount = 0
    hk.execute = AsyncMock(return_value=hk_res)
    sessions.append(hk)

    # 2. read purchase candidate IDs (no lock)
    rp = AsyncMock()
    rp.execute = AsyncMock(return_value=_scalars_result([p.id for p in purchases]))
    sessions.append(rp)

    # 3. one locked session per purchase row
    for p in purchases:
        s = AsyncMock()
        s.scalar = AsyncMock(return_value=p)
        sessions.append(s)

    # 4. read renewal candidate IDs (no lock)
    rr = AsyncMock()
    rr.execute = AsyncMock(return_value=_scalars_result([p.id for p in renewals]))
    sessions.append(rr)

    # 5. one locked session per renewal row
    for p in renewals:
        s = AsyncMock()
        s.scalar = AsyncMock(return_value=p)
        sessions.append(s)

    it = iter(sessions)

    def factory():
        session = next(it)
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    return factory


async def test_swallowed_purchase_failure_increments_retry_count_not_success():
    payment = _recon_payment()
    factory = _recon_factory([payment], [])
    bot = MagicMock()
    bot.send_message = AsyncMock()

    # process_successful_payment returns WITHOUT raising and WITHOUT setting
    # the `provisioned` flag — the exact swallowed-failure shape.
    with patch("apps.worker.jobs.reconciliation.process_successful_payment", AsyncMock()), \
         patch("apps.worker.jobs.reconciliation.AsyncSessionFactory", factory):
        await run_reconciliation(bot)

    assert payment.callback_payload["retry_count"] == 1
    assert "swallowed" in payment.callback_payload["last_error"]
    # No false "Retry موفق" owner alert: nothing succeeded, nothing escalated.
    bot.send_message.assert_not_awaited()


async def test_real_purchase_success_counted_and_no_retry_bookkeeping():
    payment = _recon_payment()
    factory = _recon_factory([payment], [])
    bot = MagicMock()
    bot.send_message = AsyncMock()

    async def _succeed(session, payment, amount_to_credit):
        payment.callback_payload = {**(payment.callback_payload or {}), "provisioned": True}

    with patch("apps.worker.jobs.reconciliation.process_successful_payment", new=_succeed), \
         patch("apps.worker.jobs.reconciliation.AsyncSessionFactory", factory), \
         patch("apps.worker.jobs.reconciliation.settings", MagicMock(owner_telegram_id=111)):
        await run_reconciliation(bot)

    assert "retry_count" not in payment.callback_payload
    bot.send_message.assert_awaited_once()
    assert "Retry موفق: 1" in bot.send_message.await_args.args[1]


async def test_swallowed_renewal_failure_increments_retry_count():
    payment = _recon_payment(kind="direct_renewal")
    factory = _recon_factory([], [payment])
    bot = MagicMock()
    bot.send_message = AsyncMock()

    with patch("apps.worker.jobs.reconciliation.process_successful_payment", AsyncMock()), \
         patch("apps.worker.jobs.reconciliation.AsyncSessionFactory", factory):
        await run_reconciliation(bot)

    assert payment.callback_payload["retry_count"] == 1
    bot.send_message.assert_not_awaited()


async def test_max_retry_escalation_is_reachable():
    # With retry_count finally growing on swallowed failures, the
    # MAX_RETRY_COUNT arm flips the payment to manual_review.
    payment = _recon_payment(payload={"retry_count": 10})
    factory = _recon_factory([payment], [])
    bot = MagicMock()
    bot.send_message = AsyncMock()

    process = AsyncMock()
    with patch("apps.worker.jobs.reconciliation.process_successful_payment", process), \
         patch("apps.worker.jobs.reconciliation.AsyncSessionFactory", factory), \
         patch("apps.worker.jobs.reconciliation.settings", MagicMock(owner_telegram_id=111)):
        await run_reconciliation(bot)

    process.assert_not_awaited()
    assert payment.payment_status == "manual_review"
    assert payment.callback_payload["escalated"] is True
    bot.send_message.assert_awaited_once()


# ════════════════════════════════════════════════════════════════════════
# 53 — never-activated falsely-expired subs: filter + recovery + classifier
# ════════════════════════════════════════════════════════════════════════


def _xui_sub(status="active", ends_at=None, activated_at=None,
             volume_bytes=10**9, used_bytes=0, strikes=0, last_usage_sync_at=None):
    sub = MagicMock()
    sub.id = uuid4()
    sub.status = status
    sub.ends_at = ends_at
    sub.activated_at = activated_at
    sub.expired_at = None
    sub.volume_bytes = volume_bytes
    sub.used_bytes = used_bytes
    sub.usage_sync_failures = strikes
    sub.last_usage_sync_at = last_usage_sync_at
    sub.plan = MagicMock(duration_days=30)
    record = MagicMock()
    record.email = "cfg-x404"
    record.panel_username = "cfg-x404"
    record.username = "cfg-x404"
    record.is_active = True
    record.inbound.xui_inbound_remote_id = 7
    record.xui_client_remote_id = "client-1"
    record.client_uuid = "uuid-1"
    sub.xui_client = record
    return sub


def _xui_inbound(*clients, inbound_id=7):
    """Fake /inbounds/list entry.

    Each client is an (email, client_id, up, down) tuple: existence lives in
    settings.clients, usage lives in clientStats — mirroring the real panel
    payload the sync now reads.
    """
    return MagicMock(
        id=inbound_id,
        settings={"clients": [
            {"id": client_id, "uuid": client_id, "email": email}
            for email, client_id, _up, _down in clients
        ]},
        client_stats=[
            {"email": email, "up": up, "down": down}
            for email, _client_id, up, down in clients
        ],
    )


def _security_settings():
    return MagicMock(xui_limit_ip=1, max_distinct_ips=0, auto_disable_ip_abuse=False)


async def test_sync_query_recovery_arm_includes_never_activated_expired_subs():
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_scalars_result([]))
    repo = MagicMock()
    repo.get_service_security_settings = AsyncMock(return_value=_security_settings())

    with patch("apps.worker.jobs.subscriptions.AsyncSessionFactory",
               return_value=_session_cm(session)), \
         patch("apps.worker.jobs.subscriptions.AppSettingsRepository", return_value=repo):
        await sync_all_subscription_states()

    sql = str(session.execute.await_args_list[0].args[0])
    # New arm: expired + never activated (ends_at NULL) + recent expired_at.
    assert "subscriptions.activated_at IS NULL" in sql
    assert "subscriptions.ends_at IS NULL" in sql
    assert "subscriptions.expired_at >" in sql
    # Existing falsely-expired arm is intact.
    assert "subscriptions.ends_at IS NOT NULL" in sql


async def test_sync_reads_usage_from_single_inbound_snapshot_without_per_email_calls():
    # Regression for the 3x-ui panel log spam: the per-email
    # getClientTraffics endpoint logs "Inbound Not Found For Email"
    # SERVER-SIDE for every unknown email, so calling it once per config per
    # minute flooded the panel's error log. The sync must read existence +
    # usage from ONE cached /inbounds/list call for the whole batch and
    # never hit the per-email endpoint.
    subs = []
    for i in range(3):
        sub = _xui_sub(status="active")
        sub.xui_client.email = f"cfg-{i}"
        sub.xui_client.xui_client_remote_id = f"client-{i}"
        sub.xui_client.client_uuid = f"uuid-{i}"
        subs.append(sub)
    inbound = _xui_inbound(
        ("cfg-0", "client-0", 10, 20),
        ("cfg-1", "client-1", 0, 0),
        ("cfg-2", "client-2", 5, 5),
    )
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[inbound])
    xui_client.get_client_traffic = AsyncMock()

    session = AsyncMock()
    await sync_xui_usage_and_status(session, xui_client, subs, _security_settings())

    xui_client.get_inbounds.assert_awaited_once()       # ONE panel call for the batch
    xui_client.get_client_traffic.assert_not_awaited()  # per-email endpoint NEVER hit
    assert [s.used_bytes for s in subs] == [30, 0, 10]  # up+down from clientStats
    assert all(s.usage_sync_failures == 0 for s in subs)


async def test_transport_error_with_404_in_email_is_not_a_gone_strike():
    # A transient transport failure while loading the inbound list must never
    # count as a "client gone" strike — even when the error text embeds the
    # config name ("x404") from a request URL.
    sub = _xui_sub(status="active", strikes=4)
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(side_effect=XUIRequestError(
        "Network error while calling X-UI endpoint "
        "https://panel/api/inbounds/list (cfg-x404): timeout"
    ))

    session = AsyncMock()
    await sync_xui_usage_and_status(session, xui_client, [sub], _security_settings())

    assert sub.usage_sync_failures == 4  # unchanged — no strike
    assert sub.status == "active"


async def test_client_absent_from_inbound_strikes_and_expires_at_threshold():
    sub = _xui_sub(status="active", strikes=4)
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[])

    session = AsyncMock()
    await sync_xui_usage_and_status(session, xui_client, [sub], _security_settings())

    assert sub.usage_sync_failures == 5
    assert sub.status == "expired"


async def test_expired_sub_recovers_when_client_present_in_inbound():
    sub = _xui_sub(
        status="expired",
        ends_at=datetime.now(timezone.utc) + timedelta(days=20),
        activated_at=datetime.now(timezone.utc) - timedelta(days=5),
        strikes=5,
    )
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[
        _xui_inbound(("cfg-x404", "client-1", 0, 0)),
    ])

    session = AsyncMock()
    await sync_xui_usage_and_status(session, xui_client, [sub], _security_settings())

    assert sub.status == "active"
    assert sub.expired_at is None
    assert sub.usage_sync_failures == 0
    assert sub.xui_client.is_active is True
    xui_client.get_inbounds.assert_awaited_once()


# ─── gone-client reprobe throttle (the CPU/log-spam loop fix) ────────────────
# A sub that strike-expired stays in the recovery filter (it still has time +
# volume). Without throttling, the 1-minute sweep re-logs + re-stamps it every
# minute forever. It must only re-record the strike once per
# REPROBE_GONE_INTERVAL, while a falsely-expired config still recovers.


async def test_strike_expired_sub_is_not_reprobed_within_interval():
    from apps.worker.jobs.subscriptions import REPROBE_GONE_INTERVAL
    # Expired 2 minutes ago via strikes, probed 2 minutes ago → no re-strike.
    recent = datetime.now(timezone.utc) - timedelta(minutes=2)
    sub = _xui_sub(status="expired", strikes=8, last_usage_sync_at=recent)
    assert REPROBE_GONE_INTERVAL > timedelta(minutes=2)  # sanity: within window

    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[])

    session = AsyncMock()
    await sync_xui_usage_and_status(session, xui_client, [sub], _security_settings())

    assert sub.usage_sync_failures == 8      # no extra strike
    assert sub.last_usage_sync_at is recent  # not re-stamped
    assert sub.status == "expired"


async def test_strike_expired_sub_is_rechecked_after_interval_and_recovers():
    # Last probed long ago → the throttle lets it through; the client is back
    # in the inbound → recover.
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    sub = _xui_sub(
        status="expired", strikes=8, last_usage_sync_at=old,
        ends_at=datetime.now(timezone.utc) + timedelta(days=5),
        activated_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[
        _xui_inbound(("cfg-x404", "client-1", 0, 10)),
    ])

    session = AsyncMock()
    await sync_xui_usage_and_status(session, xui_client, [sub], _security_settings())

    assert sub.status == "active"       # self-healed
    assert sub.usage_sync_failures == 0  # strikes cleared
    assert sub.used_bytes == 10         # usage read from clientStats


async def test_strike_expired_sub_rechecked_after_interval_still_gone_reexpires():
    # Last probed long ago → throttle lets it through; the client is STILL
    # absent from the inbound.
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    sub = _xui_sub(status="expired", strikes=8, last_usage_sync_at=old)
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[])

    session = AsyncMock()
    await sync_xui_usage_and_status(session, xui_client, [sub], _security_settings())

    assert sub.status == "expired"            # stays expired
    assert sub.usage_sync_failures == 9       # strike recorded
    assert sub.last_usage_sync_at > old       # throttle re-armed


async def test_never_activated_expired_sub_recovers_to_pending_activation():
    sub = _xui_sub(status="expired", ends_at=None, activated_at=None)
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[
        _xui_inbound(("cfg-x404", "client-1", 0, 0)),
    ])

    session = AsyncMock()
    await sync_xui_usage_and_status(session, xui_client, [sub], _security_settings())

    # Back to pending_activation (NOT active) so the first-traffic
    # activation can still run and set ends_at.
    assert sub.status == "pending_activation"
    assert sub.expired_at is None
    assert sub.xui_client.is_active is True


async def test_previously_activated_expired_sub_recovers_to_active():
    now = datetime.now(timezone.utc)
    sub = _xui_sub(
        status="expired",
        ends_at=now + timedelta(days=10),
        activated_at=now - timedelta(days=5),
    )
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[
        _xui_inbound(("cfg-x404", "client-1", 0, 100)),
    ])

    session = AsyncMock()
    await sync_xui_usage_and_status(session, xui_client, [sub], _security_settings())

    assert sub.status == "active"
    assert sub.expired_at is None
    assert sub.used_bytes == 100


async def test_pg_on_hold_recovery_restores_pending_activation():
    sub = _xui_sub(status="expired", ends_at=None, activated_at=None)
    pg_user = MagicMock(used_traffic=0, status="on_hold", expire_ts=None)
    client = AsyncMock()
    client.get_user = AsyncMock(return_value=pg_user)

    session = AsyncMock()
    server = MagicMock()
    with patch("apps.worker.jobs.subscriptions.marzban_client_for_server",
               return_value=_session_cm(client)):
        await sync_pasarguard_usage_and_status(session, server, [sub])

    assert sub.status == "pending_activation"
    assert sub.expired_at is None
    assert sub.xui_client.is_active is True
