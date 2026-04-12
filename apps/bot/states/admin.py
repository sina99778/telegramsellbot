from aiogram.fsm.state import State, StatesGroup


class AddServerStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_base_url = State()
    waiting_for_username = State()
    waiting_for_password = State()


class CreatePlanStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_protocol = State()
    waiting_for_duration_days = State()
    waiting_for_volume_gb = State()
    waiting_for_price = State()


class ManageUserStates(StatesGroup):
    waiting_for_telegram_id = State()
    waiting_for_balance_adjustment = State()


class BroadcastStates(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirmation = State()


class SupportReplyStates(StatesGroup):
    waiting_for_reply = State()
