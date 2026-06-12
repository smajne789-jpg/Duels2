from __future__ import annotations

import asyncio
import contextlib
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


class UserStates(StatesGroup):
    deposit_amount = State()
    withdraw_amount = State()
    room_amount = State()
    promo_code = State()
    default_room_amount = State()
    admin_promo = State()
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


def main_menu_keyboard(is_admin: bool) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="🎲 Играть"),
        KeyboardButton(text="👤 Профиль"),
    )
    builder.row(
        KeyboardButton(text="🎁 Промокод"),
        KeyboardButton(text="ℹ️ Помощь"),
    )
    if is_admin:
        builder.row(KeyboardButton(text="🛡 Админ-панель"))
    return builder.as_markup(resize_keyboard=True)


def game_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Создать комнату", callback_data="room:create")
    builder.button(text="📋 Список комнат", callback_data="rooms:list")
    builder.button(text="🧾 Мои комнаты", callback_data="rooms:mine")
    builder.adjust(1)
    return builder.as_markup()


def profile_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Пополнить", callback_data="profile:deposit")
    builder.button(text="💸 Вывести", callback_data="profile:withdraw")
    builder.button(text="⚙️ Настройки", callback_data="profile:settings")
    builder.button(text="🤝 Рефералы", callback_data="profile:referrals")
    builder.adjust(2, 2)
    return builder.as_markup()


def profile_settings_keyboard(auto_withdraw_enabled: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    label = "✅ Автовывод включен" if auto_withdraw_enabled else "❌ Автовывод выключен"
    builder.button(text=label, callback_data="settings:toggle_withdraw")
    builder.button(text="🎯 Ставка по умолчанию", callback_data="settings:default_room")
    builder.button(text="🔙 Назад", callback_data="profile:open")
    builder.adjust(1)
    return builder.as_markup()


def rooms_keyboard(rooms: list[RoomRender], current_user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for room in rooms:
        creator_title = "Вы" if room.creator_id == current_user_id else room.creator_name
        builder.button(
            text=f"🎲 #{room.room_id} • {fmt_amount(room.amount)} {config.bot_asset} • {creator_title}",
            callback_data=f"room:join:{room.room_id}",
        )
    builder.button(text="🔄 Обновить", callback_data="rooms:list")
    builder.button(text="🔙 Назад", callback_data="game:open")
    builder.adjust(1)
    return builder.as_markup()


def my_rooms_keyboard(room_ids: list[int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for room_id in room_ids:
        builder.button(text=f"❌ Отменить комнату #{room_id}", callback_data=f"room:cancel:{room_id}")
    builder.button(text="🔙 Назад", callback_data="game:open")
    builder.adjust(1)
    return builder.as_markup()


def force_sub_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    return builder.as_markup()


def admin_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🎁 Создать промокод", callback_data="admin:create_promo")
    builder.button(text="🎯 Мин. ставка комнаты", callback_data="admin:min_room")
    builder.button(text="💳 Мин. пополнение", callback_data="admin:min_deposit")
    builder.button(text="💸 Мин. вывод", callback_data="admin:min_withdraw")
    builder.button(text="🤝 Реф. процент", callback_data="admin:ref_percent")
    builder.button(text="👮 Добавить админа", callback_data="admin:add_admin")
    builder.button(text="➕ Выдать баланс", callback_data="admin:add_balance")
    builder.button(text="➖ Снять баланс", callback_data="admin:take_balance")
    builder.button(text="📡 Канал подписки", callback_data="admin:force_sub")
    builder.button(text="📝 Лог-чат", callback_data="admin:log_chat")
    builder.button(text="📊 Статистика", callback_data="admin:stats")
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
        builder.button(text="📢 Подписаться", url=f"https://t.me/{chat_username.lstrip('@')}")
    elif chat_id:
        builder.button(text="📢 Канал", url="https://t.me")
    builder.button(text="✅ Проверить подписку", callback_data="sub:check")
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
        await bot.send_message(int(chat_id), text)


async def ensure_registered(tg_user: Any, invited_by: int | None = None) -> Any:
    return await db.upsert_user(tg_user, invited_by=invited_by)


async def render_profile(user_id: int) -> str:
    user = await db.get_user(user_id)
    referral = await db.get_referral_summary(user_id)
    if not user:
        return "Профиль не найден."
    return (
        "👤 <b>Профиль игрока</b>\n\n"
        f"ID: <code>{user['user_id']}</code>\n"
        f"Юзернейм: @{user['username'] or 'нет'}\n"
        f"Баланс: <b>{fmt_amount(float(user['balance']))} {config.bot_asset}</b>\n"
        f"Пополнено: {fmt_amount(float(user['total_deposit']))} {config.bot_asset}\n"
        f"Выведено: {fmt_amount(float(user['total_withdraw']))} {config.bot_asset}\n"
        f"Поставлено: {fmt_amount(float(user['total_wager']))} {config.bot_asset}\n"
        f"Выиграно: {fmt_amount(float(user['total_win']))} {config.bot_asset}\n"
        f"Реф. заработок: {fmt_amount(float(user['referral_earnings']))} {config.bot_asset}\n"
        f"Приглашено: {referral['referred_count']}\n"
        f"Автовывод: {'вкл' if int(user['auto_withdraw_enabled']) else 'выкл'}\n"
        f"Ставка по умолчанию: {fmt_amount(float(user['default_room_amount']))} {config.bot_asset}"
    )


async def show_main_menu(message: Message) -> None:
    is_admin = await db.is_admin(message.from_user.id)
    text = (
        "🎰 <b>Dice Duel CryptoBot</b>\n\n"
        "Здесь можно пополнять баланс через CryptoBot, создавать комнаты, играть на кубиках, получать реферальные награды и пользоваться промокодами."
    )
    await message.answer(text, reply_markup=main_menu_keyboard(is_admin))


async def send_main_menu(message: Message, user_id: int) -> None:
    is_admin = await db.is_admin(user_id)
    text = (
        "🎰 <b>Dice Duel CryptoBot</b>\n\n"
        "Здесь можно пополнять баланс через CryptoBot, создавать комнаты, играть на кубиках, получать реферальные награды и пользоваться промокодами."
    )
    await message.answer(text, reply_markup=main_menu_keyboard(is_admin))


async def show_game_menu(target: Message | CallbackQuery) -> None:
    text = (
        "🎲 <b>Игровое меню</b>\n\n"
        "Создавайте сколько угодно комнат от минимальной суммы, заходите в комнаты других игроков и забирайте банк.\n"
        f"Комиссия проекта: <b>{fmt_amount(await db.get_setting_float('house_commission_percent', config.house_commission_percent))}%</b>."
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
        f"Администраторов: <b>{len(admins)}</b>\n"
        "Здесь можно управлять промокодами, лимитами, админами, балансами, обязательной подпиской и лог-чатом."
    )
    if isinstance(target, CallbackQuery):
        await safe_edit(target.message, text, reply_markup=admin_keyboard())
        await target.answer()
    else:
        await target.answer(text, reply_markup=admin_keyboard())


async def callback_admin_guard(callback: CallbackQuery) -> bool:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return False
    return True


async def show_rooms(callback: CallbackQuery) -> None:
    rows = await db.list_open_rooms()
    if not rows:
        text = "📋 <b>Открытые комнаты</b>\n\nСейчас свободных комнат нет. Создайте первую и заберите соперника."
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
                        "✅ <b>Пополнение подтверждено</b>\n\n"
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
async def handle_start(message: Message, command: CommandObject) -> None:
    inviter_id = None
    if command.args and command.args.startswith("ref_"):
        ref_value = command.args.split("ref_", 1)[1]
        if ref_value.isdigit():
            inviter_id = int(ref_value)
    await ensure_registered(message.from_user, invited_by=inviter_id)
    if not await is_user_subscribed(message.bot, message.from_user.id):
        await send_subscription_prompt(message.bot, message)
    await show_main_menu(message)


@router.message(Command("cancel"))
async def handle_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.")


@router.message(F.text == "🎲 Играть")
async def open_game_menu(message: Message) -> None:
    await ensure_registered(message.from_user)
    await show_game_menu(message)


@router.message(F.text == "👤 Профиль")
async def open_profile_menu(message: Message) -> None:
    await ensure_registered(message.from_user)
    await show_profile(message)


@router.message(F.text == "🎁 Промокод")
async def promo_menu(message: Message, state: FSMContext) -> None:
    await ensure_registered(message.from_user)
    await state.set_state(UserStates.promo_code)
    await message.answer("Введите промокод одним сообщением. Для отмены используйте /cancel.")


@router.message(F.text == "ℹ️ Помощь")
async def help_menu(message: Message) -> None:
    text = (
        "ℹ️ <b>Как это работает</b>\n\n"
        f"1. Пополните баланс от <b>{fmt_amount(await db.get_setting_float('min_deposit_amount', config.min_deposit_amount))} {config.bot_asset}</b>.\n"
        "2. Создайте комнату или зайдите в уже существующую.\n"
        "3. Бот бросит два кубика: первый за создателя, второй за вошедшего игрока.\n"
        "4. Победитель получает весь банк за вычетом комиссии.\n"
        "5. Проигравшая ставка реферала приносит пригласившему процент, если он есть."
    )
    await message.answer(text)


@router.message(F.text == "🛡 Админ-панель")
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
    try:
        result = await db.join_room(room_id, callback.from_user.id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    room = result["room"]
    creator_id = int(room["creator_id"])
    opponent_id = int(room["opponent_id"])
    creator_roll = int(room["creator_roll"])
    opponent_roll = int(room["opponent_roll"])
    winner_id = int(result["winner_id"])
    winner_text = "создатель комнаты" if winner_id == creator_id else "вошедший игрок"
    rerolls = int(room["rerolls"])
    reroll_text = f"\nПерекидываний из-за ничьей: <b>{rerolls}</b>" if rerolls else ""
    duel_text = (
        f"🎲 <b>Дуэль в комнате #{room_id}</b>\n\n"
        f"Ставка: <b>{fmt_amount(float(room['amount']))} {config.bot_asset}</b>\n"
        f"Первый кубик создателя: <b>{creator_roll}</b>\n"
        f"Второй кубик вошедшего: <b>{opponent_roll}</b>{reroll_text}\n\n"
        f"Победил: <b>{winner_text}</b>\n"
        f"Приз: <b>{fmt_amount(float(result['winner_prize']))} {config.bot_asset}</b>\n"
        f"Комиссия: <b>{fmt_amount(float(result['commission']))} {config.bot_asset}</b>"
    )

    with contextlib.suppress(Exception):
        await callback.bot.send_message(creator_id, duel_text)
    with contextlib.suppress(Exception):
        await callback.bot.send_message(opponent_id, duel_text)

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
    await callback.answer("Комната сыграна")
    await callback.message.answer(duel_text)


@router.callback_query(F.data == "profile:deposit")
async def cb_deposit(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserStates.deposit_amount)
    minimum = await db.get_setting_float("min_deposit_amount", config.min_deposit_amount)
    await callback.message.answer(
        "💳 <b>Пополнение</b>\n\n"
        f"Отправьте сумму пополнения.\nМинимум: <b>{fmt_amount(minimum)} {config.bot_asset}</b>"
    )
    await callback.answer()


@router.callback_query(F.data == "profile:withdraw")
async def cb_withdraw(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserStates.withdraw_amount)
    minimum = await db.get_setting_float("min_withdraw_amount", config.min_withdraw_amount)
    await callback.message.answer(
        "💸 <b>Вывод через CryptoBot чек</b>\n\n"
        f"Отправьте сумму вывода.\nМинимум: <b>{fmt_amount(minimum)} {config.bot_asset}</b>"
    )
    await callback.answer()


@router.callback_query(F.data == "profile:settings")
async def cb_profile_settings(callback: CallbackQuery) -> None:
    user = await db.get_user(callback.from_user.id)
    text = (
        "⚙️ <b>Настройки профиля</b>\n\n"
        f"Автовывод через чеки: <b>{'включен' if int(user['auto_withdraw_enabled']) else 'выключен'}</b>\n"
        f"Ставка комнаты по умолчанию: <b>{fmt_amount(float(user['default_room_amount']))} {config.bot_asset}</b>\n"
        f"Пополнение работает через CryptoBot invoice в валюте <b>{config.bot_asset}</b>."
    )
    await safe_edit(callback.message, text, reply_markup=profile_settings_keyboard(bool(user["auto_withdraw_enabled"])))
    await callback.answer()


@router.callback_query(F.data == "settings:toggle_withdraw")
async def cb_toggle_withdraw(callback: CallbackQuery) -> None:
    user = await db.toggle_auto_withdraw(callback.from_user.id)
    text = (
        "⚙️ <b>Настройки профиля</b>\n\n"
        f"Автовывод через чеки: <b>{'включен' if int(user['auto_withdraw_enabled']) else 'выключен'}</b>\n"
        f"Ставка комнаты по умолчанию: <b>{fmt_amount(float(user['default_room_amount']))} {config.bot_asset}</b>"
    )
    await safe_edit(callback.message, text, reply_markup=profile_settings_keyboard(bool(user["auto_withdraw_enabled"])))
    await callback.answer("Настройка обновлена")


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
    await safe_edit(callback.message, text, reply_markup=profile_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admin:create_promo")
async def cb_admin_create_promo(callback: CallbackQuery, state: FSMContext) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(UserStates.admin_promo)
    await callback.message.answer("Введите промокод в формате: <code>NAME 1.5 100</code>")
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
    builder.button(text="💳 Оплатить счет", url=pay_url)
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
    if not int(user["auto_withdraw_enabled"]):
        await message.answer("Автовывод отключен в настройках профиля. Включите его и повторите.")
        return
    if not config.crypto_pay_token:
        await message.answer("CRYPTO_PAY_TOKEN не задан, поэтому авто-вывод пока недоступен.")
        await state.clear()
        return
    try:
        check = await crypto.create_check(amount, config.bot_asset)
        check_id = str(check.get("check_id") or check.get("id"))
        check_url = check.get("bot_check_url") or check.get("url") or "https://t.me/CryptoBot"
        updated = await db.create_withdrawal(check_id, message.from_user.id, amount, config.bot_asset, check_url)
    except (CryptoPayError, ValueError) as exc:
        await message.answer(f"Не удалось создать чек: <code>{exc}</code>")
        await state.clear()
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="💸 Забрать чек", url=check_url)
    builder.adjust(1)
    await message.answer(
        "💸 <b>Чек создан</b>\n\n"
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


@router.message(UserStates.promo_code)
async def state_promo_code(message: Message, state: FSMContext) -> None:
    try:
        user, promo = await db.activate_promocode(message.text, message.from_user.id)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await message.answer(
        "🎁 <b>Промокод активирован</b>\n\n"
        f"Начислено: <b>{fmt_amount(float(promo['amount']))} {config.bot_asset}</b>\n"
        f"Текущий баланс: <b>{fmt_amount(float(user['balance']))} {config.bot_asset}</b>"
    )
    await send_log(
        message.bot,
        "🎁 <b>Активация промокода</b>\n"
        f"Пользователь: <code>{message.from_user.id}</code>\n"
        f"Промокод: <code>{promo['code']}</code>\n"
        f"Сумма: <b>{fmt_amount(float(promo['amount']))} {config.bot_asset}</b>",
    )
    await state.clear()


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


@router.message(UserStates.admin_promo)
async def state_admin_promo(message: Message, state: FSMContext) -> None:
    if not await admin_guard(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Формат: <code>NAME 1.5 100</code>")
        return
    code = parts[0].upper()
    try:
        amount = parse_amount(parts[1])
        activations = int(parts[2])
        if activations <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Проверьте сумму и количество активаций.")
        return
    await db.create_promocode(code, amount, activations, message.from_user.id)
    await message.answer(
        f"Промокод <b>{code}</b> создан.\n"
        f"Сумма: <b>{fmt_amount(amount)} {config.bot_asset}</b>\n"
        f"Активаций: <b>{activations}</b>"
    )
    await send_log(
        message.bot,
        "🎁 <b>Создан промокод</b>\n"
        f"Админ: <code>{message.from_user.id}</code>\n"
        f"Код: <code>{code}</code>\n"
        f"Сумма: <b>{fmt_amount(amount)} {config.bot_asset}</b>\n"
        f"Активаций: <b>{activations}</b>",
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
