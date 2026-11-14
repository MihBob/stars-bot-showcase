from aiogram.fsm.state import State, StatesGroup


class BuyStars(StatesGroup):
    waiting_qty = State()
    waiting_username = State()
    waiting_payment_method = State()
    waiting_payment = State()


class AdminPanel(StatesGroup):
    waiting_cost = State()
    waiting_margin = State()


class SupportFlow(StatesGroup):
    waiting_message = State()


class BroadcastFlow(StatesGroup):
    waiting_text = State()
