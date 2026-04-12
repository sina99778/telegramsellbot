from aiogram.fsm.state import State, StatesGroup


class UserSupportStates(StatesGroup):
    waiting_for_issue = State()
