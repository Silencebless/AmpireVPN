from aiogram.fsm.state import State, StatesGroup


class UserFlow(StatesGroup):
    promo = State()          # ввод промокода
    email = State()          # email для чека
    device_name = State()    # новое имя устройства


class AdminFlow(StatesGroup):
    input = State()          # любой текстовый ввод админа (действие хранится в data["action"])
