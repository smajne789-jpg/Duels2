from __future__ import annotations

import asyncio
import contextlib
import secrets
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from bot_services import Config, CryptoBotClient, CryptoPayError, Database, fmt_amount


router = Router()
config: Config
db: Database
crypto: CryptoBotClient
bot_username = "your_bot"
PREMIUM_EMOJI_IDS = {
    "main_menu": "5325547803936572038",
    "profile": "5390998591516977527",
    "check": "5197434882321567830",
    "win": "5260616239247563540",
    "lose": "5260258807774218256",
    "deposit": "5201691993775818138",
    "withdraw": "5312123810638483121",
    "log": "5267014542222723292",
    "play_button": "5267014542222723292",
    "profile_button": "5258204546391351475",
    "create_room_button": "5472427031100667803",
    "rooms_button": "5386367538735104399",
    "my_rooms_button": "5316727448644103237",
    "deposit_button": "5258336354642697821",
    "withdraw_button": "5260379144167890225",
    "refs_button": "5258486128742244085",
    "back_button": "5258236805890710909",
    "refresh_button": "5258420634785947640",
    "delete_button": "5210952531676504517",
    "subscribe_button": "5258073068852485953",
    "profile_balance": "5197434882321567830",
    "profile_ref_income": "5330320040883411678",
    "profile_ref_count": "5217822164362739968",
    "game_menu_title": "5399909394525737759",
}


def premium_emoji(slot: str, fallback: str) -> str:
    emoji_id = PREMIUM_EMOJI_IDS.get(slot)
    if not emoji_id:
        return fallback
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def premium_button_icon(slot: str) -> str | None:
    return PREMIUM_EMOJI_IDS.get(slot)


class UserStates(StatesGroup):
    deposit_amount = State()
    withdraw_amount = State()
    room_amount = State()
    default_room_amount = State()
    user_check_password = State()
    admin_check_main = State()
    admin_check_description = State()
    admin_check_password = State()
    admin_check_required_deposit = State()
    admin_check_image = State()
    admin_check_channels = State()
    admin_check_publish_channel = State()
    admin_house_commission = State()
    admin_broadcast = State()
    admin_min_room = State()
    admin_min_deposit = State()
    admin_min_withdraw = State()
    admin_ref_percent = State()
    admin_add_admin = State()
    admin_add_balance = State()
    admin_take_balance = State()
    admin_force_sub = State()
    admin_log_chat = State()


@dataclass(slots=True)
class RoomRender:
    room_id: int
    amount: float
    creator_id: int
    creator_name: str


class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]):
        user = getattr(event, "from_user", None)
        bot: Bot = data["bot"]
        if not user:
            return await handler(event, data)
        if isinstance(event, Message) and (event.text or "").startswith("/start"):
            return await handler(event, data)
        if await db.is_admin(user.id):
            return await handler(event, data)
        if await is_user_subscribed(bot, user.id):
            return await handler(event, data)
        await send_subscription_prompt(bot, event)
        if isinstance(event, CallbackQuery):
            await event.answer("Сначала подпишитесь на канал", show_alert=True)
        return None


def parse_amount(raw: str) -> float:
    value = raw.strip().replace(",", ".")
    amount = round(float(value), 8)
    if amount <= 0:
        raise ValueError
    return amount


def is_empty_treasury_error(error_text: str) -> bool:
    normalized = error_text.lower()
    patterns = (
        "insufficient",
        "not enough",
        "balance is too low",
        "not enough funds",
        "low balance",
    )
    return any(pattern in normalized for pattern in patterns)


def main_menu_keyboard(is_admin: bool) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="Играть", icon_custom_emoji_id=premium_button_icon("play_button")),
        KeyboardButton(text="Профиль", icon_custom_emoji_id=premium_button_icon("profile_button")),
    )
    if is_admin:
        builder.row(KeyboardButton(text="Админ-панель", icon_custom_emoji_id=premium_button_icon("log")))
    return builder.as_markup(resize_keyboard=True)


def game_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Создать комнату", callback_data="room:create", icon_custom_emoji_id=premium_button_icon("create_room_button"))
    builder.button(text="Список комнат", callback_data="rooms:list", icon_custom_emoji_id=premium_button_icon("rooms_button"))
    builder.button(text="Мои комнаты", callback_data="rooms:mine", icon_custom_emoji_id=premium_button_icon("my_rooms_button"))
    builder.adjust(1)
    return builder.as_markup()


def profile_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Пополнить", callback_data="profile:deposit", icon_custom_emoji_id=premium_button_icon("deposit_button"))
    builder.button(text="Вывести", callback_data="profile:withdraw", icon_custom_emoji_id=premium_button_icon("withdraw_button"))
    builder.button(text="Рефералы", callback_data="profile:referrals", icon_custom_emoji_id=premium_button_icon("refs_button"))
    builder.adjust(2, 1)
    return builder.as_markup()


def referrals_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Назад", callback_data="profile:open", icon_custom_emoji_id=premium_button_icon("back_button"))
    builder.adjust(1)
    return builder.as_markup()


def rooms_keyboard(rooms: list[RoomRender], current_user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for room in rooms:
        creator_title = "Вы" if room.creator_id == current_user_id else room.creator_name
        builder.button(
            text=f"#{room.room_id} • {fmt_amount(room.amount)} {config.bot_asset} • {creator_title}",
            callback_data=f"room:join:{room.room_id}",
            icon_custom_emoji_id=premium_button_icon("rooms_button"),
        )
    builder.button(text="Обновить", callback_data="rooms:list", icon_custom_emoji_id=premium_button_icon("refresh_button"))
    builder.button(text="Назад", callback_data="game:open", icon_custom_emoji_id=premium_button_icon("back_button"))
    builder.adjust(1)
    return builder.as_markup()


def my_rooms_keyboard(room_ids: list[int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for room_id in room_ids:
        builder.button(text=f"Отменить комнату #{room_id}", callback_data=f"room:cancel:{room_id}", icon_custom_emoji_id=premium_button_icon("delete_button"))
    builder.button(text="Назад", callback_data="game:open", icon_custom_emoji_id=premium_button_icon("back_button"))
    builder.adjust(1)
    return builder.as_markup()


def force_sub_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    return builder.as_markup()


def admin_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Создать чек", callback_data="admin:create_check", icon_custom_emoji_id=premium_button_icon("check"))
    builder.button(text="Список чеков", callback_data="admin:checks", icon_custom_emoji_id=premium_button_icon("check"))
    builder.button(text="Комиссия проекта", callback_data="admin:house_commission", icon_custom_emoji_id=premium_button_icon("log"))
    builder.button(text="Режим вывода", callback_data="admin:withdraw_mode", icon_custom_emoji_id=premium_button_icon("withdraw_button"))
    builder.button(text="Рассылка", callback_data="admin:broadcast", icon_custom_emoji_id=premium_button_icon("log"))
    builder.button(text="Топ рефов", callback_data="admin:refs_top", icon_custom_emoji_id=premium_button_icon("win"))
    builder.button(text="Мин. ставка комнаты", callback_data="admin:min_room", icon_custom_emoji_id=premium_button_icon("main_menu"))
    builder.button(text="Мин. пополнение", callback_data="admin:min_deposit", icon_custom_emoji_id=premium_button_icon("deposit"))
    builder.button(text="Мин. вывод", callback_data="admin:min_withdraw", icon_custom_emoji_id=premium_button_icon("withdraw"))
    builder.button(text="Реф. процент", callback_data="admin:ref_percent", icon_custom_emoji_id=premium_button_icon("profile"))
    builder.button(text="Добавить админа", callback_data="admin:add_admin", icon_custom_emoji_id=premium_button_icon("log"))
    builder.button(text="Выдать баланс", callback_data="admin:add_balance", icon_custom_emoji_id=premium_button_icon("deposit"))
    builder.button(text="Снять баланс", callback_data="admin:take_balance", icon_custom_emoji_id=premium_button_icon("withdraw"))
    builder.button(text="Канал подписки", callback_data="admin:force_sub", icon_custom_emoji_id=premium_button_icon("log"))
    builder.button(text="Лог-чат", callback_data="admin:log_chat", icon_custom_emoji_id=premium_button_icon("log"))
    builder.button(text="Статистика", callback_data="admin:stats", icon_custom_emoji_id=premium_button_icon("log"))
    builder.adjust(1)
    return builder.as_markup()


async def safe_edit(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        await message.answer(text, reply_markup=reply_markup)


async def is_user_subscribed(bot: Bot, user_id: int) -> bool:
    chat_id = await db.get_setting("force_sub_chat_id", "")
    chat_username = await db.get_setting("force_sub_chat_username", "")
    target = chat_id or chat_username
    if not target:
        return True
    try:
        member = await bot.get_chat_member(target, user_id)
    except TelegramBadRequest:
        return True
    return member.status not in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}


async def send_subscription_prompt(bot: Bot, event: TelegramObject) -> None:
    chat_username = await db.get_setting("force_sub_chat_username", "")
    chat_id = await db.get_setting("force_sub_chat_id", "")
    builder = InlineKeyboardBuilder()
    if chat_username:
        builder.button(text="Подписаться", url=f"https://t.me/{chat_username.lstrip('@')}", icon_custom_emoji_id=premium_button_icon("subscribe_button"))
    elif chat_id:
        builder.button(text="Канал", url="https://t.me", icon_custom_emoji_id=premium_button_icon("subscribe_button"))
    builder.button(text="Проверить подписку", callback_data="sub:check", icon_custom_emoji_id=premium_button_icon("refresh_button"))
    builder.adjust(1)
    text = (
        "🔒 <b>Доступ ограничен</b>\n\n"
        "Для использования бота нужно подписаться на обязательный канал и затем нажать кнопку проверки."
    )
    target_message = getattr(event, "message", None) if isinstance(event, CallbackQuery) else event
    if isinstance(target_message, Message):
        await target_message.answer(text, reply_markup=builder.as_markup())
    else:
        await bot.send_message(event.from_user.id, text, reply_markup=builder.as_markup())


async def send_log(bot: Bot, text: str) -> None:
    chat_id = await db.get_setting("log_chat_id", "")
    if not chat_id:
        return
    with contextlib.suppress(Exception):
        await bot.send_message(int(chat_id), f"{premium_emoji('log', '🧾')} {text}")


async def ensure_registered(tg_user: Any, invited_by: int | None = None) -> Any:
    return await db.upsert_user(tg_user, invited_by=invited_by)


async def render_profile(user_id: int) -> str:
    user = await db.get_user(user_id)
    referral = await db.get_referral_summary(user_id)
    if not user:
        return "👤 Профиль не найден."
    return (
        f"{premium_emoji('profile', '👤')} <b>Ваш профиль</b>\n\n"
        f"{premium_emoji('profile_balance', '💰')} Баланс: <b>{fmt_amount(float(user['balance']))} {config.bot_asset}</b>\n"
        f"{premium_emoji('profile_ref_income', '🎁')} Реф. доход: <b>{fmt_amount(float(user['referral_earnings']))} {config.bot_asset}</b>\n"
        f"{premium_emoji('profile_ref_count', '👥')} Рефералов: <b>{referral['referred_count']}</b>"
    )


async def show_main_menu(message: Message) -> None:
    is_admin = await db.is_admin(message.from_user.id)
    text = (
        f"{premium_emoji('main_menu', '🎰')} <b>Dice Duel</b>\n\n"
        "✨ Создавайте комнаты, кидайте реальные кубики Telegram, побеждайте и забирайте банк.\n"
        f"{premium_emoji('deposit', '💳')} Пополнение и вывод работают через CryptoBot."
    )
    await message.answer(text, reply_markup=main_menu_keyboard(is_admin))


async def send_main_menu(message: Message, user_id: int) -> None:
    is_admin = await db.is_admin(user_id)
    text = (
        f"{premium_emoji('main_menu', '🎰')} <b>Dice Duel</b>\n\n"
        "✨ Создавайте комнаты, кидайте реальные кубики Telegram, побеждайте и забирайте банк.\n"
        f"{premium_emoji('deposit', '💳')} Пополнение и вывод работают через CryptoBot."
    )
    await message.answer(text, reply_markup=main_menu_keyboard(is_admin))


async def show_game_menu(target: Message | CallbackQuery) -> None:
    text = (
        f"{premium_emoji('game_menu_title', '🎲')} <b>Игровое меню</b>\n\n"
        f"{premium_emoji('main_menu', '🔥')} Создавайте комнаты, заходите в чужие и играйте на реальные кубики Telegram.\n"
        f"{premium_emoji('log', '💼')} Комиссия проекта: <b>{fmt_amount(await db.get_setting_float('house_commission_percent', config.house_commission_percent))}%</b>."
    )
    if isinstance(target, CallbackQuery):
        await safe_edit(target.message, text, reply_markup=game_menu_keyboard())
        await target.answer()
    else:
        await target.answer(text, reply_markup=game_menu_keyboard())


async def show_profile(target: Message | CallbackQuery) -> None:
    text = await render_profile(target.from_user.id)
    if isinstance(target, CallbackQuery):
        await safe_edit(target.message, text, reply_markup=profile_keyboard())
        await target.answer()
    else:
        await target.answer(text, reply_markup=profile_keyboard())


async def show_admin_panel(target: Message | CallbackQuery) -> None:
    admins = await db.list_admins()
    text = (
        "🛡 <b>Админ-панель</b>\n\n"
        f"👮 Администраторов: <b>{len(admins)}</b>\n"
        "⚙️ Управление чеками, лимитами, админами, балансами, подпиской и логами."
    )
    if isinstance(target, CallbackQuery):
        await safe_edit(target.message, text, reply_markup=admin_keyboard())
        await target.answer()
    else:
        await target.answer(text, reply_markup=admin_keyboard())


async def show_gift_checks_list(target: Message | CallbackQuery) -> None:
    checks = await db.list_gift_checks(limit=20)
    if not checks:
        text = "🗂 <b>Список чеков</b>\n\nСейчас нет созданных чеков."
        if isinstance(target, CallbackQuery):
            await safe_edit(target.message, text, reply_markup=admin_keyboard())
            await target.answer()
        else:
            await target.answer(text, reply_markup=admin_keyboard())
        return
    text = (
        "🗂 <b>Список чеков</b>\n\n"
        "Выберите чек из списка ниже, чтобы открыть карточку, опубликовать или удалить его."
    )
    markup = gift_check_list_keyboard(checks)
    if isinstance(target, CallbackQuery):
        await safe_edit(target.message, text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def is_subscribed_to_channel(bot: Bot, user_id: int, channel: str) -> bool:
    try:
        member = await bot.get_chat_member(channel, user_id)
    except TelegramBadRequest:
        return False
    return member.status not in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}


async def get_missing_gift_check_channels(bot: Bot, user_id: int, gift_check: Any) -> list[str]:
    missing: list[str] = []
    for channel in gift_check_required_channels(gift_check):
        if not await is_subscribed_to_channel(bot, user_id, channel):
            missing.append(channel)
    return missing


async def send_gift_check_subscription_prompt(message: Message, gift_check: Any, missing_channels: list[str]) -> None:
    channels_text = "\n".join(f"• {channel}" for channel in missing_channels)
    await message.answer(
        "📡 <b>Для активации чека нужна подписка</b>\n\n"
        "Подпишитесь на каналы ниже и потом нажмите кнопку проверки.\n\n"
        f"{channels_text}",
        reply_markup=gift_check_subscription_keyboard(gift_check["token"], missing_channels),
    )


async def render_withdraw_mode_text() -> str:
    auto_enabled = await db.get_setting("withdraw_auto_enabled", "1")
    mode_label = "авто-вывод включен" if auto_enabled == "1" else "ручная модерация включена"
    description = (
        "Заявки сразу уходят пользователю готовым чеком."
        if auto_enabled == "1"
        else "Новые заявки уходят в лог-чат, где админ может одобрить или отклонить вывод."
    )
    return (
        f"{premium_emoji('withdraw', '💸')} <b>Режим вывода</b>\n\n"
        f"Текущее состояние: <b>{mode_label}</b>\n"
        f"{description}"
    )


def render_withdraw_request_text(withdrawal: Any, user: Any, status_label: str) -> str:
    username = f"@{user['username']}" if user and user["username"] else "без username"
    balance = fmt_amount(float(user["balance"])) if user else "0"
    return (
        f"{premium_emoji('withdraw', '💸')} <b>Заявка на вывод</b>\n\n"
        f"Статус: <b>{status_label}</b>\n"
        f"ID: <code>{withdrawal['check_id']}</code>\n"
        f"Пользователь: <code>{withdrawal['user_id']}</code> ({username})\n"
        f"Сумма: <b>{fmt_amount(float(withdrawal['amount']))} {withdrawal['asset']}</b>\n"
        f"Баланс после заявки: <b>{balance} {withdrawal['asset']}</b>"
    )


async def callback_admin_guard(callback: CallbackQuery) -> bool:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return False
    return True


async def show_rooms(callback: CallbackQuery) -> None:
    rows = [row for row in await db.list_open_rooms() if int(row["creator_id"]) != callback.from_user.id]
    if not rows:
        text = "📋 <b>Открытые комнаты</b>\n\nСейчас нет доступных чужих комнат. Создайте свою или дождитесь соперника."
        await safe_edit(callback.message, text, reply_markup=game_menu_keyboard())
        await callback.answer()
        return
    rooms = [
        RoomRender(
            room_id=int(row["id"]),
            amount=float(row["amount"]),
            creator_id=int(row["creator_id"]),
            creator_name=f"@{row['username']}" if row["username"] else row["full_name"],
        )
        for row in rows
    ]
    text = "📋 <b>Список комнат</b>\n\nВыберите комнату из списка ниже."
    await safe_edit(callback.message, text, reply_markup=rooms_keyboard(rooms, callback.from_user.id))
    await callback.answer()


async def show_my_rooms(callback: CallbackQuery) -> None:
    rows = await db.list_user_open_rooms(callback.from_user.id)
    if not rows:
        await safe_edit(
            callback.message,
            "🧾 <b>Мои комнаты</b>\n\nУ вас нет активных комнат.",
            reply_markup=game_menu_keyboard(),
        )
        await callback.answer()
        return
    text = "🧾 <b>Мои активные комнаты</b>\n\nНиже можно отменить любую незапущенную комнату."
    await safe_edit(
        callback.message,
        text,
        reply_markup=my_rooms_keyboard([int(row["id"]) for row in rows]),
    )
    await callback.answer()


async def send_duel_dice_to_both(
    bot: Bot,
    owner_id: int,
    other_id: int,
    title: str,
    round_label: str,
) -> int:
    await bot.send_message(owner_id, f"{round_label}\n{title}")
    await bot.send_message(other_id, f"{round_label}\n{title}")
    dice_message = await bot.send_dice(owner_id, emoji="🎲")
    with contextlib.suppress(Exception):
        await bot.forward_message(chat_id=other_id, from_chat_id=owner_id, message_id=dice_message.message_id)
    return int(dice_message.dice.value)


async def roll_duel_round(bot: Bot, creator_id: int, opponent_id: int, room_id: int, reroll: bool = False) -> tuple[int, int]:
    round_label = f"🎲 <b>Комната #{room_id}</b>"
    participants = [
        (creator_id, opponent_id, "создатель комнаты", "creator"),
        (opponent_id, creator_id, "вошедший игрок", "opponent"),
    ]
    if secrets.randbelow(2) == 1:
        participants.reverse()

    first_owner_id, first_other_id, first_role_label, first_role_key = participants[0]
    second_owner_id, second_other_id, second_role_label, second_role_key = participants[1]
    prefix = "Переброс" if reroll else "Бросок"

    first_roll = await send_duel_dice_to_both(
        bot,
        first_owner_id,
        first_other_id,
        f"1️⃣ {prefix} — первым кидает {first_role_label}",
        round_label,
    )
    second_roll = await send_duel_dice_to_both(
        bot,
        second_owner_id,
        second_other_id,
        f"2️⃣ {prefix} — вторым кидает {second_role_label}",
        round_label,
    )

    creator_roll = first_roll if first_role_key == "creator" else second_roll
    opponent_roll = first_roll if first_role_key == "opponent" else second_roll
    return creator_roll, opponent_roll


def gift_check_link(token: str) -> str:
    return f"https://t.me/{bot_username}?start=check_{token}"


def gift_check_admin_keyboard(token: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Открыть чек", url=gift_check_link(token), icon_custom_emoji_id=premium_button_icon("check"))
    builder.button(text="Опубликовать в канал", callback_data=f"giftcheck:publish:{token}", icon_custom_emoji_id=premium_button_icon("log"))
    builder.button(text="Удалить чек", callback_data=f"giftcheck:delete:{token}", icon_custom_emoji_id=premium_button_icon("delete_button"))
    builder.adjust(1)
    return builder.as_markup()


def gift_check_public_keyboard(token: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Активировать", url=gift_check_link(token), icon_custom_emoji_id=premium_button_icon("check"))
    builder.adjust(1)
    return builder.as_markup()


def check_skip_keyboard(step: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Пропустить", callback_data=f"giftcheckskip:{step}", icon_custom_emoji_id=premium_button_icon("refresh_button"))
    builder.adjust(1)
    return builder.as_markup()


def normalize_channel_target(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    value = value.strip().strip("/")
    if "/" in value:
        value = value.split("/", 1)[0]
    if value.startswith("@"):
        value = value[1:]
    if not value or value.startswith("+"):
        return None
    if not all(ch.isalnum() or ch == "_" for ch in value):
        return None
    return f"@{value}"


def parse_gift_check_channels(raw: str | None) -> list[str]:
    if not raw:
        return []
    channels: list[str] = []
    for chunk in raw.replace(",", "\n").splitlines():
        normalized = normalize_channel_target(chunk)
        if normalized and normalized not in channels:
            channels.append(normalized)
    return channels


def required_channels_text(channels: list[str]) -> str:
    return ", ".join(channels) if channels else "не требуется"


def gift_check_required_channels(gift_check: Any) -> list[str]:
    return parse_gift_check_channels(gift_check["required_channels"])


def gift_check_manage_keyboard(token: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Открыть чек", url=gift_check_link(token), icon_custom_emoji_id=premium_button_icon("check"))
    builder.button(text="Опубликовать", callback_data=f"giftcheck:publish:{token}", icon_custom_emoji_id=premium_button_icon("log"))
    builder.button(text="Удалить", callback_data=f"giftcheck:delete:{token}", icon_custom_emoji_id=premium_button_icon("delete_button"))
    builder.button(text="К списку", callback_data="admin:checks", icon_custom_emoji_id=premium_button_icon("back_button"))
    builder.adjust(1)
    return builder.as_markup()


def gift_check_list_keyboard(checks: list[Any]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for gift_check in checks:
        status = "ON" if int(gift_check["is_active"]) and int(gift_check["activations_left"]) > 0 else "OFF"
        builder.button(
            text=(
                f"{status} {gift_check['token']} • "
                f"{fmt_amount(float(gift_check['amount']))} {config.bot_asset} • "
                f"{int(gift_check['activations_left'])}/{int(gift_check['activations_total'])}"
            ),
            callback_data=f"giftcheck:view:{gift_check['token']}",
            icon_custom_emoji_id=premium_button_icon("check"),
        )
    builder.button(text="Обновить", callback_data="admin:checks", icon_custom_emoji_id=premium_button_icon("refresh_button"))
    builder.button(text="Назад", callback_data="admin:open", icon_custom_emoji_id=premium_button_icon("back_button"))
    builder.adjust(1)
    return builder.as_markup()


def gift_check_subscription_keyboard(token: str, channels: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for channel in channels:
        builder.button(text=channel, url=f"https://t.me/{channel.lstrip('@')}", icon_custom_emoji_id=premium_button_icon("subscribe_button"))
    builder.button(text="Проверить подписку", callback_data=f"giftchecksub:{token}", icon_custom_emoji_id=premium_button_icon("refresh_button"))
    builder.adjust(1)
    return builder.as_markup()


def withdraw_mode_keyboard(auto_enabled: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if auto_enabled:
        builder.button(text="Отключить авто-вывод", callback_data="withdrawmode:toggle", icon_custom_emoji_id=premium_button_icon("delete_button"))
    else:
        builder.button(text="Включить авто-вывод", callback_data="withdrawmode:toggle", icon_custom_emoji_id=premium_button_icon("win"))
    builder.button(text="Назад", callback_data="admin:open", icon_custom_emoji_id=premium_button_icon("back_button"))
    builder.adjust(1)
    return builder.as_markup()


def withdraw_request_keyboard(request_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Одобрить", callback_data=f"withdraw:approve:{request_id}", icon_custom_emoji_id=premium_button_icon("win"))
    builder.button(text="Отклонить", callback_data=f"withdraw:reject:{request_id}", icon_custom_emoji_id=premium_button_icon("delete_button"))
    builder.adjust(2)
    return builder.as_markup()


def render_gift_check_text(gift_check: Any) -> str:
    description = (gift_check["description"] or "Без описания").strip()
    required_channels = gift_check_required_channels(gift_check)
    password_line = "🔐 По паролю" if gift_check["password"] else "🔓 Без пароля"
    deposit_line = (
        f"💳 Обязательный депозит: <b>{fmt_amount(float(gift_check['required_deposit']))} {config.bot_asset}</b>"
        if float(gift_check["required_deposit"]) > 0
        else "💳 Обязательный депозит: <b>не требуется</b>"
    )
    channels_line = f"📡 Подписка: <b>{required_channels_text(required_channels)}</b>"
    return (
        f"{premium_emoji('check', '🎁')} <b>Чек на {fmt_amount(float(gift_check['amount']))} {config.bot_asset}</b>\n\n"
        f"💰 Сумма активации: <b>{fmt_amount(float(gift_check['amount']))} {config.bot_asset}</b>\n"
        f"👥 Активаций: <b>{int(gift_check['activations_left'])}/{int(gift_check['activations_total'])}</b>\n"
        f"{deposit_line}\n"
        f"{channels_line}\n"
        f"📝 Описание: <b>{description}</b>\n"
        f"{password_line}"
    )


async def send_gift_check_post(bot: Bot, target: int | str, gift_check: Any) -> None:
    text = render_gift_check_text(gift_check)
    if gift_check["photo_file_id"]:
        await bot.send_photo(
            target,
            photo=gift_check["photo_file_id"],
            caption=text,
            reply_markup=gift_check_public_keyboard(gift_check["token"]),
        )
    else:
        await bot.send_message(
            target,
            text,
            reply_markup=gift_check_public_keyboard(gift_check["token"]),
        )


async def claim_gift_check_for_user(message: Message, token: str, password: str | None = None) -> None:
    gift_check = await db.get_gift_check(token)
    if not gift_check:
        await message.answer("❌ Чек не найден или уже удален.")
        return
    missing_channels = await get_missing_gift_check_channels(message.bot, message.from_user.id, gift_check)
    if missing_channels:
        await send_gift_check_subscription_prompt(message, gift_check, missing_channels)
        return
    try:
        user, gift_check = await db.claim_gift_check(token, message.from_user.id, password=password)
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return
    await message.answer(
        f"{premium_emoji('check', '🎁')} <b>Чек активирован</b>\n\n"
        f"💰 Начислено: <b>{fmt_amount(float(gift_check['amount']))} {config.bot_asset}</b>\n"
        f"💳 Баланс: <b>{fmt_amount(float(user['balance']))} {config.bot_asset}</b>"
    )
    await send_log(
        message.bot,
        "🎟 <b>Активирован чек</b>\n"
        f"👤 Пользователь: <code>{message.from_user.id}</code>\n"
        f"🔑 Чек: <code>{gift_check['token']}</code>\n"
        f"💰 Сумма: <b>{fmt_amount(float(gift_check['amount']))} {config.bot_asset}</b>\n"
        f"👥 Осталось активаций: <b>{int(gift_check['activations_left'])}</b>",
    )


async def start_gift_check_flow(message: Message, state: FSMContext, token: str) -> None:
    gift_check = await db.get_gift_check(token)
    if not gift_check:
        await message.answer("❌ Чек не найден или уже удален.")
        return
    missing_channels = await get_missing_gift_check_channels(message.bot, message.from_user.id, gift_check)
    if missing_channels:
        await send_gift_check_subscription_prompt(message, gift_check, missing_channels)
        return
    if gift_check["password"]:
        await state.set_state(UserStates.user_check_password)
        await state.update_data(gift_check_token=gift_check["token"])
        await message.answer(
            render_gift_check_text(gift_check)
            + "\n\n🔐 <b>Для активации введите пароль</b>"
        )
        return
    await claim_gift_check_for_user(message, gift_check["token"])


async def ask_check_password_step(message: Message, state: FSMContext) -> None:
    await state.set_state(UserStates.admin_check_password)
    await message.answer(
        "🔐 <b>Пароль для чека</b>\n\n"
        "Отправьте пароль для активации или нажмите кнопку ниже.",
        reply_markup=check_skip_keyboard("password"),
    )


async def ask_check_required_deposit_step(message: Message, state: FSMContext) -> None:
    await state.set_state(UserStates.admin_check_required_deposit)
    await message.answer(
        "💳 <b>Обязательный депозит</b>\n\n"
        "Отправьте минимальную сумму пополнений, которая нужна для активации чека.\n"
        "Если ограничение не нужно, нажмите кнопку ниже.",
        reply_markup=check_skip_keyboard("deposit"),
    )


async def ask_check_image_step(message: Message, state: FSMContext) -> None:
    await state.set_state(UserStates.admin_check_image)
    await message.answer(
        "🖼 <b>Изображение для чека</b>\n\n"
        "Отправьте картинку одним сообщением или нажмите кнопку ниже.",
        reply_markup=check_skip_keyboard("image"),
    )


async def ask_check_channels_step(message: Message, state: FSMContext, photo_file_id: str | None) -> None:
    await state.update_data(check_photo_file_id=photo_file_id)
    await state.set_state(UserStates.admin_check_channels)
    await message.answer(
        "📡 <b>Каналы для обязательной подписки</b>\n\n"
        "Отправьте @username каналов или публичные ссылки на них, каждый с новой строки.\n"
        "Если подписка для чека не нужна, нажмите кнопку ниже.",
        reply_markup=check_skip_keyboard("channels"),
    )


async def finalize_gift_check_creation(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    required_channels = parse_gift_check_channels(data.get("check_required_channels"))
    gift_check = await db.create_gift_check(
        float(data["check_amount"]),
        int(data["check_activations"]),
        message.from_user.id,
        required_deposit=float(data.get("check_required_deposit", 0.0)),
        required_channels=required_channels,
        password=data.get("check_password"),
        description=data.get("check_description"),
        photo_file_id=data.get("check_photo_file_id"),
    )
    await message.answer(
        render_gift_check_text(gift_check)
        + f"\n\n🔗 <code>{gift_check_link(gift_check['token'])}</code>",
        reply_markup=gift_check_admin_keyboard(gift_check["token"]),
    )
    await send_log(
        message.bot,
        "🎟 <b>Создан чек</b>\n"
        f"👮 Админ: <code>{message.from_user.id}</code>\n"
        f"🔑 Токен: <code>{gift_check['token']}</code>\n"
        f"💰 Сумма: <b>{fmt_amount(float(gift_check['amount']))} {config.bot_asset}</b>\n"
        f"👥 Активаций: <b>{int(gift_check['activations_total'])}</b>\n"
        f"💳 Депозит: <b>{fmt_amount(float(gift_check['required_deposit']))} {config.bot_asset}</b>\n"
        f"📡 Каналы: <b>{required_channels_text(required_channels)}</b>\n"
        f"🔐 Пароль: <b>{'да' if gift_check['password'] else 'нет'}</b>\n"
        f"🖼 Картинка: <b>{'да' if gift_check['photo_file_id'] else 'нет'}</b>",
    )
    await state.clear()


async def invoice_worker(bot: Bot) -> None:
    while True:
        try:
            if not config.crypto_pay_token:
                await asyncio.sleep(config.invoice_poll_interval)
                continue
            pending = await db.get_pending_invoices()
            if pending:
                invoices = await crypto.get_invoices([row["invoice_id"] for row in pending])
                status_map = {str(item.get("invoice_id")): item for item in invoices}
                for row in pending:
                    data = status_map.get(str(row["invoice_id"]))
                    if not data:
                        continue
                    status = str(data.get("status", "")).lower()
                    if status not in {"paid", "completed", "confirmed"}:
                        continue
                    updated = await db.complete_invoice(str(row["invoice_id"]))
                    if not updated:
                        continue
                    amount = fmt_amount(float(row["amount"]))
                    text = (
                        f"{premium_emoji('deposit', '💳')} <b>Пополнение подтверждено</b>\n\n"
                        f"На баланс зачислено <b>{amount} {row['asset']}</b>.\n"
                        f"Текущий баланс: <b>{fmt_amount(float(updated['balance']))} {row['asset']}</b>"
                    )
                    with contextlib.suppress(Exception):
                        await bot.send_message(int(row["user_id"]), text)
                    await send_log(
                        bot,
                        "💳 <b>Пополнение</b>\n"
                        f"Пользователь: <code>{row['user_id']}</code>\n"
                        f"Сумма: <b>{amount} {row['asset']}</b>\n"
                        f"Invoice: <code>{row['invoice_id']}</code>",
                    )
        except Exception as exc:
            await send_log(bot, f"⚠️ Ошибка invoice worker: <code>{exc}</code>")
        await asyncio.sleep(config.invoice_poll_interval)


@router.message(CommandStart())
async def handle_start(message: Message, command: CommandObject, state: FSMContext) -> None:
    inviter_id = None
    gift_check_token = None
    if command.args and command.args.startswith("ref_"):
        ref_value = command.args.split("ref_", 1)[1]
        if ref_value.isdigit():
            inviter_id = int(ref_value)
    elif command.args and command.args.startswith("check_"):
        gift_check_token = command.args.split("check_", 1)[1].strip().upper()
    await ensure_registered(message.from_user, invited_by=inviter_id)
    if not await is_user_subscribed(message.bot, message.from_user.id):
        await send_subscription_prompt(message.bot, message)
        return
    if gift_check_token:
        await start_gift_check_flow(message, state, gift_check_token)
        return
    await show_main_menu(message)


@router.message(Command("cancel"))
async def handle_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.")


@router.message(F.text == "Играть")
async def open_game_menu(message: Message) -> None:
    await ensure_registered(message.from_user)
    await show_game_menu(message)


@router.message(F.text == "Профиль")
async def open_profile_menu(message: Message) -> None:
    await ensure_registered(message.from_user)
    await show_profile(message)


@router.message(F.text == "Админ-панель")
async def open_admin_from_text(message: Message) -> None:
    if not await db.is_admin(message.from_user.id):
        await message.answer("Эта кнопка доступна только администраторам.")
        return
    await show_admin_panel(message)


@router.callback_query(F.data == "sub:check")
async def check_subscription(callback: CallbackQuery) -> None:
    if await is_user_subscribed(callback.bot, callback.from_user.id):
        await callback.answer("Подписка подтверждена")
        await send_main_menu(callback.message, callback.from_user.id)
        return
    await callback.answer("Подписка еще не найдена", show_alert=True)


@router.callback_query(F.data == "admin:open")
async def cb_admin_open(callback: CallbackQuery) -> None:
    if not await callback_admin_guard(callback):
        return
    await show_admin_panel(callback)


@router.callback_query(F.data == "game:open")
async def cb_open_game(callback: CallbackQuery) -> None:
    await show_game_menu(callback)


@router.callback_query(F.data == "profile:open")
async def cb_open_profile(callback: CallbackQuery) -> None:
    await show_profile(callback)


@router.callback_query(F.data == "rooms:list")
async def cb_list_rooms(callback: CallbackQuery) -> None:
    await show_rooms(callback)


@router.callback_query(F.data == "rooms:mine")
async def cb_my_rooms(callback: CallbackQuery) -> None:
    await show_my_rooms(callback)


@router.callback_query(F.data == "room:create")
async def cb_create_room(callback: CallbackQuery, state: FSMContext) -> None:
    user = await db.get_user(callback.from_user.id)
    min_room = await db.get_setting_float("min_room_amount", config.min_room_amount)
    default_amount = float(user["default_room_amount"]) if user else min_room
    await state.set_state(UserStates.room_amount)
    await callback.message.answer(
        "➕ <b>Создание комнаты</b>\n\n"
        f"Отправьте сумму комнаты одним сообщением.\nМинимум: <b>{fmt_amount(min_room)} {config.bot_asset}</b>\n"
        f"Ставка по умолчанию у вас сейчас: <b>{fmt_amount(default_amount)} {config.bot_asset}</b>"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("room:cancel:"))
async def cb_cancel_room(callback: CallbackQuery) -> None:
    room_id = int(callback.data.split(":")[-1])
    try:
        user = await db.cancel_room(room_id, callback.from_user.id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("Комната отменена")
    await callback.message.answer(
        f"❌ Комната #{room_id} отменена.\nБаланс: <b>{fmt_amount(float(user['balance']))} {config.bot_asset}</b>"
    )
    await send_log(
        callback.bot,
        "❌ <b>Комната отменена</b>\n"
        f"Комната: <code>#{room_id}</code>\n"
        f"Игрок: <code>{callback.from_user.id}</code>",
    )
    await show_my_rooms(callback)


@router.callback_query(F.data.startswith("room:join:"))
async def cb_join_room(callback: CallbackQuery) -> None:
    room_id = int(callback.data.split(":")[-1])
    creator_room = None
    for row in await db.list_open_rooms(limit=100):
        if int(row["id"]) == room_id:
            creator_room = row
            break
    if creator_room is None:
        await callback.answer("Комната уже недоступна", show_alert=True)
        await callback.message.answer("❌ Комната уже недоступна.")
        return
    if int(creator_room["creator_id"]) == callback.from_user.id:
        await callback.answer("Нельзя зайти в собственную комнату", show_alert=True)
        await callback.message.answer("❌ Нельзя заходить в собственную комнату.")
        return

    try:
        await db.ensure_room_joinable(room_id, callback.from_user.id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        await callback.message.answer(f"❌ {exc}")
        return

    await callback.answer("🎲 Запускаю реальные кубики Telegram...")

    creator_id = int(creator_room["creator_id"])
    opponent_id = callback.from_user.id
    creator_roll, opponent_roll = await roll_duel_round(callback.bot, creator_id, opponent_id, room_id)

    rerolls = 0
    while creator_roll == opponent_roll:
        rerolls += 1
        tie_text = f"🤝 Ничья в комнате #{room_id}: <b>{creator_roll}:{opponent_roll}</b>\nПеребрасываем кубики."
        await callback.bot.send_message(creator_id, tie_text)
        await callback.bot.send_message(opponent_id, tie_text)
        creator_roll, opponent_roll = await roll_duel_round(callback.bot, creator_id, opponent_id, room_id, reroll=True)

    try:
        result = await db.join_room(room_id, opponent_id, creator_roll, opponent_roll, rerolls)
    except ValueError as exc:
        await callback.message.answer(f"❌ {exc}")
        return

    room = result["room"]
    creator_roll = int(room["creator_roll"])
    opponent_roll = int(room["opponent_roll"])
    winner_id = int(result["winner_id"])
    winner_text = "создатель комнаты" if winner_id == creator_id else "вошедший игрок"
    rerolls = int(room["rerolls"])
    reroll_text = f"\n🔁 Перебросов: <b>{rerolls}</b>" if rerolls else ""
    duel_text = (
        f"{premium_emoji('win', '🏆')} <b>Дуэль завершена • Комната #{room_id}</b>\n\n"
        f"💵 Ставка: <b>{fmt_amount(float(room['amount']))} {config.bot_asset}</b>\n"
        f"1️⃣ Кубик создателя: <b>{creator_roll}</b>\n"
        f"2️⃣ Кубик вошедшего: <b>{opponent_roll}</b>{reroll_text}\n\n"
        f"👑 Победитель: <b>{winner_text}</b>\n"
        f"🎁 Приз: <b>{fmt_amount(float(result['winner_prize']))} {config.bot_asset}</b>\n"
        f"💼 Комиссия: <b>{fmt_amount(float(result['commission']))} {config.bot_asset}</b>"
    )
    creator_result = (
        f"{premium_emoji('win', '🏆')} <b>Ты выиграл!</b>\n\n"
        if winner_id == creator_id
        else f"{premium_emoji('lose', '💔')} <b>Ты проиграл</b>\n\n"
    )
    opponent_result = (
        f"{premium_emoji('win', '🏆')} <b>Ты выиграл!</b>\n\n"
        if winner_id == opponent_id
        else f"{premium_emoji('lose', '💔')} <b>Ты проиграл</b>\n\n"
    )

    with contextlib.suppress(Exception):
        await callback.bot.send_message(creator_id, creator_result + duel_text)
    with contextlib.suppress(Exception):
        await callback.bot.send_message(opponent_id, opponent_result + duel_text)

    await send_log(
        callback.bot,
        "🎲 <b>Завершена дуэль</b>\n"
        f"Комната: <code>#{room_id}</code>\n"
        f"Создатель: <code>{creator_id}</code> | Кубик: <b>{creator_roll}</b>\n"
        f"Игрок: <code>{opponent_id}</code> | Кубик: <b>{opponent_roll}</b>\n"
        f"Банк: <b>{fmt_amount(float(room['amount']) * 2)} {config.bot_asset}</b>\n"
        f"Комиссия: <b>{fmt_amount(float(result['commission']))} {config.bot_asset}</b>\n"
        f"Реф. выплата: <b>{fmt_amount(float(result['referral_reward']))} {config.bot_asset}</b>\n"
        f"Победитель: <code>{winner_id}</code>",
    )


@router.callback_query(F.data == "profile:deposit")
async def cb_deposit(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserStates.deposit_amount)
    minimum = await db.get_setting_float("min_deposit_amount", config.min_deposit_amount)
    await callback.message.answer(
        f"{premium_emoji('deposit', '💳')} <b>Пополнение</b>\n\n"
        f"Отправьте сумму пополнения.\nМинимум: <b>{fmt_amount(minimum)} {config.bot_asset}</b>"
    )
    await callback.answer()


@router.callback_query(F.data == "profile:withdraw")
async def cb_withdraw(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserStates.withdraw_amount)
    minimum = await db.get_setting_float("min_withdraw_amount", config.min_withdraw_amount)
    await callback.message.answer(
        f"{premium_emoji('withdraw', '💸')} <b>Вывод через CryptoBot чек</b>\n\n"
        f"Минимум: <b>{fmt_amount(minimum)} {config.bot_asset}</b>\n"
        "Отправьте сумму вывода."
    )
    await callback.answer()


@router.callback_query(F.data == "admin:withdraw_mode")
async def cb_admin_withdraw_mode(callback: CallbackQuery) -> None:
    if not await callback_admin_guard(callback):
        return
    auto_enabled = await db.get_setting("withdraw_auto_enabled", "1") == "1"
    await safe_edit(callback.message, await render_withdraw_mode_text(), reply_markup=withdraw_mode_keyboard(auto_enabled))
    await callback.answer()


@router.callback_query(F.data == "withdrawmode:toggle")
async def cb_toggle_withdraw_mode(callback: CallbackQuery) -> None:
    if not await callback_admin_guard(callback):
        return
    current = await db.get_setting("withdraw_auto_enabled", "1")
    new_value = "0" if current == "1" else "1"
    await db.set_setting("withdraw_auto_enabled", new_value)
    await safe_edit(callback.message, await render_withdraw_mode_text(), reply_markup=withdraw_mode_keyboard(new_value == "1"))
    await send_log(
        callback.bot,
        f"⚙️ Режим вывода изменен админом <code>{callback.from_user.id}</code>: <b>{'авто' if new_value == '1' else 'ручная модерация'}</b>",
    )
    await callback.answer("Режим вывода обновлен")


@router.callback_query(F.data.startswith("withdraw:approve:"))
async def cb_withdraw_approve(callback: CallbackQuery) -> None:
    if not await callback_admin_guard(callback):
        return
    request_id = callback.data.split(":")[-1]
    withdrawal = await db.get_withdrawal(request_id)
    if not withdrawal:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    if withdrawal["status"] != "pending":
        await callback.answer("Заявка уже обработана", show_alert=True)
        return
    if not config.crypto_pay_token:
        await callback.answer("CRYPTO_PAY_TOKEN не задан", show_alert=True)
        return
    try:
        check = await crypto.create_check(float(withdrawal["amount"]), str(withdrawal["asset"]))
        provider_check_id = str(check.get("check_id") or check.get("id"))
        check_url = check.get("bot_check_url") or check.get("url") or "https://t.me/CryptoBot"
    except CryptoPayError as exc:
        if is_empty_treasury_error(str(exc)):
            await callback.answer("Казна пуста ей скоро пополнят", show_alert=True)
            return
        await callback.answer(f"Ошибка CryptoBot: {exc}", show_alert=True)
        return
    updated_user, updated_withdrawal = await db.approve_withdraw_request(request_id, provider_check_id, check_url, callback.from_user.id)
    user_row = await db.get_user(int(updated_withdrawal["user_id"]))
    await callback.message.edit_text(
        render_withdraw_request_text(updated_withdrawal, user_row, "одобрено"),
        reply_markup=None,
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="Забрать чек", url=check_url, icon_custom_emoji_id=premium_button_icon("withdraw_button"))
    builder.adjust(1)
    with contextlib.suppress(Exception):
        await callback.bot.send_message(
            int(updated_withdrawal["user_id"]),
            f"{premium_emoji('win', '✅')} <b>Вывод одобрен админом</b>\n\n"
            f"Сумма: <b>{fmt_amount(float(updated_withdrawal['amount']))} {updated_withdrawal['asset']}</b>\n"
            f"Текущий баланс: <b>{fmt_amount(float(updated_user['balance']))} {updated_withdrawal['asset']}</b>",
            reply_markup=builder.as_markup(),
        )
    await callback.answer("Вывод одобрен")


@router.callback_query(F.data.startswith("withdraw:reject:"))
async def cb_withdraw_reject(callback: CallbackQuery) -> None:
    if not await callback_admin_guard(callback):
        return
    request_id = callback.data.split(":")[-1]
    try:
        updated_user, updated_withdrawal = await db.reject_withdraw_request(request_id, callback.from_user.id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    user_row = await db.get_user(int(updated_withdrawal["user_id"]))
    await callback.message.edit_text(
        render_withdraw_request_text(updated_withdrawal, user_row, "отклонено"),
        reply_markup=None,
    )
    with contextlib.suppress(Exception):
        await callback.bot.send_message(
            int(updated_withdrawal["user_id"]),
            f"{premium_emoji('lose', '❌')} <b>Вывод отклонен</b>\n\n"
            f"Сумма возвращена на баланс: <b>{fmt_amount(float(updated_withdrawal['amount']))} {updated_withdrawal['asset']}</b>\n"
            f"Текущий баланс: <b>{fmt_amount(float(updated_user['balance']))} {updated_withdrawal['asset']}</b>",
        )
    await callback.answer("Вывод отклонен")


@router.callback_query(F.data == "settings:default_room")
async def cb_default_room(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserStates.default_room_amount)
    await callback.message.answer("Отправьте новую ставку комнаты по умолчанию одним сообщением.")
    await callback.answer()


@router.callback_query(F.data == "profile:referrals")
async def cb_referrals(callback: CallbackQuery) -> None:
    summary = await db.get_referral_summary(callback.from_user.id)
    text = (
        "🤝 <b>Реферальная система</b>\n\n"
        f"Ваша ссылка:\n<code>https://t.me/{bot_username}?start=ref_{callback.from_user.id}</code>\n\n"
        f"Приглашено игроков: <b>{summary['referred_count']}</b>\n"
        f"Реферальных выплат: <b>{summary['rewards_count']}</b>\n"
        f"Заработано: <b>{fmt_amount(summary['rewards_sum'])} {config.bot_asset}</b>\n"
        f"Текущий процент от проигрышной ставки: <b>{fmt_amount(await db.get_setting_float('referral_percent', config.referral_percent))}%</b>"
    )
    await safe_edit(callback.message, text, reply_markup=referrals_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admin:create_check")
async def cb_admin_create_check(callback: CallbackQuery, state: FSMContext) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(UserStates.admin_check_main)
    await callback.message.answer(
        "🎟 <b>Создание чека</b>\n\n"
        "Отправьте сумму и количество активаций в одном сообщении.\n"
        "Пример: <code>5 25</code>"
    )
    await callback.answer()


@router.callback_query(F.data == "admin:checks")
async def cb_admin_checks(callback: CallbackQuery) -> None:
    if not await callback_admin_guard(callback):
        return
    await show_gift_checks_list(callback)


@router.callback_query(F.data.startswith("giftcheck:view:"))
async def cb_gift_check_view(callback: CallbackQuery) -> None:
    if not await callback_admin_guard(callback):
        return
    token = callback.data.split(":")[-1].strip().upper()
    gift_check = await db.get_gift_check(token)
    if not gift_check:
        await callback.answer("Чек не найден", show_alert=True)
        await show_gift_checks_list(callback)
        return
    text = (
        "🎟 <b>Карточка чека</b>\n\n"
        f"{render_gift_check_text(gift_check)}\n\n"
        f"🔑 Токен: <code>{gift_check['token']}</code>\n"
        f"👮 Создал: <code>{gift_check['created_by']}</code>"
    )
    await safe_edit(callback.message, text, reply_markup=gift_check_manage_keyboard(token))
    await callback.answer()


@router.callback_query(F.data.startswith("giftcheck:delete:"))
async def cb_gift_check_delete(callback: CallbackQuery) -> None:
    if not await callback_admin_guard(callback):
        return
    token = callback.data.split(":")[-1].strip().upper()
    deleted = await db.delete_gift_check(token)
    if not deleted:
        await callback.answer("Чек уже удален", show_alert=True)
        await show_gift_checks_list(callback)
        return
    await send_log(
        callback.bot,
        "🗑 <b>Чек удален</b>\n"
        f"👮 Админ: <code>{callback.from_user.id}</code>\n"
        f"🔑 Токен: <code>{deleted['token']}</code>\n"
        f"💰 Сумма: <b>{fmt_amount(float(deleted['amount']))} {config.bot_asset}</b>",
    )
    await callback.answer("Чек удален")
    await show_gift_checks_list(callback)


@router.callback_query(F.data.startswith("giftcheck:publish:"))
async def cb_publish_gift_check(callback: CallbackQuery, state: FSMContext) -> None:
    if not await callback_admin_guard(callback):
        return
    token = callback.data.split(":")[-1].strip().upper()
    gift_check = await db.get_gift_check(token)
    if not gift_check:
        await callback.answer("Чек не найден", show_alert=True)
        return
    await state.set_state(UserStates.admin_check_publish_channel)
    await state.update_data(publish_gift_check_token=token)
    await callback.message.answer(
        "📣 <b>Публикация чека</b>\n\n"
        "Отправьте @username канала или chat_id канала, куда нужно опубликовать чек."
    )
    await callback.answer()


@router.callback_query(F.data.startswith("giftchecksub:"))
async def cb_gift_check_subscription(callback: CallbackQuery, state: FSMContext) -> None:
    token = callback.data.split(":")[-1].strip().upper()
    await callback.answer("Проверяю подписку...")
    await start_gift_check_flow(callback.message, state, token)


@router.callback_query(F.data.startswith("giftcheckskip:"))
async def cb_gift_check_skip(callback: CallbackQuery, state: FSMContext) -> None:
    if not await callback_admin_guard(callback):
        return
    step = callback.data.split(":")[-1]
    if step == "description":
        await state.update_data(check_description=None)
        await ask_check_password_step(callback.message, state)
    elif step == "password":
        await state.update_data(check_password=None)
        await ask_check_required_deposit_step(callback.message, state)
    elif step == "deposit":
        await state.update_data(check_required_deposit=0.0)
        await ask_check_image_step(callback.message, state)
    elif step == "image":
        await ask_check_channels_step(callback.message, state, photo_file_id=None)
    elif step == "channels":
        await state.update_data(check_required_channels="")
        await finalize_gift_check_creation(callback.message, state)
    await callback.answer("Пропущено")


@router.callback_query(F.data == "admin:house_commission")
async def cb_admin_house_commission(callback: CallbackQuery, state: FSMContext) -> None:
    if not await callback_admin_guard(callback):
        return
    current = await db.get_setting_float("house_commission_percent", config.house_commission_percent)
    await state.set_state(UserStates.admin_house_commission)
    await callback.message.answer(
        "💼 <b>Комиссия проекта</b>\n\n"
        f"Текущее значение: <b>{fmt_amount(current)}%</b>\n"
        "Отправьте новый процент комиссии со ставки, например <code>1</code> или <code>2.5</code>."
    )
    await callback.answer()


@router.callback_query(F.data == "admin:broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not await callback_admin_guard(callback):
        return
    await state.set_state(UserStates.admin_broadcast)
    await callback.message.answer(
        "📣 <b>Рассылка</b>\n\n"
        "Отправьте текст рассылки одним сообщением.\n"
        "Сообщение уйдет всем пользователям, которые запускали бота."
    )
    await callback.answer()


@router.callback_query(F.data == "admin:refs_top")
async def cb_admin_refs_top(callback: CallbackQuery) -> None:
    if not await callback_admin_guard(callback):
        return
    leaders = await db.get_referrals_top(limit=10)
    if not leaders:
        text = "🏆 <b>Топ по рефам</b>\n\nПока в топе никого нет."
    else:
        lines = ["🏆 <b>Топ по рефам</b>", ""]
        for index, row in enumerate(leaders, start=1):
            name = f"@{row['username']}" if row["username"] else row["full_name"]
            lines.append(
                f"{index}. <b>{name}</b>\n"
                f"👥 Рефералов: <b>{int(row['referred_count'])}</b>\n"
                f"🎁 Доход: <b>{fmt_amount(float(row['rewards_sum']))} {config.bot_asset}</b>"
            )
        text = "\n\n".join(lines)
    await safe_edit(callback.message, text, reply_markup=admin_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admin:min_room")
async def cb_admin_min_room(callback: CallbackQuery, state: FSMContext) -> None:
    if not await callback_admin_guard(callback):
        return
    await state.set_state(UserStates.admin_min_room)
    await callback.message.answer("Введите новую минимальную сумму комнаты.")
    await callback.answer()


@router.callback_query(F.data == "admin:min_deposit")
async def cb_admin_min_deposit(callback: CallbackQuery, state: FSMContext) -> None:
    if not await callback_admin_guard(callback):
        return
    await state.set_state(UserStates.admin_min_deposit)
    await callback.message.answer("Введите новую минимальную сумму пополнения.")
    await callback.answer()


@router.callback_query(F.data == "admin:min_withdraw")
async def cb_admin_min_withdraw(callback: CallbackQuery, state: FSMContext) -> None:
    if not await callback_admin_guard(callback):
        return
    await state.set_state(UserStates.admin_min_withdraw)
    await callback.message.answer("Введите новую минимальную сумму вывода.")
    await callback.answer()


@router.callback_query(F.data == "admin:ref_percent")
async def cb_admin_ref_percent(callback: CallbackQuery, state: FSMContext) -> None:
    if not await callback_admin_guard(callback):
        return
    await state.set_state(UserStates.admin_ref_percent)
    await callback.message.answer("Введите новый реферальный процент, например <code>0.01</code>.")
    await callback.answer()


@router.callback_query(F.data == "admin:add_admin")
async def cb_admin_add_admin(callback: CallbackQuery, state: FSMContext) -> None:
    if not await callback_admin_guard(callback):
        return
    await state.set_state(UserStates.admin_add_admin)
    await callback.message.answer("Введите ID пользователя или @username, которого нужно сделать админом.")
    await callback.answer()


@router.callback_query(F.data == "admin:add_balance")
async def cb_admin_add_balance(callback: CallbackQuery, state: FSMContext) -> None:
    if not await callback_admin_guard(callback):
        return
    await state.set_state(UserStates.admin_add_balance)
    await callback.message.answer("Введите <code>@username 5</code> или <code>123456789 5</code>.")
    await callback.answer()


@router.callback_query(F.data == "admin:take_balance")
async def cb_admin_take_balance(callback: CallbackQuery, state: FSMContext) -> None:
    if not await callback_admin_guard(callback):
        return
    await state.set_state(UserStates.admin_take_balance)
    await callback.message.answer("Введите <code>@username 5</code> или <code>123456789 5</code>.")
    await callback.answer()


@router.callback_query(F.data == "admin:force_sub")
async def cb_admin_force_sub(callback: CallbackQuery, state: FSMContext) -> None:
    if not await callback_admin_guard(callback):
        return
    await state.set_state(UserStates.admin_force_sub)
    await callback.message.answer("Введите @username канала или chat_id канала. Для отключения напишите <code>off</code>.")
    await callback.answer()


@router.callback_query(F.data == "admin:log_chat")
async def cb_admin_log_chat(callback: CallbackQuery, state: FSMContext) -> None:
    if not await callback_admin_guard(callback):
        return
    await state.set_state(UserStates.admin_log_chat)
    await callback.message.answer("Введите chat_id приватной группы для логов. Для отключения напишите <code>off</code>.")
    await callback.answer()


@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(callback: CallbackQuery) -> None:
    if not await callback_admin_guard(callback):
        return
    stats = await db.get_stats()
    text = (
        "📊 <b>Статистика проекта</b>\n\n"
        f"Пользователей: <b>{stats['users']}</b>\n"
        f"Открытых комнат: <b>{stats['open_rooms']}</b>\n"
        f"Сыгранных комнат: <b>{stats['finished_rooms']}</b>\n"
        f"Пополнено: <b>{fmt_amount(stats['deposits'])} {config.bot_asset}</b>\n"
        f"Выведено: <b>{fmt_amount(stats['withdrawals'])} {config.bot_asset}</b>"
    )
    await safe_edit(callback.message, text, reply_markup=admin_keyboard())
    await callback.answer()


@router.message(UserStates.deposit_amount)
async def state_deposit_amount(message: Message, state: FSMContext) -> None:
    minimum = await db.get_setting_float("min_deposit_amount", config.min_deposit_amount)
    try:
        amount = parse_amount(message.text)
    except Exception:
        await message.answer("Введите корректную сумму числом.")
        return
    if amount < minimum:
        await message.answer(f"Минимум для пополнения: {fmt_amount(minimum)} {config.bot_asset}")
        return
    if not config.crypto_pay_token:
        await message.answer("CRYPTO_PAY_TOKEN не задан, поэтому авто-пополнение пока недоступно.")
        await state.clear()
        return
    try:
        invoice = await crypto.create_invoice(amount, config.bot_asset, f"user:{message.from_user.id}")
    except CryptoPayError as exc:
        await message.answer(f"Не удалось создать инвойс: <code>{exc}</code>")
        await state.clear()
        return
    pay_url = (
        invoice.get("bot_invoice_url")
        or invoice.get("mini_app_invoice_url")
        or invoice.get("pay_url")
        or invoice.get("web_app_invoice_url")
        or "https://t.me/CryptoBot"
    )
    invoice_id = str(invoice.get("invoice_id") or invoice.get("id"))
    await db.create_invoice(invoice_id, message.from_user.id, amount, config.bot_asset, pay_url, f"user:{message.from_user.id}")
    builder = InlineKeyboardBuilder()
    builder.button(text="Оплатить счет", url=pay_url, icon_custom_emoji_id=premium_button_icon("deposit_button"))
    builder.adjust(1)
    await message.answer(
        "💳 <b>Инвойс создан</b>\n\n"
        f"Сумма: <b>{fmt_amount(amount)} {config.bot_asset}</b>\n"
        "После оплаты баланс обновится автоматически.",
        reply_markup=builder.as_markup(),
    )
    await state.clear()


@router.message(UserStates.withdraw_amount)
async def state_withdraw_amount(message: Message, state: FSMContext) -> None:
    minimum = await db.get_setting_float("min_withdraw_amount", config.min_withdraw_amount)
    user = await db.get_user(message.from_user.id)
    try:
        amount = parse_amount(message.text)
    except Exception:
        await message.answer("Введите корректную сумму числом.")
        return
    if amount < minimum:
        await message.answer(f"Минимум для вывода: {fmt_amount(minimum)} {config.bot_asset}")
        return
    if not user or float(user["balance"]) < amount:
        await message.answer("Недостаточно средств на балансе.")
        return
    auto_enabled = await db.get_setting("withdraw_auto_enabled", "1")
    if auto_enabled == "1":
        if not config.crypto_pay_token:
            await message.answer("CRYPTO_PAY_TOKEN не задан, поэтому авто-вывод пока недоступен.")
            await state.clear()
            return
        try:
            check = await crypto.create_check(amount, config.bot_asset)
            check_id = str(check.get("check_id") or check.get("id"))
            check_url = check.get("bot_check_url") or check.get("url") or "https://t.me/CryptoBot"
            updated = await db.create_withdrawal(check_id, message.from_user.id, amount, config.bot_asset, check_url)
        except CryptoPayError as exc:
            if is_empty_treasury_error(str(exc)):
                await message.answer(
                    "🏦 <b>Казна пуста</b>\n\n"
                    "Казна пуста ей скоро пополнят."
                )
                await send_log(
                    message.bot,
                    "🚨 <b>Казна пуста</b>\n"
                    "CryptoBot не смог создать чек из-за нехватки средств.\n\n"
                    f"👤 Пользователь: <code>{message.from_user.id}</code>\n"
                    f"💸 Запрошено: <b>{fmt_amount(amount)} {config.bot_asset}</b>",
                )
                await state.clear()
                return
            await message.answer(f"Не удалось создать чек: <code>{exc}</code>")
            await state.clear()
            return
        except ValueError as exc:
            await message.answer(f"Не удалось создать чек: <code>{exc}</code>")
            await state.clear()
            return
        builder = InlineKeyboardBuilder()
        builder.button(text="Забрать чек", url=check_url, icon_custom_emoji_id=premium_button_icon("withdraw_button"))
        builder.adjust(1)
        await message.answer(
            f"{premium_emoji('withdraw', '💸')} <b>Чек создан</b>\n\n"
            f"Сумма: <b>{fmt_amount(amount)} {config.bot_asset}</b>\n"
            f"Остаток на балансе: <b>{fmt_amount(float(updated['balance']))} {config.bot_asset}</b>",
            reply_markup=builder.as_markup(),
        )
        await send_log(
            message.bot,
            "💸 <b>Вывод</b>\n"
            f"Пользователь: <code>{message.from_user.id}</code>\n"
            f"Сумма: <b>{fmt_amount(amount)} {config.bot_asset}</b>\n"
            f"Чек: <code>{check_id}</code>",
        )
    else:
        log_chat_id = await db.get_setting("log_chat_id", "")
        if not log_chat_id:
            await message.answer("Лог-чат для ручной модерации не настроен. Обратитесь к администратору.")
            await state.clear()
            return
        request_id = secrets.token_hex(6).upper()
        try:
            updated, withdrawal = await db.create_withdraw_request(request_id, message.from_user.id, amount, config.bot_asset)
        except ValueError as exc:
            await message.answer(f"Не удалось создать заявку: <code>{exc}</code>")
            await state.clear()
            return
        await message.answer(
            f"{premium_emoji('withdraw', '💸')} <b>Заявка на вывод создана</b>\n\n"
            f"Сумма: <b>{fmt_amount(amount)} {config.bot_asset}</b>\n"
            f"Статус: <b>ожидает решения администратора</b>\n"
            f"Остаток на балансе: <b>{fmt_amount(float(updated['balance']))} {config.bot_asset}</b>"
        )
        await message.bot.send_message(
            int(log_chat_id),
            render_withdraw_request_text(withdrawal, updated, "ожидает решения"),
            reply_markup=withdraw_request_keyboard(request_id),
        )
    await state.clear()


@router.message(UserStates.room_amount)
async def state_room_amount(message: Message, state: FSMContext) -> None:
    minimum = await db.get_setting_float("min_room_amount", config.min_room_amount)
    try:
        amount = parse_amount(message.text)
    except Exception:
        await message.answer("Введите корректную сумму комнаты.")
        return
    if amount < minimum:
        await message.answer(f"Минимальная сумма комнаты: {fmt_amount(minimum)} {config.bot_asset}")
        return
    try:
        room = await db.create_room(message.from_user.id, amount)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await send_log(
        message.bot,
        "➕ <b>Создана комната</b>\n"
        f"Комната: <code>#{room['id']}</code>\n"
        f"Создатель: <code>{message.from_user.id}</code>\n"
        f"Ставка: <b>{fmt_amount(amount)} {config.bot_asset}</b>",
    )
    await message.answer(
        "✅ <b>Комната создана</b>\n\n"
        f"Комната <b>#{room['id']}</b> появилась в общем списке.\n"
        f"Ставка: <b>{fmt_amount(amount)} {config.bot_asset}</b>\n"
        "Как только соперник зайдет, бот сразу бросит кубики.",
        reply_markup=game_menu_keyboard(),
    )
    await state.clear()


@router.message(UserStates.user_check_password)
async def state_user_check_password(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    token = str(data.get("gift_check_token", "")).strip().upper()
    if not token:
        await message.answer("❌ Чек не найден.")
        await state.clear()
        return
    await claim_gift_check_for_user(message, token, password=(message.text or "").strip())
    await state.clear()


@router.message(UserStates.admin_check_main)
async def state_admin_check_main(message: Message, state: FSMContext) -> None:
    if not await admin_guard(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Формат: <code>5 25</code>")
        return
    try:
        amount = parse_amount(parts[0])
        activations = int(parts[1])
        if activations <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Проверьте сумму и количество активаций.")
        return
    await state.update_data(check_amount=amount, check_activations=activations)
    await state.set_state(UserStates.admin_check_description)
    await message.answer(
        "📝 <b>Описание чека</b>\n\n"
        "Отправьте описание для чека или нажмите кнопку ниже.",
        reply_markup=check_skip_keyboard("description"),
    )


@router.message(UserStates.admin_check_description)
async def state_admin_check_description(message: Message, state: FSMContext) -> None:
    if not await admin_guard(message):
        return
    description = (message.text or "").strip()
    await state.update_data(check_description=description or None)
    await ask_check_password_step(message, state)


@router.message(UserStates.admin_check_password)
async def state_admin_check_password(message: Message, state: FSMContext) -> None:
    if not await admin_guard(message):
        return
    password = (message.text or "").strip()
    await state.update_data(check_password=password or None)
    await ask_check_required_deposit_step(message, state)


@router.message(UserStates.admin_check_required_deposit)
async def state_admin_check_required_deposit(message: Message, state: FSMContext) -> None:
    if not await admin_guard(message):
        return
    try:
        amount = parse_amount(message.text)
    except Exception:
        await message.answer("Введите корректную сумму депозита числом.")
        return
    await state.update_data(check_required_deposit=amount)
    await ask_check_image_step(message, state)


@router.message(UserStates.admin_check_image, F.photo)
async def state_admin_check_image_photo(message: Message, state: FSMContext) -> None:
    if not await admin_guard(message):
        return
    await ask_check_channels_step(message, state, photo_file_id=message.photo[-1].file_id)


@router.message(UserStates.admin_check_image)
async def state_admin_check_image_text(message: Message, state: FSMContext) -> None:
    if not await admin_guard(message):
        return
    await message.answer("Отправьте картинку или нажмите кнопку `Пропустить`.", reply_markup=check_skip_keyboard("image"))


@router.message(UserStates.admin_check_channels)
async def state_admin_check_channels(message: Message, state: FSMContext) -> None:
    if not await admin_guard(message):
        return
    channels = parse_gift_check_channels(message.text or "")
    if not channels:
        await message.answer(
            "Укажите хотя бы один корректный @username канала или публичную ссылку, либо нажмите `Пропустить`.",
            reply_markup=check_skip_keyboard("channels"),
        )
        return
    await state.update_data(check_required_channels="\n".join(channels))
    await finalize_gift_check_creation(message, state)


@router.message(UserStates.default_room_amount)
async def state_default_room(message: Message, state: FSMContext) -> None:
    minimum = await db.get_setting_float("min_room_amount", config.min_room_amount)
    try:
        amount = parse_amount(message.text)
    except Exception:
        await message.answer("Введите корректную сумму.")
        return
    if amount < minimum:
        await message.answer(f"Ставка по умолчанию должна быть не меньше {fmt_amount(minimum)} {config.bot_asset}")
        return
    await db.set_default_room_amount(message.from_user.id, amount)
    await message.answer(f"Ставка по умолчанию обновлена: <b>{fmt_amount(amount)} {config.bot_asset}</b>")
    await state.clear()


async def admin_guard(message: Message) -> bool:
    if not await db.is_admin(message.from_user.id):
        await message.answer("Эта функция только для админов.")
        return False
    return True


@router.message(UserStates.admin_check_publish_channel)
async def state_admin_check_publish_channel(message: Message, state: FSMContext) -> None:
    if not await admin_guard(message):
        return
    data = await state.get_data()
    token = str(data.get("publish_gift_check_token", "")).strip().upper()
    gift_check = await db.get_gift_check(token)
    if not gift_check:
        await message.answer("❌ Чек не найден.")
        await state.clear()
        return
    target = (message.text or "").strip()
    try:
        publish_target: int | str = int(target) if target.lstrip("-").isdigit() else target
        await send_gift_check_post(message.bot, publish_target, gift_check)
    except Exception as exc:
        await message.answer(f"Не удалось опубликовать чек: <code>{exc}</code>")
        return
    await message.answer(
        "📣 <b>Чек опубликован</b>\n\n"
        f"🔑 Токен: <code>{gift_check['token']}</code>\n"
        f"🔗 Ссылка: <code>{gift_check_link(gift_check['token'])}</code>"
    )
    await send_log(
        message.bot,
        "📣 <b>Чек опубликован в канал</b>\n"
        f"👮 Админ: <code>{message.from_user.id}</code>\n"
        f"🔑 Токен: <code>{gift_check['token']}</code>\n"
        f"📍 Канал: <code>{target}</code>",
    )
    await state.clear()


async def apply_setting_amount(message: Message, state: FSMContext, key: str, label: str) -> None:
    if not await admin_guard(message):
        return
    try:
        amount = parse_amount(message.text)
    except Exception:
        await message.answer("Введите корректное число.")
        return
    await db.set_setting(key, fmt_amount(amount))
    await message.answer(f"{label} обновлена: <b>{fmt_amount(amount)} {config.bot_asset}</b>")
    await send_log(message.bot, f"⚙️ {label}: <b>{fmt_amount(amount)} {config.bot_asset}</b> админом <code>{message.from_user.id}</code>")
    await state.clear()


@router.message(UserStates.admin_house_commission)
async def state_admin_house_commission(message: Message, state: FSMContext) -> None:
    if not await admin_guard(message):
        return
    try:
        amount = parse_amount(message.text)
    except Exception:
        await message.answer("Введите корректный процент числом.")
        return
    await db.set_setting("house_commission_percent", fmt_amount(amount))
    await message.answer(f"💼 Комиссия проекта обновлена: <b>{fmt_amount(amount)}%</b>")
    await send_log(
        message.bot,
        f"💼 Комиссия проекта: <b>{fmt_amount(amount)}%</b> изменена админом <code>{message.from_user.id}</code>",
    )
    await state.clear()


@router.message(UserStates.admin_broadcast)
async def state_admin_broadcast(message: Message, state: FSMContext) -> None:
    if not await admin_guard(message):
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Отправьте текст рассылки одним сообщением.")
        return
    user_ids = await db.list_user_ids()
    sent = 0
    failed = 0
    for user_id in user_ids:
        try:
            await message.bot.send_message(user_id, f"📣 <b>Рассылка</b>\n\n{text}")
            sent += 1
        except Exception:
            failed += 1
    await message.answer(
        "📣 <b>Рассылка завершена</b>\n\n"
        f"✅ Доставлено: <b>{sent}</b>\n"
        f"❌ Ошибок: <b>{failed}</b>"
    )
    await send_log(
        message.bot,
        "📣 <b>Админ-рассылка</b>\n"
        f"👮 Админ: <code>{message.from_user.id}</code>\n"
        f"✅ Доставлено: <b>{sent}</b>\n"
        f"❌ Ошибок: <b>{failed}</b>",
    )
    await state.clear()


@router.message(UserStates.admin_min_room)
async def state_admin_min_room(message: Message, state: FSMContext) -> None:
    await apply_setting_amount(message, state, "min_room_amount", "Минимальная сумма комнаты")


@router.message(UserStates.admin_min_deposit)
async def state_admin_min_deposit(message: Message, state: FSMContext) -> None:
    await apply_setting_amount(message, state, "min_deposit_amount", "Минимальная сумма пополнения")


@router.message(UserStates.admin_min_withdraw)
async def state_admin_min_withdraw(message: Message, state: FSMContext) -> None:
    await apply_setting_amount(message, state, "min_withdraw_amount", "Минимальная сумма вывода")


@router.message(UserStates.admin_ref_percent)
async def state_admin_ref_percent(message: Message, state: FSMContext) -> None:
    if not await admin_guard(message):
        return
    try:
        percent = parse_amount(message.text)
    except Exception:
        await message.answer("Введите корректное число.")
        return
    await db.set_setting("referral_percent", fmt_amount(percent))
    await message.answer(f"Реферальный процент обновлен: <b>{fmt_amount(percent)}%</b>")
    await send_log(message.bot, f"⚙️ Реферальный процент: <b>{fmt_amount(percent)}%</b> админом <code>{message.from_user.id}</code>")
    await state.clear()


@router.message(UserStates.admin_add_admin)
async def state_admin_add_admin(message: Message, state: FSMContext) -> None:
    if not await admin_guard(message):
        return
    target = await db.get_user_by_id_or_username(message.text)
    if not target:
        await message.answer("Пользователь не найден в базе. Пусть сначала запустит бота.")
        return
    await db.add_admin(int(target["user_id"]), message.from_user.id)
    await message.answer(f"Пользователь <code>{target['user_id']}</code> добавлен в админы.")
    await send_log(message.bot, f"👮 Новый админ: <code>{target['user_id']}</code> добавлен админом <code>{message.from_user.id}</code>")
    await state.clear()


async def apply_balance_adjust(message: Message, state: FSMContext, subtract: bool) -> None:
    if not await admin_guard(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Формат: <code>@username 5</code> или <code>123456789 5</code>")
        return
    target = await db.get_user_by_id_or_username(parts[0])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    try:
        amount = parse_amount(parts[1])
    except Exception:
        await message.answer("Некорректная сумма.")
        return
    try:
        if subtract:
            updated = await db.subtract_balance(int(target["user_id"]), amount, "admin_take", f"admin:{message.from_user.id}")
            action = "снял"
        else:
            updated = await db.add_balance(int(target["user_id"]), amount, "admin_add", f"admin:{message.from_user.id}")
            action = "добавил"
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await message.answer(
        f"Баланс обновлен.\n"
        f"Пользователь: <code>{target['user_id']}</code>\n"
        f"Новый баланс: <b>{fmt_amount(float(updated['balance']))} {config.bot_asset}</b>"
    )
    await send_log(
        message.bot,
        "💼 <b>Коррекция баланса</b>\n"
        f"Админ: <code>{message.from_user.id}</code>\n"
        f"Пользователь: <code>{target['user_id']}</code>\n"
        f"Действие: {action}\n"
        f"Сумма: <b>{fmt_amount(amount)} {config.bot_asset}</b>",
    )
    await state.clear()


@router.message(UserStates.admin_add_balance)
async def state_admin_add_balance(message: Message, state: FSMContext) -> None:
    await apply_balance_adjust(message, state, subtract=False)


@router.message(UserStates.admin_take_balance)
async def state_admin_take_balance(message: Message, state: FSMContext) -> None:
    await apply_balance_adjust(message, state, subtract=True)


@router.message(UserStates.admin_force_sub)
async def state_admin_force_sub(message: Message, state: FSMContext) -> None:
    if not await admin_guard(message):
        return
    raw = (message.text or "").strip()
    if raw.lower() == "off":
        await db.set_setting("force_sub_chat_id", "")
        await db.set_setting("force_sub_chat_username", "")
        await message.answer("Обязательная подписка отключена.")
        await state.clear()
        return
    if raw.startswith("@"):
        await db.set_setting("force_sub_chat_username", raw)
        await db.set_setting("force_sub_chat_id", "")
    else:
        await db.set_setting("force_sub_chat_id", raw)
        await db.set_setting("force_sub_chat_username", "")
    await message.answer("Настройка обязательной подписки обновлена.")
    await send_log(message.bot, f"📡 Канал обязательной подписки обновлен админом <code>{message.from_user.id}</code>: <code>{raw}</code>")
    await state.clear()


@router.message(UserStates.admin_log_chat)
async def state_admin_log_chat(message: Message, state: FSMContext) -> None:
    if not await admin_guard(message):
        return
    raw = (message.text or "").strip()
    if raw.lower() == "off":
        await db.set_setting("log_chat_id", "")
        await message.answer("Лог-чат отключен.")
        await state.clear()
        return
    await db.set_setting("log_chat_id", raw)
    await message.answer("Лог-чат обновлен.")
    await state.clear()


async def on_startup(bot: Bot) -> None:
    global bot_username
    me = await bot.get_me()
    bot_username = me.username or "your_bot"
    commands = [
        BotCommand(command="start", description="Открыть главное меню"),
        BotCommand(command="cancel", description="Отменить текущее действие"),
    ]
    await bot.set_my_commands(commands)


async def main() -> None:
    global config, db, crypto
    config = Config.from_env()
    db = Database(config.db_path, config)
    await db.init()
    crypto = CryptoBotClient(config.crypto_pay_token, config.crypto_pay_base_url)

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    router.message.middleware(SubscriptionMiddleware())
    router.callback_query.middleware(SubscriptionMiddleware())
    dp.include_router(router)

    await on_startup(bot)
    worker_task = asyncio.create_task(invoice_worker(bot))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        await crypto.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
