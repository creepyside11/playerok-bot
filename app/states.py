from aiogram.fsm.state import State, StatesGroup


class AddAccount(StatesGroup):
    proxy = State()
    auth_method = State()
    cookie = State()
    email = State()
    code = State()


class AutoReplyAdd(StatesGroup):
    trigger = State()
    response = State()


class DeliveryAdd(StatesGroup):
    item_id = State()
    mode = State()
    content = State()


class ItemCreate(StatesGroup):
    game = State()
    category = State()
    obtaining = State()
    attributes = State()
    data_fields = State()
    name = State()
    price = State()
    description = State()
    image = State()
    priority = State()
    template_name = State()


class PluginUpload(StatesGroup):
    file = State()


class ItemCatalog(StatesGroup):
    game_search = State()
    category_search = State()


class ChatReply(StatesGroup):
    text = State()


class PluginSettingEdit(StatesGroup):
    value = State()


class DeliveryStockAdd(StatesGroup):
    content = State()
