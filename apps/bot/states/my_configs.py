from aiogram.fsm.state import State, StatesGroup

class UserConfigSearchStates(StatesGroup):
    waiting_for_search_query = State()
