from aiogram.fsm.state import State, StatesGroup


class TopUpStates(StatesGroup):
    waiting_for_custom_amount = State()
