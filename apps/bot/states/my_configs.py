from aiogram.fsm.state import State, StatesGroup


class UserConfigSearchStates(StatesGroup):
    waiting_for_search_query = State()


class InboundChangeStates(StatesGroup):
    # We hold the subscription_id in state so callback_data only needs to
    # carry the (short) target_inbound_id — keeps us safely under
    # Telegram's 64-byte callback_data limit.
    picking_inbound = State()
    confirming = State()
