from aiogram.fsm.state import State, StatesGroup

class RenewStates(StatesGroup):
    waiting_for_volume = State()
    waiting_for_time = State()
    waiting_for_payment_method = State()
