from aiogram.fsm.state import State, StatesGroup


class PurchaseStates(StatesGroup):
    waiting_for_custom_volume = State()
    waiting_for_custom_days = State()
    waiting_for_config_name = State()
    waiting_for_discount_code = State()
