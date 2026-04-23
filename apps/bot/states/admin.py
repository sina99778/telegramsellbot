from aiogram.fsm.state import State, StatesGroup


class AddServerStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_base_url = State()
    waiting_for_username = State()
    waiting_for_password = State()


class ServerManageStates(StatesGroup):
    waiting_for_config_domain = State()
    waiting_for_sub_domain = State()
    waiting_for_max_clients = State()
    waiting_for_new_base_url = State()
    waiting_for_new_username = State()
    waiting_for_new_password = State()


class CreatePlanStates(StatesGroup):
    waiting_for_inbound_selection = State()
    waiting_for_name = State()
    waiting_for_duration_days = State()
    waiting_for_volume_gb = State()
    waiting_for_price = State()


class ManageUserStates(StatesGroup):
    waiting_for_telegram_id = State()
    waiting_for_balance_adjustment = State()
    waiting_for_message_to_user = State()


class BroadcastStates(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirmation = State()


class RetargetingStates(StatesGroup):
    waiting_for_message = State()
    waiting_for_days = State()


class SupportReplyStates(StatesGroup):
    waiting_for_reply = State()


class SettingsStates(StatesGroup):
    waiting_for_price_gb = State()
    waiting_for_price_days = State()
    waiting_for_toman_rate = State()


class DiscountStates(StatesGroup):
    waiting_for_code = State()
    waiting_for_percent = State()
    waiting_for_max_uses = State()
    waiting_for_edit_percent = State()
    waiting_for_edit_expiry = State()
    waiting_for_edit_max_uses = State()


class GlobalSearchStates(StatesGroup):
    waiting_for_query = State()


class GatewaySettingsStates(StatesGroup):
    waiting_for_nowpayments_api_key = State()
    waiting_for_tetrapay_api_key = State()


class ReferralSettingsStates(StatesGroup):
    waiting_for_referrer_bonus = State()
    waiting_for_referee_bonus = State()
