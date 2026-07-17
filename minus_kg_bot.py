from __future__ import annotations

import asyncio
import calendar as pycalendar
import base64
import json
import logging
import math
import os
import re
import sqlite3
import time
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ButtonStyle, ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    FSInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    MenuButtonCommands,
    MenuButtonDefault,
    InputMediaPhoto,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv
from timezonefinder import TimezoneFinder
from openai import AsyncOpenAI

from minus_kg_database import MinusKgDatabase
from minus_kg_visuals import (
    render_fasting_ring,
    render_recipe_card,
    render_weight_progress_chart,
    render_welcome_animation,
    render_breathing_animation,
    render_recipe_picker_animation,
    render_recipe_choice_animation,
)


load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_id: int
    applications_archive_channel_id: int | None
    database_path: str
    support_username: str
    openai_api_key: str
    openai_model: str
    openai_vision_model: str

    @classmethod
    def load(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", "").strip()
        admin_raw = os.getenv("ADMIN_ID", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN не задано у .env")
        if not admin_raw.isdigit():
            raise RuntimeError("ADMIN_ID має бути числом")
        support = os.getenv("SUPPORT_USERNAME", "").strip()
        if support and not support.startswith("@"):
            support = "@" + support

        archive_raw = os.getenv(
            "APPLICATIONS_ARCHIVE_CHANNEL_ID",
            "",
        ).strip()
        if archive_raw and not re.fullmatch(r"-?\d+", archive_raw):
            raise RuntimeError(
                "APPLICATIONS_ARCHIVE_CHANNEL_ID должен быть числовым ID"
            )
        archive_channel_id = (
            int(archive_raw)
            if archive_raw
            else None
        )

        return cls(
            bot_token=token,
            admin_id=int(admin_raw),
            applications_archive_channel_id=archive_channel_id,
            database_path=os.getenv("MINUS_KG_DATABASE_PATH", "data/minus_kg.sqlite3").strip(),
            support_username=support,
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip(),
            openai_vision_model=os.getenv("OPENAI_VISION_MODEL", "gpt-5.6").strip(),
        )


settings = Settings.load()
db = MinusKgDatabase(settings.database_path)
router = Router()
timezone_finder = TimezoneFinder()
ai_client = (
    AsyncOpenAI(api_key=settings.openai_api_key)
    if settings.openai_api_key
    else None
)
TRIAL_SECONDS = 2 * 24 * 60 * 60
ACTIVITY_FACTORS = {"sedentary": 1.20, "light": 1.375, "moderate": 1.55, "active": 1.725}
PLANS = {
    "week": {"days": 7, "stars": 180, "uah": 149},
    "month": {"days": 30, "stars": 475, "uah": 399},
    "two_months": {"days": 60, "stars": 680, "uah": 569},
    "three_months": {"days": 90, "stars": 1070, "uah": 899},
    "half_year": {"days": 180, "stars": 1775, "uah": 1490},
}

# Average nutrition per 100 ml. Values for packaged drinks vary, so the bot
# explicitly labels sweet drinks, juice and milk as approximate estimates.
DRINK_PRESETS = {
    "water": {
        "ru": "Вода без газа",
        "uk": "Вода без газу",
        "emoji": "💧",
        "calories": 0.0,
        "protein": 0.0,
        "fat": 0.0,
        "carbs": 0.0,
        "counts_as_water": True,
    },
    "sparkling": {
        "ru": "Вода с газом без сахара",
        "uk": "Газована вода без цукру",
        "emoji": "🫧",
        "calories": 0.0,
        "protein": 0.0,
        "fat": 0.0,
        "carbs": 0.0,
        "counts_as_water": True,
    },
    "sweet": {
        "ru": "Сладкий напиток",
        "uk": "Солодкий напій",
        "emoji": "🥤",
        "calories": 42.0,
        "protein": 0.0,
        "fat": 0.0,
        "carbs": 10.5,
        "counts_as_water": False,
    },
    "juice": {
        "ru": "Сок",
        "uk": "Сік",
        "emoji": "🧃",
        "calories": 45.0,
        "protein": 0.2,
        "fat": 0.1,
        "carbs": 10.5,
        "counts_as_water": False,
    },
    "milk": {
        "ru": "Молоко",
        "uk": "Молоко",
        "emoji": "🥛",
        "calories": 60.0,
        "protein": 3.2,
        "fat": 3.2,
        "carbs": 4.7,
        "counts_as_water": False,
    },
    "tea": {
        "ru": "Чай или кофе без сахара",
        "uk": "Чай або кава без цукру",
        "emoji": "☕",
        "calories": 2.0,
        "protein": 0.1,
        "fat": 0.0,
        "carbs": 0.3,
        "counts_as_water": False,
    },
}


class Onboarding(StatesGroup):
    language = State()
    adult = State()
    name = State()
    sex = State()
    birth_date = State()
    height = State()
    current_weight = State()
    target_weight = State()
    last_target = State()
    activity = State()
    sport = State()
    wake_time = State()
    sleep_time = State()
    meals_count = State()
    safety = State()
    timezone = State()


class Actions(StatesGroup):
    food = State()
    photo = State()
    ai = State()
    menu_products = State()


class DrinkLog(StatesGroup):
    custom_volume = State()


class BodyLog(StatesGroup):
    weight = State()
    waist = State()
    hips = State()
    chest = State()


class ReminderSettings(StatesGroup):
    meal_time = State()
    body_time = State()


class CoachApplication(StatesGroup):
    focus = State()
    support_format = State()
    sport = State()
    food_notes = State()
    exclusions = State()
    contact_time = State()
    comment = State()
    confirm = State()


class CoachManagerCancellation(StatesGroup):
    reason = State()


class CoachClientCancellation(StatesGroup):
    reason = State()


class AccountSettings(StatesGroup):
    target_weight = State()
    timezone = State()
    delete_confirm = State()


class FriendChat(StatesGroup):
    active = State()


async def send_application_archive_copy(
    bot: Bot,
    text: str,
) -> None:
    channel_id = settings.applications_archive_channel_id
    if not channel_id:
        return

    try:
        await bot.send_message(
            channel_id,
            text[:4096],
        )
    except Exception:
        logging.exception(
            "Failed to duplicate application to archive channel %s",
            channel_id,
        )
        try:
            await bot.send_message(
                settings.admin_id,
                "⚠️ Не удалось отправить копию заявки в закрытый канал. "
                "Основная заявка в личном чате сохранена."
            )
        except Exception:
            logging.exception(
                "Failed to notify admin about archive duplication error"
            )


def profile_complete(profile: dict | None) -> bool:
    return bool(
        profile and profile.get("language") and profile.get("display_name")
        and profile.get("height_cm") and profile.get("current_weight_kg")
        and profile.get("target_weight_kg") and profile.get("timezone")
        and profile.get("calorie_target")
    )


def access_active(profile: dict | None) -> bool:
    if not profile:
        return False
    now = int(time.time())
    return bool((profile.get("trial_expires_at") or 0) > now or (profile.get("subscription_expires_at") or 0) > now)


def paid_subscription_active(profile: dict | None) -> bool:
    if not profile:
        return False
    return int(profile.get("subscription_expires_at") or 0) > int(time.time())


def parse_date(text: str) -> date | None:
    try:
        value = datetime.strptime(text.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None
    today = date.today()
    age = today.year - value.year - ((today.month, today.day) < (value.month, value.day))
    return value if 18 <= age <= 80 else None


def age_from_birthdate(value: str) -> int:
    birth = datetime.strptime(value, "%Y-%m-%d").date()
    today = date.today()
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def parse_number(text: str, minimum: float, maximum: float) -> float | None:
    try:
        value = float(text.strip().replace(",", "."))
    except (ValueError, AttributeError):
        return None
    return value if minimum <= value <= maximum else None


def parse_clock(text: str) -> str | None:
    try:
        return datetime.strptime(text.strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        return None


def minutes_from_time(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def time_from_minutes(value: int) -> str:
    value %= 1440
    return f"{value // 60:02d}:{value % 60:02d}"


def fasting_mode_hours(mode: str | None) -> tuple[int, int]:
    mapping = {
        "12_12": (12, 12),
        "14_10": (14, 10),
        "16_8": (16, 8),
    }
    return mapping.get(mode or "", (0, 0))


def fasting_status_data(profile: dict, local_now: datetime) -> dict:
    fast_hours, eat_hours = fasting_mode_hours(profile.get("fasting_mode"))
    start = profile.get("fasting_start")
    if not start or not eat_hours:
        raise ValueError("Fasting mode is not configured")

    start_minute = minutes_from_time(start)
    now_minute = local_now.hour * 60 + local_now.minute
    offset = (now_minute - start_minute) % 1440
    eat_minutes = eat_hours * 60
    fast_minutes = fast_hours * 60

    if offset < eat_minutes:
        phase = "eat"
        elapsed = offset
        duration = eat_minutes
        remaining = eat_minutes - offset
    else:
        phase = "fast"
        elapsed = offset - eat_minutes
        duration = fast_minutes
        remaining = 1440 - offset

    remaining = max(0, remaining)
    return {
        "phase": phase,
        "progress": min(1.0, max(0.0, elapsed / max(duration, 1))),
        "remaining_minutes": remaining,
        "remaining_text": f"{remaining // 60:02d}:{remaining % 60:02d}",
        "fast_hours": fast_hours,
        "eat_hours": eat_hours,
        "mode_label": f"{fast_hours}:{eat_hours}",
        "start": start,
        "end": profile.get("fasting_end"),
    }


def body_skip_keyboard(field: str, language: str) -> InlineKeyboardMarkup:
    label = "Пропустити" if language == "uk" else "Пропустить"
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=f"➡️ {label}",
                callback_data=f"body_skip:{field}",
                style=ButtonStyle.PRIMARY,
            )
        ]]
    )


def body_log_keyboard(language: str) -> InlineKeyboardMarkup:
    label = "Записати вагу й об'єми" if language == "uk" else "Записать вес и объёмы"
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=f"⚖️ {label}",
                callback_data="body:log",
                style=ButtonStyle.SUCCESS,
            )
        ]]
    )


def meal_reminder_action_keyboard(
    slot_number: int,
    language: str,
) -> InlineKeyboardMarkup:
    if language == "uk":
        record = "Записати, що я з'їв/з'їла"
        later = "Нагадати через 30 хвилин"
        skip = "Сьогодні пропущу"
    else:
        record = "Записать, что я съел(а)"
        later = "Напомнить через 30 минут"
        skip = "Сегодня пропущу"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🍽 {record}",
                    callback_data=f"meal_action:ate:{slot_number}",
                    style=ButtonStyle.SUCCESS,
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"⏰ {later}",
                    callback_data=f"meal_action:snooze:{slot_number}",
                    style=ButtonStyle.PRIMARY,
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🙂 {skip}",
                    callback_data=f"meal_action:skip:{slot_number}",
                    style=ButtonStyle.DANGER,
                )
            ],
        ]
    )


def reminders_overview_text(
    schedule: list[dict],
    body_prefs: dict,
    profile: dict,
    language: str,
) -> str:
    enabled_meals = sum(
        1 for slot in schedule if bool(slot.get("enabled"))
    )
    total_meals = len(schedule)
    body_enabled = bool(body_prefs.get("body_enabled", 1))
    timezone = profile.get("timezone") or "Europe/Kyiv"

    schedule_lines = []
    for slot in schedule:
        icon = "🟢" if slot.get("enabled") else "⚪"
        status = (
            "увімкнено" if slot.get("enabled") else "вимкнено"
        ) if language == "uk" else (
            "включено" if slot.get("enabled") else "выключено"
        )
        schedule_lines.append(
            f"{icon} {slot['meal_time']} — {slot['meal_name']} "
            f"({status})"
        )

    body_icon = "🟢" if body_enabled else "⚪"
    body_status = (
        "увімкнено" if body_enabled else "вимкнено"
    ) if language == "uk" else (
        "включено" if body_enabled else "выключено"
    )

    if language == "uk":
        return (
            "⏰ Ваші нагадування\n\n"
            "Це м'які підказки, а не команди. Не потрібно їсти без "
            "фізичного голоду лише тому, що прийшло повідомлення: його "
            "можна відкласти або пропустити.\n\n"
            "🍽 Прийоми їжі:\n"
            + ("\n".join(schedule_lines) if schedule_lines else "• розклад ще не створено")
            + "\n\n"
            f"{body_icon} {body_prefs.get('body_time', '09:00')} — "
            f"вага й об'єми ({body_status})\n"
            "Повідомлення про вагу приходить не частіше одного разу "
            "на два дні — лише якщо після останнього запису минуло "
            "достатньо часу.\n\n"
            f"Увімкнено прийомів їжі: {enabled_meals} із {total_meals}.\n"
            f"Часовий пояс: {timezone}.\n\n"
            "Натисніть на потрібний рядок, щоб змінити час або стан. "
            "Зелена крапка означає, що нагадування працює; сіра — вимкнене."
        )

    return (
        "⏰ Ваши напоминания\n\n"
        "Это мягкие подсказки, а не команды. Не нужно есть без "
        "физического голода только потому, что пришло сообщение: его "
        "можно отложить или пропустить.\n\n"
        "🍽 Приёмы пищи:\n"
        + ("\n".join(schedule_lines) if schedule_lines else "• расписание ещё не создано")
        + "\n\n"
        f"{body_icon} {body_prefs.get('body_time', '09:00')} — "
        f"вес и объёмы ({body_status})\n"
        "Сообщение о весе приходит не чаще одного раза в два дня — "
        "только если после последней записи прошло достаточно времени.\n\n"
        f"Включено приёмов пищи: {enabled_meals} из {total_meals}.\n"
        f"Часовой пояс: {timezone}.\n\n"
        "Нажмите на нужную строку, чтобы изменить время или состояние. "
        "Зелёная точка означает, что напоминание работает; серая — выключено."
    )


def reminders_overview_keyboard(
    schedule: list[dict],
    body_prefs: dict,
    language: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    for slot in schedule:
        enabled = bool(slot.get("enabled"))
        icon = "🟢" if enabled else "⚪"
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"{icon} {slot['meal_time']} · "
                        f"{slot['meal_name']}"
                    ),
                    callback_data=(
                        f"reminder_slot:{slot['slot_number']}"
                    ),
                    style=(
                        ButtonStyle.SUCCESS
                        if enabled
                        else ButtonStyle.PRIMARY
                    ),
                )
            ]
        )

    body_enabled = bool(body_prefs.get("body_enabled", 1))
    body_icon = "🟢" if body_enabled else "⚪"
    body_label = (
        "Вага й об'єми"
        if language == "uk"
        else "Вес и объёмы"
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=(
                    f"{body_icon} "
                    f"{body_prefs.get('body_time', '09:00')} · "
                    f"{body_label}"
                ),
                callback_data="body_reminder_settings",
                style=(
                    ButtonStyle.SUCCESS
                    if body_enabled
                    else ButtonStyle.PRIMARY
                ),
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text=(
                    "👀 Подивитися приклад"
                    if language == "uk"
                    else "👀 Посмотреть пример"
                ),
                callback_data="reminders_preview",
                style=ButtonStyle.PRIMARY,
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=(
                    "🔔 Увімкнути все"
                    if language == "uk"
                    else "🔔 Включить всё"
                ),
                callback_data="reminders_all:on",
                style=ButtonStyle.SUCCESS,
            ),
            InlineKeyboardButton(
                text=(
                    "🔕 Вимкнути все"
                    if language == "uk"
                    else "🔕 Выключить всё"
                ),
                callback_data="reminders_all:off",
                style=ButtonStyle.DANGER,
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reminder_back_keyboard(
    language: str,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "⬅️ Повернутися до нагадувань"
                    if language == "uk"
                    else "⬅️ Вернуться к напоминаниям",
                    "menu:reminders",
                    ButtonStyle.PRIMARY,
                )
            ]
        ]
    )


def meal_slot_settings_text(
    slot: dict,
    language: str,
) -> str:
    enabled = bool(slot.get("enabled"))
    if language == "uk":
        status = "увімкнено" if enabled else "вимкнено"
        return (
            f"🍽 Нагадування: {slot['meal_name']}\n\n"
            f"Час: {slot['meal_time']}\n"
            f"Стан: {status}\n\n"
            "Коли настане цей час, бот м'яко нагадає про прийом їжі "
            "і запропонує три варіанти: записати їжу, відкласти "
            "нагадування на 30 хвилин або пропустити його.\n\n"
            "Нагадування не означає, що потрібно їсти без голоду."
        )

    status = "включено" if enabled else "выключено"
    return (
        f"🍽 Напоминание: {slot['meal_name']}\n\n"
        f"Время: {slot['meal_time']}\n"
        f"Состояние: {status}\n\n"
        "Когда наступит это время, бот мягко напомнит о приёме пищи "
        "и предложит три варианта: записать еду, отложить уведомление "
        "на 30 минут или пропустить его.\n\n"
        "Напоминание не означает, что нужно есть без голода."
    )


def meal_slot_settings_keyboard(
    slot: dict,
    language: str,
) -> InlineKeyboardMarkup:
    enabled = bool(slot.get("enabled"))

    if language == "uk":
        toggle_text = (
            "Вимкнути це нагадування"
            if enabled
            else "Увімкнути це нагадування"
        )
        time_text = "Змінити час нагадування"
        back_text = "Повернутися до списку"
    else:
        toggle_text = (
            "Выключить это напоминание"
            if enabled
            else "Включить это напоминание"
        )
        time_text = "Изменить время напоминания"
        back_text = "Вернуться к списку"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🕒 {time_text}",
                    callback_data=(
                        f"reminder_edit:{slot['slot_number']}"
                    ),
                    style=ButtonStyle.PRIMARY,
                )
            ],
            [
                InlineKeyboardButton(
                    text=(
                        ("🔕 " if enabled else "🔔 ")
                        + toggle_text
                    ),
                    callback_data=(
                        f"reminder_toggle:{slot['slot_number']}"
                    ),
                    style=(
                        ButtonStyle.DANGER
                        if enabled
                        else ButtonStyle.SUCCESS
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"⬅️ {back_text}",
                    callback_data="menu:reminders",
                    style=ButtonStyle.PRIMARY,
                )
            ],
        ]
    )


def body_reminder_settings_text(
    prefs: dict,
    language: str,
) -> str:
    enabled = bool(prefs.get("body_enabled", 1))
    body_time = prefs.get("body_time", "09:00")

    if language == "uk":
        status = "увімкнено" if enabled else "вимкнено"
        return (
            "⚖️ Нагадування про вагу й об'єми\n\n"
            f"Час: {body_time}\n"
            f"Стан: {status}\n"
            "Частота: не частіше одного разу на два дні.\n\n"
            "Бот перевіряє дату останнього запису. Якщо ви вже недавно "
            "внесли вагу, зайве повідомлення не прийде.\n\n"
            "Вага обов'язкова для запису, а талія, стегна й груди — "
            "лише за бажанням. Один результат не визначає прогрес."
        )

    status = "включено" if enabled else "выключено"
    return (
        "⚖️ Напоминание о весе и объёмах\n\n"
        f"Время: {body_time}\n"
        f"Состояние: {status}\n"
        "Частота: не чаще одного раза в два дня.\n\n"
        "Бот проверяет дату последней записи. Если вы недавно уже "
        "внесли вес, лишнее сообщение не придёт.\n\n"
        "Вес обязателен для записи, а талия, бёдра и грудь — "
        "только по желанию. Один результат не определяет прогресс."
    )


def body_reminder_settings_keyboard(
    prefs: dict,
    language: str,
) -> InlineKeyboardMarkup:
    enabled = bool(prefs.get("body_enabled", 1))

    if language == "uk":
        time_text = "Змінити час нагадування"
        toggle_text = (
            "Вимкнути це нагадування"
            if enabled
            else "Увімкнути це нагадування"
        )
        back_text = "Повернутися до списку"
    else:
        time_text = "Изменить время напоминания"
        toggle_text = (
            "Выключить это напоминание"
            if enabled
            else "Включить это напоминание"
        )
        back_text = "Вернуться к списку"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🕒 {time_text}",
                    callback_data="body_reminder_edit",
                    style=ButtonStyle.PRIMARY,
                )
            ],
            [
                InlineKeyboardButton(
                    text=(
                        ("🔕 " if enabled else "🔔 ")
                        + toggle_text
                    ),
                    callback_data="body_reminder_toggle",
                    style=(
                        ButtonStyle.DANGER
                        if enabled
                        else ButtonStyle.SUCCESS
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"⬅️ {back_text}",
                    callback_data="menu:reminders",
                    style=ButtonStyle.PRIMARY,
                )
            ],
        ]
    )


async def edit_reminder_screen(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    try:
        await callback.message.edit_text(
            text,
            reply_markup=reply_markup,
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=reply_markup,
        )


def fasting_status_keyboard(language: str) -> InlineKeyboardMarkup:
    refresh = "Оновити коло" if language == "uk" else "Обновить круг"
    change = "Змінити режим" if language == "uk" else "Изменить режим"
    turn_off = "Вимкнути" if language == "uk" else "Выключить"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"🔄 {refresh}",
                callback_data="fasting_status",
                style=ButtonStyle.PRIMARY,
            )],
            [
                InlineKeyboardButton(
                    text=f"⚙️ {change}",
                    callback_data="fasting_change",
                    style=ButtonStyle.PRIMARY,
                ),
                InlineKeyboardButton(
                    text=f"⛔ {turn_off}",
                    callback_data="fasting:off",
                    style=ButtonStyle.DANGER,
                ),
            ],
        ]
    )


def nutrition_midpoint(day: dict[str, float], nutrient: str) -> float:
    return (
        float(day.get(f"{nutrient}_min", 0))
        + float(day.get(f"{nutrient}_max", 0))
    ) / 2


def personalized_food_advice(profile: dict, day: dict[str, float]) -> str:
    language = profile.get("language", "ru")
    targets = {
        "calories": float(profile.get("calorie_target") or 0),
        "protein": float(profile.get("protein_g") or 0),
        "fat": float(profile.get("fat_g") or 0),
        "carbs": float(profile.get("carbs_g") or 0),
    }
    eaten = {key: nutrition_midpoint(day, key) for key in targets}
    left = {key: targets[key] - eaten[key] for key in targets}

    suggestions: list[str] = []
    cautions: list[str] = []

    if left["calories"] <= 0:
        suggestions.append(
            "Денний орієнтир уже набрано. Не компенсуйте це голодуванням — наступний прийом зробіть звичайним і легким."
            if language == "uk" else
            "Дневной ориентир уже набран. Не компенсируйте это голодовкой — следующий приём сделайте обычным и лёгким."
        )
    else:
        if left["protein"] >= 25:
            suggestions.append(
                "150–200 г нежирної риби, курки, індички або 200–250 г кисломолочного сиру/йогурту."
                if language == "uk" else
                "150–200 г нежирной рыбы, курицы, индейки или 200–250 г творога/йогурта."
            )
        elif left["protein"] >= 10:
            suggestions.append(
                "Невелика білкова порція: 2 яйця, йогурт або 100–120 г риби/курки."
                if language == "uk" else
                "Небольшая белковая порция: 2 яйца, йогурт или 100–120 г рыбы/курицы."
            )

        if left["carbs"] >= 50:
            suggestions.append(
                "150–220 г готової гречки, вівсянки, булгуру, рису або картоплі."
                if language == "uk" else
                "150–220 г готовой гречки, овсянки, булгура, риса или картофеля."
            )
        elif left["carbs"] >= 20:
            suggestions.append(
                "Фрукт або невелика порція крупи/цільнозернового хліба."
                if language == "uk" else
                "Фрукт или небольшая порция крупы/цельнозернового хлеба."
            )

        suggestions.append(
            "Додайте 250–400 г овочів протягом решти дня, якщо їх було мало."
            if language == "uk" else
            "Добавьте 250–400 г овощей до конца дня, если их было мало."
        )

    if left["fat"] <= 0:
        cautions.append(
            "Жири вже набрані: сьогодні краще без додаткової олії, горіхів, майонезу та жирних соусів."
            if language == "uk" else
            "Жиры уже набраны: сегодня лучше без дополнительного масла, орехов, майонеза и жирных соусов."
        )
    if left["carbs"] <= 0:
        cautions.append(
            "Вуглеводи вже набрані: оберіть білок та овочі замість солодкого й великої порції гарніру."
            if language == "uk" else
            "Углеводы уже набраны: выберите белок и овощи вместо сладкого и большой порции гарнира."
        )
    if left["protein"] <= 0:
        cautions.append(
            "Білка достатньо; не потрібно спеціально добирати ще одну велику порцію м'яса."
            if language == "uk" else
            "Белка достаточно; не нужно специально добирать ещё одну большую порцию мяса."
        )

    if language == "uk":
        header = (
            "🥗 Що ще варто з'їсти сьогодні\n\n"
            f"Вже записано приблизно:\n"
            f"• {eaten['calories']:.0f} ккал\n"
            f"• білки {eaten['protein']:.0f} г\n"
            f"• жири {eaten['fat']:.0f} г\n"
            f"• вуглеводи {eaten['carbs']:.0f} г\n\n"
            "Залишок до орієнтира:\n"
            f"• {max(0, left['calories']):.0f} ккал\n"
            f"• білки {max(0, left['protein']):.0f} г\n"
            f"• жири {max(0, left['fat']):.0f} г\n"
            f"• вуглеводи {max(0, left['carbs']):.0f} г"
        )
        suggestion_title = "\n\n✅ Можна добрати:\n"
        caution_title = "\n\n⚠️ Сьогодні вже не потрібно добирати:\n"
        footer = "\n\nЦе орієнтовний баланс за внесеними продуктами, а не медична заборона."
    else:
        header = (
            "🥗 Что ещё стоит съесть сегодня\n\n"
            f"Уже записано примерно:\n"
            f"• {eaten['calories']:.0f} ккал\n"
            f"• белки {eaten['protein']:.0f} г\n"
            f"• жиры {eaten['fat']:.0f} г\n"
            f"• углеводы {eaten['carbs']:.0f} г\n\n"
            "Остаток до ориентира:\n"
            f"• {max(0, left['calories']):.0f} ккал\n"
            f"• белки {max(0, left['protein']):.0f} г\n"
            f"• жиры {max(0, left['fat']):.0f} г\n"
            f"• углеводы {max(0, left['carbs']):.0f} г"
        )
        suggestion_title = "\n\n✅ Можно добрать:\n"
        caution_title = "\n\n⚠️ Сегодня уже не нужно добирать:\n"
        footer = "\n\nЭто ориентировочный баланс по внесённым продуктам, а не медицинский запрет."

    text = header
    if suggestions:
        text += suggestion_title + "\n".join(f"• {item}" for item in suggestions)
    if cautions:
        text += caution_title + "\n".join(f"• {item}" for item in cautions)
    return text + footer


def calculate_targets(data: dict) -> dict:
    age = age_from_birthdate(data["birth_date"])
    sex_constant = 5 if data["sex"] == "male" else -161
    bmr = 10 * data["current_weight_kg"] + 6.25 * data["height_cm"] - 5 * age + sex_constant
    tdee = bmr * ACTIVITY_FACTORS[data["activity"]]
    current_bmi = data["current_weight_kg"] / ((data["height_cm"] / 100) ** 2)
    target_bmi = data["target_weight_kg"] / ((data["height_cm"] / 100) ** 2)
    restricted = bool(data["safety_restricted"] or current_bmi < 18.5 or target_bmi < 18.5)
    calories = round((tdee if restricted else tdee * 0.85) / 10) * 10
    to_lose = max(0.0, data["current_weight_kg"] - data["target_weight_kg"])
    return {
        "bmr": round(bmr),
        "tdee": round(tdee),
        "calorie_target": calories,
        "protein_g": round(calories * 0.25 / 4),
        "fat_g": round(calories * 0.30 / 9),
        "carbs_g": round(calories * 0.45 / 4),
        "restricted": restricted,
        "weeks_min": math.ceil(to_lose / 0.9) if to_lose else 0,
        "weeks_max": math.ceil(to_lose / 0.45) if to_lose else 0,
    }



def extract_json_object(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.replace("```json", "", 1).replace("```", "", 1).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI did not return JSON")
    return json.loads(cleaned[start:end + 1])



async def request_valid_menu_json(
    prompt: str,
    *,
    max_output_tokens: int = 3500,
    retry_recipe_count: int = 3,
) -> dict:
    """
    Request menu JSON with automatic regeneration/repair.
    Some model replies can contain a missing comma or be cut off;
    the user should not see a technical JSON error because of that.
    """
    last_text = ""
    last_error: Exception | None = None

    for attempt in range(3):
        if attempt == 0:
            current_prompt = (
                prompt
                + "\n\nКРИТИЧЕСКИ ВАЖНО: верни компактный, полный и проверенный JSON. "
                  "Используй только двойные кавычки, ставь запятые между всеми полями, "
                  "не добавляй комментарии и не оборачивай ответ в markdown."
            )
        elif attempt == 1:
            current_prompt = (
                prompt
                + "\n\nПредыдущая попытка содержала синтаксическую ошибку. "
                  f"Создай ответ заново. Верни максимум {retry_recipe_count} рецептов, сократи формулировки "
                  "и перед отправкой проверь, что JSON разбирается стандартным json.loads(). "
                  "Только JSON, без markdown."
            )
        else:
            current_prompt = f"""
Исправь следующий повреждённый JSON. Верни только один полный валидный JSON-объект.
Нельзя добавлять пояснения, markdown или текст до/после JSON.
Сохрани смысл меню и рецептов, но разрешается сокращать длинные фразы.
Проверь запятые, кавычки, квадратные и фигурные скобки.

ПОВРЕЖДЁННЫЙ ОТВЕТ:
{last_text[:14000]}
"""

        response = await ai_client.responses.create(
            model=settings.openai_model,
            input=current_prompt,
            max_output_tokens=max_output_tokens,
        )
        last_text = (response.output_text or "").strip()

        try:
            return extract_json_object(last_text)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            logging.warning(
                "Invalid menu JSON on attempt %s: %s",
                attempt + 1,
                exc,
            )

    raise ValueError(
        f"AI returned invalid menu JSON after retries: {last_error}"
    )


def bounded_number(value: object, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(number, maximum))


async def analyze_food_text(description: str, language: str) -> dict:
    if ai_client is None:
        raise RuntimeError("OPENAI_API_KEY is missing")

    language_name = "українською" if language == "uk" else "на русском"
    instructions = f"""
Ты — осторожный помощник по оценке пищевой ценности еды.
Отвечай {language_name}. Не ставь диагнозы и не обещай похудение.
По текстовому описанию оцени калории и БЖУ диапазоном, а не ложной точностью.
Учитывай указанные граммы, способ приготовления, масло, соусы и напитки.
Если данных мало, делай разумные предположения и перечисляй их.
Если описание вообще невозможно оценить, установи needs_clarification=true
и задай один короткий уточняющий вопрос.

Верни ТОЛЬКО валидный JSON без markdown:
{{
  "needs_clarification": false,
  "clarification_question": "",
  "summary": "короткое название приёма пищи",
  "assumptions": ["предположение"],
  "items": [
    {{
      "name": "продукт или блюдо",
      "portion": "примерная порция",
      "calories_min": 0,
      "calories_max": 0,
      "protein_min": 0,
      "protein_max": 0,
      "fat_min": 0,
      "fat_max": 0,
      "carbs_min": 0,
      "carbs_max": 0
    }}
  ]
}}
Числа должны быть реалистичными, неотрицательными. Максимум 12 позиций.
"""

    response = await ai_client.responses.create(
        model=settings.openai_model,
        instructions=instructions,
        input=description[:1500],
        max_output_tokens=1200,
    )
    raw = extract_json_object(response.output_text)

    if raw.get("needs_clarification"):
        return {
            "needs_clarification": True,
            "clarification_question": str(
                raw.get("clarification_question") or
                ("Уточните количество или размер порции." if language == "ru" else
                 "Уточніть кількість або розмір порції.")
            )[:500],
        }

    clean_items = []
    for item in (raw.get("items") or [])[:12]:
        if not isinstance(item, dict):
            continue
        clean = {
            "name": str(item.get("name") or "Блюдо")[:100],
            "portion": str(item.get("portion") or "порция не указана")[:100],
            "calories_min": bounded_number(item.get("calories_min"), 5000),
            "calories_max": bounded_number(item.get("calories_max"), 5000),
            "protein_min": bounded_number(item.get("protein_min"), 500),
            "protein_max": bounded_number(item.get("protein_max"), 500),
            "fat_min": bounded_number(item.get("fat_min"), 500),
            "fat_max": bounded_number(item.get("fat_max"), 500),
            "carbs_min": bounded_number(item.get("carbs_min"), 1000),
            "carbs_max": bounded_number(item.get("carbs_max"), 1000),
        }
        for low, high in (
            ("calories_min", "calories_max"),
            ("protein_min", "protein_max"),
            ("fat_min", "fat_max"),
            ("carbs_min", "carbs_max"),
        ):
            if clean[high] < clean[low]:
                clean[low], clean[high] = clean[high], clean[low]
        clean_items.append(clean)

    if not clean_items:
        raise ValueError("AI returned no food items")

    totals = {}
    for nutrient in ("calories", "protein", "fat", "carbs"):
        totals[f"{nutrient}_min"] = round(sum(i[f"{nutrient}_min"] for i in clean_items), 1)
        totals[f"{nutrient}_max"] = round(sum(i[f"{nutrient}_max"] for i in clean_items), 1)

    return {
        "needs_clarification": False,
        "summary": str(raw.get("summary") or "Приём пищи")[:200],
        "assumptions": [str(x)[:200] for x in (raw.get("assumptions") or [])[:4]],
        "items": clean_items,
        **totals,
    }


async def analyze_food_photo(image_bytes: bytes, language: str, caption: str = "") -> dict:
    if ai_client is None:
        raise RuntimeError("OPENAI_API_KEY is missing")

    language_name = "українською" if language == "uk" else "на русском"
    caption_note = caption.strip()[:500]
    prompt = f"""
Ты — практичный помощник по оценке пищевой ценности еды по фотографии.
Отвечай {language_name}. Не ставь диагнозы и не обещай похудение.
Определи только те продукты и блюда, которые действительно видны или очень вероятны.
Дай реалистичную рабочую оценку для дневника питания и разумные нижнюю и верхнюю границы.
Не делай диапазон чрезмерно широким: ориентируйся на наиболее вероятную порцию и видимый состав.
Если пользователь указал вес блюда, считай его главным ориентиром.
Если пользователь добавил подпись, используй её как дополнительный контекст: {caption_note or 'подписи нет'}.
Если порцию нельзя оценить достаточно полезно, не выдавай слабый результат:
установи needs_clarification=true и задай один короткий вопрос о весе, масле, соусе или составе.

Верни ТОЛЬКО валидный JSON без markdown:
{{
  "needs_clarification": false,
  "clarification_question": "",
  "summary": "короткое название блюда",
  "confidence": "low|medium|high",
  "assumptions": ["что пришлось предположить"],
  "items": [
    {{
      "name": "продукт или блюдо",
      "portion": "примерная видимая порция",
      "calories_min": 0,
      "calories_max": 0,
      "protein_min": 0,
      "protein_max": 0,
      "fat_min": 0,
      "fat_max": 0,
      "carbs_min": 0,
      "carbs_max": 0
    }}
  ]
}}
Максимум 10 позиций. Все числа реалистичные и неотрицательные.
"""

    encoded = base64.b64encode(image_bytes).decode("ascii")
    response = await ai_client.responses.create(
        model=settings.openai_vision_model,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{encoded}",
                        "detail": "high",
                    },
                ],
            }
        ],
        max_output_tokens=1400,
    )
    raw = extract_json_object(response.output_text)

    if raw.get("needs_clarification"):
        return {
            "needs_clarification": True,
            "clarification_question": str(
                raw.get("clarification_question")
                or (
                    "Пришлите более ясное фото блюда крупным планом."
                    if language == "ru"
                    else "Надішліть чіткіше фото страви крупним планом."
                )
            )[:500],
        }

    clean_items = []
    for item in (raw.get("items") or [])[:10]:
        if not isinstance(item, dict):
            continue
        clean = {
            "name": str(item.get("name") or "Блюдо")[:100],
            "portion": str(item.get("portion") or "порция неясна")[:100],
            "calories_min": bounded_number(item.get("calories_min"), 5000),
            "calories_max": bounded_number(item.get("calories_max"), 5000),
            "protein_min": bounded_number(item.get("protein_min"), 500),
            "protein_max": bounded_number(item.get("protein_max"), 500),
            "fat_min": bounded_number(item.get("fat_min"), 500),
            "fat_max": bounded_number(item.get("fat_max"), 500),
            "carbs_min": bounded_number(item.get("carbs_min"), 1000),
            "carbs_max": bounded_number(item.get("carbs_max"), 1000),
        }
        for low, high in (
            ("calories_min", "calories_max"),
            ("protein_min", "protein_max"),
            ("fat_min", "fat_max"),
            ("carbs_min", "carbs_max"),
        ):
            if clean[high] < clean[low]:
                clean[low], clean[high] = clean[high], clean[low]
        clean_items.append(clean)

    if not clean_items:
        raise ValueError("AI returned no food items")

    totals = {}
    for nutrient in ("calories", "protein", "fat", "carbs"):
        totals[f"{nutrient}_min"] = round(
            sum(item[f"{nutrient}_min"] for item in clean_items), 1
        )
        totals[f"{nutrient}_max"] = round(
            sum(item[f"{nutrient}_max"] for item in clean_items), 1
        )

    confidence = str(raw.get("confidence") or "medium").lower()
    if confidence not in {"low", "medium", "high"}:
        confidence = "medium"

    return {
        "needs_clarification": False,
        "summary": str(raw.get("summary") or "Блюдо по фото")[:200],
        "confidence": confidence,
        "assumptions": [str(x)[:200] for x in (raw.get("assumptions") or [])[:4]],
        "items": clean_items,
        **totals,
    }




def food_gallery_marker(
    local_date: str,
    day_totals: dict[str, float],
) -> str:
    values = [
        round(nutrition_midpoint(day_totals, "calories")),
        round(nutrition_midpoint(day_totals, "protein")),
        round(nutrition_midpoint(day_totals, "fat")),
        round(nutrition_midpoint(day_totals, "carbs")),
    ]
    return "__food_gallery__:" + local_date + ":" + ":".join(
        str(value) for value in values
    )


async def generate_personalized_food_gallery(
    profile: dict,
    day_totals: dict[str, float],
    avoid_titles: list[str] | None = None,
) -> dict:
    """
    Generate alternatives for the user's next meal based on what is still
    missing from today's calorie and macro targets. Cards are alternatives,
    not a list that should all be eaten.
    """
    if ai_client is None:
        raise RuntimeError("OPENAI_API_KEY is missing")

    language = profile.get("language") or "ru"
    language_name = "українською" if language == "uk" else "на русском"

    eaten = {
        key: nutrition_midpoint(day_totals, key)
        for key in ("calories", "protein", "fat", "carbs")
    }
    targets = {
        "calories": float(profile.get("calorie_target") or 0),
        "protein": float(profile.get("protein_g") or 0),
        "fat": float(profile.get("fat_g") or 0),
        "carbs": float(profile.get("carbs_g") or 0),
    }
    left = {
        key: max(0.0, targets[key] - eaten[key])
        for key in targets
    }
    exceeded = {
        key: max(0.0, eaten[key] - targets[key])
        for key in targets
    }

    local_now = datetime.now(ZoneInfo(profile["timezone"]))
    fasting_note = "режим не включён"
    if profile.get("fasting_mode"):
        try:
            fasting = fasting_status_data(profile, local_now)
            if fasting["phase"] == "eat":
                fasting_note = (
                    f"сейчас окно питания; до закрытия "
                    f"{fasting['remaining_text']}"
                )
            else:
                fasting_note = (
                    f"сейчас период без еды; до открытия "
                    f"{fasting['remaining_text']}"
                )
        except ValueError:
            fasting_note = "режим указан, но время не настроено"

    previous_titles = [
        str(title).strip()[:100]
        for title in (avoid_titles or [])[-120:]
        if str(title).strip()
    ]
    avoid_text = (
        "\nНе повторяй эти уже показанные названия:\n- "
        + "\n- ".join(previous_titles)
        if previous_titles
        else ""
    )

    if left["calories"] < 150:
        energy_rule = (
            "Дневной ориентир почти набран. Предлагай небольшие лёгкие блюда "
            "или варианты только на случай реального голода; не призывай голодать."
        )
    else:
        max_recipe = max(250, min(750, round(left["calories"] * 0.70)))
        min_recipe = max(120, min(350, round(left["calories"] * 0.20)))
        energy_rule = (
            f"Каждый рецепт должен быть альтернативой следующего приёма пищи "
            f"примерно на {min_recipe}–{max_recipe} ккал и помещаться в остаток дня."
        )

    instructions = f"""
Ты создаёшь персональную галерею вкусных диетических рецептов для Telegram-бота minus_kg.
Отвечай {language_name}. Это не лечебная диета и не медицинское назначение.

Ориентир пользователя на день:
- {targets['calories']:.0f} ккал
- белки {targets['protein']:.0f} г
- жиры {targets['fat']:.0f} г
- углеводы {targets['carbs']:.0f} г

Уже записано сегодня приблизительно:
- {eaten['calories']:.0f} ккал
- белки {eaten['protein']:.0f} г
- жиры {eaten['fat']:.0f} г
- углеводы {eaten['carbs']:.0f} г

Осталось до ориентира:
- {left['calories']:.0f} ккал
- белки {left['protein']:.0f} г
- жиры {left['fat']:.0f} г
- углеводы {left['carbs']:.0f} г

Превышение, если есть:
- калории {exceeded['calories']:.0f}
- белки {exceeded['protein']:.0f} г
- жиры {exceeded['fat']:.0f} г
- углеводы {exceeded['carbs']:.0f} г

Интервальное питание: {fasting_note}.
{energy_rule}

Создай 6 РАЗНЫХ рецептов-карточек. Это альтернативы: пользователь выбирает один,
а не должен съесть все шесть. Рецепты должны быть вкусными, простыми и из обычных
продуктов, доступных в Украине. Разнообразь блюда: салат, горячее, суп, завтрак,
перекус, десерт без чрезмерного сахара — только если это соответствует остатку БЖУ.

Правила:
- если белок уже набран, не предлагай огромные порции мяса;
- если жиры набраны, готовь без жарки, майонеза и большого количества масла;
- если углеводы набраны, уменьши крупы, хлеб, сахар и сладкие соусы;
- если калории набраны, предложи небольшие овощные/кисломолочные варианты
  только при голоде и прямо укажи, что не нужно компенсировать день голодовкой;
- не используй БАДы, детокс, мочегонные и экстремальные ограничения;
- максимум 7 ингредиентов и 4 коротких шага;
- числа калорий и БЖУ должны быть реалистичными;
- названия должны быть разными и привлекательными.
{avoid_text}

Верни ТОЛЬКО валидный компактный JSON:
{{
  "title": "Персональная подборка",
  "daily_note": "коротко: что сейчас лучше добирать и что карточки являются альтернативами",
  "recipes": [
    {{
      "title": "название",
      "meal_type": "тип приёма пищи",
      "portion": "1 порция, примерно ... г",
      "ingredients": ["ингредиент — количество"],
      "steps": ["короткий шаг"],
      "calories": 0,
      "protein": 0,
      "fat": 0,
      "carbs": 0,
      "tip": "почему этот вариант подходит сегодня"
    }}
  ]
}}
"""

    raw = await request_valid_menu_json(
        instructions,
        max_output_tokens=5200,
        retry_recipe_count=6,
    )

    recipes: list[dict] = []
    for item in (raw.get("recipes") or [])[:6]:
        if not isinstance(item, dict):
            continue

        ingredients = [
            str(value).strip()[:140]
            for value in (item.get("ingredients") or [])[:7]
            if str(value).strip()
        ]
        steps = [
            str(value).strip()[:220]
            for value in (item.get("steps") or [])[:4]
            if str(value).strip()
        ]
        title = str(item.get("title") or "").strip()[:100]
        if not title or not ingredients or not steps:
            continue

        recipes.append(
            {
                "title": title,
                "meal_type": str(item.get("meal_type") or "")[:50],
                "portion": str(item.get("portion") or "1 порция")[:100],
                "ingredients": ingredients,
                "steps": steps,
                "calories": round(bounded_number(item.get("calories"), 1500)),
                "protein": round(bounded_number(item.get("protein"), 180)),
                "fat": round(bounded_number(item.get("fat"), 150)),
                "carbs": round(bounded_number(item.get("carbs"), 300)),
                "tip": str(item.get("tip") or "")[:220],
            }
        )

    if not recipes:
        raise ValueError("AI returned no personalized recipes")

    new_titles = [recipe["title"] for recipe in recipes]
    seen_titles = list(dict.fromkeys(previous_titles + new_titles))[-120:]

    default_note = (
        "Оберіть одну картку як наступний прийом їжі; усі рецепти їсти не потрібно."
        if language == "uk"
        else
        "Выберите одну карточку как следующий приём пищи; есть все рецепты не нужно."
    )

    return {
        "kind": "food_gallery",
        "title": str(raw.get("title") or (
            "Персональна добірка" if language == "uk"
            else "Персональная подборка"
        ))[:120],
        "daily_note": str(raw.get("daily_note") or default_note)[:500],
        "optional_purchases": [],
        "seen_titles": seen_titles,
        "recipes": recipes,
    }


def food_gallery_keyboard(
    session_id: str,
    index: int,
    total: int,
    language: str,
) -> InlineKeyboardMarkup:
    previous_index = (index - 1) % total
    next_index = (index + 1) % total

    if language == "uk":
        next_text = "Далі"
        choose_text = "Обираю цю страву"
        more_text = "Показати ще 6 варіантів"
        refresh_text = "Оновити за записами сьогодні"
    else:
        next_text = "Дальше"
        choose_text = "Выбираю это блюдо"
        more_text = "Показать ещё 6 вариантов"
        refresh_text = "Обновить по записям за сегодня"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=(
                        f"food_gallery_nav:{session_id}:{previous_index}"
                    ),
                    style=ButtonStyle.PRIMARY,
                ),
                InlineKeyboardButton(
                    text=f"{index + 1}/{total}",
                    callback_data="food_gallery_noop",
                    style=ButtonStyle.SUCCESS,
                ),
                InlineKeyboardButton(
                    text=f"{next_text} ➡️",
                    callback_data=(
                        f"food_gallery_nav:{session_id}:{next_index}"
                    ),
                    style=ButtonStyle.PRIMARY,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"✅ {choose_text}",
                    callback_data=(
                        f"food_gallery_choose:{session_id}:{index}"
                    ),
                    style=ButtonStyle.SUCCESS,
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"✨ {more_text}",
                    callback_data=f"food_gallery_more:{session_id}",
                    style=ButtonStyle.SUCCESS,
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"📊 {refresh_text}",
                    callback_data="menu:foods",
                    style=ButtonStyle.PRIMARY,
                )
            ],
        ]
    )


def selected_recipe_keyboard(
    session_id: str,
    index: int,
    language: str,
) -> InlineKeyboardMarkup:
    if language == "uk":
        cook_text = "Готувати крок за кроком"
        eaten_text = "Я вже з'їв/з'їла цю порцію"
        other_text = "Обрати іншу страву"
    else:
        cook_text = "Готовить шаг за шагом"
        eaten_text = "Я уже съел(а) эту порцию"
        other_text = "Выбрать другое блюдо"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    f"🧑‍🍳 {cook_text}",
                    f"recipe_step:{session_id}:{index}:0",
                    ButtonStyle.PRIMARY,
                )
            ],
            [
                button(
                    f"🍽 {eaten_text}",
                    f"recipe_eat:{session_id}:{index}",
                    ButtonStyle.SUCCESS,
                )
            ],
            [
                button(
                    f"🔁 {other_text}",
                    f"food_gallery_show:{session_id}:{index}",
                    ButtonStyle.PRIMARY,
                )
            ],
        ]
    )


def cooking_step_keyboard(
    session_id: str,
    recipe_index: int,
    step_index: int,
    total_steps: int,
    language: str,
) -> InlineKeyboardMarkup:
    rows = []
    navigation = []

    if step_index > 0:
        navigation.append(
            button(
                "⬅️ Назад",
                (
                    f"recipe_step:{session_id}:"
                    f"{recipe_index}:{step_index - 1}"
                ),
                ButtonStyle.PRIMARY,
            )
        )

    if step_index < total_steps - 1:
        navigation.append(
            button(
                "Готово, далі ➡️"
                if language == "uk"
                else "Готово, дальше ➡️",
                (
                    f"recipe_step:{session_id}:"
                    f"{recipe_index}:{step_index + 1}"
                ),
                ButtonStyle.SUCCESS,
            )
        )

    if navigation:
        rows.append(navigation)

    if step_index == total_steps - 1:
        rows.append(
            [
                button(
                    "🍽 Записати як з'їдене"
                    if language == "uk"
                    else "🍽 Записать как съеденное",
                    f"recipe_eat:{session_id}:{recipe_index}",
                    ButtonStyle.SUCCESS,
                )
            ]
        )

    rows.append(
        [
            button(
                "⏹ Закрити рецепт"
                if language == "uk"
                else "⏹ Закрыть рецепт",
                f"recipe_close:{session_id}:{recipe_index}",
                ButtonStyle.DANGER,
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_food_gallery_card(
    message: Message,
    session_id: str,
    gallery: dict,
    index: int,
    language: str,
) -> None:
    recipes = gallery["recipes"]
    recipe = recipes[index]
    image = render_recipe_card(
        recipe=recipe,
        index=index,
        total=len(recipes),
        language=language,
    )
    caption = recipe_caption(
        gallery,
        recipe,
        index,
        len(recipes),
        language,
    )
    keyboard = food_gallery_keyboard(
        session_id,
        index,
        len(recipes),
        language,
    )

    if image:
        await message.answer_photo(
            BufferedInputFile(
                image,
                filename=f"food_gallery_{index + 1}.png",
            ),
            caption=caption,
            reply_markup=keyboard,
        )
    else:
        ingredients = "\n".join(
            f"• {value}" for value in recipe["ingredients"]
        )
        steps = "\n".join(
            f"{number}. {value}"
            for number, value in enumerate(recipe["steps"], start=1)
        )
        await message.answer(
            f"{caption}\n\n{ingredients}\n\n{steps}",
            reply_markup=keyboard,
        )


def default_day_menu_slots(
    meals_count: int,
    language: str,
) -> list[dict[str, str]]:
    meals_count = max(2, min(4, int(meals_count or 3)))

    if language == "uk":
        variants = {
            2: [
                {"name": "Перший прийом їжі", "time": "11:00"},
                {"name": "Вечеря", "time": "19:00"},
            ],
            3: [
                {"name": "Сніданок", "time": "09:00"},
                {"name": "Обід", "time": "14:00"},
                {"name": "Вечеря", "time": "19:00"},
            ],
            4: [
                {"name": "Сніданок", "time": "08:30"},
                {"name": "Обід", "time": "13:00"},
                {"name": "Перекус", "time": "16:30"},
                {"name": "Вечеря", "time": "20:00"},
            ],
        }
    else:
        variants = {
            2: [
                {"name": "Первый приём пищи", "time": "11:00"},
                {"name": "Ужин", "time": "19:00"},
            ],
            3: [
                {"name": "Завтрак", "time": "09:00"},
                {"name": "Обед", "time": "14:00"},
                {"name": "Ужин", "time": "19:00"},
            ],
            4: [
                {"name": "Завтрак", "time": "08:30"},
                {"name": "Обед", "time": "13:00"},
                {"name": "Перекус", "time": "16:30"},
                {"name": "Ужин", "time": "20:00"},
            ],
        }
    return variants[meals_count]


def prepare_day_menu_slots(
    profile: dict,
    schedule: list[dict],
) -> list[dict[str, str]]:
    language = profile.get("language") or "ru"
    meals_count = max(
        2,
        min(4, int(profile.get("meals_count") or 3)),
    )
    defaults = default_day_menu_slots(meals_count, language)
    slots: list[dict[str, str]] = []

    for index in range(meals_count):
        row = schedule[index] if index < len(schedule) else {}
        default = defaults[index]
        name = str(
            row.get("meal_name")
            or default["name"]
        ).strip()
        meal_time = str(
            row.get("meal_time")
            or default["time"]
        ).strip()

        slots.append(
            {
                "name": name[:60],
                "time": meal_time[:5],
            }
        )

    return slots


async def generate_menu_from_products(
    profile: dict,
    products_text: str,
    meal_slots: list[dict[str, str]],
) -> dict:
    """
    Generate a complete one-day plan from the user's available products.

    This feature is deliberately separate from «Что лучше съесть»:
    it creates a fresh full-day plan and does not subtract food already
    logged today.
    """
    if ai_client is None:
        raise RuntimeError("OPENAI_API_KEY is missing")

    language = profile.get("language") or "ru"
    language_name = "українською" if language == "uk" else "на русском"
    meal_count = len(meal_slots)

    slots_text = "\n".join(
        f"{index + 1}. {slot['time']} — {slot['name']}"
        for index, slot in enumerate(meal_slots)
    )

    instructions = f"""
Ты создаёшь полноценный план питания на ОДИН ЦЕЛЫЙ ДЕНЬ
для Telegram-бота minus_kg.
Отвечай {language_name}. Не ставь диагнозы и не обещай похудение.

Пользователь перечислил продукты, которые есть дома:
{products_text[:3000]}

Личный ориентир на день:
- энергия: около {profile.get('calorie_target') or 0} килокалорий
- белок: около {profile.get('protein_g') or 0} г
- жиры: около {profile.get('fat_g') or 0} г
- углеводы: около {profile.get('carbs_g') or 0} г

Нужно создать РОВНО {meal_count} приёма пищи по расписанию:
{slots_text}

КРИТИЧЕСКИЕ ПРАВИЛА:
- Это именно план на полный день, а не список альтернатив.
- Каждый пункт расписания получает ровно одно отдельное блюдо.
- Все блюда вместе должны давать примерно 90–105% дневной энергии.
- Суммарный белок, жиры и углеводы должны быть разумно близки
  к личным ориентирам, но без искусственного подгона до единицы.
- Используй прежде всего перечисленные пользователем продукты.
- Не используй один и тот же основной продукт во всех блюдах,
  если есть возможность разнообразить меню.
- Вода, соль, перец и обычные специи допустимы.
- Масло используй только если оно есть в списке. Иначе готовь без масла
  либо внеси небольшое количество в необязательные покупки.
- Не выдумывай дорогие, редкие или лечебные продукты.
- Дай реальные порции и максимум 5 коротких шагов приготовления.
- В каждом рецепте максимум 8 строк ингредиентов.
- Если продуктов недостаточно, всё равно составь полный разумный день,
  а недостающее внеси в «optional_purchases».
- Отдельно перечисли, какие продукты останутся после приготовления.
- При медицинских ограничениях не назначай лечебную диету
  и не создавай экстремальный дефицит.
- Не добавляй уже съеденную сегодня пищу: этот раздел создаёт
  самостоятельный план на новый полный день.

Верни ТОЛЬКО валидный JSON без markdown:
{{
  "title": "короткое название меню на день",
  "daily_note": "понятное объяснение общей логики меню",
  "optional_purchases": ["что можно докупить, только если действительно нужно"],
  "leftovers": ["что приблизительно останется из продуктов"],
  "recipes": [
    {{
      "title": "название блюда",
      "meal_type": "название приёма пищи из расписания",
      "meal_time": "время из расписания",
      "portion": "1 порция, примерно ... г",
      "ingredients": ["продукт — количество"],
      "steps": ["шаг 1", "шаг 2"],
      "calories": 0,
      "protein": 0,
      "fat": 0,
      "carbs": 0,
      "tip": "почему блюдо подходит в этой части дня"
    }}
  ]
}}
"""

    raw = await request_valid_menu_json(
        instructions,
        max_output_tokens=4300,
        retry_recipe_count=meal_count,
    )

    raw_recipes = [
        item
        for item in (raw.get("recipes") or [])
        if isinstance(item, dict)
    ]

    # A second generation is cheaper than showing an incomplete daily plan.
    if len(raw_recipes) < meal_count:
        raw = await request_valid_menu_json(
            instructions
            + f"""

ПРЕДЫДУЩАЯ ПОПЫТКА БЫЛА НЕПОЛНОЙ.
Верни РОВНО {meal_count} рецептов — по одному для каждого пункта:
{slots_text}
Нельзя пропускать завтрак, обед, перекус или ужин.
""",
            max_output_tokens=4300,
            retry_recipe_count=meal_count,
        )
        raw_recipes = [
            item
            for item in (raw.get("recipes") or [])
            if isinstance(item, dict)
        ]

    recipes = []
    for index, item in enumerate(raw_recipes[:meal_count]):
        ingredients = [
            str(value).strip()[:140]
            for value in (item.get("ingredients") or [])[:8]
            if str(value).strip()
        ]
        steps = [
            str(value).strip()[:220]
            for value in (item.get("steps") or [])[:5]
            if str(value).strip()
        ]
        if not ingredients or not steps:
            continue

        slot = meal_slots[index]
        recipes.append(
            {
                "title": str(item.get("title") or "Блюдо")[:100],
                "meal_type": slot["name"],
                "meal_time": slot["time"],
                "portion": str(
                    item.get("portion") or "1 порция"
                )[:100],
                "ingredients": ingredients,
                "steps": steps,
                "calories": round(
                    bounded_number(item.get("calories"), 2500)
                ),
                "protein": round(
                    bounded_number(item.get("protein"), 250)
                ),
                "fat": round(
                    bounded_number(item.get("fat"), 250)
                ),
                "carbs": round(
                    bounded_number(item.get("carbs"), 500)
                ),
                "tip": str(item.get("tip") or "")[:260],
            }
        )

    if len(recipes) != meal_count:
        raise ValueError(
            f"AI returned {len(recipes)} valid meals instead of {meal_count}"
        )

    total_calories = sum(
        float(recipe.get("calories") or 0)
        for recipe in recipes
    )
    total_protein = sum(
        float(recipe.get("protein") or 0)
        for recipe in recipes
    )
    total_fat = sum(
        float(recipe.get("fat") or 0)
        for recipe in recipes
    )
    total_carbs = sum(
        float(recipe.get("carbs") or 0)
        for recipe in recipes
    )

    return {
        "menu_type": "full_day_products",
        "title": str(
            raw.get("title")
            or (
                "Меню на день з ваших продуктів"
                if language == "uk"
                else "Меню на день из ваших продуктов"
            )
        )[:120],
        "daily_note": str(
            raw.get("daily_note") or ""
        )[:650],
        "optional_purchases": [
            str(value).strip()[:140]
            for value in (
                raw.get("optional_purchases") or []
            )[:5]
            if str(value).strip()
        ],
        "leftovers": [
            str(value).strip()[:140]
            for value in (raw.get("leftovers") or [])[:8]
            if str(value).strip()
        ],
        "totals": {
            "calories": round(total_calories),
            "protein": round(total_protein),
            "fat": round(total_fat),
            "carbs": round(total_carbs),
        },
        "recipes": recipes,
    }


def recipe_navigation_keyboard(
    session_id: str,
    index: int,
    total: int,
    language: str,
) -> InlineKeyboardMarkup:
    previous_index = (index - 1) % total
    next_index = (index + 1) % total
    back_text = "Назад" if language == "ru" else "Назад"
    next_text = "Далі" if language == "uk" else "Дальше"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"⬅️ {back_text}",
                    callback_data=f"recipe_nav:{session_id}:{previous_index}",
                    style=ButtonStyle.PRIMARY,
                ),
                InlineKeyboardButton(
                    text=f"{index + 1}/{total}",
                    callback_data="recipe_noop",
                    style=ButtonStyle.SUCCESS,
                ),
                InlineKeyboardButton(
                    text=f"{next_text} ➡️",
                    callback_data=f"recipe_nav:{session_id}:{next_index}",
                    style=ButtonStyle.PRIMARY,
                ),
            ]
        ]
    )



def recipe_mood_label(recipe: dict, language: str) -> str:
    text = (
        f"{recipe.get('meal_type') or ''} "
        f"{recipe.get('title') or ''}"
    ).lower()

    if any(word in text for word in ("суп", "борщ", "рагу", "stew")):
        return "🫶 Затишний варіант" if language == "uk" else "🫶 Уютный вариант"
    if any(word in text for word in ("салат", "овоч", "овощ")):
        return (
            "🥗 Легкий, але ситний"
            if language == "uk"
            else "🥗 Лёгкий, но сытный"
        )
    if any(word in text for word in ("снідан", "завтрак", "каша", "омлет")):
        return (
            "🌤 Спокійний початок дня"
            if language == "uk"
            else "🌤 Спокойное начало дня"
        )
    if any(word in text for word in ("перекус", "десерт", "ягод", "фрукт")):
        return (
            "😋 Коли хочеться смачного"
            if language == "uk"
            else "😋 Когда хочется вкусного"
        )
    if len(recipe.get("steps") or []) <= 3:
        return (
            "⏱ Швидко й без метушні"
            if language == "uk"
            else "⏱ Быстро и без суеты"
        )
    return (
        "🍽 Ситний варіант"
        if language == "uk"
        else "🍽 Сытный вариант"
    )


def recipe_caption(
    menu_data: dict,
    recipe: dict,
    index: int,
    total: int,
    language: str,
) -> str:
    optional = menu_data.get("optional_purchases") or []
    optional_text = ""
    if optional and index == 0:
        optional_text = (
            "\n\n🛒 За бажанням можна докупити:\n"
            if language == "uk"
            else "\n\n🛒 При желании можно докупить:\n"
        ) + "\n".join(f"• {item}" for item in optional)

    note = str(menu_data.get("daily_note") or "").strip()
    note_text = ""
    if note and index == 0:
        note_text = (
            f"\n\n🥗 Чому ця добірка підходить сьогодні\n{note}"
            if language == "uk"
            else
            f"\n\n🥗 Почему эта подборка подходит сегодня\n{note}"
        )

    tip = str(recipe.get("tip") or "").strip()
    if language == "uk":
        tip_text = (
            f"\n\n💡 Чому варіант підходить вам сьогодні\n{tip}"
            if tip else ""
        )
        choice_text = (
            f"\n\n👉 Це варіант {index + 1} із {total}. "
            "Оберіть одну страву, яка вам подобається; "
            "усі рецепти одразу готувати й їсти не потрібно."
        )
        estimate_text = (
            "\n\nДля щоденника можна використовувати цю робочу оцінку. "
            "Точність до однієї калорії не потрібна — важливі порція "
            "та загальний баланс за день."
        )
        nutrition_text = (
            f"🔥 Енергія — близько {recipe.get('calories', 0)} кілокалорій\n"
            f"🥩 Білок — {recipe.get('protein', 0)} г\n"
            f"🥑 Жири — {recipe.get('fat', 0)} г\n"
            f"🍚 Вуглеводи — {recipe.get('carbs', 0)} г"
        )
    else:
        tip_text = (
            f"\n\n💡 Почему вариант подходит вам сегодня\n{tip}"
            if tip else ""
        )
        choice_text = (
            f"\n\n👉 Это вариант {index + 1} из {total}. "
            "Выберите одно блюдо, которое вам нравится; "
            "готовить и есть все рецепты сразу не нужно."
        )
        estimate_text = (
            "\n\nДля дневника можно использовать эту рабочую оценку. "
            "Точность до одной калории не нужна — важны порция "
            "и общий баланс за день."
        )
        nutrition_text = (
            f"🔥 Энергия — около {recipe.get('calories', 0)} килокалорий\n"
            f"🥩 Белок — {recipe.get('protein', 0)} г\n"
            f"🥑 Жиры — {recipe.get('fat', 0)} г\n"
            f"🍚 Углеводы — {recipe.get('carbs', 0)} г"
        )

    mood = recipe_mood_label(recipe, language)

    return (
        f"{mood}\n"
        f"🍽 {recipe['title']}\n"
        f"{recipe.get('portion') or ''}\n\n"
        f"{nutrition_text}"
        f"{tip_text}"
        f"{note_text}"
        f"{optional_text}"
        f"{choice_text}"
        f"{estimate_text}"
    )[:1024]


async def send_recipe_card(
    message: Message,
    session_id: str,
    menu_data: dict,
    index: int,
    language: str,
) -> None:
    recipes = menu_data["recipes"]
    recipe = recipes[index]
    image = render_recipe_card(
        recipe=recipe,
        index=index,
        total=len(recipes),
        language=language,
    )
    caption = recipe_caption(menu_data, recipe, index, len(recipes), language)
    keyboard = recipe_navigation_keyboard(
        session_id,
        index,
        len(recipes),
        language,
    )

    if image:
        await message.answer_photo(
            BufferedInputFile(image, filename=f"recipe_{index + 1}.png"),
            caption=caption,
            reply_markup=keyboard,
        )
    else:
        ingredients = "\n".join(f"• {x}" for x in recipe["ingredients"])
        steps = "\n".join(
            f"{i + 1}. {x}" for i, x in enumerate(recipe["steps"])
        )
        await message.answer(
            f"{caption}\n\nИнгредиенты:\n{ingredients}\n\nШаги:\n{steps}",
            reply_markup=keyboard,
        )


def meal_icon(meal_name: str) -> str:
    value = (meal_name or "").lower()
    if any(word in value for word in ("снідан", "завтрак", "ранок")):
        return "🌅"
    if any(word in value for word in ("обід", "обед")):
        return "☀️"
    if any(word in value for word in ("перекус", "полдник")):
        return "🍎"
    if any(word in value for word in ("вечер", "вечеря", "ужин")):
        return "🌙"
    return "🍽"


def day_menu_overview_text(
    menu_data: dict,
    profile: dict,
    language: str,
) -> str:
    recipes = menu_data.get("recipes") or []
    totals = menu_data.get("totals") or {}

    meal_lines = []
    for index, recipe in enumerate(recipes):
        icon = meal_icon(str(recipe.get("meal_type") or ""))
        meal_lines.append(
            f"{icon} {recipe.get('meal_time') or ''} — "
            f"{recipe.get('meal_type') or ''}\n"
            f"   {recipe.get('title') or ''} · "
            f"около {recipe.get('calories', 0)} ккал"
            if language == "ru"
            else
            f"{icon} {recipe.get('meal_time') or ''} — "
            f"{recipe.get('meal_type') or ''}\n"
            f"   {recipe.get('title') or ''} · "
            f"близько {recipe.get('calories', 0)} ккал"
        )

    purchases = menu_data.get("optional_purchases") or []
    leftovers = menu_data.get("leftovers") or []

    if language == "uk":
        purchases_text = (
            "\n\n🛒 За бажанням можна докупити:\n"
            + "\n".join(f"• {item}" for item in purchases)
            if purchases
            else
            "\n\n🛒 Обов'язкових покупок немає — меню зібране "
            "з указаних продуктів."
        )
        leftovers_text = (
            "\n\n📦 Після приготування приблизно залишиться:\n"
            + "\n".join(f"• {item}" for item in leftovers)
            if leftovers
            else ""
        )
        return (
            f"📋 {menu_data.get('title') or 'Меню на день'}\n\n"
            "Це один повний план на день, а не список страв на вибір. "
            "Кожна картка — окремий прийом їжі.\n\n"
            + "\n\n".join(meal_lines)
            + "\n\n📊 Разом за планом:\n"
            f"• енергія — близько {totals.get('calories', 0)} "
            f"із вашого орієнтиру {profile.get('calorie_target', 0)} "
            "кілокалорій;\n"
            f"• білок — {totals.get('protein', 0)} із "
            f"{profile.get('protein_g', 0)} г;\n"
            f"• жири — {totals.get('fat', 0)} із "
            f"{profile.get('fat_g', 0)} г;\n"
            f"• вуглеводи — {totals.get('carbs', 0)} із "
            f"{profile.get('carbs_g', 0)} г.\n\n"
            f"💡 {menu_data.get('daily_note') or 'Меню можна змінювати під апетит і розклад.'}"
            f"{purchases_text}"
            f"{leftovers_text}\n\n"
            "Натисніть на потрібний прийом їжі, щоб побачити "
            "картку, інгредієнти та приготування. "
            "Страва потрапить у щоденник тільки після підтвердження, "
            "що ви її з'їли."
        )[:4096]

    purchases_text = (
        "\n\n🛒 При желании можно докупить:\n"
        + "\n".join(f"• {item}" for item in purchases)
        if purchases
        else
        "\n\n🛒 Обязательных покупок нет — меню собрано "
        "из указанных продуктов."
    )
    leftovers_text = (
        "\n\n📦 После приготовления примерно останется:\n"
        + "\n".join(f"• {item}" for item in leftovers)
        if leftovers
        else ""
    )
    return (
        f"📋 {menu_data.get('title') or 'Меню на день'}\n\n"
        "Это один полный план на день, а не список блюд на выбор. "
        "Каждая карточка — отдельный приём пищи.\n\n"
        + "\n\n".join(meal_lines)
        + "\n\n📊 Итого по плану:\n"
        f"• энергия — около {totals.get('calories', 0)} "
        f"из вашего ориентира {profile.get('calorie_target', 0)} "
        "килокалорий;\n"
        f"• белок — {totals.get('protein', 0)} из "
        f"{profile.get('protein_g', 0)} г;\n"
        f"• жиры — {totals.get('fat', 0)} из "
        f"{profile.get('fat_g', 0)} г;\n"
        f"• углеводы — {totals.get('carbs', 0)} из "
        f"{profile.get('carbs_g', 0)} г.\n\n"
        f"💡 {menu_data.get('daily_note') or 'Меню можно менять под аппетит и расписание.'}"
        f"{purchases_text}"
        f"{leftovers_text}\n\n"
        "Нажмите на нужный приём пищи, чтобы увидеть карточку, "
        "ингредиенты и приготовление. Блюдо попадёт в дневник "
        "только после подтверждения, что вы его съели."
    )[:4096]


def day_menu_overview_keyboard(
    session_id: str,
    menu_data: dict,
    language: str,
) -> InlineKeyboardMarkup:
    rows = []
    recipes = menu_data.get("recipes") or []

    for index, recipe in enumerate(recipes):
        icon = meal_icon(str(recipe.get("meal_type") or ""))
        label = (
            f"{icon} {recipe.get('meal_time') or ''} · "
            f"{recipe.get('meal_type') or ''}"
        )[:58]
        rows.append(
            [
                button(
                    label,
                    f"day_menu_open:{session_id}:{index}",
                    (
                        ButtonStyle.SUCCESS
                        if index % 2
                        else ButtonStyle.PRIMARY
                    ),
                )
            ]
        )

    rows.append(
        [
            button(
                "🔄 Створити інше меню"
                if language == "uk"
                else "🔄 Создать другое меню",
                "menu:create_menu",
                ButtonStyle.SUCCESS,
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def day_menu_recipe_keyboard(
    session_id: str,
    index: int,
    total: int,
    language: str,
) -> InlineKeyboardMarkup:
    previous_index = (index - 1) % total
    next_index = (index + 1) % total

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "⬅️ Назад",
                    f"day_menu_nav:{session_id}:{previous_index}",
                    ButtonStyle.PRIMARY,
                ),
                button(
                    f"{index + 1}/{total}",
                    "day_menu_noop",
                    ButtonStyle.SUCCESS,
                ),
                button(
                    "Далі ➡️" if language == "uk" else "Дальше ➡️",
                    f"day_menu_nav:{session_id}:{next_index}",
                    ButtonStyle.PRIMARY,
                ),
            ],
            [
                button(
                    "🧑‍🍳 Готувати крок за кроком"
                    if language == "uk"
                    else "🧑‍🍳 Готовить шаг за шагом",
                    f"recipe_step:{session_id}:{index}:0",
                    ButtonStyle.PRIMARY,
                )
            ],
            [
                button(
                    "🍽 Я вже з'їв/з'їла цю порцію"
                    if language == "uk"
                    else "🍽 Я уже съел(а) эту порцию",
                    f"recipe_eat:{session_id}:{index}",
                    ButtonStyle.SUCCESS,
                )
            ],
            [
                button(
                    "📋 Повернутися до плану дня"
                    if language == "uk"
                    else "📋 Вернуться к плану дня",
                    f"day_menu_overview:{session_id}",
                    ButtonStyle.PRIMARY,
                )
            ],
        ]
    )


def day_menu_recipe_caption(
    recipe: dict,
    index: int,
    total: int,
    language: str,
) -> str:
    tip = str(recipe.get("tip") or "").strip()
    icon = meal_icon(str(recipe.get("meal_type") or ""))

    if language == "uk":
        return (
            f"{icon} {recipe.get('meal_time') or ''} — "
            f"{recipe.get('meal_type') or ''}\n"
            f"🍽 {recipe.get('title') or ''}\n"
            f"{recipe.get('portion') or '1 порція'}\n\n"
            f"🔥 Енергія — близько {recipe.get('calories', 0)} "
            "кілокалорій\n"
            f"🥩 Білок — {recipe.get('protein', 0)} г\n"
            f"🥑 Жири — {recipe.get('fat', 0)} г\n"
            f"🍚 Вуглеводи — {recipe.get('carbs', 0)} г"
            + (
                f"\n\n💡 Чому страва стоїть у цьому місці дня\n{tip}"
                if tip else ""
            )
            + f"\n\n📋 Це прийом їжі {index + 1} із {total} "
            "у вашому плані на день. Він не записується як з'їдений, "
            "доки ви самі не підтвердите це кнопкою."
        )[:1024]

    return (
        f"{icon} {recipe.get('meal_time') or ''} — "
        f"{recipe.get('meal_type') or ''}\n"
        f"🍽 {recipe.get('title') or ''}\n"
        f"{recipe.get('portion') or '1 порция'}\n\n"
        f"🔥 Энергия — около {recipe.get('calories', 0)} "
        "килокалорий\n"
        f"🥩 Белок — {recipe.get('protein', 0)} г\n"
        f"🥑 Жиры — {recipe.get('fat', 0)} г\n"
        f"🍚 Углеводы — {recipe.get('carbs', 0)} г"
        + (
            f"\n\n💡 Почему блюдо стоит в этой части дня\n{tip}"
            if tip else ""
        )
        + f"\n\n📋 Это приём пищи {index + 1} из {total} "
        "в вашем плане на день. Он не записывается как съеденный, "
        "пока вы сами не подтвердите это кнопкой."
    )[:1024]


async def send_day_menu_recipe_card(
    message: Message,
    session_id: str,
    menu_data: dict,
    index: int,
    language: str,
) -> None:
    recipes = menu_data.get("recipes") or []
    if not recipes:
        return

    index %= len(recipes)
    recipe = recipes[index]
    image = render_recipe_card(
        recipe=recipe,
        index=index,
        total=len(recipes),
        language=language,
    )
    caption = day_menu_recipe_caption(
        recipe,
        index,
        len(recipes),
        language,
    )
    keyboard = day_menu_recipe_keyboard(
        session_id,
        index,
        len(recipes),
        language,
    )

    if image:
        await message.answer_photo(
            BufferedInputFile(
                image,
                filename=f"day_menu_{index + 1}.png",
            ),
            caption=caption,
            reply_markup=keyboard,
        )
    else:
        ingredients = "\n".join(
            f"• {item}"
            for item in recipe.get("ingredients", [])
        )
        steps = "\n".join(
            f"{number}. {item}"
            for number, item in enumerate(
                recipe.get("steps", []),
                start=1,
            )
        )
        await message.answer(
            f"{caption}\n\n{ingredients}\n\n{steps}",
            reply_markup=keyboard,
        )


AI_TRIAL_DAILY_LIMIT = 5
AI_PAID_DAILY_LIMIT = 30


def format_history(messages: list[dict], language: str) -> str:
    if not messages:
        return "нет предыдущего диалога" if language == "ru" else "попереднього діалогу немає"
    labels = {
        "user": "Пользователь" if language == "ru" else "Користувач",
        "assistant": "minus_kg",
    }
    lines = []
    for item in messages[-8:]:
        role = labels.get(item.get("role"), "Сообщение")
        content = str(item.get("content") or "").strip().replace("\x00", "")
        if content:
            lines.append(f"{role}: {content[:1200]}")
    return "\n".join(lines) or ("нет предыдущего диалога" if language == "ru" else "попереднього діалогу немає")



def clean_ai_answer(text: str) -> str:
    """
    Convert occasional Markdown from the model into clean Telegram plain text.
    This keeps answers readable even if the model ignores formatting rules.
    """
    value = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\x00", "").strip()

    # Remove fenced code markers and inline code markers.
    value = re.sub(r"```(?:[a-zA-Z0-9_+-]+)?\s*", "", value)
    value = value.replace("```", "")
    value = value.replace("`", "")

    # Keep link text but remove Markdown URL wrappers.
    value = re.sub(r"\[([^\]]+)\]\((?:https?://)?[^)]+\)", r"\1", value)

    cleaned_lines: list[str] = []
    for raw_line in value.split("\n"):
        line = raw_line.strip()

        # Markdown headings become ordinary clean headings.
        line = re.sub(r"^#{1,6}\s*", "", line)

        # Markdown quotes become normal text.
        line = re.sub(r"^>\s*", "", line)

        # Markdown bullets become Telegram-friendly bullets.
        line = re.sub(r"^[*-]\s+", "• ", line)

        # Remove bold/italic markers.
        line = line.replace("**", "")
        line = line.replace("__", "")
        line = re.sub(r"(?<!\w)\*(?=\S)|(?<=\S)\*(?!\w)", "", line)
        line = re.sub(r"(?<!\w)_(?=\S)|(?<=\S)_(?!\w)", "", line)

        # Avoid accidental multiple spaces.
        line = re.sub(r"[ \t]{2,}", " ", line).strip()
        cleaned_lines.append(line)

    value = "\n".join(cleaned_lines)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()

    if not value:
        raise ValueError("AI returned an empty formatted answer")
    return value


async def ask_weight_ai(
    profile: dict,
    question: str,
    history: list[dict],
    day_totals: dict[str, float],
) -> str:
    if ai_client is None:
        raise RuntimeError("OPENAI_API_KEY is missing")

    language = profile.get("language") or "ru"
    language_rule = "Отвечай на русском языке." if language == "ru" else "Відповідай українською мовою."
    safety_restricted = bool(profile.get("safety_restricted"))
    medical_note = (
        "У пользователя отмечены медицинские или особые обстоятельства. Не назначай ему дефицит, интервальное голодание, дозировки, лечение или строгий рацион. Рекомендуй согласовать персональный план с врачом."
        if language == "ru"
        else
        "Користувач зазначив медичні або особливі обставини. Не призначай дефіцит, інтервальне харчування, дозування, лікування чи суворий раціон. Радь погодити персональний план із лікарем."
    ) if safety_restricted else (
        "У пользователя не отмечены особые медицинские обстоятельства, но всё равно не ставь диагнозов и не заменяй врача."
        if language == "ru"
        else
        "Користувач не зазначив особливих медичних обставин, але все одно не став діагнозів і не замінюй лікаря."
    )

    history_text = format_history(history, language)
    instructions = f"""
Ты — minus_kg, заботливый цифровой собеседник внутри Telegram-бота.
Ты помогаешь с устойчивым снижением веса, питанием, привычками, сном и движением,
но также умеешь спокойно выслушать человека, когда ему тяжело, одиноко, тревожно,
стыдно за еду или кажется, что ничего не получается.
Ты не человек, не психолог и не врач. Не выдавай себя за специалиста и не ставь диагнозов.
{language_rule}

Правила:
- Пиши тепло, понятно и по-человечески. Обычно 3–8 коротких абзацев.
- Если человек жалуется или делится переживанием, сначала коротко покажи, что ты понял его чувство. Не преувеличивай и не говори пустое «всё будет хорошо».
- После эмоционального сообщения задай один простой открытый вопрос. Не устраивай допрос и не выдавай длинный список советов.
- Предлагай один маленький следующий шаг, а не полную перестройку жизни за один вечер.
- Если человек просто хочет выговориться, не переводи разговор насильно на калории и вес.
- Говори как внимательный поддерживающий собеседник, но никогда не называй себя психологом, терапевтом, врачом или настоящим другом-человеком.
- Термины объясняй обычными словами. Не используй сокращение «КБЖУ» без расшифровки; лучше пиши «калории, белки, жиры и углеводы».
- Не используй Markdown-разметку: никаких **звёздочек**, __подчёркиваний__, # решёток, обратных кавычек и таблиц.
- Для списков используй только обычный символ «•».
- Заголовки пиши обычным текстом с подходящим эмодзи, например «🥗 Лучший вариант».
- Используй данные профиля только когда они помогают ответу. Не придумывай отсутствующие сведения.
- Не обещай гарантированное похудение и не стыди человека за еду или вес.
- Не назначай лекарства, БАДы, слабительные, мочегонные, рвоту, голодание сутками, экстремально низкую калорийность или опасные тренировки.
- Не поддерживай расстройство пищевого поведения, самонаказание, очистительное поведение или навязчивое ограничение еды.
- При симптомах, требующих диагностики, беременности/кормлении, диабете, болезнях почек/печени/сердца, после бариатрической операции или при лекарствах, влияющих на вес, советуй обратиться к квалифицированному врачу.
- При острой боли в груди, потере сознания, выраженной одышке, тяжёлой аллергической реакции или угрозе самоповреждения советуй срочно обратиться в экстренную службу своей страны.
- Для рецептов и меню учитывай дневной ориентир, но давай диапазоны и варианты замены.
- Не называй расчёты медицинским назначением.
{medical_note}

Профиль:
Имя: {profile.get('display_name') or 'не указано'}
Рост: {profile.get('height_cm') or 'не указан'} см
Текущий вес: {profile.get('current_weight_kg') or 'не указан'} кг
Цель: {profile.get('target_weight_kg') or 'не указана'} кг
Ориентир калорий: {profile.get('calorie_target') or 'не рассчитан'} ккал
Белки/жиры/углеводы: {profile.get('protein_g') or 0}/{profile.get('fat_g') or 0}/{profile.get('carbs_g') or 0} г
Сегодня записано: {day_totals.get('calories_min', 0):.0f}–{day_totals.get('calories_max', 0):.0f} ккал

Предыдущий диалог:
{history_text}
"""

    response = await ai_client.responses.create(
        model=settings.openai_model,
        instructions=instructions,
        input=question[:2500],
        max_output_tokens=900,
    )
    answer = clean_ai_answer(response.output_text or "")
    return answer[:3900]


def build_meal_schedule(wake: str, sleep: str, count: int, language: str) -> list[tuple[int, str, str]]:
    wake_m = minutes_from_time(wake)
    sleep_m = minutes_from_time(sleep)
    if sleep_m <= wake_m:
        sleep_m += 1440
    start, end = wake_m + 60, max(wake_m + 180, sleep_m - 120)
    names = {
        2: (["Перший прийом їжі", "Вечеря"], ["Первый приём пищи", "Ужин"]),
        3: (["Сніданок", "Обід", "Вечеря"], ["Завтрак", "Обед", "Ужин"]),
        4: (["Сніданок", "Обід", "Перекус", "Вечеря"], ["Завтрак", "Обед", "Перекус", "Ужин"]),
    }[count][0 if language == "uk" else 1]
    step = (end - start) / max(1, len(names) - 1)
    return [(i + 1, name, time_from_minutes(round(start + i * step))) for i, name in enumerate(names)]


def button(text: str, data: str, style: ButtonStyle = ButtonStyle.PRIMARY) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


def options_keyboard(options: list[tuple[str, str]], prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[button(label, f"{prefix}:{value}")] for label, value in options])


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        button("🇺🇦 Українська", "lang:uk"),
        button("🇷🇺 Русский", "lang:ru"),
    ]])


def yes_no_keyboard(prefix: str, language: str) -> InlineKeyboardMarkup:
    yes, no = (("Так", "Ні") if language == "uk" else ("Да", "Нет"))
    return InlineKeyboardMarkup(inline_keyboard=[[
        button(yes, f"{prefix}:yes", ButtonStyle.SUCCESS),
        button(no, f"{prefix}:no", ButtonStyle.DANGER),
    ]])


def sex_keyboard(language: str) -> InlineKeyboardMarkup:
    female, male = (("Жінка", "Чоловік") if language == "uk" else ("Женщина", "Мужчина"))
    return InlineKeyboardMarkup(inline_keyboard=[[button(female, "sex:female"), button(male, "sex:male")]])


def timezone_keyboard(language: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(
                text="📍 Надіслати місцезнаходження" if language == "uk" else "📍 Отправить местоположение",
                request_location=True,
                style=ButtonStyle.SUCCESS,
            )],
            [KeyboardButton(
                text="🇺🇦 Час Києва" if language == "uk" else "🇺🇦 Время Киева",
                style=ButtonStyle.PRIMARY,
            )],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def is_kyiv_timezone_choice(text: str | None) -> bool:
    """
    Recognize the built-in Kyiv button in Russian, Ukrainian and English.

    Ukrainian city names change by grammatical case:
    «Київ» → «Києва». Checking only «київ» caused the bot to reject
    its own «Час Києва» button.
    """
    normalized = re.sub(
        r"\s+",
        " ",
        (text or "").lower().strip(),
    )
    return any(
        value in normalized
        for value in (
            "київ",
            "києв",
            "киев",
            "kyiv",
            "europe/kyiv",
        )
    )


def drink_goal_ml(profile: dict) -> int:
    """A moderate, non-medical hydration orientation based on current weight."""
    weight = float(profile.get("current_weight_kg") or 0)
    if weight <= 0:
        return 2000
    return int(round(max(1500.0, min(3000.0, weight * 30.0)) / 100.0) * 100)


def drink_type_keyboard(language: str) -> InlineKeyboardMarkup:
    rows = []
    for code in ("water", "sparkling", "sweet", "juice", "milk", "tea"):
        preset = DRINK_PRESETS[code]
        rows.append(
            [button(
                f"{preset['emoji']} {preset[language]}",
                f"drink:type:{code}",
                ButtonStyle.PRIMARY,
            )]
        )
    rows.append(
        [button(
            "⬅️ Головне меню" if language == "uk" else "⬅️ Главное меню",
            "menu:back",
            ButtonStyle.SUCCESS,
        )]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def drink_volume_keyboard(code: str, language: str) -> InlineKeyboardMarkup:
    rows = [
        [
            button("150 мл", f"drink:volume:{code}:150", ButtonStyle.PRIMARY),
            button("200 мл", f"drink:volume:{code}:200", ButtonStyle.PRIMARY),
        ],
        [
            button("300 мл", f"drink:volume:{code}:300", ButtonStyle.PRIMARY),
            button("500 мл", f"drink:volume:{code}:500", ButtonStyle.PRIMARY),
        ],
        [button(
            "✍️ Інший об'єм" if language == "uk" else "✍️ Другой объём",
            f"drink:custom:{code}",
            ButtonStyle.SUCCESS,
        )],
        [button(
            "⬅️ Обрати інший напій" if language == "uk" else "⬅️ Выбрать другой напиток",
            "menu:drink",
            ButtonStyle.PRIMARY,
        )],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def drink_values(code: str, volume_ml: int) -> dict[str, float | str | bool]:
    preset = DRINK_PRESETS[code]
    factor = volume_ml / 100.0
    return {
        "calories": round(float(preset["calories"]) * factor, 1),
        "protein": round(float(preset["protein"]) * factor, 1),
        "fat": round(float(preset["fat"]) * factor, 1),
        "carbs": round(float(preset["carbs"]) * factor, 1),
        "counts_as_water": bool(preset["counts_as_water"]),
    }


async def save_drink_entry(
    message: Message,
    user_id: int,
    code: str,
    volume_ml: int,
) -> None:
    profile = await db.get_profile(user_id)
    if not profile_complete(profile):
        await message.answer("Сначала завершите анкету командой /start.")
        return

    language = profile.get("language") or "ru"
    preset = DRINK_PRESETS.get(code)
    if not preset:
        await message.answer(
            "Не вдалося визначити напій." if language == "uk" else "Не удалось определить напиток."
        )
        return

    values = drink_values(code, volume_ml)
    local_date = datetime.now(ZoneInfo(profile["timezone"])).date().isoformat()
    await db.add_drink_log(
        user_id=user_id,
        drink_code=code,
        drink_name=str(preset[language]),
        volume_ml=volume_ml,
        calories=float(values["calories"]),
        protein=float(values["protein"]),
        fat=float(values["fat"]),
        carbs=float(values["carbs"]),
        counts_as_water=bool(values["counts_as_water"]),
        local_date=local_date,
    )

    drink_totals = await db.daily_drink_totals(user_id, local_date)
    nutrition_totals = await db.daily_food_totals(user_id, local_date)
    goal = drink_goal_ml(profile)
    calories_today = nutrition_midpoint(nutrition_totals, "calories")
    calorie_target = float(profile.get("calorie_target") or 0)
    calorie_left = calorie_target - calories_today
    approximate = code in {"sweet", "juice", "milk"}

    if language == "uk":
        note = (
            "\n\nℹ️ Калорійність орієнтовна: для точнішого розрахунку "
            "звіряйтеся з етикеткою напою."
            if approximate else ""
        )
        text = (
            f"✅ Записано: {preset['emoji']} {preset[language]} — {volume_ml} мл\n"
            f"🔥 Енергія: приблизно {float(values['calories']):.0f} ккал\n\n"
            f"🥤 Усієї рідини сьогодні: {drink_totals['fluid_ml']:.0f} із "
            f"орієнтовних {goal} мл\n"
            f"💧 Води без цукру: {drink_totals['water_ml']:.0f} мл\n"
            f"🔥 Калорії з напоїв: {drink_totals['drink_calories']:.0f} ккал\n\n"
            f"📊 Разом їжа + напої: {calories_today:.0f} із "
            f"{calorie_target:.0f} ккал\n"
            f"Залишок до орієнтира: {max(0.0, calorie_left):.0f} ккал"
            f"{note}\n\n"
            "Орієнтир рідини не є медичною нормою: спека, активність і стан "
            "здоров'я можуть змінювати потребу."
        )
        add_more = "➕ Додати ще напій"
        back = "🏠 Головне меню"
    else:
        note = (
            "\n\nℹ️ Калорийность ориентировочная: для более точного расчёта "
            "сверяйтесь с этикеткой напитка."
            if approximate else ""
        )
        text = (
            f"✅ Записано: {preset['emoji']} {preset[language]} — {volume_ml} мл\n"
            f"🔥 Энергия: примерно {float(values['calories']):.0f} ккал\n\n"
            f"🥤 Всего жидкости сегодня: {drink_totals['fluid_ml']:.0f} из "
            f"ориентировочных {goal} мл\n"
            f"💧 Воды без сахара: {drink_totals['water_ml']:.0f} мл\n"
            f"🔥 Калории из напитков: {drink_totals['drink_calories']:.0f} ккал\n\n"
            f"📊 Всего еда + напитки: {calories_today:.0f} из "
            f"{calorie_target:.0f} ккал\n"
            f"Остаток до ориентира: {max(0.0, calorie_left):.0f} ккал"
            f"{note}\n\n"
            "Ориентир жидкости не является медицинской нормой: жара, активность "
            "и состояние здоровья могут менять потребность."
        )
        add_more = "➕ Добавить ещё напиток"
        back = "🏠 Главное меню"

    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [button(add_more, "menu:drink", ButtonStyle.SUCCESS)],
                [button(back, "menu:back", ButtonStyle.PRIMARY)],
            ]
        ),
    )


def main_menu(language: str) -> InlineKeyboardMarkup:
    if language == "uk":
        rows = [
            [button("🧾 Моя інформація", "menu:profile", ButtonStyle.PRIMARY), button("⚖️ Записати вагу", "menu:weight", ButtonStyle.SUCCESS)],
            [button("🍽 Порахувати їжу", "menu:food", ButtonStyle.PRIMARY), button("📸 Калорії за фото", "menu:photo", ButtonStyle.SUCCESS)],
            [button("🥤 Записати напій", "menu:drink", ButtonStyle.SUCCESS)],
            [button("📅 Мій календар", "menu:calendar", ButtonStyle.PRIMARY), button("⏰ Нагадування", "menu:reminders", ButtonStyle.SUCCESS)],
            [button("🕐 Інтервальне харчування", "menu:fasting", ButtonStyle.PRIMARY), button("🥗 Що краще їсти", "menu:foods", ButtonStyle.SUCCESS)],
            [button("🫶 Поговорити з minus_kg", "menu:ai", ButtonStyle.PRIMARY)],
            [button("📋 Створити меню з продуктів", "menu:create_menu", ButtonStyle.SUCCESS)],
            [button("👩‍💼 Персональний супровід", "menu:coach", ButtonStyle.PRIMARY), button("⭐ Підписка", "menu:subscription", ButtonStyle.SUCCESS)],
            [button("🤝 Партнерська програма", "menu:referral", ButtonStyle.PRIMARY), button("⚙️ Налаштування", "menu:settings", ButtonStyle.SUCCESS)],
        ]
    else:
        rows = [
            [button("🧾 Моя информация", "menu:profile", ButtonStyle.PRIMARY), button("⚖️ Записать вес", "menu:weight", ButtonStyle.SUCCESS)],
            [button("🍽 Посчитать еду", "menu:food", ButtonStyle.PRIMARY), button("📸 Калории по фото", "menu:photo", ButtonStyle.SUCCESS)],
            [button("🥤 Записать напиток", "menu:drink", ButtonStyle.SUCCESS)],
            [button("📅 Мой календарь", "menu:calendar", ButtonStyle.PRIMARY), button("⏰ Напоминания", "menu:reminders", ButtonStyle.SUCCESS)],
            [button("🕐 Интервальное питание", "menu:fasting", ButtonStyle.PRIMARY), button("🥗 Что лучше съесть", "menu:foods", ButtonStyle.SUCCESS)],
            [button("🫶 Поговорить с minus_kg", "menu:ai", ButtonStyle.PRIMARY)],
            [button("📋 Создать меню из продуктов", "menu:create_menu", ButtonStyle.SUCCESS)],
            [button("👩‍💼 Персональное сопровождение", "menu:coach", ButtonStyle.PRIMARY), button("⭐ Подписка", "menu:subscription", ButtonStyle.SUCCESS)],
            [button("🤝 Партнёрская программа", "menu:referral", ButtonStyle.PRIMARY), button("⚙️ Настройки", "menu:settings", ButtonStyle.SUCCESS)],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def persistent_menu_keyboard(language: str) -> ReplyKeyboardMarkup:
    """A compact one-button keyboard that stays below the input field."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="🏠 Меню",
                    style=ButtonStyle.PRIMARY,
                )
            ]
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=(
            "Напишіть повідомлення або натисніть Меню"
            if language == "uk"
            else "Напишите сообщение или нажмите Меню"
        ),
    )


def subscription_keyboard(language: str) -> InlineKeyboardMarkup:
    if language == "uk":
        names = {
            "week": "7 днів · спробувати",
            "month": "1 місяць · популярний",
            "two_months": "2 місяці · вигідний баланс",
            "three_months": "3 місяці · довший супровід",
            "half_year": "6 місяців · найкраща ціна",
        }
    else:
        names = {
            "week": "7 дней · попробовать",
            "month": "1 месяц · популярный",
            "two_months": "2 месяца · выгодный баланс",
            "three_months": "3 месяца · дольше вместе",
            "half_year": "6 месяцев · лучшая цена",
        }

    rows = []
    for code, plan in PLANS.items():
        rows.append(
            [
                button(
                    (
                        f"{names[code]}\n"
                        f"{plan['stars']} ⭐ · ≈{plan['uah']} грн"
                    ),
                    f"buy:{code}",
                    (
                        ButtonStyle.SUCCESS
                        if code in {
                            "month",
                            "two_months",
                            "half_year",
                        }
                        else ButtonStyle.PRIMARY
                    ),
                )
            ]
        )

    rows.append(
        [
            button(
                "🤝 Як отримати бонуси"
                if language == "uk"
                else "🤝 Как получить бонусы",
                "menu:referral",
                ButtonStyle.PRIMARY,
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_access_status(
    profile: dict,
    language: str,
) -> str:
    now = int(time.time())
    timezone = ZoneInfo(
        profile.get("timezone") or "Europe/Kyiv"
    )
    trial_end = int(profile.get("trial_expires_at") or 0)
    paid_end = int(
        profile.get("subscription_expires_at") or 0
    )

    if paid_end > now:
        end_text = datetime.fromtimestamp(
            paid_end,
            timezone,
        ).strftime("%d.%m.%Y %H:%M")
        return (
            f"⭐ Платна підписка активна до {end_text}."
            if language == "uk"
            else
            f"⭐ Платная подписка активна до {end_text}."
        )

    if trial_end > now:
        end_text = datetime.fromtimestamp(
            trial_end,
            timezone,
        ).strftime("%d.%m.%Y %H:%M")
        return (
            f"🎁 Безкоштовний період активний до {end_text}."
            if language == "uk"
            else
            f"🎁 Бесплатный период активен до {end_text}."
        )

    return (
        "⏳ Зараз доступ неактивний."
        if language == "uk"
        else
        "⏳ Сейчас доступ неактивен."
    )


def subscription_screen_text(
    profile: dict,
    balance: int,
    language: str,
) -> str:
    status = subscription_access_status(profile, language)

    if language == "uk":
        return (
            "⭐ Підписка minus_kg\n\n"
            f"{status}\n\n"
            "Що відкриває підписка:\n"
            "• діалог із minus_kg про харчування, мотивацію та складні дні;\n"
            "• аналіз страв за фото;\n"
            "• персональні рецепти й анімована галерея;\n"
            "• повне меню на день із продуктів удома;\n"
            "• щоденник харчування, календар, графік ваги й заміри;\n"
            "• нагадування та інтервальне харчування.\n\n"
            "Ваші записи не видаляються після завершення доступу. "
            "Після продовження профіль відкриється з того самого місця.\n\n"
            f"🤝 Внутрішні бонуси: {balance} ⭐\n"
            "Один бонус зменшує наступну оплату на одну Star. "
            "Бонуси діють лише всередині minus_kg і не виводяться.\n\n"
            "Як обрати термін:\n"
            "• 7 днів — спокійно перевірити всі функції;\n"
            "• 1 місяць — пройти перший повний цикл;\n"
            "• 2–3 місяці — закріплювати звички без поспіху;\n"
            "• 6 місяців — найнижча вартість одного дня.\n\n"
            "Сума у Stars є точною. Ціна в гривнях показана приблизно, "
            "а остаточну суму Telegram покаже перед підтвердженням покупки.\n\n"
            "Оберіть зручний термін:"
        )

    return (
        "⭐ Подписка minus_kg\n\n"
        f"{status}\n\n"
        "Что открывает подписка:\n"
        "• диалог с minus_kg о питании, мотивации и сложных днях;\n"
        "• анализ блюд по фотографии;\n"
        "• персональные рецепты и анимированная галерея;\n"
        "• полное меню на день из продуктов дома;\n"
        "• дневник питания, календарь, график веса и замеры;\n"
        "• напоминания и интервальное питание.\n\n"
        "Ваши записи не удаляются после завершения доступа. "
        "После продления профиль откроется с того же места.\n\n"
        f"🤝 Внутренние бонусы: {balance} ⭐\n"
        "Один бонус уменьшает следующую оплату на одну Star. "
        "Бонусы действуют только внутри minus_kg и не выводятся.\n\n"
        "Как выбрать срок:\n"
        "• 7 дней — спокойно проверить все функции;\n"
        "• 1 месяц — пройти первый полный цикл;\n"
        "• 2–3 месяца — закреплять привычки без спешки;\n"
        "• 6 месяцев — самая низкая стоимость одного дня.\n\n"
        "Сумма в Stars является точной. Цена в гривнах показана "
        "приблизительно, а итоговую сумму Telegram покажет перед "
        "подтверждением покупки.\n\n"
        "Выберите удобный срок:"
    )



async def send_access_expired(
    message: Message,
    language: str,
) -> None:
    """Show the paywall immediately when trial and paid access have ended."""
    if language == "uk":
        text = (
            "⏳ Безкоштовний період або підписка завершилися.\n\n"
            "Ваші анкета, вага, календар, заміри та історія харчування "
            "збережені — після продовження доступу все залишиться на місці.\n\n"
            "⭐ Оберіть термін підписки:"
        )
    else:
        text = (
            "⏳ Бесплатный период или подписка закончились.\n\n"
            "Ваша анкета, вес, календарь, замеры и история питания "
            "сохранены — после продления доступа всё останется на месте.\n\n"
            "⭐ Выберите срок подписки:"
        )

    subscription_rows = list(
        subscription_keyboard(language).inline_keyboard
    )
    subscription_rows.append(
        [
            button(
                "🤝 Партнерська"
                if language == "uk"
                else "🤝 Партнёрская",
                "menu:referral",
                ButtonStyle.PRIMARY,
            ),
            button(
                "⚙️ Налаштування"
                if language == "uk"
                else "⚙️ Настройки",
                "menu:settings",
                ButtonStyle.PRIMARY,
            ),
        ]
    )
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=subscription_rows
        ),
    )

async def begin_onboarding(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Onboarding.language)
    animation = render_welcome_animation()
    if animation:
        try:
            await message.answer_animation(
                BufferedInputFile(animation, filename="minus_kg_welcome.gif")
            )
        except Exception:
            logging.exception("Welcome animation could not be sent")
    await message.answer(
        "Привіт! Я minus_kg 👋\n"
        "Привет! Я minus_kg 👋\n\n"
        "Я допоможу поступово змінювати вагу без голодування, сорому та покарань. "
        "Буду рахувати харчування, показувати прогрес, підбирати рецепти, "
        "нагадувати про важливе й підтримувати, коли день видався складним.\n\n"
        "Я помогу постепенно менять вес без голодовок, стыда и наказаний. "
        "Буду считать питание, показывать прогресс, подбирать рецепты, "
        "напоминать о важном и поддерживать, когда день оказался тяжёлым.\n\n"
        "Анкета займёт примерно 3–5 минут. Выберите удобный язык:",
        reply_markup=language_keyboard(),
    )


@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user:
        return
    await bot.set_chat_menu_button(
        chat_id=message.chat.id,
        menu_button=MenuButtonDefault(),
    )
    await state.clear()

    existing_profile = await db.get_profile(message.from_user.id)
    start_payload = ""
    if message.text:
        command_parts = message.text.strip().split(maxsplit=1)
        if len(command_parts) == 2:
            start_payload = command_parts[1].strip()

    if existing_profile is None and start_payload.startswith("ref_"):
        inviter_raw = start_payload[4:]
        if inviter_raw.isdigit():
            await db.register_referral(
                invited_user_id=message.from_user.id,
                inviter_user_id=int(inviter_raw),
            )

    await db.touch_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )
    profile = await db.get_profile(message.from_user.id)
    if not profile_complete(profile):
        await begin_onboarding(message, state)
        return
    language = profile["language"]

    if not access_active(profile):
        await send_access_expired(message, language)
        return

    text = (
        f"Вітаю, {profile['display_name']}! Я на місці 🫶\n\n"
        "Натисніть «🏠 Меню», щоб записати їжу або вагу, відкрити календар, "
        "підібрати рецепт чи просто поговорити."
        if language == "uk"
        else
        f"Привет, {profile['display_name']}! Я на месте 🫶\n\n"
        "Нажмите «🏠 Меню», чтобы записать еду или вес, открыть календарь, "
        "подобрать рецепт или просто поговорить."
    )
    await message.answer(
        text,
        reply_markup=persistent_menu_keyboard(language),
    )
    await message.answer(
        "Головне меню:" if language == "uk" else "Главное меню:",
        reply_markup=main_menu(language),
    )


@router.message(F.text == "🏠 Меню")
async def persistent_menu_button_handler(
    message: Message,
    state: FSMContext,
) -> None:
    if not message.from_user:
        return

    profile = await db.get_profile(message.from_user.id)
    if not profile_complete(profile):
        await state.clear()
        await begin_onboarding(message, state)
        return

    await state.clear()
    language = profile.get("language") or "ru"

    if not access_active(profile):
        await send_access_expired(message, language)
        return

    await message.answer(
        "Головне меню:" if language == "uk" else "Главное меню:",
        reply_markup=main_menu(language),
    )


@router.callback_query(F.data.startswith("lang:"))
async def language_handler(callback: CallbackQuery, state: FSMContext) -> None:
    language = callback.data.split(":", 1)[1]
    await state.update_data(language=language)
    await state.set_state(Onboarding.adult)
    await callback.message.edit_text(
        "🔞 Вам уже виповнилося 18 років?\n\nЦя версія створює розрахунок лише для дорослих." if language == "uk" else "🔞 Вам уже исполнилось 18 лет?\n\nЭта версия рассчитывает питание только для взрослых.",
        reply_markup=yes_no_keyboard("adult", language),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adult:"))
async def adult_handler(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    language = data["language"]
    if callback.data.endswith("no"):
        await callback.message.edit_text(
            "Ця версія minus_kg розрахована лише на повнолітніх."
            if language == "uk" else "Эта версия minus_kg рассчитана только на совершеннолетних."
        )
        await state.clear()
        await callback.answer()
        return
    await state.update_data(age_confirmed=1)
    await state.set_state(Onboarding.name)
    await callback.message.answer("🙂 Як до вас звертатися?\n\nНапишіть ім'я або зручне звертання — я не буду повторювати його в кожному повідомленні." if language == "uk" else "🙂 Как к вам обращаться?\n\nНапишите имя или удобное обращение — я не буду повторять его в каждом сообщении.")
    await callback.answer()


@router.message(Onboarding.name)
async def name_handler(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not 2 <= len(name) <= 40:
        await message.answer("Напишите короткое имя.")
        return
    await state.update_data(display_name=name)
    data = await state.get_data()
    await state.set_state(Onboarding.sex)
    await message.answer(
        "🧮 Вкажіть стать для розрахунку обміну енергії.\n\nЦей пункт потрібен лише для формули й не визначає, як я буду до вас звертатися." if data["language"] == "uk" else "🧮 Укажите пол для расчёта обмена энергии.\n\nЭтот пункт нужен только для формулы и не определяет, как я буду к вам обращаться.",
        reply_markup=sex_keyboard(data["language"]),
    )


@router.callback_query(F.data.startswith("sex:"))
async def sex_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(sex=callback.data.split(":", 1)[1])
    data = await state.get_data()
    await state.set_state(Onboarding.birth_date)
    await callback.message.answer(
        "🎂 Напишіть дату народження у форматі ДД.ММ.РРРР.\n\nНаприклад: 14.05.1993. Вік потрібен для приблизного розрахунку витрат енергії." if data["language"] == "uk" else "🎂 Напишите дату рождения в формате ДД.ММ.ГГГГ.\n\nНапример: 14.05.1993. Возраст нужен для примерного расчёта расхода энергии."
    )
    await callback.answer()


@router.message(Onboarding.birth_date)
async def birth_handler(message: Message, state: FSMContext) -> None:
    birth = parse_date(message.text or "")
    data = await state.get_data()
    if not birth:
        await message.answer(
            "Дата має відповідати віку 18–80 років." if data["language"] == "uk" else "Дата должна соответствовать возрасту 18–80 лет."
        )
        return
    await state.update_data(birth_date=birth.isoformat())
    await state.set_state(Onboarding.height)
    await message.answer("📏 Який у вас зріст у сантиметрах?\n\nНаприклад: 168. Зріст допомагає приблизно оцінити, скільки енергії витрачає організм." if data["language"] == "uk" else "📏 Какой у вас рост в сантиметрах?\n\nНапример: 168. Рост помогает примерно оценить, сколько энергии расходует организм.")


@router.message(Onboarding.height)
async def height_handler(message: Message, state: FSMContext) -> None:
    value = parse_number(message.text or "", 130, 220)
    data = await state.get_data()
    if value is None:
        await message.answer("Введите число от 130 до 220.")
        return
    await state.update_data(height_cm=value)
    await state.set_state(Onboarding.current_weight)
    await message.answer("⚖️ Напишіть поточну вагу в кілограмах.\n\nМожна використовувати кому або крапку, наприклад: 81,4." if data["language"] == "uk" else "⚖️ Напишите текущий вес в килограммах.\n\nМожно использовать запятую или точку, например: 81,4.")


@router.message(Onboarding.current_weight)
async def current_weight_handler(message: Message, state: FSMContext) -> None:
    value = parse_number(message.text or "", 35, 300)
    data = await state.get_data()
    if value is None:
        await message.answer("Введите число от 35 до 300.")
        return
    await state.update_data(current_weight_kg=value, start_weight_kg=value)
    await state.set_state(Onboarding.target_weight)
    await message.answer("🎯 Яку вагу ви обрали як ціль?\n\nНе хвилюйтеся: її можна буде змінити в налаштуваннях. Бот перевірить, щоб ціль не була небезпечно низькою." if data["language"] == "uk" else "🎯 Какой вес вы выбрали как цель?\n\nНе переживайте: её можно будет изменить в настройках. Бот проверит, чтобы цель не была опасно низкой.")


@router.message(Onboarding.target_weight)
async def target_weight_handler(message: Message, state: FSMContext) -> None:
    value = parse_number(message.text or "", 35, 300)
    data = await state.get_data()
    if value is None or value >= float(data["current_weight_kg"]):
        await message.answer(
            "Цільова вага має бути меншою за поточну."
            if data["language"] == "uk"
            else "Целевой вес должен быть меньше текущего."
        )
        return
    await state.update_data(target_weight_kg=value)
    await state.set_state(Onboarding.last_target)
    options = [
        ("Менше року тому", "under_year"), ("1–3 роки тому", "one_three"),
        ("Більше 3 років тому", "over_three"), ("Ніколи", "never"),
    ] if data["language"] == "uk" else [
        ("Меньше года назад", "under_year"), ("1–3 года назад", "one_three"),
        ("Больше 3 лет назад", "over_three"), ("Никогда", "never"),
    ]
    await message.answer(
        "🗓 Коли ви востаннє були в цій вазі?\n\nЦе допоможе не порівнювати теперішній організм із дуже давнім періодом життя." if data["language"] == "uk" else "🗓 Когда вы в последний раз были в этом весе?\n\nЭто поможет не сравнивать нынешний организм с очень давним периодом жизни.",
        reply_markup=options_keyboard(options, "last"),
    )


@router.callback_query(F.data.startswith("last:"))
async def last_target_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(last_target_period=callback.data.split(":", 1)[1])
    data = await state.get_data()
    await state.set_state(Onboarding.activity)
    options = [
        ("Сидячий спосіб життя", "sedentary"), ("Трохи активний", "light"),
        ("Середня активність", "moderate"), ("Активний", "active"),
    ] if data["language"] == "uk" else [
        ("Сидячий образ жизни", "sedentary"), ("Немного активный", "light"),
        ("Средняя активность", "moderate"), ("Активный", "active"),
    ]
    await callback.message.answer(
        "🚶 Як зазвичай проходить ваш тиждень?\n\nОберіть звичайний рівень, а не найактивніший день:\n• сидячий — більшість часу сидите;\n• трохи активний — прогулянки та побутові справи;\n• середній — багато ходьби або кілька тренувань;\n• активний — фізична робота або регулярні інтенсивні тренування." if data["language"] == "uk" else "🚶 Как обычно проходит ваша неделя?\n\nВыберите обычный уровень, а не самый активный день:\n• сидячий — большую часть времени сидите;\n• немного активный — прогулки и бытовые дела;\n• средний — много ходьбы или несколько тренировок;\n• активный — физическая работа или регулярные интенсивные тренировки.",
        reply_markup=options_keyboard(options, "activity"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("activity:"))
async def activity_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(activity=callback.data.split(":", 1)[1])
    data = await state.get_data()
    await state.set_state(Onboarding.sport)
    options = [
        ("Без тренувань", "none"), ("Ходьба та легка активність", "walking"),
        ("Домашні тренування", "home"), ("Спортзал", "gym"),
    ] if data["language"] == "uk" else [
        ("Без тренировок", "none"), ("Ходьба и лёгкая активность", "walking"),
        ("Домашние тренировки", "home"), ("Спортзал", "gym"),
    ]
    await callback.message.answer(
        "🏃 Який рух вам реально підходить зараз?\n\nТут немає правильної відповіді. Можна знижувати вагу й без спортзалу — важливо обрати те, що ви справді зможете робити." if data["language"] == "uk" else "🏃 Какое движение вам реально подходит сейчас?\n\nЗдесь нет правильного ответа. Можно снижать вес и без спортзала — важно выбрать то, что вы действительно сможете делать.",
        reply_markup=options_keyboard(options, "sport"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sport:"))
async def sport_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(sport_mode=callback.data.split(":", 1)[1])
    data = await state.get_data()
    await state.set_state(Onboarding.wake_time)
    await callback.message.answer(
        "🌅 О котрій ви зазвичай прокидаєтесь?\n\nНапишіть час, наприклад: 07:30. Я використаю його для зручного розкладу харчування." if data["language"] == "uk" else "🌅 Во сколько вы обычно просыпаетесь?\n\nНапишите время, например: 07:30. Я использую его для удобного расписания питания."
    )
    await callback.answer()


@router.message(Onboarding.wake_time)
async def wake_handler(message: Message, state: FSMContext) -> None:
    value = parse_clock(message.text or "")
    data = await state.get_data()
    if not value:
        await message.answer("Введите время в формате 07:30.")
        return
    await state.update_data(wake_time=value)
    await state.set_state(Onboarding.sleep_time)
    await message.answer("🌙 О котрій ви зазвичай лягаєте спати?\n\nНаприклад: 23:30. Розклад можна буде змінити в нагадуваннях." if data["language"] == "uk" else "🌙 Во сколько вы обычно ложитесь спать?\n\nНапример: 23:30. Расписание можно будет изменить в напоминаниях.")


@router.message(Onboarding.sleep_time)
async def sleep_handler(message: Message, state: FSMContext) -> None:
    value = parse_clock(message.text or "")
    data = await state.get_data()
    if not value:
        await message.answer("Введите время в формате 23:30.")
        return
    await state.update_data(sleep_time=value)
    await state.set_state(Onboarding.meals_count)
    await message.answer(
        "🍽 Скільки прийомів їжі вам зручно в звичайний день?\n\nОберіть не «ідеальну» кількість, а ту, якої реально дотримуватися." if data["language"] == "uk" else "🍽 Сколько приёмов пищи вам удобно в обычный день?\n\nВыберите не «идеальное» количество, а то, которого реально придерживаться.",
        reply_markup=options_keyboard([("2", "2"), ("3", "3"), ("4", "4")], "meals"),
    )


@router.callback_query(F.data.startswith("meals:"))
async def meals_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(meals_count=int(callback.data.split(":", 1)[1]))
    data = await state.get_data()
    await state.set_state(Onboarding.safety)
    text = (
        "🩺 Питання безпеки.\n\nЧи є вагітність або годування грудьми, розлад харчової поведінки зараз чи в минулому, діабет, серйозні хвороби або ліки, що впливають на вагу?\n\nЯкщо так, я не створюватиму автоматичний дефіцит і запропоную погодити план із лікарем."
        if data["language"] == "uk"
        else "🩺 Вопрос безопасности.\n\nЕсть ли беременность или кормление грудью, расстройство пищевого поведения сейчас или в прошлом, диабет, серьёзные заболевания либо лекарства, влияющие на вес?\n\nЕсли да, я не буду создавать автоматический дефицит и предложу согласовать план с врачом."
    )
    await callback.message.answer(text, reply_markup=yes_no_keyboard("safety", data["language"]))
    await callback.answer()


@router.callback_query(F.data.startswith("safety:"))
async def safety_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(safety_restricted=int(callback.data.endswith("yes")))
    data = await state.get_data()
    await state.set_state(Onboarding.timezone)
    await callback.message.answer(
        "📍 Оберіть часовий пояс.\n\nМожна надіслати геолокацію або натиснути «Час Києва». Координати потрібні лише для визначення часу й не зберігаються."
        if data["language"] == "uk" else "📍 Выберите часовой пояс.\n\nМожно отправить геолокацию или нажать «Время Киева». Координаты нужны только для определения времени и не сохраняются.",
        reply_markup=timezone_keyboard(data["language"]),
    )
    await callback.answer()

async def finalize_onboarding(message: Message, state: FSMContext, timezone: str) -> None:
    data = await state.get_data()
    language = data["language"]
    targets = calculate_targets(data)
    values = {
        **data,
        "timezone": timezone,
        "bmr": targets["bmr"],
        "tdee": targets["tdee"],
        "calorie_target": targets["calorie_target"],
        "protein_g": targets["protein_g"],
        "fat_g": targets["fat_g"],
        "carbs_g": targets["carbs_g"],
        "safety_restricted": int(targets["restricted"]),
        "trial_expires_at": int(time.time()) + TRIAL_SECONDS,
    }
    await db.save_profile(message.from_user.id, values)
    schedule = build_meal_schedule(data["wake_time"], data["sleep_time"], int(data["meals_count"]), language)
    await db.set_meal_schedule(message.from_user.id, schedule)
    await db.add_weight(
        message.from_user.id,
        float(data["current_weight_kg"]),
        datetime.now(ZoneInfo(timezone)).date().isoformat(),
    )
    await state.clear()
    schedule_text = "\n".join(f"• {name}: {meal_time}" for _, name, meal_time in schedule)
    remaining = float(data["current_weight_kg"]) - float(data["target_weight_kg"])
    if language == "uk":
        warning = (
            "\n\n⚠️ Ви зазначили обставини, за яких автоматичний дефіцит може бути небезпечним. "
            "Тому я показую орієнтир для підтримання ваги. Персональний план краще погодити з лікарем."
            if targets["restricted"]
            else f"\n\nОрієнтовний шлях до цілі може зайняти {targets['weeks_min']}–{targets['weeks_max']} тижнів. "
                 "Це не обіцянка й не дедлайн: організм не працює за календарем."
        )
        text = (
            f"✅ Готово, {data['display_name']}! Ваш особистий план створено.\n\n"
            f"🔥 Орієнтир енергії: приблизно {targets['calorie_target']} кілокалорій на день. "
            "Це не жорстка межа — важливіше середній результат за тиждень.\n\n"
            f"🥩 Білок: приблизно {targets['protein_g']} г. Він допомагає зберігати м'язи й довше відчувати ситість.\n"
            f"🥑 Жири: приблизно {targets['fat_g']} г. Вони потрібні для гормонів, шкіри та засвоєння вітамінів.\n"
            f"🍚 Вуглеводи: приблизно {targets['carbs_g']} г. Це важливе джерело енергії.\n\n"
            f"🎯 До обраної цілі залишилося близько {remaining:.1f} кг.{warning}\n\n"
            f"⏰ Початковий розклад нагадувань:\n{schedule_text}\n\n"
            "🎁 Усі функції доступні безкоштовно протягом 2 днів. Натисніть велику кнопку «🏠 Меню», щоб обрати першу дію."
        )
    else:
        warning = (
            "\n\n⚠️ Вы указали обстоятельства, при которых автоматический дефицит может быть небезопасен. "
            "Поэтому я показываю ориентир для поддержания веса. Персональный план лучше согласовать с врачом."
            if targets["restricted"]
            else f"\n\nОриентировочный путь к цели может занять {targets['weeks_min']}–{targets['weeks_max']} недель. "
                 "Это не обещание и не дедлайн: организм не работает по календарю."
        )
        text = (
            f"✅ Готово, {data['display_name']}! Ваш личный план создан.\n\n"
            f"🔥 Ориентир энергии: примерно {targets['calorie_target']} килокалорий в день. "
            "Это не жёсткий предел — важнее средний результат за неделю.\n\n"
            f"🥩 Белок: примерно {targets['protein_g']} г. Он помогает сохранять мышцы и дольше чувствовать сытость.\n"
            f"🥑 Жиры: примерно {targets['fat_g']} г. Они нужны для гормонов, кожи и усвоения витаминов.\n"
            f"🍚 Углеводы: примерно {targets['carbs_g']} г. Это важный источник энергии.\n\n"
            f"🎯 До выбранной цели осталось около {remaining:.1f} кг.{warning}\n\n"
            f"⏰ Начальное расписание напоминаний:\n{schedule_text}\n\n"
            "🎁 Все функции доступны бесплатно в течение 2 дней. Нажмите большую кнопку «🏠 Меню», чтобы выбрать первое действие."
        )
    referral_award = await db.qualify_referral_and_award_day(
        message.from_user.id
    )

    await message.answer(
        text,
        reply_markup=persistent_menu_keyboard(language),
    )
    await message.answer(
        "Головне меню:" if language == "uk" else "Главное меню:",
        reply_markup=main_menu(language),
    )

    if referral_award:
        inviter_id = int(referral_award["inviter_user_id"])
        inviter_profile = await db.get_profile(inviter_id)
        inviter_language = (
            inviter_profile.get("language")
            if inviter_profile
            else "ru"
        )
        inviter_expiry = datetime.fromtimestamp(
            int(referral_award["new_expiry"]),
            ZoneInfo(
                inviter_profile.get("timezone") or "Europe/Kyiv"
                if inviter_profile else "Europe/Kyiv"
            ),
        )
        try:
            await message.bot.send_message(
                inviter_id,
                (
                    "🎁 Запрошений вами користувач завершив анкету. "
                    f"Вам додано 1 день доступу — до {inviter_expiry:%d.%m.%Y %H:%M}."
                    if inviter_language == "uk"
                    else
                    "🎁 Приглашённый вами пользователь завершил анкету. "
                    f"Вам добавлен 1 день доступа — до {inviter_expiry:%d.%m.%Y %H:%M}."
                ),
            )
        except Exception:
            logging.exception(
                "Failed to notify inviter user_id=%s",
                inviter_id,
            )


@router.message(Onboarding.timezone, F.location)
async def timezone_location_handler(message: Message, state: FSMContext) -> None:
    timezone = timezone_finder.timezone_at(
        lat=message.location.latitude,
        lng=message.location.longitude,
    ) or "Europe/Kyiv"
    await finalize_onboarding(message, state, timezone)


@router.message(Onboarding.timezone)
async def timezone_text_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not is_kyiv_timezone_choice(message.text):
        await message.answer(
            (
                "Натисніть «🇺🇦 Час Києва» або надішліть геолокацію. "
                "Координати не зберігаються."
                if data["language"] == "uk"
                else
                "Нажмите «🇺🇦 Время Киева» или отправьте геолокацию. "
                "Координаты не сохраняются."
            ),
            reply_markup=timezone_keyboard(data["language"]),
        )
        return
    await finalize_onboarding(message, state, "Europe/Kyiv")


@router.callback_query(F.data == "menu:profile")
async def profile_menu(callback: CallbackQuery) -> None:
    profile = await db.get_profile(callback.from_user.id)
    if not profile_complete(profile):
        await callback.answer(
            "Сначала заполните анкету через /start.",
            show_alert=True,
        )
        return

    language = profile["language"]
    user_id = callback.from_user.id

    start_weight = float(profile["start_weight_kg"])
    current_weight = float(profile["current_weight_kg"])
    target_weight = float(profile["target_weight_kg"])

    planned_change = max(0.0, start_weight - target_weight)
    actual_change = start_weight - current_weight
    remaining = max(0.0, current_weight - target_weight)

    if planned_change > 0:
        progress_percent = max(
            0,
            min(100, round(actual_change / planned_change * 100)),
        )
    else:
        progress_percent = 100

    filled = round(progress_percent / 10)
    progress_bar = "●" * filled + "○" * (10 - filled)

    registered = datetime.fromtimestamp(
        profile["registered_at"]
    ).strftime("%d.%m.%Y")

    end_ts = max(
        int(profile.get("trial_expires_at") or 0),
        int(profile.get("subscription_expires_at") or 0),
    )
    end_text = (
        datetime.fromtimestamp(
            end_ts,
            ZoneInfo(profile["timezone"]),
        ).strftime("%d.%m.%Y %H:%M")
        if end_ts > int(time.time())
        else (
            "доступ неактивний"
            if language == "uk"
            else "доступ неактивен"
        )
    )

    local_date = datetime.now(
        ZoneInfo(profile["timezone"])
    ).date().isoformat()
    day_totals = await db.daily_food_totals(
        user_id,
        local_date,
    )

    eaten_calories = nutrition_midpoint(
        day_totals,
        "calories",
    )
    eaten_protein = nutrition_midpoint(
        day_totals,
        "protein",
    )
    eaten_fat = nutrition_midpoint(
        day_totals,
        "fat",
    )
    eaten_carbs = nutrition_midpoint(
        day_totals,
        "carbs",
    )

    calorie_target = float(profile["calorie_target"])
    protein_target = float(profile["protein_g"])
    fat_target = float(profile["fat_g"])
    carbs_target = float(profile["carbs_g"])

    calorie_left = max(0.0, calorie_target - eaten_calories)
    protein_left = max(0.0, protein_target - eaten_protein)
    fat_left = max(0.0, fat_target - eaten_fat)
    carbs_left = max(0.0, carbs_target - eaten_carbs)

    bonus_balance = await db.get_bonus_balance(user_id)

    if language == "uk":
        if actual_change > 0.05:
            progress_note = (
                f"Від старту вага зменшилася на {actual_change:.1f} кг. "
                "Це вже частина шляху — не потрібно прискорювати його голодуванням."
            )
        elif actual_change < -0.05:
            progress_note = (
                f"Зараз вага на {abs(actual_change):.1f} кг вище стартової. "
                "Це не вирок: вода, сіль, цикл, стрес і час зважування "
                "можуть сильно впливати на цифру."
            )
        else:
            progress_note = (
                "Зараз ви біля стартової точки. Перші зміни краще оцінювати "
                "не за одним днем, а за тенденцією протягом 2–4 тижнів."
            )

        if day_totals.get("food_count"):
            today_text = (
                "🍽 Сьогодні вже записано приблизно:\n"
                f"• енергія — {eaten_calories:.0f} кілокалорій;\n"
                f"• білок — {eaten_protein:.0f} г;\n"
                f"• жири — {eaten_fat:.0f} г;\n"
                f"• вуглеводи — {eaten_carbs:.0f} г.\n\n"
                "До денного орієнтиру приблизно залишилося:\n"
                f"• {calorie_left:.0f} кілокалорій;\n"
                f"• білок — {protein_left:.0f} г;\n"
                f"• жири — {fat_left:.0f} г;\n"
                f"• вуглеводи — {carbs_left:.0f} г."
            )
        else:
            today_text = (
                "🍽 Сьогодні харчування ще не записувалося.\n"
                "Після першого запису тут з'явиться зрозумілий підсумок дня."
            )

        text = (
            f"🧾 {profile['display_name']}, ось ваша поточна картина\n\n"
            f"⚖️ Шлях до цілі\n"
            f"• стартова вага — {start_weight:.1f} кг;\n"
            f"• поточна вага — {current_weight:.1f} кг;\n"
            f"• обрана ціль — {target_weight:.1f} кг;\n"
            f"• залишилося приблизно {remaining:.1f} кг.\n\n"
            f"{progress_bar} {progress_percent}% шляху\n"
            f"{progress_note}\n\n"
            f"🥗 Орієнтир на звичайний день\n"
            f"• близько {calorie_target:.0f} кілокалорій — це кількість "
            "енергії, а не сувора межа;\n"
            f"• білок — близько {protein_target:.0f} г: допомагає довше "
            "відчувати ситість і підтримувати м'язи;\n"
            f"• жири — близько {fat_target:.0f} г: потрібні для гормонів, "
            "шкіри та засвоєння вітамінів;\n"
            f"• вуглеводи — близько {carbs_target:.0f} г: дають енергію "
            "для дня й активності.\n\n"
            "Не потрібно потрапляти в кожну цифру ідеально. Важливіше "
            "загальний баланс протягом тижня.\n\n"
            f"{today_text}\n\n"
            f"📅 Профіль створено: {registered}\n"
            f"⭐ Доступ до: {end_text}\n"
            f"🤝 Партнерські бонуси: {bonus_balance} ⭐"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button(
                        "⚖️ Записати нову вагу",
                        "menu:weight",
                        ButtonStyle.SUCCESS,
                    )
                ],
                [
                    button(
                        "🍽 Записати їжу",
                        "menu:food",
                        ButtonStyle.PRIMARY,
                    ),
                    button(
                        "📅 Відкрити календар",
                        "menu:calendar",
                        ButtonStyle.PRIMARY,
                    ),
                ],
                [
                    button(
                        "🎯 Змінити ціль",
                        "settings:target",
                        ButtonStyle.SUCCESS,
                    )
                ],
            ]
        )
    else:
        if actual_change > 0.05:
            progress_note = (
                f"От старта вес снизился на {actual_change:.1f} кг. "
                "Это уже часть пути — не нужно ускорять его голоданием."
            )
        elif actual_change < -0.05:
            progress_note = (
                f"Сейчас вес на {abs(actual_change):.1f} кг выше стартового. "
                "Это не приговор: вода, соль, цикл, стресс и время взвешивания "
                "могут сильно влиять на цифру."
            )
        else:
            progress_note = (
                "Сейчас вы возле стартовой точки. Первые изменения лучше "
                "оценивать не по одному дню, а по тенденции за 2–4 недели."
            )

        if day_totals.get("food_count"):
            today_text = (
                "🍽 Сегодня уже записано примерно:\n"
                f"• энергия — {eaten_calories:.0f} килокалорий;\n"
                f"• белок — {eaten_protein:.0f} г;\n"
                f"• жиры — {eaten_fat:.0f} г;\n"
                f"• углеводы — {eaten_carbs:.0f} г.\n\n"
                "До дневного ориентира примерно осталось:\n"
                f"• {calorie_left:.0f} килокалорий;\n"
                f"• белок — {protein_left:.0f} г;\n"
                f"• жиры — {fat_left:.0f} г;\n"
                f"• углеводы — {carbs_left:.0f} г."
            )
        else:
            today_text = (
                "🍽 Сегодня питание ещё не записывалось.\n"
                "После первой записи здесь появится понятный итог дня."
            )

        text = (
            f"🧾 {profile['display_name']}, вот ваша текущая картина\n\n"
            f"⚖️ Путь к цели\n"
            f"• стартовый вес — {start_weight:.1f} кг;\n"
            f"• текущий вес — {current_weight:.1f} кг;\n"
            f"• выбранная цель — {target_weight:.1f} кг;\n"
            f"• осталось примерно {remaining:.1f} кг.\n\n"
            f"{progress_bar} {progress_percent}% пути\n"
            f"{progress_note}\n\n"
            f"🥗 Ориентир на обычный день\n"
            f"• около {calorie_target:.0f} килокалорий — это количество "
            "энергии, а не строгая граница;\n"
            f"• белок — около {protein_target:.0f} г: помогает дольше "
            "чувствовать сытость и поддерживать мышцы;\n"
            f"• жиры — около {fat_target:.0f} г: нужны для гормонов, кожи "
            "и усвоения витаминов;\n"
            f"• углеводы — около {carbs_target:.0f} г: дают энергию "
            "для дня и активности.\n\n"
            "Не нужно попадать в каждую цифру идеально. Важнее общий "
            "баланс в течение недели.\n\n"
            f"{today_text}\n\n"
            f"📅 Профиль создан: {registered}\n"
            f"⭐ Доступ до: {end_text}\n"
            f"🤝 Партнёрские бонусы: {bonus_balance} ⭐"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button(
                        "⚖️ Записать новый вес",
                        "menu:weight",
                        ButtonStyle.SUCCESS,
                    )
                ],
                [
                    button(
                        "🍽 Записать еду",
                        "menu:food",
                        ButtonStyle.PRIMARY,
                    ),
                    button(
                        "📅 Открыть календарь",
                        "menu:calendar",
                        ButtonStyle.PRIMARY,
                    ),
                ],
                [
                    button(
                        "🎯 Изменить цель",
                        "settings:target",
                        ButtonStyle.SUCCESS,
                    )
                ],
            ]
        )

    await callback.message.answer(
        text[:4096],
        reply_markup=keyboard,
    )
    await callback.answer()


async def start_body_log(
    message: Message,
    state: FSMContext,
    language: str,
) -> None:
    await state.clear()
    await state.set_state(BodyLog.weight)

    if language == "uk":
        text = (
            "⚖️ Запишімо поточну вагу.\n\n"
            "Введіть число в кілограмах, наприклад: 59,8.\n\n"
            "Для порівняння краще зважуватися приблизно в однакових умовах: "
            "вранці, після туалету, до їжі та в схожому одязі. "
            "Але пропускати запис через «неідеальні» умови не потрібно."
        )
    else:
        text = (
            "⚖️ Запишем текущий вес.\n\n"
            "Введите число в килограммах, например: 59,8.\n\n"
            "Для сравнения лучше взвешиваться примерно в одинаковых условиях: "
            "утром, после туалета, до еды и в похожей одежде. "
            "Но пропускать запись из-за «неидеальных» условий не нужно."
        )
    await message.answer(text)


@router.callback_query(F.data.in_({"menu:weight", "body:log"}))
async def weight_menu(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    await start_body_log(callback.message, state, language)
    await callback.answer()


@router.message(BodyLog.weight)
async def body_weight_handler(
    message: Message,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile["language"] if profile else "ru"
    value = parse_number(message.text or "", 35, 300)

    if value is None:
        await message.answer(
            "Не вдалося розпізнати вагу. Введіть лише число, наприклад 59,8."
            if language == "uk"
            else
            "Не удалось распознать вес. Введите только число, например 59,8."
        )
        return

    await state.update_data(weight_kg=value)
    await state.set_state(BodyLog.waist)

    await message.answer(
        (
            "📏 Бажаєте записати обхват талії?\n\n"
            "Вимірюйте сантиметровою стрічкою без сильного натягування, "
            "приблизно посередині між нижнім ребром і верхом тазової кістки.\n\n"
            "Введіть число в сантиметрах або натисніть «Пропустити»."
            if language == "uk"
            else
            "📏 Хотите записать обхват талии?\n\n"
            "Измеряйте сантиметровой лентой без сильного натяжения, "
            "примерно посередине между нижним ребром и верхом тазовой кости.\n\n"
            "Введите число в сантиметрах или нажмите «Пропустить»."
        ),
        reply_markup=body_skip_keyboard("waist", language),
    )


@router.message(BodyLog.waist)
async def body_waist_handler(
    message: Message,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile["language"] if profile else "ru"
    value = parse_number(message.text or "", 40, 250)

    if value is None:
        await message.answer(
            "Введіть число від 40 до 250 або натисніть «Пропустити»."
            if language == "uk"
            else
            "Введите число от 40 до 250 или нажмите «Пропустить»."
        )
        return

    await state.update_data(waist_cm=value)
    await state.set_state(BodyLog.hips)

    await message.answer(
        (
            "📏 Тепер обхват стегон.\n\n"
            "Стрічка проходить горизонтально через найширшу частину стегон і сідниць.\n\n"
            "Введіть число в сантиметрах або натисніть «Пропустити»."
            if language == "uk"
            else
            "📏 Теперь обхват бёдер.\n\n"
            "Лента проходит горизонтально через самую широкую часть бёдер и ягодиц.\n\n"
            "Введите число в сантиметрах или нажмите «Пропустить»."
        ),
        reply_markup=body_skip_keyboard("hips", language),
    )


@router.message(BodyLog.hips)
async def body_hips_handler(
    message: Message,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile["language"] if profile else "ru"
    value = parse_number(message.text or "", 40, 250)

    if value is None:
        await message.answer(
            "Введіть число від 40 до 250 або натисніть «Пропустити»."
            if language == "uk"
            else
            "Введите число от 40 до 250 или нажмите «Пропустить»."
        )
        return

    await state.update_data(hips_cm=value)
    await state.set_state(BodyLog.chest)

    await message.answer(
        (
            "📏 Останній необов'язковий замір — обхват грудей.\n\n"
            "Тримайте стрічку горизонтально через найширшу частину грудей, "
            "не затягуючи її.\n\n"
            "Введіть число в сантиметрах або натисніть «Пропустити»."
            if language == "uk"
            else
            "📏 Последний необязательный замер — обхват груди.\n\n"
            "Держите ленту горизонтально через самую широкую часть груди, "
            "не затягивая её.\n\n"
            "Введите число в сантиметрах или нажмите «Пропустить»."
        ),
        reply_markup=body_skip_keyboard("chest", language),
    )


async def finish_body_log(
    message: Message,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile["language"] if profile else "ru"
    data = await state.get_data()

    weight = float(data["weight_kg"])
    previous_weight = float(profile["current_weight_kg"])
    start_weight = float(profile["start_weight_kg"])
    target_weight = float(profile["target_weight_kg"])
    change = weight - previous_weight
    remaining = max(0.0, weight - target_weight)

    local_date = datetime.now(
        ZoneInfo(profile["timezone"])
    ).date().isoformat()

    await db.add_body_measurement(
        user_id=message.from_user.id,
        weight_kg=weight,
        waist_cm=data.get("waist_cm"),
        hips_cm=data.get("hips_cm"),
        chest_cm=data.get("chest_cm"),
        local_date=local_date,
    )
    await state.clear()

    measurements = []
    if data.get("waist_cm") is not None:
        measurements.append(
            ("талія" if language == "uk" else "талия")
            + f" — {data['waist_cm']:.1f} см"
        )
    if data.get("hips_cm") is not None:
        measurements.append(
            ("стегна" if language == "uk" else "бёдра")
            + f" — {data['hips_cm']:.1f} см"
        )
    if data.get("chest_cm") is not None:
        measurements.append(
            ("груди" if language == "uk" else "грудь")
            + f" — {data['chest_cm']:.1f} см"
        )

    large_daily_change = abs(change) >= max(2.0, previous_weight * 0.03)

    if language == "uk":
        if change < -0.05:
            interpretation = (
                f"Вага нижча за попередній запис на {abs(change):.1f} кг."
            )
        elif change > 0.05:
            interpretation = (
                f"Вага вища за попередній запис на {change:.1f} кг."
            )
        else:
            interpretation = "Вага майже не змінилася."

        if large_daily_change:
            context = (
                "\n\nЗміна досить велика для одного запису. Це може бути "
                "різниця в умовах зважування або помилка введення. "
                "Перевірте число; за потреби просто запишіть вагу ще раз."
            )
        elif change > 0.05:
            context = (
                "\n\nОдин плюс на вагах не означає набір жиру. На цифру "
                "впливають вода, сіль, цикл, стрес, травлення та час зважування."
            )
        elif change < -0.05:
            context = (
                "\n\nЦе приємна зміна, але не потрібно намагатися прискорити "
                "її голодуванням. Найважливіша тенденція за кілька тижнів."
            )
        else:
            context = (
                "\n\nСтабільна вага теж є корисною інформацією. "
                "Оцінюємо не один день, а загальний напрямок."
            )

        body_text = (
            "\n\n📏 Збережені заміри:\n"
            + "\n".join(f"• {item}" for item in measurements)
            if measurements
            else "\n\n📏 Обхвати цього разу не записувалися."
        )

        text = (
            "✅ Запис збережено\n\n"
            f"• нова вага — {weight:.1f} кг;\n"
            f"• попередня вага — {previous_weight:.1f} кг;\n"
            f"• зміна — {change:+.1f} кг;\n"
            f"• від старту — {weight - start_weight:+.1f} кг;\n"
            f"• до обраної цілі — приблизно {remaining:.1f} кг.\n\n"
            f"{interpretation}{context}"
            f"{body_text}\n\n"
            "Наступний запис можна зробити через 1–2 дні в схожих умовах."
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button(
                        "📅 Подивитися в календарі",
                        "menu:calendar",
                        ButtonStyle.PRIMARY,
                    )
                ],
                [
                    button(
                        "✏️ Записати вагу ще раз",
                        "menu:weight",
                        ButtonStyle.SUCCESS,
                    ),
                    button(
                        "🧾 Моя інформація",
                        "menu:profile",
                        ButtonStyle.PRIMARY,
                    ),
                ],
            ]
        )
    else:
        if change < -0.05:
            interpretation = (
                f"Вес ниже предыдущей записи на {abs(change):.1f} кг."
            )
        elif change > 0.05:
            interpretation = (
                f"Вес выше предыдущей записи на {change:.1f} кг."
            )
        else:
            interpretation = "Вес почти не изменился."

        if large_daily_change:
            context = (
                "\n\nИзменение довольно большое для одной записи. Это может быть "
                "разница в условиях взвешивания или ошибка ввода. "
                "Проверьте число; при необходимости просто запишите вес ещё раз."
            )
        elif change > 0.05:
            context = (
                "\n\nОдин плюс на весах не означает набор жира. На цифру "
                "влияют вода, соль, цикл, стресс, пищеварение и время взвешивания."
            )
        elif change < -0.05:
            context = (
                "\n\nЭто приятное изменение, но не нужно пытаться ускорить "
                "его голоданием. Важнее тенденция за несколько недель."
            )
        else:
            context = (
                "\n\nСтабильный вес тоже является полезной информацией. "
                "Оцениваем не один день, а общее направление."
            )

        body_text = (
            "\n\n📏 Сохранённые замеры:\n"
            + "\n".join(f"• {item}" for item in measurements)
            if measurements
            else "\n\n📏 Обхваты в этот раз не записывались."
        )

        text = (
            "✅ Запись сохранена\n\n"
            f"• новый вес — {weight:.1f} кг;\n"
            f"• предыдущий вес — {previous_weight:.1f} кг;\n"
            f"• изменение — {change:+.1f} кг;\n"
            f"• от старта — {weight - start_weight:+.1f} кг;\n"
            f"• до выбранной цели — примерно {remaining:.1f} кг.\n\n"
            f"{interpretation}{context}"
            f"{body_text}\n\n"
            "Следующую запись можно сделать через 1–2 дня в похожих условиях."
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button(
                        "📅 Посмотреть в календаре",
                        "menu:calendar",
                        ButtonStyle.PRIMARY,
                    )
                ],
                [
                    button(
                        "✏️ Записать вес ещё раз",
                        "menu:weight",
                        ButtonStyle.SUCCESS,
                    ),
                    button(
                        "🧾 Моя информация",
                        "menu:profile",
                        ButtonStyle.PRIMARY,
                    ),
                ],
            ]
        )

    await message.answer(
        text[:4096],
        reply_markup=keyboard,
    )


@router.message(BodyLog.chest)
async def body_chest_handler(
    message: Message,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile["language"] if profile else "ru"
    value = parse_number(message.text or "", 40, 250)

    if value is None:
        await message.answer(
            "Введіть число від 40 до 250 або натисніть «Пропустити»."
            if language == "uk"
            else
            "Введите число от 40 до 250 или нажмите «Пропустить»."
        )
        return

    await state.update_data(chest_cm=value)
    await finish_body_log(message, state)


@router.callback_query(F.data.startswith("body_skip:"))
async def body_skip_handler(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    field = callback.data.split(":", 1)[1]
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    current = await state.get_state()

    if field == "waist" and current == BodyLog.waist.state:
        await state.set_state(BodyLog.hips)
        await callback.message.answer(
            (
                "📏 Тепер обхват стегон.\n\n"
                "Стрічка проходить горизонтально через найширшу частину стегон і сідниць.\n\n"
                "Введіть число або натисніть «Пропустити»."
                if language == "uk"
                else
                "📏 Теперь обхват бёдер.\n\n"
                "Лента проходит горизонтально через самую широкую часть бёдер и ягодиц.\n\n"
                "Введите число или нажмите «Пропустить»."
            ),
            reply_markup=body_skip_keyboard("hips", language),
        )
    elif field == "hips" and current == BodyLog.hips.state:
        await state.set_state(BodyLog.chest)
        await callback.message.answer(
            (
                "📏 Останній необов'язковий замір — обхват грудей.\n\n"
                "Тримайте стрічку горизонтально через найширшу частину грудей.\n\n"
                "Введіть число або натисніть «Пропустити»."
                if language == "uk"
                else
                "📏 Последний необязательный замер — обхват груди.\n\n"
                "Держите ленту горизонтально через самую широкую часть груди.\n\n"
                "Введите число или нажмите «Пропустить»."
            ),
            reply_markup=body_skip_keyboard("chest", language),
        )
    elif field == "chest" and current == BodyLog.chest.state:
        await finish_body_log(callback.message, state)

    await callback.answer()


MONTH_NAMES = {
    "ru": [
        "",
        "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
        "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
    ],
    "uk": [
        "",
        "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
        "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень",
    ],
}

WEEKDAY_LABELS = {
    "ru": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
    "uk": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"],
}


def shift_calendar_month(
    year: int,
    month: int,
    delta: int,
) -> tuple[int, int]:
    absolute = year * 12 + (month - 1) + delta
    return absolute // 12, absolute % 12 + 1


def calendar_month_bounds(
    year: int,
    month: int,
) -> tuple[date, date]:
    last_day = pycalendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def nutrition_average(day: dict, name: str) -> float:
    return (
        float(day.get(f"{name}_min") or 0)
        + float(day.get(f"{name}_max") or 0)
    ) / 2


async def calendar_keyboard(
    user_id: int,
    profile: dict,
    year: int,
    month: int,
) -> InlineKeyboardMarkup:
    language = profile.get("language") or "ru"
    first_day, last_day = calendar_month_bounds(year, month)
    # Add the previous month to calculate weight direction on the first day.
    previous_start = shift_calendar_month(year, month, -1)
    extended_start = date(previous_start[0], previous_start[1], 1)
    data = await db.calendar_month_data(
        user_id,
        extended_start.isoformat(),
        last_day.isoformat(),
    )

    weight_rows = sorted(
        (
            (day, float(values["weight_kg"]))
            for day, values in data.items()
            if values.get("weight_kg") is not None
        ),
        key=lambda pair: pair[0],
    )
    previous_weight_by_day: dict[str, float | None] = {}
    previous_weight: float | None = None
    for day, weight in weight_rows:
        previous_weight_by_day[day] = previous_weight
        previous_weight = weight

    previous_year, previous_month = shift_calendar_month(year, month, -1)
    next_year, next_month = shift_calendar_month(year, month, 1)
    rows: list[list[InlineKeyboardButton]] = [
        [
            button(
                "◀️",
                f"calendar_month:{previous_year:04d}-{previous_month:02d}",
                ButtonStyle.PRIMARY,
            ),
            button(
                f"{MONTH_NAMES[language][month]} {year}",
                "calendar_noop",
                ButtonStyle.SUCCESS,
            ),
            button(
                "▶️",
                f"calendar_month:{next_year:04d}-{next_month:02d}",
                ButtonStyle.PRIMARY,
            ),
        ],
        [
            button(label, "calendar_noop", ButtonStyle.PRIMARY)
            for label in WEEKDAY_LABELS[language]
        ],
    ]

    today = datetime.now(
        ZoneInfo(profile["timezone"])
    ).date()

    month_grid = pycalendar.Calendar(firstweekday=0).monthdayscalendar(
        year,
        month,
    )
    for week in month_grid:
        row: list[InlineKeyboardButton] = []
        for day_number in week:
            if day_number == 0:
                row.append(
                    button("·", "calendar_noop", ButtonStyle.PRIMARY)
                )
                continue

            current = date(year, month, day_number)
            day_key = current.isoformat()
            item = data.get(day_key) or {}
            weight = item.get("weight_kg")
            previous = previous_weight_by_day.get(day_key)
            calories = nutrition_average(item, "calories")
            target = float(profile.get("calorie_target") or 0)
            fasting_status = item.get("fasting_status")

            marker = ""
            style = ButtonStyle.PRIMARY

            # Calendar buttons are very narrow (7 columns). Short marks keep
            # dates readable, unlike full emoji which Telegram truncates to “…”.
            if weight is not None:
                if previous is not None and float(weight) < previous - 0.05:
                    marker = "↓"
                    style = ButtonStyle.SUCCESS
                elif previous is not None and float(weight) > previous + 0.05:
                    marker = "↑"
                    style = ButtonStyle.DANGER
                else:
                    marker = "="
                    style = ButtonStyle.PRIMARY
            elif fasting_status == "success":
                marker = "✓"
                style = ButtonStyle.SUCCESS
            elif fasting_status == "missed":
                marker = "!"
                style = ButtonStyle.DANGER
            elif target > 0 and calories > target * 1.10:
                marker = "+"
                style = ButtonStyle.DANGER
            elif item.get("food_count"):
                marker = "•"
                style = ButtonStyle.SUCCESS

            if current == today and not marker:
                marker = "*"

            label = f"{day_number}{marker}" if marker else str(day_number)
            row.append(
                button(
                    label,
                    f"calendar_day:{day_key}",
                    style,
                )
            )
        rows.append(row)

    rows.extend(
        [
            [
                button(
                    "📈 Графік ваги"
                    if language == "uk"
                    else "📈 График веса",
                    f"calendar_chart:{year:04d}-{month:02d}",
                    ButtonStyle.PRIMARY,
                ),
                button(
                    "📊 Підсумки"
                    if language == "uk"
                    else "📊 Итоги",
                    f"calendar_summary:{year:04d}-{month:02d}",
                    ButtonStyle.SUCCESS,
                ),
            ],
            [
                button(
                    "⚖️ Записати вагу й об'єми"
                    if language == "uk"
                    else "⚖️ Записать вес и объёмы",
                    "body:log",
                    ButtonStyle.SUCCESS,
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_calendar_month(
    message: Message,
    user_id: int,
    year: int,
    month: int,
) -> None:
    profile = await db.get_profile(user_id)
    language = profile["language"] if profile else "ru"
    keyboard = await calendar_keyboard(
        user_id,
        profile,
        year,
        month,
    )
    if language == "uk":
        text = (
            "📅 Мій календар\n\n"
            "↓ вага зменшилась · ↑ вага зросла · = вага без змін\n"
            "• записано їжу · ✓ режим дотримано · ! не вийшло\n"
            "+ калорії вище орієнтиру · * сьогодні\n\n"
            "Натисніть на дату, щоб побачити деталі."
        )
    else:
        text = (
            "📅 Мой календарь\n\n"
            "↓ вес снизился · ↑ вес вырос · = вес без изменений\n"
            "• записана еда · ✓ режим соблюдён · ! не получилось\n"
            "+ калории выше ориентира · * сегодня\n\n"
            "Нажмите на дату, чтобы увидеть подробности."
        )
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "menu:calendar")
async def calendar_menu(callback: CallbackQuery) -> None:
    profile = await db.get_profile(callback.from_user.id)
    if not profile_complete(profile):
        await callback.answer(
            "Сначала завершите анкету.",
            show_alert=True,
        )
        return
    local_today = datetime.now(
        ZoneInfo(profile["timezone"])
    ).date()
    await send_calendar_month(
        callback.message,
        callback.from_user.id,
        local_today.year,
        local_today.month,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("calendar_month:"))
async def calendar_month_handler(callback: CallbackQuery) -> None:
    value = callback.data.split(":", 1)[1]
    try:
        year, month = map(int, value.split("-"))
        if not 1 <= month <= 12:
            raise ValueError
    except ValueError:
        await callback.answer("Ошибка месяца.", show_alert=True)
        return

    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    keyboard = await calendar_keyboard(
        callback.from_user.id,
        profile,
        year,
        month,
    )
    text = (
        "📅 Оберіть дату:"
        if language == "uk"
        else "📅 Выберите дату:"
    )
    try:
        await callback.message.edit_text(
            text,
            reply_markup=keyboard,
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=keyboard,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("calendar_day:"))
async def calendar_day_handler(callback: CallbackQuery) -> None:
    day_key = callback.data.split(":", 1)[1]
    try:
        selected = date.fromisoformat(day_key)
    except ValueError:
        await callback.answer("Ошибка даты.", show_alert=True)
        return

    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    details = await db.calendar_day_details(
        callback.from_user.id,
        day_key,
    )

    calories = nutrition_average(details, "calories")
    protein = nutrition_average(details, "protein")
    fat = nutrition_average(details, "fat")
    carbs = nutrition_average(details, "carbs")
    target = float(profile.get("calorie_target") or 0)
    difference = calories - target

    if language == "uk":
        title = f"📅 {selected:%d.%m.%Y}"
        weight_title = "Вага й об'єми"
        food_title = "Харчування"
        fasting_title = "Інтервальний режим"
        no_data = "немає запису"
        fast_label = {
            "success": "✅ дотримано",
            "missed": "🔸 не вийшло",
            None: "не відмічено",
        }.get(details.get("fasting_status"), "не відмічено")
    else:
        title = f"📅 {selected:%d.%m.%Y}"
        weight_title = "Вес и объёмы"
        food_title = "Питание"
        fasting_title = "Интервальный режим"
        no_data = "нет записи"
        fast_label = {
            "success": "✅ соблюдён",
            "missed": "🔸 не получилось",
            None: "не отмечено",
        }.get(details.get("fasting_status"), "не отмечено")

    if details.get("weight_kg") is None:
        body_text = no_data
    else:
        body_parts = [f"{details['weight_kg']:.1f} кг"]
        if details.get("waist_cm") is not None:
            body_parts.append(
                ("талія" if language == "uk" else "талия")
                + f" {details['waist_cm']:.1f} см"
            )
        if details.get("hips_cm") is not None:
            body_parts.append(
                ("стегна" if language == "uk" else "бёдра")
                + f" {details['hips_cm']:.1f} см"
            )
        if details.get("chest_cm") is not None:
            body_parts.append(
                ("груди" if language == "uk" else "грудь")
                + f" {details['chest_cm']:.1f} см"
            )
        body_text = " · ".join(body_parts)

    if details.get("food_count") or details.get("drink_count"):
        food_text = (
            f"≈ {calories:.0f} ккал\n"
            f"Білки / жири / вуглеводи: {protein:.0f}/{fat:.0f}/{carbs:.0f} г"
            if language == "uk"
            else
            f"≈ {calories:.0f} ккал\n"
            f"Белки / жиры / углеводы: {protein:.0f}/{fat:.0f}/{carbs:.0f} г"
        )
        if target > 0:
            if difference > 0:
                food_text += (
                    f"\nВище орієнтиру приблизно на {difference:.0f} ккал."
                    if language == "uk"
                    else f"\nВыше ориентира примерно на {difference:.0f} ккал."
                )
            else:
                food_text += (
                    f"\nДо орієнтиру залишалося приблизно {abs(difference):.0f} ккал."
                    if language == "uk"
                    else f"\nДо ориентира оставалось примерно {abs(difference):.0f} ккал."
                )
    else:
        food_text = no_data

    food_rows = []
    for row in (details.get("foods") or [])[:8]:
        description = str(row.get("description") or "").strip()
        if description:
            food_rows.append(f"• {description[:160]}")
    foods_list = (
        "\n\n" + (
            "Записи:\n" if language == "ru" else "Записи:\n"
        ) + "\n".join(food_rows)
        if food_rows else ""
    )

    drink_rows = []
    for row in (details.get("drinks") or [])[:10]:
        name = str(row.get("drink_name") or "").strip()
        if name:
            drink_rows.append(
                f"• {name} — {int(row.get('volume_ml') or 0)} мл, "
                f"{float(row.get('calories') or 0):.0f} ккал"
            )
    if details.get("drink_count"):
        drink_text = (
            f"{details.get('fluid_ml', 0):.0f} мл рідини · "
            f"{details.get('water_ml', 0):.0f} мл води · "
            f"{details.get('drink_calories', 0):.0f} ккал"
            if language == "uk" else
            f"{details.get('fluid_ml', 0):.0f} мл жидкости · "
            f"{details.get('water_ml', 0):.0f} мл воды · "
            f"{details.get('drink_calories', 0):.0f} ккал"
        )
        if drink_rows:
            drink_text += "\n" + "\n".join(drink_rows)
    else:
        drink_text = no_data

    text = (
        f"{title}\n\n"
        f"⚖️ {weight_title}: {body_text}\n\n"
        f"🍽 {food_title}:\n{food_text}{foods_list}\n\n"
        f"🥤 {'Рідина' if language == 'uk' else 'Жидкость'}:\n{drink_text}\n\n"
        f"🕐 {fasting_title}: {fast_label}"
    )

    month_value = f"{selected.year:04d}-{selected.month:02d}"
    keyboard_rows = [
        [
            button(
                "⬅️ До календаря"
                if language == "uk"
                else "⬅️ К календарю",
                f"calendar_month:{month_value}",
                ButtonStyle.PRIMARY,
            )
        ]
    ]
    if profile.get("fasting_mode"):
        keyboard_rows.append(
            [
                button(
                    "✅ Дотримано"
                    if language == "uk"
                    else "✅ Соблюдено",
                    f"fasting_day:success:{day_key}",
                    ButtonStyle.SUCCESS,
                ),
                button(
                    "🔸 Не вийшло"
                    if language == "uk"
                    else "🔸 Не получилось",
                    f"fasting_day:missed:{day_key}",
                    ButtonStyle.DANGER,
                ),
            ]
        )
    await callback.message.answer(
        text[:4096],
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=keyboard_rows
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("calendar_chart:"))
async def calendar_chart_handler(callback: CallbackQuery) -> None:
    value = callback.data.split(":", 1)[1]
    try:
        year, month = map(int, value.split("-"))
        first_day, last_day = calendar_month_bounds(year, month)
    except (ValueError, TypeError):
        await callback.answer("Ошибка периода.", show_alert=True)
        return

    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    # Show up to six months so the graph remains useful even in a quiet month.
    chart_start = first_day - timedelta(days=180)
    points = await db.weight_history(
        callback.from_user.id,
        chart_start.isoformat(),
        last_day.isoformat(),
        limit=240,
    )
    if not points:
        await callback.answer(
            "Пока нет записей веса."
            if language == "ru"
            else "Поки немає записів ваги.",
            show_alert=True,
        )
        return

    image = render_weight_progress_chart(
        points=points,
        target_kg=float(profile.get("target_weight_kg") or 0),
        language=language,
        start_weight_kg=float(
            profile.get("start_weight_kg")
            or points[0]["weight_kg"]
        ),
    )
    caption = (
        "📈 Графік показує останній запис ваги за кожен день. "
        "Щоденні коливання води нормальні — дивимося на тенденцію."
        if language == "uk"
        else
        "📈 График показывает последнюю запись веса за каждый день. "
        "Ежедневные колебания воды нормальны — смотрим на тенденцию."
    )
    if image:
        await callback.message.answer_photo(
            BufferedInputFile(
                image,
                filename="weight_progress.png",
            ),
            caption=caption,
        )
    else:
        rows = [
            f"• {point['local_date']} — {float(point['weight_kg']):.1f} кг"
            for point in points[-30:]
        ]
        await callback.message.answer(
            caption + "\n\n" + "\n".join(rows)
        )
    await callback.answer()


@router.callback_query(F.data.startswith("calendar_summary:"))
async def calendar_summary_handler(callback: CallbackQuery) -> None:
    value = callback.data.split(":", 1)[1]
    try:
        year, month = map(int, value.split("-"))
        first_day, last_day = calendar_month_bounds(year, month)
    except (ValueError, TypeError):
        await callback.answer("Ошибка периода.", show_alert=True)
        return

    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    month_data = await db.calendar_month_data(
        callback.from_user.id,
        first_day.isoformat(),
        last_day.isoformat(),
    )
    weights = [
        (day, float(item["weight_kg"]))
        for day, item in sorted(month_data.items())
        if item.get("weight_kg") is not None
    ]
    food_days = [
        item for item in month_data.values()
        if item.get("food_count")
    ]
    fasting_success = sum(
        1 for item in month_data.values()
        if item.get("fasting_status") == "success"
    )
    fasting_missed = sum(
        1 for item in month_data.values()
        if item.get("fasting_status") == "missed"
    )

    if weights:
        weight_change = weights[-1][1] - weights[0][1]
        weight_line = (
            f"{weights[0][1]:.1f} → {weights[-1][1]:.1f} кг "
            f"({weight_change:+.1f} кг)"
        )
    else:
        weight_line = (
            "немає записів" if language == "uk" else "нет записей"
        )

    if food_days:
        average_calories = sum(
            nutrition_average(item, "calories")
            for item in food_days
        ) / len(food_days)
        food_line = f"{average_calories:.0f} ккал/день"
    else:
        food_line = (
            "немає записів" if language == "uk" else "нет записей"
        )

    bodies = await db.body_history(
        callback.from_user.id,
        first_day.isoformat(),
        last_day.isoformat(),
    )
    body_lines = []
    if len(bodies) >= 2:
        for key, ru, uk in (
            ("waist_cm", "талия", "талія"),
            ("hips_cm", "бёдра", "стегна"),
            ("chest_cm", "грудь", "груди"),
        ):
            first_value = next(
                (
                    float(row[key]) for row in bodies
                    if row.get(key) is not None
                ),
                None,
            )
            last_value = next(
                (
                    float(row[key]) for row in reversed(bodies)
                    if row.get(key) is not None
                ),
                None,
            )
            if first_value is not None and last_value is not None:
                body_lines.append(
                    f"• {uk if language == 'uk' else ru}: "
                    f"{last_value - first_value:+.1f} см"
                )

    local_today = datetime.now(
        ZoneInfo(profile["timezone"])
    ).date()
    week_start = local_today - timedelta(days=6)
    week_data = await db.calendar_month_data(
        callback.from_user.id,
        week_start.isoformat(),
        local_today.isoformat(),
    )
    week_weights = [
        float(item["weight_kg"])
        for _, item in sorted(week_data.items())
        if item.get("weight_kg") is not None
    ]
    if len(week_weights) >= 2:
        week_change = week_weights[-1] - week_weights[0]
        week_line = f"{week_change:+.1f} кг"
    else:
        week_line = (
            "недостатньо записів"
            if language == "uk"
            else "недостаточно записей"
        )

    if language == "uk":
        text = (
            f"📊 Підсумки: {MONTH_NAMES[language][month]} {year}\n\n"
            f"⚖️ Вага: {weight_line}\n"
            f"📆 Днів із записом ваги: {len(weights)}\n"
            f"🍽 Днів із харчуванням: {len(food_days)}\n"
            f"🔥 Середні калорії: {food_line}\n"
            f"🕐 Інтервальний режим: ✅ {fasting_success} · 🔸 {fasting_missed}\n\n"
            f"Останні 7 днів: {week_line}"
        )
        if body_lines:
            text += "\n\n📏 Зміна об'ємів:\n" + "\n".join(body_lines)
        text += (
            "\n\nОдин день не визначає прогрес. Дивимося на тенденцію "
            "за кілька тижнів."
        )
    else:
        text = (
            f"📊 Итоги: {MONTH_NAMES[language][month]} {year}\n\n"
            f"⚖️ Вес: {weight_line}\n"
            f"📆 Дней с записью веса: {len(weights)}\n"
            f"🍽 Дней с питанием: {len(food_days)}\n"
            f"🔥 Средние калории: {food_line}\n"
            f"🕐 Интервальный режим: ✅ {fasting_success} · 🔸 {fasting_missed}\n\n"
            f"Последние 7 дней: {week_line}"
        )
        if body_lines:
            text += "\n\n📏 Изменение объёмов:\n" + "\n".join(body_lines)
        text += (
            "\n\nОдин день не определяет прогресс. Смотрим на тенденцию "
            "за несколько недель."
        )

    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.startswith("fasting_day:"))
async def fasting_day_status_handler(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Ошибка отметки.", show_alert=True)
        return
    status, day_key = parts[1], parts[2]
    try:
        date.fromisoformat(day_key)
    except ValueError:
        await callback.answer("Ошибка даты.", show_alert=True)
        return
    if status not in {"success", "missed"}:
        await callback.answer("Ошибка отметки.", show_alert=True)
        return

    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    await db.set_fasting_day_status(
        callback.from_user.id,
        day_key,
        status,
    )
    text = (
        (
            "✅ День отмечен: режим соблюдён."
            if status == "success"
            else "🔸 День отмечен: сегодня не получилось. Без наказаний — продолжаем завтра."
        )
        if language == "ru"
        else
        (
            "✅ День відмічено: режим дотримано."
            if status == "success"
            else "🔸 День відмічено: сьогодні не вийшло. Без покарань — продовжуємо завтра."
        )
    )
    await callback.answer(text, show_alert=True)


@router.callback_query(F.data == "calendar_noop")
async def calendar_noop_handler(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "menu:reminders")
async def reminders_menu(callback: CallbackQuery) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    schedule = await db.get_all_meal_schedule(
        callback.from_user.id
    )
    prefs = await db.get_reminder_preferences(
        callback.from_user.id
    )

    await edit_reminder_screen(
        callback,
        reminders_overview_text(
            schedule,
            prefs,
            profile or {},
            language,
        ),
        reminders_overview_keyboard(
            schedule,
            prefs,
            language,
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("reminder_slot:"))
async def reminder_slot_handler(
    callback: CallbackQuery,
) -> None:
    slot_number = int(callback.data.split(":", 1)[1])
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    slot = await db.get_meal_slot(
        callback.from_user.id,
        slot_number,
    )

    if not slot:
        await callback.answer(
            "Напоминание не найдено.",
            show_alert=True,
        )
        return

    await edit_reminder_screen(
        callback,
        meal_slot_settings_text(slot, language),
        meal_slot_settings_keyboard(slot, language),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("reminder_edit:"))
async def reminder_edit_handler(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    slot_number = int(callback.data.split(":", 1)[1])
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    slot = await db.get_meal_slot(
        callback.from_user.id,
        slot_number,
    )

    if not slot:
        await callback.answer(
            "Напоминание не найдено.",
            show_alert=True,
        )
        return

    await state.set_state(ReminderSettings.meal_time)
    await state.update_data(
        reminder_slot_number=slot_number,
        reminder_settings_chat_id=callback.message.chat.id,
        reminder_settings_message_id=callback.message.message_id,
    )

    if language == "uk":
        text = (
            f"🕒 Новий час для «{slot['meal_name']}»\n\n"
            "Напишіть час у форматі години:хвилини.\n"
            "Наприклад: 08:30 або 21:00.\n\n"
            "До введення нового часу попереднє налаштування "
            "продовжує діяти."
        )
    else:
        text = (
            f"🕒 Новое время для «{slot['meal_name']}»\n\n"
            "Напишите время в формате часы:минуты.\n"
            "Например: 08:30 или 21:00.\n\n"
            "До ввода нового времени прежняя настройка "
            "продолжает действовать."
        )

    await edit_reminder_screen(
        callback,
        text,
        reminder_back_keyboard(language),
    )
    await callback.answer()


@router.message(ReminderSettings.meal_time, F.text)
async def reminder_time_input(
    message: Message,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile["language"] if profile else "ru"
    value = parse_clock(message.text or "")

    if not value:
        await message.answer(
            (
                "Не вдалося розпізнати час. Напишіть його точно "
                "у форматі 08:30."
                if language == "uk"
                else
                "Не удалось распознать время. Напишите его точно "
                "в формате 08:30."
            )
        )
        return

    data = await state.get_data()
    slot_number = int(data["reminder_slot_number"])
    await db.update_meal_time(
        message.from_user.id,
        slot_number,
        value,
    )
    slot = await db.get_meal_slot(
        message.from_user.id,
        slot_number,
    )
    schedule = await db.get_all_meal_schedule(
        message.from_user.id
    )
    prefs = await db.get_reminder_preferences(
        message.from_user.id
    )
    await state.clear()

    confirmation = (
        f"✅ Час для «{slot['meal_name']}» змінено на {value}."
        if language == "uk"
        else
        f"✅ Время для «{slot['meal_name']}» изменено на {value}."
    )
    await message.answer(confirmation)

    try:
        await message.bot.edit_message_text(
            chat_id=int(data["reminder_settings_chat_id"]),
            message_id=int(data["reminder_settings_message_id"]),
            text=reminders_overview_text(
                schedule,
                prefs,
                profile or {},
                language,
            ),
            reply_markup=reminders_overview_keyboard(
                schedule,
                prefs,
                language,
            ),
        )
    except Exception:
        await message.answer(
            reminders_overview_text(
                schedule,
                prefs,
                profile or {},
                language,
            ),
            reply_markup=reminders_overview_keyboard(
                schedule,
                prefs,
                language,
            ),
        )


@router.callback_query(F.data.startswith("reminder_toggle:"))
async def reminder_toggle_handler(
    callback: CallbackQuery,
) -> None:
    slot_number = int(callback.data.split(":", 1)[1])
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"

    enabled = await db.toggle_meal_reminder(
        callback.from_user.id,
        slot_number,
    )
    slot = await db.get_meal_slot(
        callback.from_user.id,
        slot_number,
    )

    if enabled is None or not slot:
        await callback.answer(
            "Напоминание не найдено.",
            show_alert=True,
        )
        return

    await edit_reminder_screen(
        callback,
        meal_slot_settings_text(slot, language),
        meal_slot_settings_keyboard(slot, language),
    )
    await callback.answer(
        (
            "Нагадування увімкнено"
            if enabled
            else "Нагадування вимкнено"
        ) if language == "uk" else (
            "Напоминание включено"
            if enabled
            else "Напоминание выключено"
        )
    )


@router.callback_query(F.data == "body_reminder_settings")
async def body_reminder_settings_handler(
    callback: CallbackQuery,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    prefs = await db.get_reminder_preferences(
        callback.from_user.id
    )

    await edit_reminder_screen(
        callback,
        body_reminder_settings_text(prefs, language),
        body_reminder_settings_keyboard(prefs, language),
    )
    await callback.answer()


@router.callback_query(F.data == "body_reminder_edit")
async def body_reminder_edit_handler(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"

    await state.set_state(ReminderSettings.body_time)
    await state.update_data(
        reminder_settings_chat_id=callback.message.chat.id,
        reminder_settings_message_id=callback.message.message_id,
    )

    text = (
        "🕒 Новий час нагадування про вагу\n\n"
        "Напишіть зручний час у форматі 09:00.\n\n"
        "Це не означає, що повідомлення приходитиме щодня: "
        "бот надсилає його не частіше одного разу на два дні."
        if language == "uk"
        else
        "🕒 Новое время напоминания о весе\n\n"
        "Напишите удобное время в формате 09:00.\n\n"
        "Это не означает, что сообщение будет приходить ежедневно: "
        "бот отправляет его не чаще одного раза в два дня."
    )

    await edit_reminder_screen(
        callback,
        text,
        reminder_back_keyboard(language),
    )
    await callback.answer()


@router.message(ReminderSettings.body_time, F.text)
async def body_reminder_time_input(
    message: Message,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile["language"] if profile else "ru"
    value = parse_clock(message.text or "")

    if not value:
        await message.answer(
            (
                "Не вдалося розпізнати час. Напишіть його у форматі 09:00."
                if language == "uk"
                else
                "Не удалось распознать время. Напишите его в формате 09:00."
            )
        )
        return

    data = await state.get_data()
    await db.update_body_reminder_time(
        message.from_user.id,
        value,
    )
    schedule = await db.get_all_meal_schedule(
        message.from_user.id
    )
    prefs = await db.get_reminder_preferences(
        message.from_user.id
    )
    await state.clear()

    await message.answer(
        (
            f"✅ Нагадування про вагу буде приходити о {value}, "
            "але не частіше одного разу на два дні."
            if language == "uk"
            else
            f"✅ Напоминание о весе будет приходить в {value}, "
            "но не чаще одного раза в два дня."
        )
    )

    try:
        await message.bot.edit_message_text(
            chat_id=int(data["reminder_settings_chat_id"]),
            message_id=int(data["reminder_settings_message_id"]),
            text=reminders_overview_text(
                schedule,
                prefs,
                profile or {},
                language,
            ),
            reply_markup=reminders_overview_keyboard(
                schedule,
                prefs,
                language,
            ),
        )
    except Exception:
        await message.answer(
            reminders_overview_text(
                schedule,
                prefs,
                profile or {},
                language,
            ),
            reply_markup=reminders_overview_keyboard(
                schedule,
                prefs,
                language,
            ),
        )


@router.callback_query(F.data == "body_reminder_toggle")
async def body_reminder_toggle_handler(
    callback: CallbackQuery,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"

    enabled = await db.toggle_body_reminder(
        callback.from_user.id
    )
    prefs = await db.get_reminder_preferences(
        callback.from_user.id
    )

    await edit_reminder_screen(
        callback,
        body_reminder_settings_text(prefs, language),
        body_reminder_settings_keyboard(prefs, language),
    )
    await callback.answer(
        (
            "Нагадування увімкнено"
            if enabled
            else "Нагадування вимкнено"
        ) if language == "uk" else (
            "Напоминание включено"
            if enabled
            else "Напоминание выключено"
        )
    )


@router.callback_query(F.data == "reminders_preview")
async def reminders_preview_handler(
    callback: CallbackQuery,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"

    if language == "uk":
        text = (
            "👀 Приклад повідомлення про їжу\n\n"
            "🍽 Настав орієнтовний час для «Вечері».\n\n"
            "Це м'яке нагадування, а не команда. Якщо ви голодні — "
            "оберіть звичайну порцію. Якщо ще не хочеться їсти, "
            "відкладіть повідомлення або пропустіть його без почуття провини.\n\n"
            "Під справжнім повідомленням будуть кнопки:\n"
            "• записати, що ви з'їли;\n"
            "• нагадати через 30 хвилин;\n"
            "• сьогодні пропустити.\n\n"
            "⚖️ Нагадування про вагу окремо пояснить, що один результат "
            "не визначає прогрес, а заміри тіла необов'язкові."
        )
    else:
        text = (
            "👀 Пример сообщения о еде\n\n"
            "🍽 Наступило ориентировочное время для «Ужина».\n\n"
            "Это мягкое напоминание, а не команда. Если вы голодны — "
            "выберите обычную порцию. Если есть пока не хочется, "
            "отложите уведомление или пропустите его без чувства вины.\n\n"
            "Под настоящим сообщением будут кнопки:\n"
            "• записать, что вы съели;\n"
            "• напомнить через 30 минут;\n"
            "• сегодня пропустить.\n\n"
            "⚖️ Напоминание о весе отдельно объяснит, что один результат "
            "не определяет прогресс, а замеры тела необязательны."
        )

    await edit_reminder_screen(
        callback,
        text,
        reminder_back_keyboard(language),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("reminders_all:"))
async def reminders_all_handler(
    callback: CallbackQuery,
) -> None:
    desired = callback.data.split(":", 1)[1] == "on"
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"

    schedule = await db.get_all_meal_schedule(
        callback.from_user.id
    )
    for slot in schedule:
        if bool(slot.get("enabled")) != desired:
            await db.toggle_meal_reminder(
                callback.from_user.id,
                int(slot["slot_number"]),
            )

    prefs = await db.get_reminder_preferences(
        callback.from_user.id
    )
    if bool(prefs.get("body_enabled", 1)) != desired:
        await db.toggle_body_reminder(
            callback.from_user.id
        )

    schedule = await db.get_all_meal_schedule(
        callback.from_user.id
    )
    prefs = await db.get_reminder_preferences(
        callback.from_user.id
    )

    await edit_reminder_screen(
        callback,
        reminders_overview_text(
            schedule,
            prefs,
            profile or {},
            language,
        ),
        reminders_overview_keyboard(
            schedule,
            prefs,
            language,
        ),
    )
    await callback.answer(
        (
            "Усі нагадування увімкнено"
            if desired
            else "Усі нагадування вимкнено"
        ) if language == "uk" else (
            "Все напоминания включены"
            if desired
            else "Все напоминания выключены"
        )
    )


@router.callback_query(F.data.startswith("meal_action:"))
async def meal_notification_action(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Ошибка действия.", show_alert=True)
        return
    action = parts[1]
    slot_number = int(parts[2])
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    slot = await db.get_meal_slot(callback.from_user.id, slot_number)
    meal_name = slot["meal_name"] if slot else (
        "прийом їжі" if language == "uk" else "приём пищи"
    )

    if action == "ate":
        await state.set_state(Actions.food)
        await callback.message.answer(
            f"✅ Добре! Напишіть, що і скільки ви з'їли під час «{meal_name}»."
            if language == "uk" else
            f"✅ Хорошо! Напишите, что и сколько вы съели во время «{meal_name}»."
        )
    elif action == "snooze":
        due_at = int(time.time()) + 30 * 60
        await db.create_meal_snooze(
            callback.from_user.id,
            slot_number,
            meal_name,
            due_at,
        )
        local_due = datetime.fromtimestamp(
            due_at,
            ZoneInfo(profile["timezone"]),
        ).strftime("%H:%M")
        await callback.message.answer(
            f"⏰ Напомню в {local_due}."
            if language == "ru" else f"⏰ Нагадаю о {local_due}."
        )
    elif action == "skip":
        await callback.message.answer(
            (
                "🙂 На сьогодні нагадування пропущено. Це не помилка й не "
                "«зрив». Наступне повідомлення прийде за звичайним розкладом."
                if language == "uk"
                else
                "🙂 На сегодня напоминание пропущено. Это не ошибка и не "
                "«срыв». Следующее сообщение придёт по обычному расписанию."
            )
        )
    else:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer()


def fasting_mode_name(
    mode: str | None,
    language: str,
) -> str:
    names = {
        "uk": {
            "12_12": "12:12 · м'який старт",
            "14_10": "14:10 · помірний режим",
            "16_8": "16:8 · довша пауза",
        },
        "ru": {
            "12_12": "12:12 · мягкий старт",
            "14_10": "14:10 · умеренный режим",
            "16_8": "16:8 · более длинная пауза",
        },
    }
    return names.get(language, names["ru"]).get(
        mode or "",
        "не выбран" if language == "ru" else "не обрано",
    )


def fasting_options_text(
    profile: dict,
    language: str,
) -> str:
    current = profile.get("fasting_mode")
    current_text = fasting_mode_name(current, language)
    wake_time = profile.get("wake_time") or "07:00"

    if language == "uk":
        return (
            "🕐 Оберіть зручний режим інтервального харчування\n\n"
            "Перше число показує тривалість паузи без їжі, "
            "друге — скільки годин відкрите вікно харчування.\n\n"
            "🌿 12:12 — м'який старт\n"
            "Наприклад, 12 годин між вечерею та першим прийомом їжі. "
            "Найзручніший варіант для знайомства з режимом.\n\n"
            "⚖️ 14:10 — помірний режим\n"
            "Пауза трохи довша. Варто обирати, якщо 12:12 уже проходить "
            "спокійно, без сильного голоду та погіршення самопочуття.\n\n"
            "🌙 16:8 — довша пауза\n"
            "Не є «кращим» або обов'язковим варіантом. Обирайте лише тоді, "
            "коли такий розклад реально зручний і не викликає слабкості, "
            "запаморочення чи нав'язливого голоду.\n\n"
            f"Поточний режим: {current_text}.\n"
            f"За вашим часом пробудження {wake_time} бот запропонує "
            "відкрити вікно приблизно через годину після підйому.\n\n"
            "Поки режим увімкнений, бот нагадуватиме:\n"
            "• коли вікно харчування відкриється;\n"
            "• за 30 хвилин до його завершення;\n"
            "• коли вікно закриється.\n\n"
            "Це інструмент розкладу, а не перевірка сили волі. "
            "За поганого самопочуття режим потрібно зупинити."
        )

    return (
        "🕐 Выберите удобный режим интервального питания\n\n"
        "Первое число показывает длительность паузы без еды, "
        "второе — сколько часов открыто окно питания.\n\n"
        "🌿 12:12 — мягкий старт\n"
        "Например, 12 часов между ужином и первым приёмом пищи. "
        "Самый понятный вариант для знакомства с режимом.\n\n"
        "⚖️ 14:10 — умеренный режим\n"
        "Пауза немного длиннее. Его разумно выбирать, если 12:12 уже "
        "проходит спокойно, без сильного голода и ухудшения самочувствия.\n\n"
        "🌙 16:8 — более длинная пауза\n"
        "Не является «лучшим» или обязательным вариантом. Выбирайте только "
        "тогда, когда такой график действительно удобен и не вызывает "
        "слабости, головокружения или навязчивого голода.\n\n"
        f"Текущий режим: {current_text}.\n"
        f"При вашем времени пробуждения {wake_time} бот предложит "
        "открывать окно примерно через час после подъёма.\n\n"
        "Пока режим включён, бот будет напоминать:\n"
        "• когда окно питания откроется;\n"
        "• за 30 минут до его завершения;\n"
        "• когда окно закроется.\n\n"
        "Это инструмент расписания, а не проверка силы воли. "
        "При плохом самочувствии режим нужно остановить."
    )


def fasting_options_keyboard(
    profile: dict,
    language: str,
) -> InlineKeyboardMarkup:
    current = profile.get("fasting_mode")

    if language == "uk":
        labels = {
            "12_12": "🌿 12:12 · Почати м'яко",
            "14_10": "⚖️ 14:10 · Помірний режим",
            "16_8": "🌙 16:8 · Довша пауза",
        }
        back_text = "Повернутися до поточного режиму"
        off_text = "Вимкнути інтервальне харчування"
    else:
        labels = {
            "12_12": "🌿 12:12 · Начать мягко",
            "14_10": "⚖️ 14:10 · Умеренный режим",
            "16_8": "🌙 16:8 · Более длинная пауза",
        }
        back_text = "Вернуться к текущему режиму"
        off_text = "Выключить интервальное питание"

    rows = []
    for mode in ("12_12", "14_10", "16_8"):
        selected = mode == current
        text = labels[mode] + ("  ✅" if selected else "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=text,
                    callback_data=f"fasting:{mode}",
                    style=(
                        ButtonStyle.SUCCESS
                        if selected
                        else ButtonStyle.PRIMARY
                    ),
                )
            ]
        )

    if current:
        rows.append(
            [
                button(
                    f"↩️ {back_text}",
                    "fasting_status",
                    ButtonStyle.PRIMARY,
                )
            ]
        )
        rows.append(
            [
                button(
                    f"⛔ {off_text}",
                    "fasting:off",
                    ButtonStyle.DANGER,
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_fasting_options(
    message: Message,
    profile: dict,
    *,
    edit_existing: bool = False,
) -> None:
    language = profile.get("language") or "ru"
    text = fasting_options_text(profile, language)
    keyboard = fasting_options_keyboard(profile, language)

    if edit_existing:
        try:
            if message.photo:
                await message.edit_caption(
                    caption=text[:1024],
                    reply_markup=keyboard,
                )
            else:
                await message.edit_text(
                    text[:4096],
                    reply_markup=keyboard,
                )
            return
        except Exception:
            logging.exception(
                "Fasting options could not replace current message"
            )

    await message.answer(
        text[:4096],
        reply_markup=keyboard,
    )


async def send_fasting_status(message: Message, profile: dict) -> None:
    language = profile["language"]
    local_now = datetime.now(ZoneInfo(profile["timezone"]))
    status = fasting_status_data(profile, local_now)
    eating = status["phase"] == "eat"

    if language == "uk":
        if eating:
            meaning = (
                "🍽 Зараз відкрите вікно харчування. Можна спокійно поїсти "
                "звичайну порцію — не потрібно намагатися з'їсти все наперед."
            )
            phase_label = "МОЖНА ЇСТИ"
        else:
            meaning = (
                "🌙 Зараз триває період без їжі. Можна пити воду, несолодкий "
                "чай або каву без калорійних добавок. Якщо самопочуття погіршується, "
                "не потрібно терпіти заради таймера."
            )
            phase_label = "БЕЗ ЇЖІ"
        if eating:
            warning_time = time_from_minutes(
                minutes_from_time(status["end"]) - 30
            )
            notification_text = (
                f"Наступне повідомлення: о {warning_time} — попередження "
                f"за 30 хвилин, потім о {status['end']} — завершення вікна."
            )
        else:
            notification_text = (
                f"Наступне повідомлення: о {status['start']} — "
                "відкриття вікна харчування."
            )

        caption = (
            f"🕐 Ваш режим: {status['mode_label']}\n\n"
            f"• без їжі — {status['fast_hours']} годин;\n"
            f"• вікно харчування — {status['eat_hours']} годин;\n"
            f"• сьогодні можна їсти з {status['start']} до {status['end']};\n"
            f"• до наступної зміни залишилося {status['remaining_text']}.\n\n"
            f"{meaning}\n\n"
            f"🔔 {notification_text}\n"
            "Нагадування працюють автоматично, поки режим увімкнений.\n\n"
            "Коло не оновлюється щосекунди, щоб не засмічувати чат. "
            "Натисніть «Оновити коло», коли захочете побачити точний залишок."
        )
    else:
        if eating:
            meaning = (
                "🍽 Сейчас открыто окно питания. Можно спокойно поесть обычную порцию — "
                "не нужно стараться съесть всё заранее."
            )
            phase_label = "МОЖНО ЕСТЬ"
        else:
            meaning = (
                "🌙 Сейчас идёт период без еды. Можно пить воду, несладкий чай или кофе "
                "без калорийных добавок. Если самочувствие ухудшается, не нужно терпеть ради таймера."
            )
            phase_label = "БЕЗ ЕДЫ"
        if eating:
            warning_time = time_from_minutes(
                minutes_from_time(status["end"]) - 30
            )
            notification_text = (
                f"Следующее сообщение: в {warning_time} — предупреждение "
                f"за 30 минут, затем в {status['end']} — закрытие окна."
            )
        else:
            notification_text = (
                f"Следующее сообщение: в {status['start']} — "
                "открытие окна питания."
            )

        caption = (
            f"🕐 Ваш режим: {status['mode_label']}\n\n"
            f"• без еды — {status['fast_hours']} часов;\n"
            f"• окно питания — {status['eat_hours']} часов;\n"
            f"• сегодня можно есть с {status['start']} до {status['end']};\n"
            f"• до следующей смены осталось {status['remaining_text']}.\n\n"
            f"{meaning}\n\n"
            f"🔔 {notification_text}\n"
            "Напоминания работают автоматически, пока режим включён.\n\n"
            "Круг не обновляется каждую секунду, чтобы не засорять чат. "
            "Нажмите «Обновить круг», когда захотите увидеть точный остаток."
        )

    image = render_fasting_ring(
        mode_label=status["mode_label"],
        phase=phase_label,
        remaining_text=status["remaining_text"],
        progress=status["progress"],
        eating=eating,
        language=language,
    )
    if image:
        await message.answer_photo(
            BufferedInputFile(image, filename="fasting_status.png"),
            caption=caption,
            reply_markup=fasting_status_keyboard(language),
        )
    else:
        bar_count = round(status["progress"] * 10)
        bar = "●" * bar_count + "○" * (10 - bar_count)
        await message.answer(caption + f"\n\n{bar}", reply_markup=fasting_status_keyboard(language))


@router.callback_query(F.data == "menu:fasting")
async def fasting_menu(callback: CallbackQuery) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    if profile.get("safety_restricted"):
        await callback.message.answer(
            "⚠️ Інтервальне харчування не вмикається автоматично через зазначені обставини. Потрібне погодження з лікарем."
            if language == "uk" else
            "⚠️ Интервальное питание не включается автоматически из-за указанных обстоятельств. Нужно согласование с врачом."
        )
    elif profile.get("fasting_mode"):
        await send_fasting_status(callback.message, profile)
    else:
        await show_fasting_options(callback.message, profile)
    await callback.answer()


@router.callback_query(F.data == "fasting_change")
async def fasting_change_handler(callback: CallbackQuery) -> None:
    profile = await db.get_profile(callback.from_user.id)
    await show_fasting_options(
        callback.message,
        profile,
        edit_existing=True,
    )
    await callback.answer()


@router.callback_query(F.data == "fasting_status")
async def fasting_status_handler(callback: CallbackQuery) -> None:
    profile = await db.get_profile(callback.from_user.id)
    if not profile.get("fasting_mode"):
        await show_fasting_options(callback.message, profile)
    else:
        await send_fasting_status(callback.message, profile)
    await callback.answer()


@router.callback_query(F.data.startswith("fasting:"))
async def fasting_select(callback: CallbackQuery) -> None:
    mode = callback.data.split(":", 1)[1]
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"]

    if mode == "off":
        await db.save_profile(
            callback.from_user.id,
            {
                "fasting_mode": None,
                "fasting_start": None,
                "fasting_end": None,
            },
        )
        await callback.message.answer(
            (
                "✅ Інтервальне харчування вимкнено. "
                "Нагадування про відкриття й закриття вікна більше "
                "не приходитимуть. Звичайні нагадування про їжу "
                "залишилися без змін."
                if language == "uk"
                else
                "✅ Интервальное питание выключено. "
                "Напоминания об открытии и закрытии окна больше "
                "не будут приходить. Обычные напоминания о еде "
                "остались без изменений."
            )
        )
    else:
        fast_hours, eating_hours = fasting_mode_hours(mode)
        start = time_from_minutes(
            minutes_from_time(profile["wake_time"]) + 60
        )
        end = time_from_minutes(
            minutes_from_time(start) + eating_hours * 60
        )
        await db.save_profile(
            callback.from_user.id,
            {
                "fasting_mode": mode,
                "fasting_start": start,
                "fasting_end": end,
            },
        )
        profile = await db.get_profile(callback.from_user.id)

        await callback.message.answer(
            (
                f"✅ Обрано режим {fast_hours}:{eating_hours}. "
                f"Вікно харчування встановлено з {start} до {end}.\n\n"
                "Це не жорстке правило: якщо з'являється слабкість, "
                "запаморочення або сильний нав'язливий голод, "
                "режим краще вимкнути й поїсти."
                if language == "uk"
                else
                f"✅ Выбран режим {fast_hours}:{eating_hours}. "
                f"Окно питания установлено с {start} до {end}.\n\n"
                "Это не жёсткое правило: если появляются слабость, "
                "головокружение или сильный навязчивый голод, "
                "режим лучше выключить и поесть."
            )
        )
        await send_fasting_status(
            callback.message,
            profile,
        )

    await callback.answer()


@router.callback_query(F.data == "menu:back")
async def inline_main_menu(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile.get("language") if profile else "ru"
    if not profile_complete(profile):
        await callback.answer("Сначала завершите анкету.", show_alert=True)
        return
    await state.clear()
    await callback.message.answer(
        "Головне меню:" if language == "uk" else "Главное меню:",
        reply_markup=main_menu(language),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:drink")
async def drink_menu(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile.get("language") if profile else "ru"
    if not profile_complete(profile):
        await callback.answer("Сначала завершите анкету.", show_alert=True)
        return
    if not access_active(profile):
        await send_access_expired(callback.message, language)
        await callback.answer()
        return

    await state.clear()
    await callback.message.answer(
        "🥤 Що ви випили? Оберіть напій, а потім об'єм. "
        "Вода буде врахована без калорій, а молоко, сік і солодкі напої — "
        "у денному балансі енергії."
        if language == "uk" else
        "🥤 Что вы выпили? Выберите напиток, а затем объём. "
        "Вода будет учтена без калорий, а молоко, сок и сладкие напитки — "
        "в дневном балансе энергии.",
        reply_markup=drink_type_keyboard(language),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("drink:type:"))
async def drink_type_handler(callback: CallbackQuery, state: FSMContext) -> None:
    code = callback.data.rsplit(":", 1)[-1]
    profile = await db.get_profile(callback.from_user.id)
    language = profile.get("language") if profile else "ru"
    preset = DRINK_PRESETS.get(code)
    if not preset:
        await callback.answer("Неизвестный напиток.", show_alert=True)
        return
    await state.clear()
    await callback.message.answer(
        f"{preset['emoji']} {preset[language]}\n\n"
        + ("Оберіть об'єм:" if language == "uk" else "Выберите объём:"),
        reply_markup=drink_volume_keyboard(code, language),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("drink:volume:"))
async def drink_volume_handler(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    if len(parts) != 4 or parts[2] not in DRINK_PRESETS:
        await callback.answer("Ошибка выбора.", show_alert=True)
        return
    try:
        volume_ml = int(parts[3])
    except ValueError:
        await callback.answer("Ошибка объёма.", show_alert=True)
        return
    await state.clear()
    await save_drink_entry(
        callback.message,
        callback.from_user.id,
        parts[2],
        volume_ml,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("drink:custom:"))
async def drink_custom_volume_start(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    code = callback.data.rsplit(":", 1)[-1]
    profile = await db.get_profile(callback.from_user.id)
    language = profile.get("language") if profile else "ru"
    if code not in DRINK_PRESETS:
        await callback.answer("Неизвестный напиток.", show_alert=True)
        return
    await state.set_state(DrinkLog.custom_volume)
    await state.update_data(drink_code=code)
    await callback.message.answer(
        "Введіть об'єм у мілілітрах одним числом, наприклад 250."
        if language == "uk" else
        "Введите объём в миллилитрах одним числом, например 250."
    )
    await callback.answer()


@router.message(DrinkLog.custom_volume, F.text)
async def drink_custom_volume_input(
    message: Message,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile.get("language") if profile else "ru"
    raw = (message.text or "").strip().replace(",", ".")
    try:
        volume_ml = int(float(raw))
    except ValueError:
        volume_ml = 0
    if not 30 <= volume_ml <= 3000:
        await message.answer(
            "Введіть число від 30 до 3000 мл."
            if language == "uk" else
            "Введите число от 30 до 3000 мл."
        )
        return
    data = await state.get_data()
    code = str(data.get("drink_code") or "")
    await state.clear()
    if code not in DRINK_PRESETS:
        await message.answer(
            "Не вдалося визначити напій. Відкрийте меню ще раз."
            if language == "uk" else
            "Не удалось определить напиток. Откройте меню ещё раз."
        )
        return
    await save_drink_entry(message, message.from_user.id, code, volume_ml)


@router.callback_query(F.data == "menu:food")
async def food_menu(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"

    if not profile_complete(profile):
        await callback.answer("Сначала завершите анкету.", show_alert=True)
        return
    if not access_active(profile):
        text = (
            "Пробний доступ завершився. Відкрийте «Підписка» у меню."
            if language == "uk"
            else "Пробный доступ закончился. Откройте «Подписка» в меню."
        )
        await callback.message.answer(text)
        await callback.answer()
        return

    await state.set_state(Actions.food)
    text = (
        "🍽 Розкажіть, що ви з'їли. Я спробую оцінити калорії, білок, жири й вуглеводи "
        "та одразу запишу результат у сьогоднішній щоденник.\n\n"
        "Чим точніша кількість і спосіб приготування, тим корисніша оцінка.\n"
        "Наприклад: гречка 150 г, куряче філе 120 г, салат 200 г і одна столова ложка олії."
        if language == "uk"
        else
        "🍽 Расскажите, что вы съели. Я постараюсь оценить калории, белок, жиры и углеводы "
        "и сразу запишу результат в сегодняшний дневник.\n\n"
        "Чем точнее количество и способ приготовления, тем полезнее оценка.\n"
        "Например: гречка 150 г, куриное филе 120 г, салат 200 г и одна столовая ложка масла."
    )
    await callback.message.answer(text)
    await callback.answer()


@router.message(Actions.food, F.text)
async def food_text_handler(
    message: Message,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile["language"] if profile else "ru"
    description = (message.text or "").strip()

    if len(description) < 3:
        await message.answer(
            (
                "Опишіть їжу трохи детальніше: назва продукту, приблизна "
                "кількість і спосіб приготування."
                if language == "uk"
                else
                "Опишите еду чуть подробнее: название продукта, примерное "
                "количество и способ приготовления."
            )
        )
        return

    status = await message.answer(
        (
            "🧮 Розбираю продукти, оцінюю порції та рахую приблизний діапазон..."
            if language == "uk"
            else
            "🧮 Разбираю продукты, оцениваю порции и считаю примерный диапазон..."
        )
    )

    try:
        result = await analyze_food_text(description, language)
    except Exception:
        logging.exception(
            "Food analysis failed for user_id=%s",
            message.from_user.id,
        )
        await status.edit_text(
            (
                "Зараз не вдалося порахувати їжу через технічну помилку. "
                "Ваш запис не втрачено — спробуйте надіслати його ще раз за хвилину."
                if language == "uk"
                else
                "Сейчас не удалось посчитать еду из-за технической ошибки. "
                "Ваш текст не потерян — попробуйте отправить его ещё раз через минуту."
            )
        )
        await state.clear()
        return

    if result.get("needs_clarification"):
        await status.edit_text(
            (
                "❓ Щоб оцінка була кориснішою, уточніть:\n\n"
                if language == "uk"
                else
                "❓ Чтобы оценка была полезнее, уточните:\n\n"
            )
            + result["clarification_question"]
        )
        return

    local_date = datetime.now(
        ZoneInfo(profile["timezone"])
    ).date().isoformat()

    await db.add_food_log(
        user_id=message.from_user.id,
        description=description,
        calories_min=result["calories_min"],
        calories_max=result["calories_max"],
        protein_min=result["protein_min"],
        protein_max=result["protein_max"],
        fat_min=result["fat_min"],
        fat_max=result["fat_max"],
        carbs_min=result["carbs_min"],
        carbs_max=result["carbs_max"],
        local_date=local_date,
    )

    day = await db.daily_food_totals(
        message.from_user.id,
        local_date,
    )
    await state.clear()

    item_lines = []
    for item in result["items"][:8]:
        item_lines.append(
            f"• {item['name']} — {item['portion']}: "
            f"{item['calories_min']:.0f}–{item['calories_max']:.0f} ккал"
        )

    meal_calories = (
        float(result["calories_min"])
        + float(result["calories_max"])
    ) / 2
    meal_protein = (
        float(result["protein_min"])
        + float(result["protein_max"])
    ) / 2
    meal_fat = (
        float(result["fat_min"])
        + float(result["fat_max"])
    ) / 2
    meal_carbs = (
        float(result["carbs_min"])
        + float(result["carbs_max"])
    ) / 2

    eaten_calories = nutrition_midpoint(day, "calories")
    eaten_protein = nutrition_midpoint(day, "protein")
    eaten_fat = nutrition_midpoint(day, "fat")
    eaten_carbs = nutrition_midpoint(day, "carbs")

    calorie_target = float(profile["calorie_target"])
    protein_target = float(profile["protein_g"])
    fat_target = float(profile["fat_g"])
    carbs_target = float(profile["carbs_g"])

    calorie_left = calorie_target - eaten_calories
    protein_left = protein_target - eaten_protein
    fat_left = fat_target - eaten_fat
    carbs_left = carbs_target - eaten_carbs

    assumptions = [
        str(item).strip()
        for item in (result.get("assumptions") or [])
        if str(item).strip()
    ]

    if language == "uk":
        meal_explanation_parts = []

        if meal_protein >= 25:
            meal_explanation_parts.append(
                "У прийомі їжі досить багато білка — він допомагає довше "
                "відчувати ситість і підтримувати м'язи."
            )
        elif meal_protein < 12:
            meal_explanation_parts.append(
                "Білка в цьому прийомі їжі небагато. Це не помилка, але "
                "в наступний прийом можна додати яйця, рибу, птицю, сир, "
                "йогурт або бобові."
            )

        if meal_fat >= 20:
            meal_explanation_parts.append(
                "Помітна частина калорій припадає на жири. У цій страві "
                "основний внесок, ймовірно, дає олія, соус, сир або жирніше м'ясо."
            )

        if meal_carbs >= 35:
            meal_explanation_parts.append(
                "Вуглеводи дають енергію. Крупа, картопля, хліб, фрукти "
                "або солодкі добавки зазвичай формують більшу частину цього показника."
            )

        if not meal_explanation_parts:
            meal_explanation_parts.append(
                "Це помірний за складом прийом їжі. Його зручніше оцінювати "
                "разом з іншими записами за весь день."
            )

        if calorie_left < -50:
            next_step = (
                f"Сьогодні вийшло приблизно на {abs(calorie_left):.0f} "
                "кілокалорій вище орієнтиру. Не потрібно завтра голодувати, "
                "пропускати їжу або карати себе тренуванням. Наступний прийом "
                "їжі зробіть звичайним і поверніться до плану без компенсацій."
            )
        elif calorie_left <= 150:
            next_step = (
                "Ви вже близько до денного орієнтиру. Якщо фізичного голоду "
                "немає, спеціально «добирати норму» не потрібно. Якщо голод є, "
                "оберіть невелику звичну порцію й орієнтуйтеся на самопочуття."
            )
        else:
            suggestions = []
            if protein_left > 18:
                suggestions.append(
                    "джерело білка: риба, яйця, птиця, кисломолочний сир або бобові"
                )
            if carbs_left > 30:
                suggestions.append(
                    "джерело енергії: крупа, картопля, цільнозерновий хліб або фрукт"
                )
            if fat_left > 10:
                suggestions.append(
                    "невелику порцію жирів: горіхи, насіння, авокадо або трохи олії"
                )
            suggestions.append(
                "овочі — для об'єму страви та клітковини"
            )

            next_step = (
                f"До денного орієнтиру залишилося приблизно "
                f"{max(0, calorie_left):.0f} кілокалорій. "
                "На наступний прийом їжі можна обрати:\n"
                + "\n".join(f"• {item}" for item in suggestions)
                + "\n\nЦе варіанти, а не обов'язковий список."
            )

        assumptions_text = ""
        if assumptions:
            assumptions_text = (
                "\n\n🔎 Що довелося припустити:\n"
                + "\n".join(f"• {item}" for item in assumptions[:6])
            )

        text = (
            f"✅ Записано в сьогоднішній щоденник\n\n"
            f"🍽 {result['summary']}\n\n"
            + "\n".join(item_lines)
            + "\n\n"
            f"Оцінка цього прийому їжі:\n"
            f"• енергія — {result['calories_min']:.0f}–"
            f"{result['calories_max']:.0f} кілокалорій;\n"
            f"• білок — {result['protein_min']:.0f}–"
            f"{result['protein_max']:.0f} г;\n"
            f"• жири — {result['fat_min']:.0f}–"
            f"{result['fat_max']:.0f} г;\n"
            f"• вуглеводи — {result['carbs_min']:.0f}–"
            f"{result['carbs_max']:.0f} г.\n\n"
            "💡 Що це означає\n"
            + "\n\n".join(meal_explanation_parts)
            + "\n\n"
            "📊 Підсумок за сьогодні\n"
            f"• з'їдено приблизно {eaten_calories:.0f} із "
            f"{calorie_target:.0f} кілокалорій;\n"
            f"• білок — {eaten_protein:.0f} із {protein_target:.0f} г;\n"
            f"• жири — {eaten_fat:.0f} із {fat_target:.0f} г;\n"
            f"• вуглеводи — {eaten_carbs:.0f} із {carbs_target:.0f} г.\n\n"
            "🥗 Що робити далі\n"
            f"{next_step}"
            f"{assumptions_text}\n\n"
            "Оцінка приблизна: олія, соуси, маринад, рецепт і точна "
            "вага можуть помітно змінити результат."
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button(
                        "➕ Записати ще їжу",
                        "menu:food",
                        ButtonStyle.SUCCESS,
                    )
                ],
                [
                    button(
                        "🥗 Що краще з'їсти сьогодні",
                        "menu:foods",
                        ButtonStyle.PRIMARY,
                    )
                ],
                [
                    button(
                        "📅 Відкрити календар",
                        "menu:calendar",
                        ButtonStyle.PRIMARY,
                    ),
                    button(
                        "🧾 Моя інформація",
                        "menu:profile",
                        ButtonStyle.SUCCESS,
                    ),
                ],
            ]
        )
    else:
        meal_explanation_parts = []

        if meal_protein >= 25:
            meal_explanation_parts.append(
                "В приёме пищи достаточно белка — он помогает дольше "
                "чувствовать сытость и поддерживать мышцы."
            )
        elif meal_protein < 12:
            meal_explanation_parts.append(
                "Белка в этом приёме пищи немного. Это не ошибка, но "
                "в следующий приём можно добавить яйца, рыбу, птицу, творог, "
                "йогурт или бобовые."
            )

        if meal_fat >= 20:
            meal_explanation_parts.append(
                "Заметная часть калорий приходится на жиры. В этом блюде "
                "основной вклад, вероятно, даёт масло, соус, сыр или более жирное мясо."
            )

        if meal_carbs >= 35:
            meal_explanation_parts.append(
                "Углеводы дают энергию. Крупа, картофель, хлеб, фрукты "
                "или сладкие добавки обычно формируют большую часть этого показателя."
            )

        if not meal_explanation_parts:
            meal_explanation_parts.append(
                "Это умеренный по составу приём пищи. Его удобнее оценивать "
                "вместе с остальными записями за весь день."
            )

        if calorie_left < -50:
            next_step = (
                f"Сегодня получилось примерно на {abs(calorie_left):.0f} "
                "килокалорий выше ориентира. Не нужно завтра голодать, "
                "пропускать еду или наказывать себя тренировкой. Следующий "
                "приём пищи сделайте обычным и вернитесь к плану без компенсаций."
            )
        elif calorie_left <= 150:
            next_step = (
                "Вы уже близко к дневному ориентиру. Если физического голода "
                "нет, специально «добирать норму» не нужно. Если голод есть, "
                "выберите небольшую обычную порцию и ориентируйтесь на самочувствие."
            )
        else:
            suggestions = []
            if protein_left > 18:
                suggestions.append(
                    "источник белка: рыбу, яйца, птицу, творог или бобовые"
                )
            if carbs_left > 30:
                suggestions.append(
                    "источник энергии: крупу, картофель, цельнозерновой хлеб или фрукт"
                )
            if fat_left > 10:
                suggestions.append(
                    "небольшую порцию жиров: орехи, семена, авокадо или немного масла"
                )
            suggestions.append(
                "овощи — для объёма блюда и клетчатки"
            )

            next_step = (
                f"До дневного ориентира осталось примерно "
                f"{max(0, calorie_left):.0f} килокалорий. "
                "Для следующего приёма пищи можно выбрать:\n"
                + "\n".join(f"• {item}" for item in suggestions)
                + "\n\nЭто варианты, а не обязательный список."
            )

        assumptions_text = ""
        if assumptions:
            assumptions_text = (
                "\n\n🔎 Что пришлось предположить:\n"
                + "\n".join(f"• {item}" for item in assumptions[:6])
            )

        text = (
            f"✅ Записано в сегодняшний дневник\n\n"
            f"🍽 {result['summary']}\n\n"
            + "\n".join(item_lines)
            + "\n\n"
            f"Оценка этого приёма пищи:\n"
            f"• энергия — {result['calories_min']:.0f}–"
            f"{result['calories_max']:.0f} килокалорий;\n"
            f"• белок — {result['protein_min']:.0f}–"
            f"{result['protein_max']:.0f} г;\n"
            f"• жиры — {result['fat_min']:.0f}–"
            f"{result['fat_max']:.0f} г;\n"
            f"• углеводы — {result['carbs_min']:.0f}–"
            f"{result['carbs_max']:.0f} г.\n\n"
            "💡 Что это означает\n"
            + "\n\n".join(meal_explanation_parts)
            + "\n\n"
            "📊 Итог за сегодня\n"
            f"• съедено примерно {eaten_calories:.0f} из "
            f"{calorie_target:.0f} килокалорий;\n"
            f"• белок — {eaten_protein:.0f} из {protein_target:.0f} г;\n"
            f"• жиры — {eaten_fat:.0f} из {fat_target:.0f} г;\n"
            f"• углеводы — {eaten_carbs:.0f} из {carbs_target:.0f} г.\n\n"
            "🥗 Что делать дальше\n"
            f"{next_step}"
            f"{assumptions_text}\n\n"
            "Оценка приблизительная: масло, соусы, маринад, рецепт и точный "
            "вес могут заметно изменить результат."
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button(
                        "➕ Записать ещё еду",
                        "menu:food",
                        ButtonStyle.SUCCESS,
                    )
                ],
                [
                    button(
                        "🥗 Что лучше съесть сегодня",
                        "menu:foods",
                        ButtonStyle.PRIMARY,
                    )
                ],
                [
                    button(
                        "📅 Открыть календарь",
                        "menu:calendar",
                        ButtonStyle.PRIMARY,
                    ),
                    button(
                        "🧾 Моя информация",
                        "menu:profile",
                        ButtonStyle.SUCCESS,
                    ),
                ],
            ]
        )

    await status.edit_text(
        text[:4096],
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "menu:photo")
async def photo_menu(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"

    if not profile_complete(profile):
        await callback.answer(
            "Сначала завершите анкету.",
            show_alert=True,
        )
        return

    used = await db.photo_analysis_count(callback.from_user.id)
    is_admin_test = callback.from_user.id == settings.admin_id
    if (
        used >= 1
        and not paid_subscription_active(profile)
        and not is_admin_test
    ):
        text = (
            "📸 Перший безкоштовний аналіз уже використано.\n\n"
            "Наступні фото доступні після оформлення підписки. "
            "Ваш попередній запис і весь щоденник збережені."
            if language == "uk"
            else
            "📸 Первый бесплатный анализ уже использован.\n\n"
            "Следующие фотографии доступны после оформления подписки. "
            "Предыдущая запись и весь дневник сохранены."
        )
        await callback.message.answer(
            text,
            reply_markup=subscription_keyboard(language),
        )
        await callback.answer()
        return

    await state.set_state(Actions.photo)

    if language == "uk":
        text = (
            "📸 Надішліть одне чітке фото всієї страви.\n\n"
            "Я визначу склад, розрахую робочу оцінку калорій, білка, "
            "жирів і вуглеводів та одразу запишу її в сьогоднішній щоденник.\n\n"
            "Для точнішого результату додайте підпис, наприклад:\n"
            "«макарони з сиром, уся порція 200 г, соусу приблизно 20 г».\n\n"
            "Якщо важливої інформації не вистачить, я поставлю лише одне "
            "коротке уточнювальне запитання, а не вигадуватиму число.\n\n"
            "Фото передається OpenAI для аналізу; сам файл бот не зберігає."
        )
    else:
        text = (
            "📸 Пришлите одну чёткую фотографию всего блюда.\n\n"
            "Я определю состав, рассчитаю рабочую оценку калорий, белка, "
            "жиров и углеводов и сразу запишу её в сегодняшний дневник.\n\n"
            "Для более точного результата добавьте подпись, например:\n"
            "«макароны с сыром, вся порция 200 г, соуса примерно 20 г».\n\n"
            "Если важной информации не хватит, я задам только один "
            "короткий уточняющий вопрос, а не буду придумывать цифру.\n\n"
            "Фотография передаётся OpenAI для анализа; сам файл бот не сохраняет."
        )

    if is_admin_test:
        text += (
            "\n\n🛠 Увімкнено режим адміністратора: тестові фото "
            "можна перевіряти без обмежень."
            if language == "uk"
            else
            "\n\n🛠 Включён режим администратора: тестовые фотографии "
            "можно проверять без ограничений."
        )
    elif used == 0:
        text += (
            "\n\n🎁 Це ваш перший безкоштовний аналіз."
            if language == "uk"
            else "\n\n🎁 Это ваш первый бесплатный анализ."
        )

    await callback.message.answer(text)
    await callback.answer()


@router.message(Actions.photo, F.photo)
async def photo_handler(
    message: Message,
    state: FSMContext,
    bot: Bot,
) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile["language"] if profile else "ru"

    used = await db.photo_analysis_count(message.from_user.id)
    is_admin_test = message.from_user.id == settings.admin_id
    if (
        used >= 1
        and not paid_subscription_active(profile)
        and not is_admin_test
    ):
        await state.clear()
        await message.answer(
            (
                "Первый бесплатный анализ уже использован. "
                "Откройте раздел «Подписка»."
            )
            if language == "ru"
            else
            (
                "Перший безкоштовний аналіз уже використано. "
                "Відкрийте розділ «Підписка»."
            )
        )
        return

    status = await message.answer(
        (
            "📸 Розпізнаю страву, оцінюю порцію та готую запис у щоденник..."
            if language == "uk"
            else
            "📸 Распознаю блюдо, оцениваю порцию и готовлю запись в дневник..."
        )
    )

    try:
        buffer = BytesIO()
        await bot.download(
            message.photo[-1],
            destination=buffer,
        )
        image_bytes = buffer.getvalue()
        if not image_bytes:
            raise ValueError("Empty Telegram image")

        result = await analyze_food_photo(
            image_bytes,
            language,
            message.caption or "",
        )
    except Exception:
        logging.exception(
            "Photo analysis failed for user_id=%s",
            message.from_user.id,
        )
        await status.edit_text(
            (
                "Сейчас фотографию не удалось обработать из-за технической "
                "ошибки. Попробуйте ещё раз через минуту или отправьте другое фото."
            )
            if language == "ru"
            else
            (
                "Зараз фотографію не вдалося обробити через технічну "
                "помилку. Спробуйте ще раз за хвилину або надішліть інше фото."
            )
        )
        await state.clear()
        return

    if result.get("needs_clarification"):
        await status.edit_text(
            (
                "❓ Мне не хватает одной детали, чтобы сделать полезную запись:\n\n"
                if language == "ru"
                else
                "❓ Мені не вистачає однієї деталі, щоб зробити корисний запис:\n\n"
            )
            + result["clarification_question"]
            + (
                "\n\nОтправьте фотографию ещё раз с ответом в подписи."
                if language == "ru"
                else
                "\n\nНадішліть фотографію ще раз із відповіддю в підписі."
            )
        )
        return

    local_date = datetime.now(
        ZoneInfo(profile["timezone"])
    ).date().isoformat()

    await db.record_photo_analysis(
        user_id=message.from_user.id,
        file_unique_id=message.photo[-1].file_unique_id,
        summary=result["summary"],
        calories_min=result["calories_min"],
        calories_max=result["calories_max"],
        protein_min=result["protein_min"],
        protein_max=result["protein_max"],
        fat_min=result["fat_min"],
        fat_max=result["fat_max"],
        carbs_min=result["carbs_min"],
        carbs_max=result["carbs_max"],
        local_date=local_date,
    )

    day = await db.daily_food_totals(
        message.from_user.id,
        local_date,
    )
    await state.clear()

    working_calories = (
        float(result["calories_min"])
        + float(result["calories_max"])
    ) / 2
    working_protein = (
        float(result["protein_min"])
        + float(result["protein_max"])
    ) / 2
    working_fat = (
        float(result["fat_min"])
        + float(result["fat_max"])
    ) / 2
    working_carbs = (
        float(result["carbs_min"])
        + float(result["carbs_max"])
    ) / 2

    eaten_calories = nutrition_midpoint(day, "calories")
    eaten_protein = nutrition_midpoint(day, "protein")
    eaten_fat = nutrition_midpoint(day, "fat")
    eaten_carbs = nutrition_midpoint(day, "carbs")

    calorie_target = float(profile["calorie_target"])
    protein_target = float(profile["protein_g"])
    fat_target = float(profile["fat_g"])
    carbs_target = float(profile["carbs_g"])

    calorie_left = calorie_target - eaten_calories
    protein_left = protein_target - eaten_protein
    fat_left = fat_target - eaten_fat
    carbs_left = carbs_target - eaten_carbs

    item_lines = [
        f"• {item['name']} — {item['portion']}"
        for item in result["items"][:8]
    ]

    considered = [
        str(item).strip()
        for item in (result.get("assumptions") or [])
        if str(item).strip()
    ]

    if language == "uk":
        if calorie_left < -50:
            next_step = (
                f"Сьогодні вийшло приблизно на {abs(calorie_left):.0f} "
                "кілокалорій вище орієнтиру. Це не потрібно компенсувати "
                "голодуванням. Наступний прийом їжі зробіть звичайним."
            )
        elif calorie_left <= 150:
            next_step = (
                "Ви вже близько до денного орієнтиру. Якщо голоду немає, "
                "спеціально їсти заради цифр не потрібно."
            )
        else:
            suggestions = []
            if protein_left > 18:
                suggestions.append(
                    "білок: риба, яйця, птиця, кисломолочний сир або бобові"
                )
            if carbs_left > 30:
                suggestions.append(
                    "енергія: крупа, картопля, цільнозерновий хліб або фрукт"
                )
            if fat_left > 10:
                suggestions.append(
                    "жири: невелика порція горіхів, насіння, авокадо або олії"
                )
            suggestions.append(
                "овочі — для клітковини та об'єму страви"
            )
            next_step = (
                f"До денного орієнтиру залишилося близько "
                f"{max(0, calorie_left):.0f} кілокалорій. "
                "Наступний прийом їжі можна скласти з таких частин:\n"
                + "\n".join(f"• {item}" for item in suggestions)
            )

        considered_text = ""
        if considered:
            considered_text = (
                "\n\n👁 Що я врахував під час розрахунку:\n"
                + "\n".join(f"• {item}" for item in considered[:5])
            )

        text = (
            "✅ Страву записано в сьогоднішній щоденник\n\n"
            f"📸 {result['summary']}\n"
            + "\n".join(item_lines)
            + "\n\n"
            f"🎯 Робоча оцінка для щоденника: "
            f"близько {working_calories:.0f} кілокалорій.\n\n"
            f"У цій порції приблизно:\n"
            f"• білок — {working_protein:.0f} г;\n"
            f"• жири — {working_fat:.0f} г;\n"
            f"• вуглеводи — {working_carbs:.0f} г.\n\n"
            "📊 Підсумок за сьогодні\n"
            f"• енергія — {eaten_calories:.0f} із "
            f"{calorie_target:.0f} кілокалорій;\n"
            f"• білок — {eaten_protein:.0f} із {protein_target:.0f} г;\n"
            f"• жири — {eaten_fat:.0f} із {fat_target:.0f} г;\n"
            f"• вуглеводи — {eaten_carbs:.0f} із {carbs_target:.0f} г.\n\n"
            "🥗 Що робити далі\n"
            f"{next_step}"
            f"{considered_text}\n\n"
            "Для схуднення не потрібна лабораторна точність кожної тарілки. "
            "Важливі регулярні записи й загальна тенденція за тиждень. "
            "Я використовую одну робочу оцінку, щоб щоденник залишався "
            "простим і корисним."
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button(
                        "📸 Записати наступну страву",
                        "menu:photo",
                        ButtonStyle.SUCCESS,
                    )
                ],
                [
                    button(
                        "🥗 Що краще з'їсти далі",
                        "menu:foods",
                        ButtonStyle.PRIMARY,
                    )
                ],
                [
                    button(
                        "🧾 Моя інформація",
                        "menu:profile",
                        ButtonStyle.SUCCESS,
                    ),
                    button(
                        "📅 Календар",
                        "menu:calendar",
                        ButtonStyle.PRIMARY,
                    ),
                ],
            ]
        )
    else:
        if calorie_left < -50:
            next_step = (
                f"Сегодня получилось примерно на {abs(calorie_left):.0f} "
                "килокалорий выше ориентира. Это не нужно компенсировать "
                "голоданием. Следующий приём пищи сделайте обычным."
            )
        elif calorie_left <= 150:
            next_step = (
                "Вы уже близко к дневному ориентиру. Если голода нет, "
                "специально есть ради цифр не нужно."
            )
        else:
            suggestions = []
            if protein_left > 18:
                suggestions.append(
                    "белок: рыбу, яйца, птицу, творог или бобовые"
                )
            if carbs_left > 30:
                suggestions.append(
                    "энергию: крупу, картофель, цельнозерновой хлеб или фрукт"
                )
            if fat_left > 10:
                suggestions.append(
                    "жиры: небольшую порцию орехов, семян, авокадо или масла"
                )
            suggestions.append(
                "овощи — для клетчатки и объёма блюда"
            )
            next_step = (
                f"До дневного ориентира осталось около "
                f"{max(0, calorie_left):.0f} килокалорий. "
                "Следующий приём пищи можно собрать из таких частей:\n"
                + "\n".join(f"• {item}" for item in suggestions)
            )

        considered_text = ""
        if considered:
            considered_text = (
                "\n\n👁 Что я учёл при расчёте:\n"
                + "\n".join(f"• {item}" for item in considered[:5])
            )

        text = (
            "✅ Блюдо записано в сегодняшний дневник\n\n"
            f"📸 {result['summary']}\n"
            + "\n".join(item_lines)
            + "\n\n"
            f"🎯 Рабочая оценка для дневника: "
            f"около {working_calories:.0f} килокалорий.\n\n"
            f"В этой порции примерно:\n"
            f"• белок — {working_protein:.0f} г;\n"
            f"• жиры — {working_fat:.0f} г;\n"
            f"• углеводы — {working_carbs:.0f} г.\n\n"
            "📊 Итог за сегодня\n"
            f"• энергия — {eaten_calories:.0f} из "
            f"{calorie_target:.0f} килокалорий;\n"
            f"• белок — {eaten_protein:.0f} из {protein_target:.0f} г;\n"
            f"• жиры — {eaten_fat:.0f} из {fat_target:.0f} г;\n"
            f"• углеводы — {eaten_carbs:.0f} из {carbs_target:.0f} г.\n\n"
            "🥗 Что делать дальше\n"
            f"{next_step}"
            f"{considered_text}\n\n"
            "Для снижения веса не нужна лабораторная точность каждой тарелки. "
            "Важны регулярные записи и общая тенденция за неделю. "
            "Я использую одну рабочую оценку, чтобы дневник оставался "
            "простым и полезным."
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button(
                        "📸 Записать следующее блюдо",
                        "menu:photo",
                        ButtonStyle.SUCCESS,
                    )
                ],
                [
                    button(
                        "🥗 Что лучше съесть дальше",
                        "menu:foods",
                        ButtonStyle.PRIMARY,
                    )
                ],
                [
                    button(
                        "🧾 Моя информация",
                        "menu:profile",
                        ButtonStyle.SUCCESS,
                    ),
                    button(
                        "📅 Календарь",
                        "menu:calendar",
                        ButtonStyle.PRIMARY,
                    ),
                ],
            ]
        )

    await status.edit_text(
        text[:4096],
        reply_markup=keyboard,
    )


@router.message(Actions.photo)
async def photo_required_handler(message: Message) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile["language"] if profile else "ru"
    await message.answer(
        (
            "Надішліть саме фотографію всієї страви. "
            "Вагу та склад можна написати в підписі до фото."
            if language == "uk"
            else
            "Пришлите именно фотографию всего блюда. "
            "Вес и состав можно написать в подписи к фотографии."
        )
    )



async def send_recipe_loading(
    message: Message,
    language: str,
    *,
    more: bool = False,
) -> Message:
    animation = render_recipe_picker_animation(language)
    if language == "uk":
        caption = (
            "✨ Шукаю ще шість нових варіантів без повторів..."
            if more
            else
            "🥗 Дивлюся записи за сьогодні й збираю шість різних варіантів..."
        )
    else:
        caption = (
            "✨ Ищу ещё шесть новых вариантов без повторов..."
            if more
            else
            "🥗 Смотрю записи за сегодня и собираю шесть разных вариантов..."
        )

    if animation:
        return await message.answer_animation(
            BufferedInputFile(
                animation,
                filename="recipe_picker.gif",
            ),
            caption=caption,
        )
    return await message.answer(caption)


async def update_recipe_loading_error(
    loading_message: Message,
    text: str,
) -> None:
    if loading_message.animation:
        await loading_message.edit_caption(caption=text)
    else:
        await loading_message.edit_text(text)


@router.callback_query(F.data == "menu:foods")
async def foods_menu(callback: CallbackQuery) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"

    if not profile_complete(profile):
        await callback.answer(
            "Сначала завершите анкету.",
            show_alert=True,
        )
        return
    if not access_active(profile):
        await callback.message.answer(
            "Пробний доступ завершився. Відкрийте «Підписка»."
            if language == "uk"
            else "Пробный доступ закончился. Откройте «Подписка»."
        )
        await callback.answer()
        return

    local_date = datetime.now(
        ZoneInfo(profile["timezone"])
    ).date().isoformat()
    day = await db.daily_food_totals(
        callback.from_user.id,
        local_date,
    )
    marker = food_gallery_marker(local_date, day)

    await callback.answer()
    loading = await send_recipe_loading(
        callback.message,
        language,
    )

    try:
        cached = await db.get_latest_menu_session_by_products(
            callback.from_user.id,
            marker,
            local_date,
            max_age_seconds=21600,
        )
        from_cache = bool(
            cached
            and (cached.get("menu_data") or {}).get("recipes")
        )
        if from_cache:
            session_id = cached["session_id"]
            gallery = cached["menu_data"]
            # The animation should remain visible briefly even if data is cached.
            await asyncio.sleep(1.5)
        else:
            gallery = await generate_personalized_food_gallery(
                profile,
                day,
            )
            session_id = uuid.uuid4().hex[:16]
            await db.save_menu_session(
                session_id=session_id,
                user_id=callback.from_user.id,
                products_text=marker,
                menu_data=gallery,
                local_date=local_date,
            )
    except Exception:
        logging.exception(
            "Food gallery generation failed for user_id=%s",
            callback.from_user.id,
        )
        await update_recipe_loading_error(
            loading,
            (
                "Не вдалося створити добірку. Спробуйте ще раз через хвилину."
                if language == "uk"
                else
                "Не удалось создать подборку. Попробуйте ещё раз через минуту."
            ),
        )
        return

    try:
        await loading.delete()
    except Exception:
        logging.exception("Recipe loading message could not be deleted")

    summary = personalized_food_advice(profile, day)
    suffix = (
        "\n\n✨ Я підготував 6 різних варіантів. "
        "Перегортайте картки й оберіть одну страву, яка справді хочеться. "
        "Усі шість готувати не потрібно — це галерея, а не кулінарний марафон 😌"
        if language == "uk"
        else
        "\n\n✨ Я подготовил 6 разных вариантов. "
        "Листайте карточки и выберите одно блюдо, которое действительно хочется. "
        "Готовить все шесть не нужно — это галерея, а не кулинарный марафон 😌"
    )
    await callback.message.answer(
        (summary + suffix)[:4096]
    )
    await send_food_gallery_card(
        callback.message,
        session_id,
        gallery,
        0,
        language,
    )


@router.callback_query(F.data.startswith("food_gallery_nav:"))
async def food_gallery_navigation_handler(
    callback: CallbackQuery,
) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer(
            "Ошибка карточки.",
            show_alert=True,
        )
        return

    session_id = parts[1]
    try:
        index = int(parts[2])
    except ValueError:
        await callback.answer(
            "Ошибка карточки.",
            show_alert=True,
        )
        return

    session = await db.get_menu_session(
        session_id,
        callback.from_user.id,
    )
    if not session:
        await callback.answer(
            "Подборка больше не найдена. Откройте раздел заново.",
            show_alert=True,
        )
        return

    gallery = session["menu_data"]
    recipes = gallery.get("recipes") or []
    if not recipes:
        await callback.answer(
            "В подборке нет рецептов.",
            show_alert=True,
        )
        return

    index %= len(recipes)
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    recipe = recipes[index]
    image = render_recipe_card(
        recipe=recipe,
        index=index,
        total=len(recipes),
        language=language,
    )
    caption = recipe_caption(
        gallery,
        recipe,
        index,
        len(recipes),
        language,
    )
    keyboard = food_gallery_keyboard(
        session_id,
        index,
        len(recipes),
        language,
    )

    if image and callback.message.photo:
        await callback.message.edit_media(
            media=InputMediaPhoto(
                media=BufferedInputFile(
                    image,
                    filename=f"food_gallery_{index + 1}.png",
                ),
                caption=caption,
            ),
            reply_markup=keyboard,
        )
    else:
        await send_food_gallery_card(
            callback.message,
            session_id,
            gallery,
            index,
            language,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("food_gallery_more:"))
async def food_gallery_more_handler(
    callback: CallbackQuery,
) -> None:
    session_id = callback.data.split(":", 1)[1]
    session = await db.get_menu_session(
        session_id,
        callback.from_user.id,
    )
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"

    if not session or not profile:
        await callback.answer(
            "Откройте раздел «Что лучше есть» заново.",
            show_alert=True,
        )
        return

    await callback.answer()
    loading = await send_recipe_loading(
        callback.message,
        language,
        more=True,
    )

    try:
        local_date = datetime.now(
            ZoneInfo(profile["timezone"])
        ).date().isoformat()
        day = await db.daily_food_totals(
            callback.from_user.id,
            local_date,
        )
        previous_gallery = session["menu_data"]
        avoid_titles = previous_gallery.get("seen_titles") or [
            recipe.get("title", "")
            for recipe in previous_gallery.get("recipes", [])
        ]
        gallery = await generate_personalized_food_gallery(
            profile,
            day,
            avoid_titles=avoid_titles,
        )
        new_session_id = uuid.uuid4().hex[:16]
        await db.save_menu_session(
            session_id=new_session_id,
            user_id=callback.from_user.id,
            products_text=food_gallery_marker(local_date, day),
            menu_data=gallery,
            local_date=local_date,
        )
    except Exception:
        logging.exception(
            "More food recipes failed for user_id=%s",
            callback.from_user.id,
        )
        await update_recipe_loading_error(
            loading,
            (
                "Не вдалося створити нові рецепти. Спробуйте ще раз."
                if language == "uk"
                else
                "Не удалось создать новые рецепты. Попробуйте ещё раз."
            ),
        )
        return

    try:
        await loading.delete()
    except Exception:
        logging.exception("More-recipes animation could not be deleted")

    await callback.message.answer(
        (
            "✨ Готово! Нижче ще 6 нових варіантів без повторів."
            if language == "uk"
            else "✨ Готово! Ниже ещё 6 новых вариантов без повторов."
        )
    )
    await send_food_gallery_card(
        callback.message,
        new_session_id,
        gallery,
        0,
        language,
    )


@router.callback_query(F.data.startswith("food_gallery_choose:"))
async def food_gallery_choose_handler(
    callback: CallbackQuery,
) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer(
            "Ошибка выбора.",
            show_alert=True,
        )
        return

    session_id = parts[1]
    try:
        index = int(parts[2])
    except ValueError:
        await callback.answer(
            "Ошибка выбора.",
            show_alert=True,
        )
        return

    session = await db.get_menu_session(
        session_id,
        callback.from_user.id,
    )
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"

    if not session or not profile:
        await callback.answer(
            "Откройте галерею заново.",
            show_alert=True,
        )
        return

    recipes = (session.get("menu_data") or {}).get("recipes") or []
    if not recipes:
        await callback.answer(
            "Рецепт не найден.",
            show_alert=True,
        )
        return

    index %= len(recipes)
    recipe = recipes[index]
    local_date = datetime.now(
        ZoneInfo(profile["timezone"])
    ).date().isoformat()

    await db.save_recipe_choice(
        user_id=callback.from_user.id,
        session_id=session_id,
        recipe_index=index,
        recipe=recipe,
        local_date=local_date,
    )
    await callback.answer(
        "Выбор сохранён"
        if language == "ru"
        else "Вибір збережено"
    )

    animation = render_recipe_choice_animation(
        recipe,
        language,
    )
    if animation:
        await callback.message.answer_animation(
            BufferedInputFile(
                animation,
                filename="recipe_selected.gif",
            )
        )

    if language == "uk":
        text = (
            f"✅ Ви обрали: {recipe.get('title')}\n\n"
            f"Планована порція: {recipe.get('portion') or '1 порція'}.\n"
            f"Робоча оцінка: близько {recipe.get('calories', 0)} "
            "кілокалорій.\n\n"
            "Я зберіг вибір, але поки не додав страву до харчового "
            "щоденника. Запис з'явиться лише після кнопки "
            "«Я вже з'їв/з'їла цю порцію».\n\n"
            "Можна відкрити приготування крок за кроком або повернутися "
            "до галереї."
        )
    else:
        text = (
            f"✅ Вы выбрали: {recipe.get('title')}\n\n"
            f"Планируемая порция: {recipe.get('portion') or '1 порция'}.\n"
            f"Рабочая оценка: около {recipe.get('calories', 0)} "
            "килокалорий.\n\n"
            "Я сохранил выбор, но пока не добавил блюдо в дневник "
            "питания. Запись появится только после кнопки "
            "«Я уже съел(а) эту порцию».\n\n"
            "Можно открыть приготовление шаг за шагом или вернуться "
            "к галерее."
        )

    await callback.message.answer(
        text,
        reply_markup=selected_recipe_keyboard(
            session_id,
            index,
            language,
        ),
    )


@router.callback_query(F.data.startswith("food_gallery_show:"))
async def food_gallery_show_handler(
    callback: CallbackQuery,
) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer(
            "Ошибка карточки.",
            show_alert=True,
        )
        return

    session_id = parts[1]
    try:
        index = int(parts[2])
    except ValueError:
        await callback.answer(
            "Ошибка карточки.",
            show_alert=True,
        )
        return

    session = await db.get_menu_session(
        session_id,
        callback.from_user.id,
    )
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"

    if not session:
        await callback.answer(
            "Откройте галерею заново.",
            show_alert=True,
        )
        return

    recipes = (session.get("menu_data") or {}).get("recipes") or []
    if not recipes:
        await callback.answer(
            "Рецепты не найдены.",
            show_alert=True,
        )
        return

    await send_food_gallery_card(
        callback.message,
        session_id,
        session["menu_data"],
        index % len(recipes),
        language,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("recipe_step:"))
async def recipe_step_handler(
    callback: CallbackQuery,
) -> None:
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer(
            "Ошибка шага.",
            show_alert=True,
        )
        return

    session_id = parts[1]
    try:
        recipe_index = int(parts[2])
        step_index = int(parts[3])
    except ValueError:
        await callback.answer(
            "Ошибка шага.",
            show_alert=True,
        )
        return

    session = await db.get_menu_session(
        session_id,
        callback.from_user.id,
    )
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"

    if not session:
        await callback.answer(
            "Рецепт больше не найден.",
            show_alert=True,
        )
        return

    recipes = (session.get("menu_data") or {}).get("recipes") or []
    if not recipes:
        await callback.answer(
            "Рецепт больше не найден.",
            show_alert=True,
        )
        return

    recipe_index %= len(recipes)
    recipe = recipes[recipe_index]
    steps = [
        str(value).strip()
        for value in (recipe.get("steps") or [])
        if str(value).strip()
    ]
    if not steps:
        await callback.answer(
            "В рецепте нет шагов.",
            show_alert=True,
        )
        return

    step_index = max(0, min(step_index, len(steps) - 1))
    ingredients = [
        str(value).strip()
        for value in (recipe.get("ingredients") or [])
        if str(value).strip()
    ]

    if language == "uk":
        ingredients_text = ""
        if step_index == 0:
            ingredients_text = (
                "\n\n🧺 Підготуйте інгредієнти:\n"
                + "\n".join(f"• {item}" for item in ingredients[:8])
            )
        text = (
            f"🧑‍🍳 {recipe.get('title')}\n\n"
            f"Крок {step_index + 1} із {len(steps)}\n\n"
            f"{steps[step_index]}"
            f"{ingredients_text}\n\n"
            "Рухайтеся у своєму темпі. Кнопка «Готово, далі» "
            "не запускає секундомір — вона просто відкриває наступний крок."
        )
    else:
        ingredients_text = ""
        if step_index == 0:
            ingredients_text = (
                "\n\n🧺 Подготовьте ингредиенты:\n"
                + "\n".join(f"• {item}" for item in ingredients[:8])
            )
        text = (
            f"🧑‍🍳 {recipe.get('title')}\n\n"
            f"Шаг {step_index + 1} из {len(steps)}\n\n"
            f"{steps[step_index]}"
            f"{ingredients_text}\n\n"
            "Двигайтесь в своём темпе. Кнопка «Готово, дальше» "
            "не запускает секундомер — она просто открывает следующий шаг."
        )

    keyboard = cooking_step_keyboard(
        session_id,
        recipe_index,
        step_index,
        len(steps),
        language,
    )

    try:
        await callback.message.edit_text(
            text[:4096],
            reply_markup=keyboard,
        )
    except Exception:
        await callback.message.answer(
            text[:4096],
            reply_markup=keyboard,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("recipe_close:"))
async def recipe_close_handler(
    callback: CallbackQuery,
) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return

    session_id = parts[1]
    try:
        index = int(parts[2])
    except ValueError:
        await callback.answer()
        return

    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"

    await callback.message.answer(
        (
            "Рецепт закрито. Ваш вибір збережено — можна повернутися "
            "до нього через цю переписку або обрати іншу страву."
            if language == "uk"
            else
            "Рецепт закрыт. Ваш выбор сохранён — можно вернуться "
            "к нему в этой переписке или выбрать другое блюдо."
        ),
        reply_markup=selected_recipe_keyboard(
            session_id,
            index,
            language,
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("recipe_eat:"))
async def recipe_eat_handler(
    callback: CallbackQuery,
) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer(
            "Ошибка записи.",
            show_alert=True,
        )
        return

    session_id = parts[1]
    try:
        index = int(parts[2])
    except ValueError:
        await callback.answer(
            "Ошибка записи.",
            show_alert=True,
        )
        return

    session = await db.get_menu_session(
        session_id,
        callback.from_user.id,
    )
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"

    if not session or not profile:
        await callback.answer(
            "Рецепт больше не найден.",
            show_alert=True,
        )
        return

    recipes = (session.get("menu_data") or {}).get("recipes") or []
    if not recipes:
        await callback.answer(
            "Рецепт больше не найден.",
            show_alert=True,
        )
        return

    index %= len(recipes)
    recipe = recipes[index]
    local_date = datetime.now(
        ZoneInfo(profile["timezone"])
    ).date().isoformat()

    # Ensures the choice exists even when an old message button is pressed.
    await db.save_recipe_choice(
        user_id=callback.from_user.id,
        session_id=session_id,
        recipe_index=index,
        recipe=recipe,
        local_date=local_date,
    )
    result = await db.mark_recipe_choice_eaten(
        user_id=callback.from_user.id,
        session_id=session_id,
        recipe_index=index,
        local_date=local_date,
    )

    if not result:
        await callback.answer(
            "Не удалось записать блюдо.",
            show_alert=True,
        )
        return

    if result.get("already_eaten"):
        await callback.answer(
            "Эта порция уже записана"
            if language == "ru"
            else "Цю порцію вже записано",
            show_alert=True,
        )
        return

    day = await db.daily_food_totals(
        callback.from_user.id,
        local_date,
    )
    eaten_calories = nutrition_midpoint(day, "calories")
    calorie_target = float(profile.get("calorie_target") or 0)
    remaining = calorie_target - eaten_calories

    if language == "uk":
        if remaining >= 0:
            balance_text = (
                f"До денного орієнтиру залишилося близько "
                f"{remaining:.0f} кілокалорій."
            )
        else:
            balance_text = (
                f"Сьогодні вийшло приблизно на {abs(remaining):.0f} "
                "кілокалорій вище орієнтиру. Компенсувати це "
                "голодуванням не потрібно."
            )
        text = (
            f"✅ {recipe.get('title')} записано як з'їдене.\n\n"
            f"Додано близько {recipe.get('calories', 0)} кілокалорій, "
            f"білок {recipe.get('protein', 0)} г, "
            f"жири {recipe.get('fat', 0)} г і "
            f"вуглеводи {recipe.get('carbs', 0)} г.\n\n"
            f"{balance_text}"
        )
    else:
        if remaining >= 0:
            balance_text = (
                f"До дневного ориентира осталось около "
                f"{remaining:.0f} килокалорий."
            )
        else:
            balance_text = (
                f"Сегодня получилось примерно на {abs(remaining):.0f} "
                "килокалорий выше ориентира. Компенсировать это "
                "голоданием не нужно."
            )
        text = (
            f"✅ {recipe.get('title')} записано как съеденное.\n\n"
            f"Добавлено около {recipe.get('calories', 0)} килокалорий, "
            f"белок {recipe.get('protein', 0)} г, "
            f"жиры {recipe.get('fat', 0)} г и "
            f"углеводы {recipe.get('carbs', 0)} г.\n\n"
            f"{balance_text}"
        )

    await callback.message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button(
                        "🥗 Що краще з'їсти далі"
                        if language == "uk"
                        else "🥗 Что лучше съесть дальше",
                        "menu:foods",
                        ButtonStyle.PRIMARY,
                    )
                ],
                [
                    button(
                        "🧾 Моя інформація"
                        if language == "uk"
                        else "🧾 Моя информация",
                        "menu:profile",
                        ButtonStyle.SUCCESS,
                    ),
                    button(
                        "📅 Календар"
                        if language == "uk"
                        else "📅 Календарь",
                        "menu:calendar",
                        ButtonStyle.PRIMARY,
                    ),
                ],
            ]
        ),
    )
    await callback.answer(
        "Записано"
        if language == "ru"
        else "Записано"
    )


@router.callback_query(F.data == "food_gallery_noop")
async def food_gallery_noop_handler(
    callback: CallbackQuery,
) -> None:
    await callback.answer(
        "Перелистывайте карточки стрелками"
    )


COACH_FOCUS_LABELS = {
    "menu": {"ru": "Меню и расчёты", "uk": "Меню та розрахунки"},
    "control": {"ru": "Контроль и отчёты", "uk": "Контроль і звіти"},
    "training": {"ru": "Питание и тренировки", "uk": "Харчування і тренування"},
    "all": {"ru": "Полное сопровождение", "uk": "Повний супровід"},
}

COACH_FORMAT_LABELS = {
    "daily": {"ru": "Связь каждый день", "uk": "Зв'язок щодня"},
    "three_week": {"ru": "Три раза в неделю", "uk": "Тричі на тиждень"},
    "request": {"ru": "По необходимости", "uk": "За потреби"},
}

COACH_SPORT_LABELS = {
    "none": {"ru": "Без тренировок", "uk": "Без тренувань"},
    "walking": {"ru": "Ходьба", "uk": "Ходьба"},
    "home": {"ru": "Домашние тренировки", "uk": "Домашні тренування"},
    "gym": {"ru": "Спортзал", "uk": "Спортзал"},
}

COACH_TIME_LABELS = {
    "morning": {"ru": "Утром", "uk": "Вранці"},
    "day": {"ru": "Днём", "uk": "Вдень"},
    "evening": {"ru": "Вечером", "uk": "Увечері"},
    "any": {"ru": "В любое время", "uk": "У будь-який час"},
}

COACH_STATUS_LABELS = {
    "new": {"ru": "заявка ожидает рассмотрения", "uk": "заявка очікує розгляду"},
    "in_progress": {"ru": "менеджер взял заявку в работу", "uk": "менеджер взяв заявку в роботу"},
    "closed": {"ru": "заявка обработана", "uk": "заявку опрацьовано"},
    "cancelled": {"ru": "заявка отменена", "uk": "заявку скасовано"},
}


def coach_label(mapping: dict, key: str, language: str) -> str:
    item = mapping.get(key) or {}
    return item.get(language) or item.get("ru") or key


def coach_cancel_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "❌ Скасувати" if language == "uk" else "❌ Отменить",
                    "coach_apply:cancel",
                    ButtonStyle.DANGER,
                )
            ]
        ]
    )


async def show_coach_confirmation(
    message: Message,
    state: FSMContext,
    language: str,
) -> None:
    data = await state.get_data()
    if language == "uk":
        text = (
            "📋 Перевірте заявку:\n\n"
            f"Допомога: {coach_label(COACH_FOCUS_LABELS, data['coach_focus'], language)}\n"
            f"Формат: {coach_label(COACH_FORMAT_LABELS, data['coach_format'], language)}\n"
            f"Активність: {coach_label(COACH_SPORT_LABELS, data['coach_sport'], language)}\n"
            "🚫 Не вживає зовсім / алергії / непереносимість:\n"
            f"{data['coach_food_notes']}\n\n"
            "⚠️ Іноді вживає, але хоче скоротити:\n"
            f"{data['coach_exclusions']}\n"
            f"Зручний час: "
            f"{coach_label(COACH_TIME_LABELS, data['coach_contact_time'], language)}\n"
            f"Що заважає знижувати вагу: "
            f"{data.get('coach_comment') or 'не вказано'}\n\n"
            "Натискаючи «Надіслати», ви погоджуєтеся, щоб менеджер "
            "зв'язався з вами в Telegram щодо цієї послуги."
        )
        send_text = "✅ Надіслати заявку"
        edit_text = "↩️ Заповнити заново"
    else:
        text = (
            "📋 Проверьте заявку:\n\n"
            f"Помощь: {coach_label(COACH_FOCUS_LABELS, data['coach_focus'], language)}\n"
            f"Формат: {coach_label(COACH_FORMAT_LABELS, data['coach_format'], language)}\n"
            f"Активность: {coach_label(COACH_SPORT_LABELS, data['coach_sport'], language)}\n"
            "🚫 Не употребляет совсем / аллергии / непереносимость:\n"
            f"{data['coach_food_notes']}\n\n"
            "⚠️ Иногда употребляет, но хочет сократить:\n"
            f"{data['coach_exclusions']}\n"
            f"Удобное время: "
            f"{coach_label(COACH_TIME_LABELS, data['coach_contact_time'], language)}\n"
            f"Что мешает снижать вес: "
            f"{data.get('coach_comment') or 'не указано'}\n\n"
            "Нажимая «Отправить», вы соглашаетесь, чтобы менеджер "
            "связался с вами в Telegram по поводу этой услуги."
        )
        send_text = "✅ Отправить заявку"
        edit_text = "↩️ Заполнить заново"

    await state.set_state(CoachApplication.confirm)
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button(
                        send_text,
                        "coach_apply:confirm",
                        ButtonStyle.SUCCESS,
                    )
                ],
                [
                    button(
                        edit_text,
                        "coach_apply:restart",
                        ButtonStyle.PRIMARY,
                    ),
                    button(
                        "❌ Скасувати" if language == "uk" else "❌ Отменить",
                        "coach_apply:cancel",
                        ButtonStyle.DANGER,
                    ),
                ],
            ]
        ),
    )


@router.callback_query(F.data == "menu:coach")
async def coach_menu(callback: CallbackQuery) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    support = settings.support_username or "@support"
    latest = await db.latest_coach_application(callback.from_user.id)

    if latest and latest["status"] in {"new", "in_progress"}:
        status = coach_label(
            COACH_STATUS_LABELS,
            latest["status"],
            language,
        )
        application_number = int(
            latest.get("public_number")
            or latest["id"]
        )
        text = (
            f"👩‍💼 Персональний супровід — 3499 грн/місяць.\n\n"
            f"Ваша заявка №{application_number}: {status}.\n"
            f"Підтримка: {support}\n\n"
            "Якщо ви передумали, заявку можна скасувати. "
            "Бот обов'язково запитає причину."
            if language == "uk"
            else
            f"👩‍💼 Персональное сопровождение — 3499 грн/месяц.\n\n"
            f"Ваша заявка №{application_number}: {status}.\n"
            f"Поддержка: {support}\n\n"
            "Если вы передумали, заявку можно отменить. "
            "Бот обязательно попросит указать причину."
        )
        await callback.message.answer(
            text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        button(
                            "❌ Скасувати мою заявку"
                            if language == "uk"
                            else "❌ Отменить мою заявку",
                            f"coach_client_cancel:{latest['id']}",
                            ButtonStyle.DANGER,
                        )
                    ]
                ]
            ),
        )
        await callback.answer()
        return

    if language == "uk":
        text = (
            "👩‍💼 Персональне схуднення — 3499 грн/місяць.\n\n"
            "У супровід можуть входити:\n"
            "• персональне меню й розрахунки;\n"
            "• перевірка харчування та звіти;\n"
            "• підтримка й коригування плану;\n"
            "• тренування за бажанням.\n\n"
            "Це не медична послуга. За наявності захворювань план "
            "потрібно погоджувати з лікарем."
        )
        apply_text = "📝 Залишити заявку"
    else:
        text = (
            "👩‍💼 Персональное похудение — 3499 грн/месяц.\n\n"
            "В сопровождение могут входить:\n"
            "• персональное меню и расчёты;\n"
            "• проверка питания и отчёты;\n"
            "• поддержка и корректировка плана;\n"
            "• тренировки по желанию.\n\n"
            "Это не медицинская услуга. При наличии заболеваний план "
            "нужно согласовывать с врачом."
        )
        apply_text = "📝 Оставить заявку"

    await callback.message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button(
                        apply_text,
                        "coach_apply:start",
                        ButtonStyle.SUCCESS,
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@router.callback_query(
    F.data.startswith("coach_client_cancel:")
)
async def coach_client_cancel_start(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"

    try:
        application_id = int(
            callback.data.split(":", 1)[1]
        )
    except ValueError:
        await callback.answer(
            "Ошибка заявки.",
            show_alert=True,
        )
        return

    application = await db.get_coach_application(
        application_id
    )
    if (
        not application
        or int(application["user_id"])
        != callback.from_user.id
        or application["status"]
        not in {"new", "in_progress"}
    ):
        await callback.answer(
            "Эту заявку уже нельзя отменить.",
            show_alert=True,
        )
        return

    application_number = int(
        application.get("public_number")
        or application_id
    )

    await state.set_state(
        CoachClientCancellation.reason
    )
    await state.update_data(
        client_cancel_application_id=application_id,
    )

    await callback.message.answer(
        (
            f"❌ Скасування заявки №{application_number}\\n\\n"
            "Напишіть, чому ви передумали. "
            "Наприклад: змінилися плани, не підходить ціна, "
            "поки не готові починати або інша причина.\\n\\n"
            "Заявка скасується після відправлення відповіді."
            if language == "uk"
            else
            f"❌ Отмена заявки №{application_number}\\n\\n"
            "Напишите, почему вы передумали. "
            "Например: изменились планы, не подходит цена, "
            "пока не готовы начинать или другая причина.\\n\\n"
            "Заявка отменится после отправки ответа."
        )
    )
    await callback.answer()


@router.message(
    CoachClientCancellation.reason,
    F.text,
)
async def coach_client_cancel_reason_handler(
    message: Message,
    state: FSMContext,
    bot: Bot,
) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile["language"] if profile else "ru"
    reason = (message.text or "").strip()

    if not 3 <= len(reason) <= 1000:
        await message.answer(
            "Напишите причину длиной от 3 до 1000 символов."
            if language == "ru"
            else
            "Напишіть причину довжиною від 3 до 1000 символів."
        )
        return

    data = await state.get_data()
    application_id = int(
        data.get("client_cancel_application_id") or 0
    )
    application = await db.get_coach_application(
        application_id
    )

    if (
        not application
        or int(application["user_id"])
        != message.from_user.id
    ):
        await state.clear()
        await message.answer(
            "Заявка больше не найдена."
            if language == "ru"
            else "Заявку більше не знайдено."
        )
        return

    application_number = int(
        application.get("public_number")
        or application_id
    )

    cancelled = await db.cancel_coach_application(
        application_id,
        cancelled_by="client",
        reason=reason,
    )
    await state.clear()

    if not cancelled:
        await message.answer(
            "Заявка уже завершена или отменена."
            if language == "ru"
            else "Заявку вже завершено або скасовано."
        )
        return

    await message.answer(
        (
            f"❌ Заявку №{application_number} скасовано за вашим запитом.\\n\\n"
            f"Причина: {reason}\\n\\n"
            "Власник отримав повідомлення. "
            "Нову заявку можна створити в будь-який момент."
            if language == "uk"
            else
            f"❌ Заявка №{application_number} отменена по вашему запросу.\\n\\n"
            f"Причина: {reason}\\n\\n"
            "Владелец получил уведомление. "
            "Новую заявку можно создать в любой момент."
        )
    )

    display_name = (
        profile.get("display_name")
        or message.from_user.first_name
    )
    contact = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else f"Telegram ID: {message.from_user.id}"
    )
    manager_text = (
        f"❌ КЛИЕНТ ОТМЕНИЛ ЗАЯВКУ №{application_number}\\n\\n"
        f"Пользователь: {display_name}\\n"
        f"Контакт: {contact}\\n"
        f"Причина: {reason}"
    )

    try:
        await bot.send_message(
            settings.admin_id,
            manager_text,
        )
    except Exception:
        logging.exception(
            "Failed to notify owner about client cancellation"
        )

    await send_application_archive_copy(
        bot,
        manager_text,
    )


@router.callback_query(F.data == "coach_apply:start")
@router.callback_query(F.data == "coach_apply:restart")
async def coach_application_start(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    await state.clear()
    await state.set_state(CoachApplication.focus)

    options = (
        [
            ("🍽 Меню та розрахунки", "menu"),
            ("📊 Контроль і звіти", "control"),
            ("🏃 Харчування і тренування", "training"),
            ("⭐ Повний супровід", "all"),
        ]
        if language == "uk"
        else
        [
            ("🍽 Меню и расчёты", "menu"),
            ("📊 Контроль и отчёты", "control"),
            ("🏃 Питание и тренировки", "training"),
            ("⭐ Полное сопровождение", "all"),
        ]
    )
    await callback.message.answer(
        "Що для вас найважливіше?"
        if language == "uk"
        else "Что для вас важнее всего?",
        reply_markup=options_keyboard(options, "coach_focus"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("coach_focus:"))
async def coach_focus_handler(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    await state.update_data(
        coach_focus=callback.data.split(":", 1)[1]
    )
    await state.set_state(CoachApplication.support_format)
    options = (
        [
            ("Щодня", "daily"),
            ("Тричі на тиждень", "three_week"),
            ("За потреби", "request"),
        ]
        if language == "uk"
        else
        [
            ("Каждый день", "daily"),
            ("Три раза в неделю", "three_week"),
            ("По необходимости", "request"),
        ]
    )
    await callback.message.answer(
        "Як часто вам потрібен зв'язок із менеджером?"
        if language == "uk"
        else "Как часто вам нужна связь с менеджером?",
        reply_markup=options_keyboard(options, "coach_format"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("coach_format:"))
async def coach_format_handler(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    await state.update_data(
        coach_format=callback.data.split(":", 1)[1]
    )
    await state.set_state(CoachApplication.sport)
    options = (
        [
            ("Без тренувань", "none"),
            ("Ходьба", "walking"),
            ("Домашні тренування", "home"),
            ("Спортзал", "gym"),
        ]
        if language == "uk"
        else
        [
            ("Без тренировок", "none"),
            ("Ходьба", "walking"),
            ("Домашние тренировки", "home"),
            ("Спортзал", "gym"),
        ]
    )
    await callback.message.answer(
        "Який формат активності вам підходить?"
        if language == "uk"
        else "Какой формат активности вам подходит?",
        reply_markup=options_keyboard(options, "coach_sport"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("coach_sport:"))
async def coach_sport_handler(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    await state.update_data(
        coach_sport=callback.data.split(":", 1)[1]
    )
    await state.set_state(CoachApplication.food_notes)
    await callback.message.answer(
        (
            "🚫 Що ви НЕ ВЖИВАЄТЕ ЗОВСІМ?\n\n"
            "Сюди належать:\n"
            "• алергії та непереносимість;\n"
            "• продукти, які ви принципово не їсте;\n"
            "• продукти, які не можна пропонувати в меню ніколи.\n\n"
            "Приклад: «алергія на горіхи, не їм рибу» або «немає».\n\n"
            "Не пишіть тут пиво, солодке чи фастфуд, якщо іноді їх "
            "вживаєте, але просто хочете скоротити — це буде наступне питання."
        )
        if language == "uk"
        else
        (
            "🚫 Что вы НЕ УПОТРЕБЛЯЕТЕ СОВСЕМ?\n\n"
            "Сюда относятся:\n"
            "• аллергии и непереносимость;\n"
            "• продукты, которые вы принципиально не едите;\n"
            "• продукты, которые нельзя предлагать в меню никогда.\n\n"
            "Пример: «аллергия на орехи, не ем рыбу» или «нет».\n\n"
            "Не пишите здесь пиво, сладкое или фастфуд, если иногда "
            "их употребляете, но просто хотите сократить — это будет "
            "следующий вопрос."
        ),
        reply_markup=coach_cancel_keyboard(language),
    )
    await callback.answer()


@router.message(CoachApplication.food_notes, F.text)
async def coach_food_notes_handler(
    message: Message,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile["language"] if profile else "ru"
    text = (message.text or "").strip()
    if not 2 <= len(text) <= 1000:
        await message.answer(
            "Напишите ответ длиной до 1000 символов."
            if language == "ru"
            else "Напишіть відповідь довжиною до 1000 символів."
        )
        return

    await state.update_data(coach_food_notes=text)
    await state.set_state(CoachApplication.exclusions)

    await message.answer(
        (
            "⚠️ Що ви ІНОДІ ВЖИВАЄТЕ, але хочете скоротити?\n\n"
            "Сюди можна написати:\n"
            "• пиво або інший алкоголь;\n"
            "• солодкі напої та десерти;\n"
            "• фастфуд;\n"
            "• нічні перекуси;\n"
            "• великі порції певних продуктів.\n\n"
            "Приклад: «пиво у вихідні, солодке ввечері». "
            "Якщо нічого скорочувати не потрібно — напишіть «немає».\n\n"
            "Не повторюйте тут алергії та продукти, які не їсте зовсім."
        )
        if language == "uk"
        else
        (
            "⚠️ Что вы ИНОГДА УПОТРЕБЛЯЕТЕ, но хотите сократить?\n\n"
            "Сюда можно написать:\n"
            "• пиво или другой алкоголь;\n"
            "• сладкие напитки и десерты;\n"
            "• фастфуд;\n"
            "• ночные перекусы;\n"
            "• большие порции отдельных продуктов.\n\n"
            "Пример: «пиво по выходным, сладкое вечером». "
            "Если ничего сокращать не нужно — напишите «нет».\n\n"
            "Не повторяйте здесь аллергии и продукты, которые "
            "не употребляете совсем."
        ),
        reply_markup=coach_cancel_keyboard(language),
    )


@router.message(CoachApplication.exclusions, F.text)
async def coach_exclusions_handler(
    message: Message,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile["language"] if profile else "ru"
    text = (message.text or "").strip()

    if not 2 <= len(text) <= 1000:
        await message.answer(
            "Напишите ответ длиной до 1000 символов."
            if language == "ru"
            else "Напишіть відповідь довжиною до 1000 символів."
        )
        return

    await state.update_data(coach_exclusions=text)
    await state.set_state(CoachApplication.contact_time)

    options = (
        [
            ("Вранці", "morning"),
            ("Вдень", "day"),
            ("Увечері", "evening"),
            ("Будь-коли", "any"),
        ]
        if language == "uk"
        else
        [
            ("Утром", "morning"),
            ("Днём", "day"),
            ("Вечером", "evening"),
            ("В любое время", "any"),
        ]
    )
    await message.answer(
        "Коли зручно отримати повідомлення від менеджера?"
        if language == "uk"
        else "Когда удобно получить сообщение от менеджера?",
        reply_markup=options_keyboard(options, "coach_time"),
    )


@router.callback_query(F.data.startswith("coach_time:"))
async def coach_time_handler(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    await state.update_data(
        coach_contact_time=callback.data.split(":", 1)[1]
    )
    await state.set_state(CoachApplication.comment)
    skip = "Пропустити" if language == "uk" else "Пропустить"
    await callback.message.answer(
        (
            "🧩 Що зараз найбільше заважає вам знижувати вагу?\n\n"
            "Наприклад: стрес, вечірні переїдання, брак часу на готування, "
            "важко дотримуватися режиму, постійно хочеться солодкого або "
            "немає підтримки.\n\n"
            "Напишіть своїми словами. Менеджер побачить відповідь "
            "під назвою «Що заважає знижувати вагу». "
            "Якщо нічого не хочете додавати — натисніть «Пропустити»."
        )
        if language == "uk"
        else
        (
            "🧩 Что сейчас больше всего мешает вам снижать вес?\n\n"
            "Например: стресс, вечерние переедания, нет времени готовить, "
            "сложно соблюдать режим, постоянно хочется сладкого или "
            "не хватает поддержки.\n\n"
            "Напишите своими словами. Менеджер увидит ответ "
            "под строкой «Что мешает снижать вес». "
            "Если ничего не хотите добавлять — нажмите «Пропустить»."
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button(
                        f"➡️ {skip}",
                        "coach_comment:skip",
                        ButtonStyle.PRIMARY,
                    )
                ],
                [
                    button(
                        "❌ Скасувати" if language == "uk" else "❌ Отменить",
                        "coach_apply:cancel",
                        ButtonStyle.DANGER,
                    )
                ],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "coach_comment:skip")
async def coach_comment_skip(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    await state.update_data(coach_comment="")
    await show_coach_confirmation(
        callback.message,
        state,
        language,
    )
    await callback.answer()


@router.message(CoachApplication.comment, F.text)
async def coach_comment_handler(
    message: Message,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile["language"] if profile else "ru"
    text = (message.text or "").strip()
    if len(text) > 1500:
        await message.answer(
            "Сократите ответ до 1500 символов."
            if language == "ru"
            else "Скоротіть відповідь до 1500 символів."
        )
        return
    await state.update_data(coach_comment=text)
    await show_coach_confirmation(
        message,
        state,
        language,
    )


@router.callback_query(F.data == "coach_apply:cancel")
async def coach_application_cancel(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    await state.clear()
    await callback.message.answer(
        "Заявку скасовано."
        if language == "uk"
        else "Заявка отменена."
    )
    await callback.answer()


@router.callback_query(F.data == "coach_apply:confirm")
async def coach_application_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    data = await state.get_data()

    required = {
        "coach_focus",
        "coach_format",
        "coach_sport",
        "coach_food_notes",
        "coach_exclusions",
        "coach_contact_time",
    }
    if not required.issubset(data):
        await callback.answer(
            "Заполните заявку заново.",
            show_alert=True,
        )
        return

    latest = await db.latest_coach_application(callback.from_user.id)
    if latest and latest["status"] in {"new", "in_progress"}:
        await state.clear()
        await callback.answer(
            "Заявка уже отправлена.",
            show_alert=True,
        )
        return

    created_application = await db.create_coach_application(
        user_id=callback.from_user.id,
        focus=data["coach_focus"],
        support_format=data["coach_format"],
        sport_preference=data["coach_sport"],
        food_notes=data["coach_food_notes"],
        exclusions=data["coach_exclusions"],
        contact_time=data["coach_contact_time"],
        comment=data.get("coach_comment") or "",
    )
    application_id = int(created_application["id"])
    application_number = int(
        created_application["public_number"]
    )
    await state.clear()

    username = callback.from_user.username
    user_line = (
        f"@{username}"
        if username
        else f"Telegram ID: {callback.from_user.id}"
    )
    current = float(profile.get("current_weight_kg") or 0)
    target = float(profile.get("target_weight_kg") or 0)
    remaining = max(0.0, current - target)
    safety = (
        "⚠️ Пользователь отметил медицинские/особые обстоятельства."
        if profile.get("safety_restricted")
        else "Ограничения в анкете не отмечены."
    )
    fasting = profile.get("fasting_mode") or "выключено"

    admin_text = (
        f"🆕 ЗАЯВКА НА ПЕРСОНАЛЬНОЕ СОПРОВОЖДЕНИЕ "
        f"№{application_number}\n\n"
        f"Пользователь: {profile.get('display_name') or callback.from_user.first_name}\n"
        f"Контакт: {user_line}\n"
        f"Вес: {current:.1f} → {target:.1f} кг\n"
        f"Осталось: {remaining:.1f} кг\n"
        f"Ориентир: {profile.get('calorie_target')} ккал, "
        f"Белки / жиры / углеводы {profile.get('protein_g')}/"
        f"{profile.get('fat_g')}/{profile.get('carbs_g')}\n"
        f"Интервальный режим: {fasting}\n"
        f"{safety}\n\n"
        f"Нужна помощь: "
        f"{coach_label(COACH_FOCUS_LABELS, data['coach_focus'], 'ru')}\n"
        f"Формат связи: "
        f"{coach_label(COACH_FORMAT_LABELS, data['coach_format'], 'ru')}\n"
        f"Активность: "
        f"{coach_label(COACH_SPORT_LABELS, data['coach_sport'], 'ru')}\n"
        f"Удобное время: "
        f"{coach_label(COACH_TIME_LABELS, data['coach_contact_time'], 'ru')}\n"
        "🚫 НЕ УПОТРЕБЛЯЕТ СОВСЕМ / АЛЛЕРГИИ:\n"
        f"{data['coach_food_notes']}\n\n"
        "⚠️ ИНОГДА УПОТРЕБЛЯЕТ, НО ХОЧЕТ СОКРАТИТЬ:\n"
        f"{data['coach_exclusions']}\n\n"
        "🧩 ЧТО МЕШАЕТ СНИЖАТЬ ВЕС:\n"
        f"{data.get('coach_comment') or 'не указано'}"
    )

    admin_rows = [
        [
            button(
                "✅ Взять в работу",
                f"coach_admin:accept:{application_id}",
                ButtonStyle.SUCCESS,
            )
        ],
        [
            button(
                "🏁 Завершить",
                f"coach_admin:close:{application_id}",
                ButtonStyle.SUCCESS,
            ),
            button(
                "❌ Отменить заявку",
                f"coach_admin:cancel:{application_id}",
                ButtonStyle.DANGER,
            ),
        ],
    ]
    if username:
        admin_rows.append(
            [
                InlineKeyboardButton(
                    text="💬 Открыть профиль",
                    url=f"https://t.me/{username}",
                    style=ButtonStyle.PRIMARY,
                )
            ]
        )

    try:
        await bot.send_message(
            settings.admin_id,
            admin_text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=admin_rows
            ),
        )
    except Exception:
        logging.exception(
            "Failed to send coach application %s to admin",
            application_id,
        )

    await send_application_archive_copy(
        bot,
        "📁 КОПИЯ ЗАЯВКИ ДЛЯ ЗАКРЫТОГО АРХИВА\n\n"
        + admin_text
        + "\n\nСтатус: ожидает решения владельца."
    )

    user_text = (
        f"✅ Заявку №{application_number} надіслано менеджеру.\n"
        "Вам напишуть у Telegram у вибраний час."
        if language == "uk"
        else
        f"✅ Заявка №{application_number} отправлена менеджеру.\n"
        "Вам напишут в Telegram в выбранное время."
    )
    await callback.message.answer(user_text)
    await callback.answer()


@router.callback_query(F.data.startswith("coach_admin:"))
async def coach_admin_action(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
) -> None:
    if callback.from_user.id != settings.admin_id:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Ошибка заявки.", show_alert=True)
        return

    action = parts[1]
    try:
        application_id = int(parts[2])
    except ValueError:
        await callback.answer("Ошибка заявки.", show_alert=True)
        return

    application = await db.get_coach_application(application_id)
    if not application:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    application_number = int(
        application.get("public_number")
        or application_id
    )

    if action == "accept":
        new_status = "in_progress"
        admin_note = "✅ Заявка взята в работу"
        user_ru = (
            f"✅ Менеджер взял вашу заявку №{application_number} в работу. "
            "С вами свяжутся в Telegram."
        )
        user_uk = (
            f"✅ Менеджер взяв вашу заявку №{application_number} у роботу. "
            "З вами зв'яжуться в Telegram."
        )
    elif action == "close":
        new_status = "closed"
        admin_note = (
            f"🏁 Заявка №{application_number} завершена"
        )
        user_ru = (
            f"🏁 Работа по заявке №{application_number} завершена.\n\n"
            "Спасибо за обращение. При необходимости вы сможете "
            "создать новую заявку на персональное сопровождение."
        )
        user_uk = (
            f"🏁 Роботу за заявкою №{application_number} завершено.\n\n"
            "Дякуємо за звернення. За потреби ви зможете "
            "створити нову заявку на персональний супровід."
        )
    elif action == "cancel":
        await state.set_state(
            CoachManagerCancellation.reason
        )
        await state.update_data(
            manager_cancel_application_id=application_id,
        )
        await callback.message.answer(
            f"❌ Отмена заявки №{application_number}\n\n"
            "Напишите причину отказа одним сообщением.\n"
            "Например: нет свободных мест, не подходит запрос, "
            "не удалось связаться или другая причина.\n\n"
            "Заявка отменится только после отправки причины."
        )
        await callback.answer(
            "Теперь напишите причину отмены.",
            show_alert=True,
        )
        return
    else:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    await db.update_coach_application_status(
        application_id,
        new_status,
    )

    await send_application_archive_copy(
        bot,
        f"📌 ОБНОВЛЕНИЕ ЗАЯВКИ №{application_number}\n\n"
        f"{admin_note}"
    )
    user_profile = await db.get_profile(application["user_id"])
    user_language = (
        user_profile.get("language")
        if user_profile
        else "ru"
    )

    try:
        await bot.send_message(
            application["user_id"],
            user_uk if user_language == "uk" else user_ru,
        )
    except Exception:
        logging.exception(
            "Failed to notify user about coach application %s",
            application_id,
        )

    try:
        original = callback.message.text or ""
        if admin_note not in original:
            await callback.message.edit_text(
                original + f"\n\n{admin_note}",
                reply_markup=None,
            )
    except Exception:
        logging.exception(
            "Failed to edit admin coach application message"
        )

    await callback.answer(admin_note)



@router.message(
    CoachManagerCancellation.reason,
    F.text,
)
async def coach_manager_cancel_reason_handler(
    message: Message,
    state: FSMContext,
    bot: Bot,
) -> None:
    if message.from_user.id != settings.admin_id:
        await state.clear()
        return

    reason = (message.text or "").strip()
    if not 3 <= len(reason) <= 1000:
        await message.answer(
            "Причина должна содержать от 3 до 1000 символов."
        )
        return

    data = await state.get_data()
    application_id = int(
        data.get("manager_cancel_application_id") or 0
    )
    application = await db.get_coach_application(
        application_id
    )

    if not application:
        await state.clear()
        await message.answer("Заявка больше не найдена.")
        return

    application_number = int(
        application.get("public_number")
        or application_id
    )

    cancelled = await db.cancel_coach_application(
        application_id,
        cancelled_by="manager",
        reason=reason,
    )
    await state.clear()

    if not cancelled:
        await message.answer(
            "Заявка уже завершена или отменена."
        )
        return

    user_profile = await db.get_profile(
        application["user_id"]
    )
    user_language = (
        user_profile.get("language")
        if user_profile
        else "ru"
    )

    user_text = (
        f"❌ Заявку №{application_number} скасовано менеджером.\\n\\n"
        f"Причина: {reason}\\n\\n"
        "Вона більше не перебуває на розгляді. "
        "За потреби можна створити нову заявку."
        if user_language == "uk"
        else
        f"❌ Заявка №{application_number} отменена менеджером.\\n\\n"
        f"Причина: {reason}\\n\\n"
        "Она больше не находится на рассмотрении. "
        "При необходимости можно создать новую заявку."
    )

    try:
        await bot.send_message(
            application["user_id"],
            user_text,
        )
    except Exception:
        logging.exception(
            "Failed to notify client about manager cancellation"
        )

    archive_text = (
        f"❌ ЗАЯВКА №{application_number} ОТМЕНЕНА ВЛАДЕЛЬЦЕМ\\n\\n"
        f"Причина: {reason}"
    )
    await send_application_archive_copy(
        bot,
        archive_text,
    )

    await message.answer(
        f"✅ Заявка №{application_number} отменена. "
        "Причина сохранена и отправлена клиенту."
    )


def settings_keyboard(language: str) -> InlineKeyboardMarkup:
    if language == "uk":
        rows = [
            [button("🌐 Змінити мову", "settings:language")],
            [button("🎯 Змінити цільову вагу", "settings:target")],
            [button("📍 Змінити часовий пояс", "settings:timezone")],
            [button("🧮 Оновити денні орієнтири", "settings:recalculate", ButtonStyle.SUCCESS)],
            [button("🗑 Видалити мої дані", "settings:delete", ButtonStyle.DANGER)],
        ]
    else:
        rows = [
            [button("🌐 Изменить язык", "settings:language")],
            [button("🎯 Изменить целевой вес", "settings:target")],
            [button("📍 Изменить часовой пояс", "settings:timezone")],
            [button("🧮 Обновить дневные ориентиры", "settings:recalculate", ButtonStyle.SUCCESS)],
            [button("🗑 Удалить мои данные", "settings:delete", ButtonStyle.DANGER)],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def meal_names_for_language(
    meals_count: int,
    language: str,
) -> list[str]:
    if meals_count == 2:
        return (
            ["Перший прийом їжі", "Вечеря"]
            if language == "uk"
            else ["Первый приём пищи", "Ужин"]
        )
    if meals_count == 3:
        return (
            ["Сніданок", "Обід", "Вечеря"]
            if language == "uk"
            else ["Завтрак", "Обед", "Ужин"]
        )
    return (
        ["Сніданок", "Обід", "Перекус", "Вечеря"]
        if language == "uk"
        else ["Завтрак", "Обед", "Перекус", "Ужин"]
    )


async def send_referral_panel(
    message: Message,
    user_id: int,
    bot: Bot,
) -> None:
    profile = await db.get_profile(user_id)
    language = profile.get("language") if profile else "ru"
    stats = await db.referral_stats(user_id)
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"

    share_text = (
        "Спробуй minus_kg — персональний помічник для харчування, "
        "калорій, ваги та рецептів."
        if language == "uk"
        else
        "Попробуй minus_kg — персональный помощник для питания, "
        "калорий, веса и рецептов."
    )
    share_url = (
        "https://t.me/share/url?url="
        + quote(link, safe="")
        + "&text="
        + quote(share_text, safe="")
    )

    if language == "uk":
        text = (
            "🤝 Партнерська програма minus_kg\n\n"
            "Ваше персональне посилання:\n"
            f"{link}\n\n"
            "Як нараховуються винагороди:\n"
            "• друг переходить за посиланням і вперше запускає бота;\n"
            "• після завершення анкети ви отримуєте +1 день доступу;\n"
            "• після кожної його оплати ви отримуєте 5% від фактично "
            "сплачених Stars внутрішніми бонусами;\n"
            "• 1 бонус = знижка 1 Star на вашу наступну підписку.\n\n"
            "Статистика:\n"
            f"• перейшли за посиланням: {stats['invited']}\n"
            f"• завершили анкету: {stats['qualified']}\n"
            f"• безкоштовних днів: {stats['free_days']}\n"
            f"• оплат від запрошених: {stats['paid_orders']}\n"
            f"• зароблено бонусів: {stats['earned_bonus']} ⭐\n"
            f"• доступно зараз: {stats['bonus_balance']} ⭐\n\n"
            "Бонуси діють лише всередині minus_kg, не є справжніми "
            "Telegram Stars і не виводяться."
        )
        share_button = "📤 Поділитися посиланням"
    else:
        text = (
            "🤝 Партнёрская программа minus_kg\n\n"
            "Ваша персональная ссылка:\n"
            f"{link}\n\n"
            "Как начисляются награды:\n"
            "• друг переходит по ссылке и впервые запускает бота;\n"
            "• после завершения анкеты вы получаете +1 день доступа;\n"
            "• после каждой его оплаты вы получаете 5% от фактически "
            "оплаченных Stars внутренними бонусами;\n"
            "• 1 бонус = скидка 1 Star на вашу следующую подписку.\n\n"
            "Статистика:\n"
            f"• перешли по ссылке: {stats['invited']}\n"
            f"• завершили анкету: {stats['qualified']}\n"
            f"• бесплатных дней: {stats['free_days']}\n"
            f"• оплат от приглашённых: {stats['paid_orders']}\n"
            f"• заработано бонусов: {stats['earned_bonus']} ⭐\n"
            f"• доступно сейчас: {stats['bonus_balance']} ⭐\n\n"
            "Бонусы работают только внутри minus_kg, не являются "
            "настоящими Telegram Stars и не выводятся."
        )
        share_button = "📤 Поделиться ссылкой"

    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=share_button,
                        url=share_url,
                        style=ButtonStyle.SUCCESS,
                    )
                ],
                [
                    button(
                        "⭐ Підписка"
                        if language == "uk"
                        else "⭐ Подписка",
                        "menu:subscription",
                        ButtonStyle.PRIMARY,
                    )
                ],
            ]
        ),
    )


@router.callback_query(F.data == "menu:referral")
async def referral_menu_handler(
    callback: CallbackQuery,
    bot: Bot,
) -> None:
    await send_referral_panel(
        callback.message,
        callback.from_user.id,
        bot,
    )
    await callback.answer()


@router.callback_query(F.data == "menu:settings")
async def settings_menu_handler(callback: CallbackQuery) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile.get("language") if profile else "ru"
    await callback.message.answer(
        (
            "⚙️ Налаштування\n\n"
            "Тут можна змінити мову, ціль, часовий пояс або заново "
            "розрахувати денні орієнтири калорій, білка, жирів і вуглеводів.\n\n"
            "Час прийомів їжі змінюється в розділі «Нагадування». "
            "Перед видаленням даних бот обов'язково попросить підтвердження."
            if language == "uk"
            else
            "⚙️ Настройки\n\n"
            "Здесь можно изменить язык, цель, часовой пояс или заново "
            "рассчитать дневные ориентиры калорий, белка, жиров и углеводов.\n\n"
            "Время приёмов пищи меняется в разделе «Напоминания». "
            "Перед удалением данных бот обязательно попросит подтверждение."
        ),
        reply_markup=settings_keyboard(language),
    )
    await callback.answer()


@router.callback_query(F.data == "settings:language")
async def settings_language_handler(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "Оберіть мову / Выберите язык:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button("🇺🇦 Українська", "settings_lang:uk"),
                    button("🇷🇺 Русский", "settings_lang:ru"),
                ]
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("settings_lang:"))
async def settings_language_save_handler(
    callback: CallbackQuery,
) -> None:
    language = callback.data.split(":", 1)[1]
    if language not in {"uk", "ru"}:
        await callback.answer("Ошибка языка.", show_alert=True)
        return

    profile = await db.get_profile(callback.from_user.id)
    meals_count = int(profile.get("meals_count") or 3)
    await db.save_profile(
        callback.from_user.id,
        {"language": language},
    )
    await db.update_meal_names(
        callback.from_user.id,
        meal_names_for_language(meals_count, language),
    )
    await callback.message.answer(
        "✅ Мову змінено."
        if language == "uk"
        else "✅ Язык изменён.",
        reply_markup=main_menu(language)
        if access_active(profile)
        else settings_keyboard(language),
    )
    await callback.answer()


def target_change_preview_text(
    profile: dict,
    new_target: float,
    targets: dict,
    language: str,
) -> str:
    current_weight = float(profile["current_weight_kg"])
    old_target = float(profile["target_weight_kg"])
    old_remaining = max(0.0, current_weight - old_target)
    new_remaining = max(0.0, current_weight - new_target)
    target_shift = new_target - old_target

    current_calories = int(profile.get("calorie_target") or 0)
    current_protein = int(profile.get("protein_g") or 0)
    current_fat = int(profile.get("fat_g") or 0)
    current_carbs = int(profile.get("carbs_g") or 0)

    changed_targets = any(
        (
            targets["calorie_target"] != current_calories,
            targets["protein_g"] != current_protein,
            targets["fat_g"] != current_fat,
            targets["carbs_g"] != current_carbs,
        )
    )

    if language == "uk":
        if target_shift > 0:
            direction = (
                f"Нова ціль на {target_shift:.1f} кг вища за попередню. "
                "Шлях стане коротшим і може бути психологічно комфортнішим."
            )
        else:
            direction = (
                f"Нова ціль на {abs(target_shift):.1f} кг нижча за попередню. "
                "Шлях стане довшим, тому важливо не прискорювати його "
                "надмірним обмеженням їжі."
            )

        if changed_targets:
            target_note = (
                "Денні орієнтири трохи зміняться, тому що нова ціль "
                "вплинула на перевірку безпеки розрахунку."
            )
        else:
            target_note = (
                "Денні орієнтири не зміняться. У цьому розрахунку вони "
                "залежать насамперед від поточної ваги, зросту, віку "
                "та активності. Нова ціль змінює довжину шляху, а не "
                "обов'язково кількість їжі на день."
            )

        return (
            "🎯 Перевірте нову ціль\n\n"
            f"• поточна вага — {current_weight:.1f} кг;\n"
            f"• попередня ціль — {old_target:.1f} кг;\n"
            f"• нова ціль — {new_target:.1f} кг;\n"
            f"• раніше залишалося близько {old_remaining:.1f} кг;\n"
            f"• тепер залишиться близько {new_remaining:.1f} кг.\n\n"
            f"{direction}\n\n"
            f"Орієнтовний шлях: {targets['weeks_min']}–"
            f"{targets['weeks_max']} тижнів. Це не дедлайн: "
            "вага змінюється нерівномірно.\n\n"
            "Після підтвердження:\n"
            f"• енергія — близько {targets['calorie_target']} "
            "кілокалорій;\n"
            f"• білок — близько {targets['protein_g']} г;\n"
            f"• жири — близько {targets['fat_g']} г;\n"
            f"• вуглеводи — близько {targets['carbs_g']} г.\n\n"
            f"{target_note}\n\n"
            "Ціль можна змінити пізніше. Підтвердити?"
        )

    if target_shift > 0:
        direction = (
            f"Новая цель на {target_shift:.1f} кг выше прежней. "
            "Путь станет короче и может быть психологически комфортнее."
        )
    else:
        direction = (
            f"Новая цель на {abs(target_shift):.1f} кг ниже прежней. "
            "Путь станет длиннее, поэтому важно не ускорять его "
            "чрезмерным ограничением еды."
        )

    if changed_targets:
        target_note = (
            "Дневные ориентиры немного изменятся, потому что новая цель "
            "повлияла на проверку безопасности расчёта."
        )
    else:
        target_note = (
            "Дневные ориентиры не изменятся. В этом расчёте они зависят "
            "прежде всего от текущего веса, роста, возраста и активности. "
            "Новая цель меняет длину пути, а не обязательно количество "
            "еды на день."
        )

    return (
        "🎯 Проверьте новую цель\n\n"
        f"• текущий вес — {current_weight:.1f} кг;\n"
        f"• прежняя цель — {old_target:.1f} кг;\n"
        f"• новая цель — {new_target:.1f} кг;\n"
        f"• раньше оставалось около {old_remaining:.1f} кг;\n"
        f"• теперь останется около {new_remaining:.1f} кг.\n\n"
        f"{direction}\n\n"
        f"Ориентировочный путь: {targets['weeks_min']}–"
        f"{targets['weeks_max']} недель. Это не дедлайн: "
        "вес меняется неравномерно.\n\n"
        "После подтверждения:\n"
        f"• энергия — около {targets['calorie_target']} "
        "килокалорий;\n"
        f"• белок — около {targets['protein_g']} г;\n"
        f"• жиры — около {targets['fat_g']} г;\n"
        f"• углеводы — около {targets['carbs_g']} г.\n\n"
        f"{target_note}\n\n"
        "Цель можно изменить позже. Подтвердить?"
    )


def target_change_keyboard(
    language: str,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "✅ Зберегти нову ціль"
                    if language == "uk"
                    else "✅ Сохранить новую цель",
                    "settings:target_apply",
                    ButtonStyle.SUCCESS,
                )
            ],
            [
                button(
                    "⬅️ Не змінювати"
                    if language == "uk"
                    else "⬅️ Не изменять",
                    "settings:target_cancel",
                    ButtonStyle.PRIMARY,
                )
            ],
        ]
    )


@router.callback_query(F.data == "settings:target")
async def settings_target_handler(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile.get("language") if profile else "ru"
    current_weight = float(profile["current_weight_kg"])
    target_weight = float(profile["target_weight_kg"])

    await state.clear()
    await state.set_state(AccountSettings.target_weight)

    if language == "uk":
        text = (
            "🎯 Зміна цільової ваги\n\n"
            f"Поточна вага: {current_weight:.1f} кг.\n"
            f"Поточна ціль: {target_weight:.1f} кг.\n"
            f"До неї залишилося близько "
            f"{max(0.0, current_weight - target_weight):.1f} кг.\n\n"
            "Напишіть нову ціль числом, наприклад: 71 або 70,5.\n\n"
            "Я спочатку покажу, як зміниться шлях і денні орієнтири, "
            "а збережу ціль лише після вашого підтвердження."
        )
    else:
        text = (
            "🎯 Изменение целевого веса\n\n"
            f"Текущий вес: {current_weight:.1f} кг.\n"
            f"Текущая цель: {target_weight:.1f} кг.\n"
            f"До неё осталось около "
            f"{max(0.0, current_weight - target_weight):.1f} кг.\n\n"
            "Напишите новую цель числом, например: 71 или 70,5.\n\n"
            "Я сначала покажу, как изменятся путь и дневные ориентиры, "
            "а сохраню цель только после вашего подтверждения."
        )

    await callback.message.answer(text)
    await callback.answer()


@router.message(AccountSettings.target_weight, F.text)
async def settings_target_preview_handler(
    message: Message,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile.get("language") if profile else "ru"
    value = parse_number(message.text or "", 35, 300)
    current = float(profile.get("current_weight_kg") or 0)
    old_target = float(profile.get("target_weight_kg") or 0)
    height = float(profile.get("height_cm") or 0)

    if value is None:
        await message.answer(
            "Введіть лише число, наприклад 70,5."
            if language == "uk"
            else "Введите только число, например 70,5."
        )
        return

    if value >= current:
        await message.answer(
            (
                f"Ціль має бути нижчою за поточну вагу "
                f"{current:.1f} кг. Введіть менше число."
                if language == "uk"
                else
                f"Цель должна быть ниже текущего веса "
                f"{current:.1f} кг. Введите меньшее число."
            )
        )
        return

    if abs(value - old_target) < 0.05:
        await state.clear()
        await message.answer(
            (
                f"Ціль уже встановлена на {old_target:.1f} кг — "
                "нічого змінювати не потрібно."
                if language == "uk"
                else
                f"Цель уже установлена на {old_target:.1f} кг — "
                "ничего менять не нужно."
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        button(
                            "⚙️ Налаштування"
                            if language == "uk"
                            else "⚙️ Настройки",
                            "menu:settings",
                            ButtonStyle.PRIMARY,
                        )
                    ]
                ]
            ),
        )
        return

    target_bmi = value / ((height / 100) ** 2)
    if target_bmi < 18.5:
        await message.answer(
            (
                "Ця ціль виглядає занадто низькою для автоматичного "
                "плану. Бот не встановлює її без консультації лікаря. "
                "Оберіть вищу ціль."
                if language == "uk"
                else
                "Эта цель выглядит слишком низкой для автоматического "
                "плана. Бот не устанавливает её без консультации врача. "
                "Выберите более высокую цель."
            )
        )
        return

    calculation_data = dict(profile)
    calculation_data["target_weight_kg"] = value
    targets = calculate_targets(calculation_data)

    await state.update_data(
        pending_target_weight=value,
    )
    await message.answer(
        target_change_preview_text(
            profile,
            value,
            targets,
            language,
        ),
        reply_markup=target_change_keyboard(language),
    )


@router.callback_query(F.data == "settings:target_apply")
async def settings_target_apply_handler(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile.get("language") if profile else "ru"
    data = await state.get_data()
    value = data.get("pending_target_weight")

    if value is None:
        await callback.answer(
            (
                "Попередній розрахунок уже неактивний. "
                "Введіть ціль ще раз."
                if language == "uk"
                else
                "Предыдущий расчёт уже неактивен. "
                "Введите цель ещё раз."
            ),
            show_alert=True,
        )
        return

    value = float(value)
    current = float(profile["current_weight_kg"])
    if value >= current:
        await state.clear()
        await callback.answer(
            "Цель больше не подходит к текущему весу.",
            show_alert=True,
        )
        return

    old_target = float(profile["target_weight_kg"])
    calculation_data = dict(profile)
    calculation_data["target_weight_kg"] = value
    targets = calculate_targets(calculation_data)

    await db.save_profile(
        callback.from_user.id,
        {
            "target_weight_kg": value,
            "bmr": targets["bmr"],
            "tdee": targets["tdee"],
            "calorie_target": targets["calorie_target"],
            "protein_g": targets["protein_g"],
            "fat_g": targets["fat_g"],
            "carbs_g": targets["carbs_g"],
            "safety_restricted": int(targets["restricted"]),
        },
    )
    await state.clear()

    remaining = max(0.0, current - value)
    shift = value - old_target

    if language == "uk":
        shift_text = (
            f"Ціль стала на {shift:.1f} кг вищою."
            if shift > 0
            else f"Ціль стала на {abs(shift):.1f} кг нижчою."
        )
        text = (
            "✅ Нову ціль збережено\n\n"
            f"• попередня ціль — {old_target:.1f} кг;\n"
            f"• нова ціль — {value:.1f} кг;\n"
            f"• до цілі залишилося близько {remaining:.1f} кг.\n\n"
            f"{shift_text}\n\n"
            "Денні орієнтири:\n"
            f"• енергія — близько {targets['calorie_target']} "
            "кілокалорій;\n"
            f"• білок — близько {targets['protein_g']} г;\n"
            f"• жири — близько {targets['fat_g']} г;\n"
            f"• вуглеводи — близько {targets['carbs_g']} г.\n\n"
            "Нова ціль уже використовується в профілі, календарі, "
            "рецептах і персональних рекомендаціях. "
            "Її можна змінити ще раз у будь-який момент."
        )
    else:
        shift_text = (
            f"Цель стала на {shift:.1f} кг выше."
            if shift > 0
            else f"Цель стала на {abs(shift):.1f} кг ниже."
        )
        text = (
            "✅ Новая цель сохранена\n\n"
            f"• прежняя цель — {old_target:.1f} кг;\n"
            f"• новая цель — {value:.1f} кг;\n"
            f"• до цели осталось около {remaining:.1f} кг.\n\n"
            f"{shift_text}\n\n"
            "Дневные ориентиры:\n"
            f"• энергия — около {targets['calorie_target']} "
            "килокалорий;\n"
            f"• белок — около {targets['protein_g']} г;\n"
            f"• жиры — около {targets['fat_g']} г;\n"
            f"• углеводы — около {targets['carbs_g']} г.\n\n"
            "Новая цель уже используется в профиле, календаре, "
            "рецептах и персональных рекомендациях. "
            "Её можно изменить ещё раз в любой момент."
        )

    await callback.message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button(
                        "🧾 Моя інформація"
                        if language == "uk"
                        else "🧾 Моя информация",
                        "menu:profile",
                        ButtonStyle.SUCCESS,
                    )
                ],
                [
                    button(
                        "📅 Відкрити календар"
                        if language == "uk"
                        else "📅 Открыть календарь",
                        "menu:calendar",
                        ButtonStyle.PRIMARY,
                    ),
                    button(
                        "⚙️ Налаштування"
                        if language == "uk"
                        else "⚙️ Настройки",
                        "menu:settings",
                        ButtonStyle.PRIMARY,
                    ),
                ],
            ]
        ),
    )
    await callback.answer(
        "Ціль оновлено"
        if language == "uk"
        else "Цель обновлена"
    )


@router.callback_query(F.data == "settings:target_cancel")
async def settings_target_cancel_handler(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile.get("language") if profile else "ru"
    await state.clear()

    await callback.message.answer(
        (
            "Ціль не змінено."
            if language == "uk"
            else "Цель не изменена."
        ),
        reply_markup=settings_keyboard(language),
    )
    await callback.answer()


@router.callback_query(F.data == "settings:timezone")
async def settings_timezone_handler(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile.get("language") if profile else "ru"
    await state.set_state(AccountSettings.timezone)
    await callback.message.answer(
        (
            "Надішліть геолокацію або оберіть час Києва. "
            "Координати не зберігаються."
            if language == "uk"
            else
            "Отправьте геолокацию или выберите время Киева. "
            "Координаты не сохраняются."
        ),
        reply_markup=timezone_keyboard(language),
    )
    await callback.answer()


@router.message(AccountSettings.timezone, F.location)
async def settings_timezone_location_handler(
    message: Message,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile.get("language") if profile else "ru"
    timezone = timezone_finder.timezone_at(
        lat=message.location.latitude,
        lng=message.location.longitude,
    ) or "Europe/Kyiv"
    await db.save_profile(
        message.from_user.id,
        {"timezone": timezone},
    )
    await state.clear()
    await message.answer(
        (
            f"✅ Часовий пояс: {timezone}"
            if language == "uk"
            else f"✅ Часовой пояс: {timezone}"
        ),
        reply_markup=persistent_menu_keyboard(language),
    )


@router.message(AccountSettings.timezone, F.text)
async def settings_timezone_text_handler(
    message: Message,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile.get("language") if profile else "ru"
    if not is_kyiv_timezone_choice(message.text):
        await message.answer(
            (
                "Натисніть «🇺🇦 Час Києва» або надішліть геолокацію. "
                "Інший текст тут не змінює часовий пояс."
                if language == "uk"
                else
                "Нажмите «🇺🇦 Время Киева» или отправьте геолокацию. "
                "Другой текст здесь не изменяет часовой пояс."
            ),
            reply_markup=timezone_keyboard(language),
        )
        return

    await db.save_profile(
        message.from_user.id,
        {"timezone": "Europe/Kyiv"},
    )
    await state.clear()
    await message.answer(
        (
            "✅ Часовий пояс встановлено: Київ, Europe/Kyiv.\n\n"
            "Нагадування, календар та інтервальне харчування "
            "тепер використовують київський час."
            if language == "uk"
            else
            "✅ Часовой пояс установлен: Киев, Europe/Kyiv.\n\n"
            "Напоминания, календарь и интервальное питание "
            "теперь используют киевское время."
        ),
        reply_markup=persistent_menu_keyboard(language),
    )


def activity_description(
    activity: str,
    language: str,
) -> str:
    labels = {
        "uk": {
            "sedentary": "переважно сидячий день",
            "light": "невелика активність і прогулянки",
            "moderate": "середня активність або кілька тренувань",
            "active": "висока активність або фізична робота",
        },
        "ru": {
            "sedentary": "преимущественно сидячий день",
            "light": "небольшая активность и прогулки",
            "moderate": "средняя активность или несколько тренировок",
            "active": "высокая активность или физическая работа",
        },
    }
    return labels.get(language, labels["ru"]).get(
        activity,
        "не указана" if language == "ru" else "не вказана",
    )


def recalculation_preview_text(
    profile: dict,
    targets: dict,
    language: str,
) -> str:
    age = age_from_birthdate(profile["birth_date"])
    current_calories = int(profile.get("calorie_target") or 0)
    current_protein = int(profile.get("protein_g") or 0)
    current_fat = int(profile.get("fat_g") or 0)
    current_carbs = int(profile.get("carbs_g") or 0)

    if language == "uk":
        safety_text = (
            "\n\n⚠️ Через зазначені в анкеті обставини або низьку "
            "цільову вагу бот не створюватиме автоматичний дефіцит. "
            "Буде показано орієнтир для підтримання ваги."
            if targets["restricted"]
            else
            "\n\nБот використає помірне зниження енергії, без "
            "екстремального дефіциту."
        )
        return (
            "🧮 Перевірте дані перед оновленням\n\n"
            "Розрахунок використовує:\n"
            f"• поточну вагу — {float(profile['current_weight_kg']):.1f} кг;\n"
            f"• зріст — {float(profile['height_cm']):.0f} см;\n"
            f"• вік — {age} років;\n"
            f"• обрану ціль — {float(profile['target_weight_kg']):.1f} кг;\n"
            f"• активність — {activity_description(profile['activity'], language)}.\n\n"
            "Зараз у профілі:\n"
            f"• {current_calories} кілокалорій;\n"
            f"• білок — {current_protein} г;\n"
            f"• жири — {current_fat} г;\n"
            f"• вуглеводи — {current_carbs} г.\n\n"
            "Після оновлення буде:\n"
            f"• близько {targets['calorie_target']} кілокалорій;\n"
            f"• білок — близько {targets['protein_g']} г;\n"
            f"• жири — близько {targets['fat_g']} г;\n"
            f"• вуглеводи — близько {targets['carbs_g']} г."
            f"{safety_text}\n\n"
            "Ці значення є практичними орієнтирами, а не медичною "
            "нормою й не жорсткою межею. Підтвердити оновлення?"
        )

    safety_text = (
        "\n\n⚠️ Из-за указанных в анкете обстоятельств или низкой "
        "целевой массы бот не будет создавать автоматический дефицит. "
        "Будет показан ориентир для поддержания веса."
        if targets["restricted"]
        else
        "\n\nБот использует умеренное снижение энергии, без "
        "экстремального дефицита."
    )
    return (
        "🧮 Проверьте данные перед обновлением\n\n"
        "Расчёт использует:\n"
        f"• текущий вес — {float(profile['current_weight_kg']):.1f} кг;\n"
        f"• рост — {float(profile['height_cm']):.0f} см;\n"
        f"• возраст — {age} лет;\n"
        f"• выбранную цель — {float(profile['target_weight_kg']):.1f} кг;\n"
        f"• активность — {activity_description(profile['activity'], language)}.\n\n"
        "Сейчас в профиле:\n"
        f"• {current_calories} килокалорий;\n"
        f"• белок — {current_protein} г;\n"
        f"• жиры — {current_fat} г;\n"
        f"• углеводы — {current_carbs} г.\n\n"
        "После обновления будет:\n"
        f"• около {targets['calorie_target']} килокалорий;\n"
        f"• белок — около {targets['protein_g']} г;\n"
        f"• жиры — около {targets['fat_g']} г;\n"
        f"• углеводы — около {targets['carbs_g']} г."
        f"{safety_text}\n\n"
        "Эти значения являются практическими ориентирами, а не "
        "медицинской нормой и не жёсткой границей. Подтвердить обновление?"
    )


def recalculation_preview_keyboard(
    language: str,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "✅ Оновити орієнтири"
                    if language == "uk"
                    else "✅ Обновить ориентиры",
                    "settings:recalculate_apply",
                    ButtonStyle.SUCCESS,
                )
            ],
            [
                button(
                    "⬅️ Повернутися до налаштувань"
                    if language == "uk"
                    else "⬅️ Вернуться к настройкам",
                    "menu:settings",
                    ButtonStyle.PRIMARY,
                )
            ],
        ]
    )


@router.callback_query(F.data == "settings:recalculate")
async def settings_recalculate_preview_handler(
    callback: CallbackQuery,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile.get("language") if profile else "ru"

    if not profile_complete(profile):
        await callback.answer(
            "Сначала завершите анкету.",
            show_alert=True,
        )
        return

    targets = calculate_targets(dict(profile))
    await callback.message.answer(
        recalculation_preview_text(
            profile,
            targets,
            language,
        ),
        reply_markup=recalculation_preview_keyboard(language),
    )
    await callback.answer()


@router.callback_query(F.data == "settings:recalculate_apply")
async def settings_recalculate_apply_handler(
    callback: CallbackQuery,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile.get("language") if profile else "ru"

    if not profile_complete(profile):
        await callback.answer(
            "Сначала завершите анкету.",
            show_alert=True,
        )
        return

    old_calories = int(profile.get("calorie_target") or 0)
    old_protein = int(profile.get("protein_g") or 0)
    old_fat = int(profile.get("fat_g") or 0)
    old_carbs = int(profile.get("carbs_g") or 0)

    targets = calculate_targets(dict(profile))
    await db.save_profile(
        callback.from_user.id,
        {
            "bmr": targets["bmr"],
            "tdee": targets["tdee"],
            "calorie_target": targets["calorie_target"],
            "protein_g": targets["protein_g"],
            "fat_g": targets["fat_g"],
            "carbs_g": targets["carbs_g"],
            "safety_restricted": int(targets["restricted"]),
        },
    )

    calorie_change = targets["calorie_target"] - old_calories
    protein_change = targets["protein_g"] - old_protein
    fat_change = targets["fat_g"] - old_fat
    carbs_change = targets["carbs_g"] - old_carbs

    if language == "uk":
        if all(
            value == 0
            for value in (
                calorie_change,
                protein_change,
                fat_change,
                carbs_change,
            )
        ):
            change_note = (
                "Значення не змінилися, тому що дані анкети й поточна "
                "вага відповідають попередньому розрахунку."
            )
        else:
            change_note = (
                "Зміни від попереднього розрахунку:\n"
                f"• енергія — {calorie_change:+d} кілокалорій;\n"
                f"• білок — {protein_change:+d} г;\n"
                f"• жири — {fat_change:+d} г;\n"
                f"• вуглеводи — {carbs_change:+d} г."
            )

        restricted_note = (
            "\n\n⚠️ Зараз використовується орієнтир для підтримання "
            "ваги. Для персонального дефіциту потрібне погодження з лікарем."
            if targets["restricted"]
            else ""
        )

        result_text = (
            "✅ Денні орієнтири оновлено\n\n"
            f"🔥 Енергія — близько {targets['calorie_target']} "
            "кілокалорій на день. Це не сувора межа: важливіше "
            "середнє значення за тиждень.\n\n"
            f"🥩 Білок — близько {targets['protein_g']} г. "
            "Допомагає підтримувати м'язи й ситість.\n\n"
            f"🥑 Жири — близько {targets['fat_g']} г. "
            "Потрібні для гормонів, шкіри та засвоєння вітамінів.\n\n"
            f"🍚 Вуглеводи — близько {targets['carbs_g']} г. "
            "Це важливе джерело енергії.\n\n"
            f"{change_note}"
            f"{restricted_note}\n\n"
            "Нові значення уже використовуються в щоденнику, "
            "рецептах і персональних рекомендаціях."
        )
    else:
        if all(
            value == 0
            for value in (
                calorie_change,
                protein_change,
                fat_change,
                carbs_change,
            )
        ):
            change_note = (
                "Значения не изменились, потому что данные анкеты и "
                "текущий вес соответствуют предыдущему расчёту."
            )
        else:
            change_note = (
                "Изменения относительно прошлого расчёта:\n"
                f"• энергия — {calorie_change:+d} килокалорий;\n"
                f"• белок — {protein_change:+d} г;\n"
                f"• жиры — {fat_change:+d} г;\n"
                f"• углеводы — {carbs_change:+d} г."
            )

        restricted_note = (
            "\n\n⚠️ Сейчас используется ориентир для поддержания веса. "
            "Для персонального дефицита требуется согласование с врачом."
            if targets["restricted"]
            else ""
        )

        result_text = (
            "✅ Дневные ориентиры обновлены\n\n"
            f"🔥 Энергия — около {targets['calorie_target']} "
            "килокалорий в день. Это не строгая граница: важнее "
            "среднее значение за неделю.\n\n"
            f"🥩 Белок — около {targets['protein_g']} г. "
            "Помогает поддерживать мышцы и сытость.\n\n"
            f"🥑 Жиры — около {targets['fat_g']} г. "
            "Нужны для гормонов, кожи и усвоения витаминов.\n\n"
            f"🍚 Углеводы — около {targets['carbs_g']} г. "
            "Это важный источник энергии.\n\n"
            f"{change_note}"
            f"{restricted_note}\n\n"
            "Новые значения уже используются в дневнике, рецептах "
            "и персональных рекомендациях."
        )

    await callback.message.answer(
        result_text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button(
                        "🧾 Моя інформація"
                        if language == "uk"
                        else "🧾 Моя информация",
                        "menu:profile",
                        ButtonStyle.SUCCESS,
                    )
                ],
                [
                    button(
                        "⚙️ Повернутися до налаштувань"
                        if language == "uk"
                        else "⚙️ Вернуться к настройкам",
                        "menu:settings",
                        ButtonStyle.PRIMARY,
                    )
                ],
            ]
        ),
    )
    await callback.answer(
        "Орієнтири оновлено"
        if language == "uk"
        else "Ориентиры обновлены"
    )


@router.callback_query(F.data == "settings:delete")
async def settings_delete_handler(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile.get("language") if profile else "ru"
    await state.set_state(AccountSettings.delete_confirm)
    await callback.message.answer(
        (
            "⚠️ Будуть видалені анкета, вага, заміри, харчування, "
            "рецепти, нагадування, партнерські бонуси та заявки.\n\n"
            "Для підтвердження напишіть: ВИДАЛИТИ"
            if language == "uk"
            else
            "⚠️ Будут удалены анкета, вес, замеры, питание, "
            "рецепты, напоминания, партнёрские бонусы и заявки.\n\n"
            "Для подтверждения напишите: УДАЛИТЬ"
        )
    )
    await callback.answer()


@router.message(AccountSettings.delete_confirm, F.text)
async def settings_delete_confirm_handler(
    message: Message,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile.get("language") if profile else "ru"
    expected = "ВИДАЛИТИ" if language == "uk" else "УДАЛИТЬ"

    if (message.text or "").strip().upper() != expected:
        await message.answer(
            (
                f"Видалення скасовано. Для видалення потрібно точно написати {expected}."
                if language == "uk"
                else
                f"Удаление отменено. Для удаления нужно точно написать {expected}."
            )
        )
        await state.clear()
        return

    await db.delete_user_data(message.from_user.id)
    await state.clear()
    await message.answer(
        (
            "✅ Ваші дані видалено. Для створення нового профілю натисніть /start."
            if language == "uk"
            else
            "✅ Ваши данные удалены. Для создания нового профиля нажмите /start."
        ),
        reply_markup=ReplyKeyboardRemove(),
    )


@router.callback_query(F.data == "menu:subscription")
async def subscription_menu(callback: CallbackQuery) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    await db.release_expired_bonus_reservations()
    balance = await db.get_bonus_balance(callback.from_user.id)

    await callback.message.answer(
        subscription_screen_text(
            profile or {},
            balance,
            language,
        ),
        reply_markup=subscription_keyboard(language),
    )
    await callback.answer()


async def create_subscription_invoice(
    *,
    message: Message,
    bot: Bot,
    user_id: int,
    plan_code: str,
    use_bonus: bool,
) -> None:
    plan = PLANS.get(plan_code)
    profile = await db.get_profile(user_id)
    if not plan or not profile:
        await message.answer("Ошибка тарифа.")
        return

    language = profile.get("language") or "ru"
    invoice_id = uuid.uuid4().hex
    invoice = await db.create_pending_invoice(
        invoice_id=invoice_id,
        user_id=user_id,
        plan_code=plan_code,
        original_amount=int(plan["stars"]),
        use_bonus=use_bonus,
    )
    title = (
        f"minus_kg · {plan['days']} днів"
        if language == "uk"
        else f"minus_kg · {plan['days']} дней"
    )

    description = (
        "Доступ до особистого кабінету та функцій minus_kg."
        if language == "uk"
        else "Доступ к личному кабинету и функциям minus_kg."
    )
    if int(invoice["bonus_used"]) > 0:
        description += (
            f" Використано бонусів: {invoice['bonus_used']}."
            if language == "uk"
            else f" Использовано бонусов: {invoice['bonus_used']}."
        )

    invoice_url = await bot.create_invoice_link(
        title=title,
        description=description,
        payload=f"minuskg_invoice:{invoice_id}",
        currency="XTR",
        prices=[
            LabeledPrice(
                label=title,
                amount=int(invoice["payable_amount"]),
            )
        ],
        provider_token="",
    )

    now = int(time.time())
    current_end = max(
        now,
        int(profile.get("trial_expires_at") or 0),
        int(profile.get("subscription_expires_at") or 0),
    )
    expected_end = current_end + int(plan["days"]) * 86400
    expected_end_text = datetime.fromtimestamp(
        expected_end,
        ZoneInfo(profile.get("timezone") or "Europe/Kyiv"),
    ).strftime("%d.%m.%Y %H:%M")

    if language == "uk":
        summary = (
            "🧾 Перевірте покупку\n\n"
            f"Термін: {plan['days']} днів\n"
            f"Повна ціна: {plan['stars']} ⭐\n"
            f"Використано внутрішніх бонусів: "
            f"{invoice['bonus_used']} ⭐\n"
            f"До сплати в Telegram: "
            f"{invoice['payable_amount']} ⭐\n\n"
            f"Після успішної оплати доступ буде активний до "
            f"{expected_end_text}.\n\n"
            "Невикористані дні поточного доступу не згорять: "
            "новий термін додасться після них. Оплата активує "
            "доступ автоматично.\n\n"
            "Посилання на оплату діє 30 хвилин. Якщо не оплачувати "
            "рахунок, зарезервовані бонуси повернуться на баланс."
        )
        pay_text = "⭐ Перейти до оплати"
        back_text = "⬅️ Обрати інший термін"
    else:
        summary = (
            "🧾 Проверьте покупку\n\n"
            f"Срок: {plan['days']} дней\n"
            f"Полная цена: {plan['stars']} ⭐\n"
            f"Использовано внутренних бонусов: "
            f"{invoice['bonus_used']} ⭐\n"
            f"К оплате в Telegram: "
            f"{invoice['payable_amount']} ⭐\n\n"
            f"После успешной оплаты доступ будет активен до "
            f"{expected_end_text}.\n\n"
            "Неиспользованные дни текущего доступа не сгорят: "
            "новый срок добавится после них. Оплата активирует "
            "доступ автоматически.\n\n"
            "Ссылка на оплату действует 30 минут. Если не оплачивать "
            "счёт, зарезервированные бонусы вернутся на баланс."
        )
        pay_text = "⭐ Перейти к оплате"
        back_text = "⬅️ Выбрать другой срок"

    await message.answer(
        summary,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=pay_text,
                        url=invoice_url,
                        style=ButtonStyle.SUCCESS,
                    )
                ],
                [
                    button(
                        back_text,
                        "menu:subscription",
                        ButtonStyle.PRIMARY,
                    )
                ],
            ]
        ),
    )


@router.callback_query(F.data.startswith("buy:"))
async def buy_handler(callback: CallbackQuery, bot: Bot) -> None:
    plan_code = callback.data.split(":", 1)[1]
    plan = PLANS.get(plan_code)
    profile = await db.get_profile(callback.from_user.id)
    if not plan or not profile:
        await callback.answer("Ошибка тарифа.", show_alert=True)
        return

    await db.release_expired_bonus_reservations()
    balance = await db.get_bonus_balance(callback.from_user.id)
    language = profile.get("language") or "ru"
    usable = min(balance, max(0, int(plan["stars"]) - 1))

    if usable > 0:
        text = (
            (
                f"У вас {balance} бонусів. Використати {usable} ⭐ як знижку?\n"
                f"До сплати буде {int(plan['stars']) - usable} ⭐."
            )
            if language == "uk"
            else
            (
                f"У вас {balance} бонусов. Использовать {usable} ⭐ как скидку?\n"
                f"К оплате будет {int(plan['stars']) - usable} ⭐."
            )
        )
        await callback.message.answer(
            text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        button(
                            "✅ Використати бонуси"
                            if language == "uk"
                            else "✅ Использовать бонусы",
                            f"buy_bonus:{plan_code}:yes",
                            ButtonStyle.SUCCESS,
                        )
                    ],
                    [
                        button(
                            "Оплатити повну суму"
                            if language == "uk"
                            else "Оплатить полную сумму",
                            f"buy_bonus:{plan_code}:no",
                            ButtonStyle.PRIMARY,
                        )
                    ],
                ]
            ),
        )
    else:
        await create_subscription_invoice(
            message=callback.message,
            bot=bot,
            user_id=callback.from_user.id,
            plan_code=plan_code,
            use_bonus=False,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("buy_bonus:"))
async def buy_bonus_handler(
    callback: CallbackQuery,
    bot: Bot,
) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3 or parts[1] not in PLANS:
        await callback.answer("Ошибка тарифа.", show_alert=True)
        return

    await create_subscription_invoice(
        message=callback.message,
        bot=bot,
        user_id=callback.from_user.id,
        plan_code=parts[1],
        use_bonus=parts[2] == "yes",
    )
    await callback.answer()


@router.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery) -> None:
    prefix = "minuskg_invoice:"
    payload = query.invoice_payload or ""
    valid = False

    if payload.startswith(prefix):
        invoice_id = payload[len(prefix):]
        valid = await db.validate_pending_invoice(
            invoice_id=invoice_id,
            user_id=query.from_user.id,
            amount=query.total_amount,
            currency=query.currency,
        )

    await query.answer(
        ok=valid,
        error_message=(
            None
            if valid
            else
            "Счёт устарел или бонусы изменились. Создайте новый счёт."
        ),
    )


@router.message(F.successful_payment)
async def successful_payment_handler(
    message: Message,
    bot: Bot,
) -> None:
    if not message.from_user or not message.successful_payment:
        return

    payment = message.successful_payment
    prefix = "minuskg_invoice:"
    payload = payment.invoice_payload or ""
    if not payload.startswith(prefix):
        return

    invoice_id = payload[len(prefix):]
    invoice = await db.get_pending_invoice(invoice_id)
    profile = await db.get_profile(message.from_user.id)
    if not invoice or not profile:
        return

    plan = PLANS.get(str(invoice["plan_code"]))
    if not plan:
        return

    now = int(time.time())
    base = max(
        now,
        int(profile.get("trial_expires_at") or 0),
        int(profile.get("subscription_expires_at") or 0),
    )
    expires_at = base + int(plan["days"]) * 86400

    result = await db.complete_pending_invoice(
        invoice_id=invoice_id,
        charge_id=payment.telegram_payment_charge_id,
        user_id=message.from_user.id,
        amount=payment.total_amount,
        currency=payment.currency,
        expires_at=expires_at,
    )
    if not result:
        await message.answer(
            "Платёж получен, но не удалось активировать доступ автоматически. "
            "Напишите в поддержку."
        )
        return

    expiry = datetime.fromtimestamp(
        expires_at,
        ZoneInfo(profile.get("timezone") or "Europe/Kyiv"),
    )
    language = profile.get("language") or "ru"
    if language == "uk":
        text = (
            f"✅ Оплату підтверджено. Доступ до {expiry:%d.%m.%Y %H:%M}.\n"
            f"Списано бонусів: {result['bonus_used']} ⭐."
        )
    else:
        text = (
            f"✅ Оплата подтверждена. Доступ до {expiry:%d.%m.%Y %H:%M}.\n"
            f"Списано бонусов: {result['bonus_used']} ⭐."
        )
    await message.answer(text, reply_markup=main_menu(language))

    inviter_id = result.get("inviter_user_id")
    referral_bonus = int(result.get("referral_bonus") or 0)
    if inviter_id and referral_bonus > 0:
        inviter_profile = await db.get_profile(int(inviter_id))
        inviter_language = (
            inviter_profile.get("language")
            if inviter_profile
            else "ru"
        )
        new_balance = await db.get_bonus_balance(int(inviter_id))
        try:
            await bot.send_message(
                int(inviter_id),
                (
                    f"🤝 Ваш запрошений користувач оплатив підписку. "
                    f"Нараховано {referral_bonus} бонусів ⭐.\n"
                    f"Баланс: {new_balance} ⭐."
                    if inviter_language == "uk"
                    else
                    f"🤝 Ваш приглашённый пользователь оплатил подписку. "
                    f"Начислено {referral_bonus} бонусов ⭐.\n"
                    f"Баланс: {new_balance} ⭐."
                ),
            )
        except Exception:
            logging.exception(
                "Failed to notify referral inviter user_id=%s",
                inviter_id,
            )


CRISIS_PATTERNS = [
    r"\bхочу умереть\b", r"\bне хочу жить\b", r"\bубью себя\b",
    r"\bпокончу с собой\b", r"\bсуицид", r"\bсамоубий",
    r"\bнавредить себе\b", r"\bпорезать вены\b",
    r"\bне хочу жити\b", r"\bхочу померти\b", r"\bвб'ю себе\b",
    r"\bвбити себе\b", r"\bсуїцид", r"\bсамогуб",
    r"\bнашкодити собі\b", r"\bпорізати вени\b",
]
CRISIS_FALSE_POSITIVES = [
    "умираю со смеху", "умереть со смеху", "померти зі сміху",
    "умираю от усталости",
]


def is_crisis_message(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if any(phrase in normalized for phrase in CRISIS_FALSE_POSITIVES):
        return False
    return any(re.search(pattern, normalized) for pattern in CRISIS_PATTERNS)


def friend_start_keyboard(language: str) -> InlineKeyboardMarkup:
    if language == "uk":
        rows = [
            [button("😔 Мені зараз важко", "friend_topic:hard", ButtonStyle.PRIMARY),
             button("😕 У мене не виходить", "friend_topic:stuck", ButtonStyle.SUCCESS)],
            [button("🍽 Тягне їсти через емоції", "friend_topic:emotional_food", ButtonStyle.PRIMARY)],
            [button("💬 Просто хочу поговорити", "friend_topic:talk", ButtonStyle.SUCCESS)],
            [button("🧘 Хвилина дихання", "friend_breathe", ButtonStyle.PRIMARY),
             button("🏠 Завершити розмову", "friend_exit", ButtonStyle.DANGER)],
        ]
    else:
        rows = [
            [button("😔 Мне сейчас тяжело", "friend_topic:hard", ButtonStyle.PRIMARY),
             button("😕 У меня не получается", "friend_topic:stuck", ButtonStyle.SUCCESS)],
            [button("🍽 Тянет есть из-за эмоций", "friend_topic:emotional_food", ButtonStyle.PRIMARY)],
            [button("💬 Просто хочу поговорить", "friend_topic:talk", ButtonStyle.SUCCESS)],
            [button("🧘 Минута дыхания", "friend_breathe", ButtonStyle.PRIMARY),
             button("🏠 Завершить разговор", "friend_exit", ButtonStyle.DANGER)],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def friend_response_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("🧘 Хвилина дихання" if language == "uk" else "🧘 Минута дыхания",
                "friend_breathe", ButtonStyle.PRIMARY),
         button("🧹 Нова розмова" if language == "uk" else "🧹 Новый разговор",
                "friend_reset", ButtonStyle.SUCCESS)],
        [button("🏠 Завершити й відкрити меню" if language == "uk" else "🏠 Завершить и открыть меню",
                "friend_exit", ButtonStyle.DANGER)],
    ])


def crisis_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("🟢 Я зараз у безпеці" if language == "uk" else "🟢 Я сейчас в безопасности",
                "friend_safe", ButtonStyle.SUCCESS)],
        [button("🚨 Мені небезпечно зараз" if language == "uk" else "🚨 Мне опасно сейчас",
                "friend_danger", ButtonStyle.DANGER)],
    ])


async def typing_loop(bot: Bot, chat_id: int) -> None:
    try:
        while True:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception("Typing indicator failed")


async def send_crisis_support(message: Message, language: str, question: str, local_date: str) -> None:
    if language == "uk":
        answer = (
            "Мені дуже важливо, що ви написали про це. Зараз головне — ваша безпека, "
            "а не вага, харчування чи сила волі.\n\n"
            "Якщо є ризик, що ви можете нашкодити собі найближчим часом, "
            "зателефонуйте 112 або 103 просто зараз. Якщо можете, скажіть людині поруч: "
            "«Мені небезпечно залишатися наодинці, побудь зі мною».\n\n"
            "Перейдіть туди, де є інші люди, і відсуньте подалі ліки, зброю, "
            "гострі предмети або інші речі, якими можна собі зашкодити. "
            "Не залишайтеся наодинці з цим станом.\n\n"
            "Натисніть нижче: ви зараз у безпеці чи небезпека безпосередня?"
        )
    else:
        answer = (
            "Мне очень важно, что вы написали об этом. Сейчас главное — ваша безопасность, "
            "а не вес, питание или сила воли.\n\n"
            "Если есть риск, что вы можете причинить себе вред в ближайшее время, "
            "позвоните 112 или 103 прямо сейчас. По возможности скажите человеку рядом: "
            "«Мне опасно оставаться одной/одному, побудь со мной».\n\n"
            "Перейдите туда, где есть другие люди, и уберите подальше лекарства, оружие, "
            "острые предметы или другие вещи, которыми можно навредить себе. "
            "Не оставайтесь наедине с этим состоянием.\n\n"
            "Нажмите ниже: вы сейчас в безопасности или опасность непосредственная?"
        )
    # Crisis text is deliberately not stored in the ordinary AI history
    # and does not consume the daily conversation allowance.
    await message.answer(answer, reply_markup=crisis_keyboard(language))


async def process_friend_text(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    profile = await db.get_profile(message.from_user.id)
    language = profile["language"] if profile else "ru"
    question = (message.text or "").strip()
    if not profile_complete(profile):
        await state.clear()
        await message.answer("Сначала заполните короткую анкету через /start." if language == "ru" else "Спочатку заповніть коротку анкету через /start.")
        return
    local_date = datetime.now(ZoneInfo(profile["timezone"])).date().isoformat()
    if is_crisis_message(question):
        await send_crisis_support(message, language, question, local_date)
        return
    if not access_active(profile):
        await state.clear()
        await send_access_expired(message, language)
        return
    if len(question) < 2:
        await message.answer("Напишіть хоча б кілька слів — я уважно прочитаю." if language == "uk" else "Напишите хотя бы несколько слов — я внимательно прочитаю.")
        return
    if len(question) > 2500:
        await message.answer("Повідомлення дуже довге. Скоротіть його до 2500 символів або надішліть двома частинами." if language == "uk" else "Сообщение очень длинное. Сократите его до 2500 символов или отправьте двумя частями.")
        return
    used = await db.ai_questions_today(message.from_user.id, local_date)
    limit = AI_PAID_DAILY_LIMIT if paid_subscription_active(profile) else AI_TRIAL_DAILY_LIMIT
    if used >= limit:
        await message.answer(
            "На сьогодні ліміт повідомлень із minus_kg закінчився. Він оновиться опівночі. Ваші записи, календар і рецепти продовжують працювати."
            if language == "uk" else
            "На сегодня лимит сообщений с minus_kg закончился. Он обновится в полночь. Ваши записи, календарь и рецепты продолжают работать."
        )
        return
    status = await message.answer("🫶 Я поруч. Уважно читаю ваше повідомлення..." if language == "uk" else "🫶 Я рядом. Внимательно читаю ваше сообщение...")
    typing_task = asyncio.create_task(typing_loop(message.bot, message.chat.id))
    try:
        history = await db.recent_ai_messages(message.from_user.id, limit=12)
        day_totals = await db.daily_food_totals(message.from_user.id, local_date)
        answer = await ask_weight_ai(profile, question, history, day_totals)
        await db.add_ai_exchange(message.from_user.id, question, answer, local_date)
    except Exception:
        logging.exception("Friend chat failed for user_id=%s", message.from_user.id)
        await status.edit_text(
            "Зараз мені не вдалося сформувати відповідь. Це технічна помилка, а не проблема у вашому повідомленні. Спробуйте ще раз за хвилину."
            if language == "uk" else
            "Сейчас мне не удалось сформировать ответ. Это техническая ошибка, а не проблема в вашем сообщении. Попробуйте ещё раз через минуту."
        )
        return
    finally:
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
    remaining = max(0, limit - used - 1)
    footer = (f"\n\n💬 Діалог уже відкритий — просто напишіть наступне повідомлення. Сьогодні залишилося: {remaining}."
              if language == "uk" else
              f"\n\n💬 Диалог уже открыт — просто напишите следующее сообщение. Сегодня осталось: {remaining}.")
    await status.edit_text((answer + footer)[:4096], reply_markup=friend_response_keyboard(language))
    await state.set_state(FriendChat.active)


@router.callback_query(F.data == "menu:ai")
async def ai_menu(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    if not profile_complete(profile):
        await callback.answer("Сначала завершите анкету.", show_alert=True)
        return
    if not access_active(profile):
        await send_access_expired(callback.message, language)
        await callback.answer()
        return
    local_date = datetime.now(ZoneInfo(profile["timezone"])).date().isoformat()
    used = await db.ai_questions_today(callback.from_user.id, local_date)
    limit = AI_PAID_DAILY_LIMIT if paid_subscription_active(profile) else AI_TRIAL_DAILY_LIMIT
    remaining = max(0, limit - used)
    await state.set_state(FriendChat.active)
    if language == "uk":
        text = (
            "🫶 Я поруч, і тепер не потрібно щоразу натискати цю кнопку.\n\n"
            "Можна писати звичайними повідомленнями: про харчування, вагу, мотивацію, втому, стрес, самотність або те, що сьогодні просто нічого не виходить. Я постараюся вислухати, пояснити й запропонувати один зрозумілий наступний крок.\n\n"
            "Я не психолог і не лікар та не ставлю діагнозів. Але можу бути уважним цифровим співрозмовником і допомогти сформулювати, що відбувається.\n\n"
            f"Сьогодні залишилося повідомлень: {remaining}. Що зараз найбільше хочеться сказати?"
        )
    else:
        text = (
            "🫶 Я рядом, и теперь не нужно каждый раз нажимать эту кнопку.\n\n"
            "Можно писать обычными сообщениями: о питании, весе, мотивации, усталости, стрессе, одиночестве или о том, что сегодня просто ничего не получается. Я постараюсь выслушать, объяснить и предложить один понятный следующий шаг.\n\n"
            "Я не психолог и не врач и не ставлю диагнозов. Но могу быть внимательным цифровым собеседником и помочь сформулировать, что происходит.\n\n"
            f"Сегодня осталось сообщений: {remaining}. Что сейчас больше всего хочется сказать?"
        )
    await callback.message.answer(text, reply_markup=friend_start_keyboard(language))
    await callback.answer()


@router.callback_query(F.data.startswith("friend_topic:"))
async def friend_topic_handler(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    topic = callback.data.split(":", 1)[1]
    await state.set_state(FriendChat.active)
    prompts = {
        "ru": {
            "hard": "Я рядом. Не нужно сейчас красиво формулировать. Что именно сегодня оказалось самым тяжёлым?",
            "stuck": "Давайте без обвинений себя. В какой момент обычно всё начинает идти не так: вечером, после стресса, на выходных или когда вы очень голодны?",
            "emotional_food": "Это бывает у многих, и стыд здесь не помогает. Что вы чувствуете прямо перед желанием есть: голод, тревогу, скуку, злость, усталость или что-то другое?",
            "talk": "Хорошо. Можно говорить не только о весе. Что сейчас занимает ваши мысли сильнее всего?",
        },
        "uk": {
            "hard": "Я поруч. Не потрібно зараз красиво формулювати. Що саме сьогодні виявилося найважчим?",
            "stuck": "Давайте без звинувачень себе. У який момент зазвичай усе починає йти не так: увечері, після стресу, на вихідних чи коли ви дуже голодні?",
            "emotional_food": "Таке буває у багатьох, і сором тут не допомагає. Що ви відчуваєте безпосередньо перед бажанням їсти: голод, тривогу, нудьгу, злість, втому чи щось інше?",
            "talk": "Добре. Можна говорити не лише про вагу. Що зараз найбільше займає ваші думки?",
        },
    }
    await callback.message.answer(prompts[language].get(topic, prompts[language]["talk"]), reply_markup=friend_response_keyboard(language))
    await callback.answer()


@router.message(FriendChat.active, F.text)
async def friend_chat_text_handler(message: Message, state: FSMContext) -> None:
    await process_friend_text(message, state)


@router.message(FriendChat.active)
async def friend_chat_non_text_handler(message: Message) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile["language"] if profile else "ru"
    await message.answer(
        "Поки що в цьому діалозі я найкраще розумію текст. Напишіть словами, що відбувається. Фото їжі можна надіслати через окрему кнопку в меню."
        if language == "uk" else
        "Пока в этом диалоге я лучше всего понимаю текст. Напишите словами, что происходит. Фотографию еды можно отправить через отдельную кнопку в меню."
    )


@router.callback_query(F.data == "friend_reset")
async def friend_reset_handler(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    await db.clear_ai_history(callback.from_user.id)
    await state.set_state(FriendChat.active)
    await callback.message.answer(
        "🧹 Попередню розмову очищено. Починаємо з чистого аркуша. Про що хочете поговорити зараз?"
        if language == "uk" else
        "🧹 Предыдущий разговор очищен. Начинаем с чистого листа. О чём хотите поговорить сейчас?",
        reply_markup=friend_start_keyboard(language),
    )
    await callback.answer()


@router.callback_query(F.data == "friend_exit")
async def friend_exit_handler(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    await state.clear()
    await callback.message.answer(
        "Домовилися. Розмову завершено, але ви можете повернутися будь-коли."
        if language == "uk" else
        "Договорились. Разговор завершён, но вы можете вернуться в любое время.",
        reply_markup=persistent_menu_keyboard(language),
    )
    await callback.message.answer("Головне меню:" if language == "uk" else "Главное меню:", reply_markup=main_menu(language))
    await callback.answer()


@router.callback_query(F.data == "friend_breathe")
async def friend_breathe_handler(callback: CallbackQuery) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    animation = render_breathing_animation(language)
    caption = (
        "Спробуйте дивитися на коло й дихати без зусиль. Це не лікування й не тест — лише коротка пауза, щоб трохи сповільнитися."
        if language == "uk" else
        "Попробуйте смотреть на круг и дышать без усилий. Это не лечение и не тест — только короткая пауза, чтобы немного замедлиться."
    )
    if animation:
        await callback.message.answer_animation(BufferedInputFile(animation, filename="minus_kg_breathing.gif"), caption=caption, reply_markup=friend_response_keyboard(language))
    else:
        await callback.message.answer(
            "Вдихайте приблизно 4 секунди, зробіть коротку паузу й повільно видихайте близько 6 секунд. Повторіть кілька разів у комфортному темпі."
            if language == "uk" else
            "Вдыхайте примерно 4 секунды, сделайте короткую паузу и медленно выдыхайте около 6 секунд. Повторите несколько раз в комфортном темпе.",
            reply_markup=friend_response_keyboard(language),
        )
    await callback.answer()


@router.callback_query(F.data == "friend_safe")
async def friend_safe_handler(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    await state.set_state(FriendChat.active)
    await callback.message.answer(
        "Дякую, що відповіли. Добре, що безпосередньої небезпеки зараз немає. Будь ласка, все одно напишіть або зателефонуйте людині, якій довіряєте, і скажіть, що вам потрібна присутність. Що сталося перед тим, як з'явилися ці думки?"
        if language == "uk" else
        "Спасибо, что ответили. Хорошо, что непосредственной опасности сейчас нет. Пожалуйста, всё равно напишите или позвоните человеку, которому доверяете, и скажите, что вам нужна поддержка и присутствие. Что произошло перед тем, как появились эти мысли?",
        reply_markup=friend_response_keyboard(language),
    )
    await callback.answer()


@router.callback_query(F.data == "friend_danger")
async def friend_danger_handler(callback: CallbackQuery) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    await callback.message.answer(
        "🚨 Будь ласка, телефонуйте 112 або 103 зараз. Якщо важко говорити, покажіть це повідомлення людині поруч і попросіть її зробити дзвінок. Перейдіть у місце, де є люди, та не залишайтеся наодинці."
        if language == "uk" else
        "🚨 Пожалуйста, позвоните 112 или 103 сейчас. Если трудно говорить, покажите это сообщение человеку рядом и попросите его сделать звонок. Перейдите туда, где есть люди, и не оставайтесь в одиночестве."
    )
    await callback.answer()


@router.callback_query(F.data == "menu:create_menu")
async def create_menu_handler(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"

    if not profile_complete(profile):
        await callback.answer(
            "Сначала завершите анкету.",
            show_alert=True,
        )
        return
    if not access_active(profile):
        await callback.message.answer(
            "Пробний доступ завершився. Відкрийте «Підписка» у меню."
            if language == "uk"
            else
            "Пробный доступ закончился. Откройте «Подписка» в меню."
        )
        await callback.answer()
        return

    meals_count = max(
        2,
        min(4, int(profile.get("meals_count") or 3)),
    )
    await state.set_state(Actions.menu_products)

    if language == "uk":
        text = (
            "📋 Створимо повне меню на один день із продуктів, які є вдома.\n\n"
            f"У вашій анкеті обрано {meals_count} прийоми їжі. "
            "Я складу окрему страву для кожного прийому, розподілю порції "
            "за вашим орієнтиром і покажу загальний підсумок дня.\n\n"
            "Напишіть усі продукти та приблизну кількість.\n"
            "Наприклад:\n"
            "куряче філе 500 г, яйця 4 шт., гречка 300 г, картопля 1 кг, "
            "помідори, огірки, кисломолочний сир 200 г, йогурт, яблука.\n\n"
            "Вода, сіль, перець і звичайні спеції можна не перелічувати. "
            "Олію, соуси та солодкі добавки краще вказати окремо."
        )
    else:
        text = (
            "📋 Создадим полноценное меню на один день из продуктов, "
            "которые есть дома.\n\n"
            f"В вашей анкете выбрано {meals_count} приёма пищи. "
            "Я составлю отдельное блюдо для каждого приёма, распределю "
            "порции по вашему ориентиру и покажу общий итог дня.\n\n"
            "Напишите все продукты и примерное количество.\n"
            "Например:\n"
            "куриное филе 500 г, яйца 4 шт., гречка 300 г, картофель 1 кг, "
            "помидоры, огурцы, творог 200 г, йогурт, яблоки.\n\n"
            "Воду, соль, перец и обычные специи можно не перечислять. "
            "Масло, соусы и сладкие добавки лучше указать отдельно."
        )

    await callback.message.answer(text)
    await callback.answer()


@router.message(Actions.menu_products, F.text)
async def menu_products_handler(
    message: Message,
    state: FSMContext,
) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile["language"] if profile else "ru"
    products_text = (message.text or "").strip()

    if len(products_text) < 10:
        await message.answer(
            "Перелічіть хоча б кілька продуктів і приблизну кількість."
            if language == "uk"
            else
            "Перечислите хотя бы несколько продуктов и примерное количество."
        )
        return
    if len(products_text) > 3000:
        await message.answer(
            "Скоротіть список до 3000 символів."
            if language == "uk"
            else
            "Сократите список до 3000 символов."
        )
        return

    loading = await send_recipe_loading(
        message,
        language,
    )

    try:
        local_date = datetime.now(
            ZoneInfo(profile["timezone"])
        ).date().isoformat()
        schedule = await db.get_all_meal_schedule(
            message.from_user.id
        )
        meal_slots = prepare_day_menu_slots(
            profile,
            schedule,
        )
        menu_data = await generate_menu_from_products(
            profile,
            products_text,
            meal_slots,
        )
        session_id = uuid.uuid4().hex[:16]
        await db.save_menu_session(
            session_id=session_id,
            user_id=message.from_user.id,
            products_text=products_text,
            menu_data=menu_data,
            local_date=local_date,
        )
    except Exception:
        logging.exception(
            "Full-day products menu failed for user_id=%s",
            message.from_user.id,
        )
        await update_recipe_loading_error(
            loading,
            (
                "Не вдалося скласти повне меню. Спробуйте ще раз за хвилину "
                "або трохи уточніть кількість продуктів."
                if language == "uk"
                else
                "Не удалось составить полное меню. Попробуйте ещё раз через "
                "минуту или немного уточните количество продуктов."
            ),
        )
        await state.clear()
        return

    try:
        await loading.delete()
    except Exception:
        logging.exception(
            "Full-day menu loading animation could not be deleted"
        )

    await state.clear()

    await message.answer(
        day_menu_overview_text(
            menu_data,
            profile,
            language,
        ),
        reply_markup=day_menu_overview_keyboard(
            session_id,
            menu_data,
            language,
        ),
    )

    # Show the first meal immediately so the result feels visual,
    # while the overview remains the main navigation screen.
    await send_day_menu_recipe_card(
        message,
        session_id,
        menu_data,
        0,
        language,
    )


@router.callback_query(F.data.startswith("day_menu_open:"))
async def day_menu_open_handler(
    callback: CallbackQuery,
) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer(
            "Ошибка меню.",
            show_alert=True,
        )
        return

    session_id = parts[1]
    try:
        index = int(parts[2])
    except ValueError:
        await callback.answer(
            "Ошибка меню.",
            show_alert=True,
        )
        return

    session = await db.get_menu_session(
        session_id,
        callback.from_user.id,
    )
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"

    if not session:
        await callback.answer(
            "Меню больше не найдено. Создайте новое.",
            show_alert=True,
        )
        return

    await send_day_menu_recipe_card(
        callback.message,
        session_id,
        session["menu_data"],
        index,
        language,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("day_menu_nav:"))
async def day_menu_nav_handler(
    callback: CallbackQuery,
) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer(
            "Ошибка карточки.",
            show_alert=True,
        )
        return

    session_id = parts[1]
    try:
        index = int(parts[2])
    except ValueError:
        await callback.answer(
            "Ошибка карточки.",
            show_alert=True,
        )
        return

    session = await db.get_menu_session(
        session_id,
        callback.from_user.id,
    )
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"

    if not session:
        await callback.answer(
            "Меню больше не найдено.",
            show_alert=True,
        )
        return

    menu_data = session["menu_data"]
    recipes = menu_data.get("recipes") or []
    if not recipes:
        await callback.answer(
            "В меню нет блюд.",
            show_alert=True,
        )
        return

    index %= len(recipes)
    recipe = recipes[index]
    image = render_recipe_card(
        recipe=recipe,
        index=index,
        total=len(recipes),
        language=language,
    )
    caption = day_menu_recipe_caption(
        recipe,
        index,
        len(recipes),
        language,
    )
    keyboard = day_menu_recipe_keyboard(
        session_id,
        index,
        len(recipes),
        language,
    )

    if image and callback.message.photo:
        await callback.message.edit_media(
            media=InputMediaPhoto(
                media=BufferedInputFile(
                    image,
                    filename=f"day_menu_{index + 1}.png",
                ),
                caption=caption,
            ),
            reply_markup=keyboard,
        )
    else:
        await send_day_menu_recipe_card(
            callback.message,
            session_id,
            menu_data,
            index,
            language,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("day_menu_overview:"))
async def day_menu_overview_handler(
    callback: CallbackQuery,
) -> None:
    session_id = callback.data.split(":", 1)[1]
    session = await db.get_menu_session(
        session_id,
        callback.from_user.id,
    )
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"

    if not session or not profile:
        await callback.answer(
            "Меню больше не найдено.",
            show_alert=True,
        )
        return

    await callback.message.answer(
        day_menu_overview_text(
            session["menu_data"],
            profile,
            language,
        ),
        reply_markup=day_menu_overview_keyboard(
            session_id,
            session["menu_data"],
            language,
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "day_menu_noop")
async def day_menu_noop_handler(
    callback: CallbackQuery,
) -> None:
    await callback.answer(
        "Перелистывайте приёмы пищи стрелками"
    )


@router.message(Actions.menu_products)
async def menu_products_text_required(message: Message) -> None:
    profile = await db.get_profile(message.from_user.id)
    language = profile["language"] if profile else "ru"
    await message.answer(
        "Отправьте список продуктов обычным текстом."
        if language == "ru"
        else "Надішліть список продуктів звичайним текстом."
    )


@router.callback_query(F.data.startswith("recipe_nav:"))
async def recipe_navigation_handler(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Ошибка карточки.", show_alert=True)
        return

    session_id = parts[1]
    try:
        index = int(parts[2])
    except ValueError:
        await callback.answer("Ошибка карточки.", show_alert=True)
        return

    session = await db.get_menu_session(
        session_id,
        callback.from_user.id,
    )
    if not session:
        await callback.answer(
            "Это меню больше не найдено. Создайте новое.",
            show_alert=True,
        )
        return

    menu_data = session["menu_data"]
    recipes = menu_data.get("recipes") or []
    if not recipes:
        await callback.answer("В меню нет рецептов.", show_alert=True)
        return

    index %= len(recipes)
    profile = await db.get_profile(callback.from_user.id)
    language = profile["language"] if profile else "ru"
    recipe = recipes[index]
    image = render_recipe_card(
        recipe=recipe,
        index=index,
        total=len(recipes),
        language=language,
    )
    caption = recipe_caption(
        menu_data,
        recipe,
        index,
        len(recipes),
        language,
    )
    keyboard = recipe_navigation_keyboard(
        session_id,
        index,
        len(recipes),
        language,
    )

    if image and callback.message.photo:
        await callback.message.edit_media(
            media=InputMediaPhoto(
                media=BufferedInputFile(
                    image,
                    filename=f"recipe_{index + 1}.png",
                ),
                caption=caption,
            ),
            reply_markup=keyboard,
        )
    else:
        await send_recipe_card(
            callback.message,
            session_id,
            menu_data,
            index,
            language,
        )
    await callback.answer()


@router.callback_query(F.data == "recipe_noop")
async def recipe_noop_handler(callback: CallbackQuery) -> None:
    await callback.answer()


async def process_notifications(bot: Bot) -> None:
    open_jokes = {
        "uk": [
            "🟢 Вікно харчування відкрилося. Кухня працює, але не у форматі шведського столу 😄",
            "🍽 Час їсти. Почніть із нормальної порції, а не з переговорів із печивом.",
            "🟢 Можна їсти. Білок і овочі вже чекають на головні ролі.",
        ],
        "ru": [
            "🟢 Окно питания открылось. Кухня работает, но не в формате шведского стола 😄",
            "🍽 Пора есть. Начните с нормальной порции, а не с переговоров с печеньем.",
            "🟢 Можно есть. Белок и овощи уже ждут главные роли.",
        ],
    }
    close_jokes = {
        "uk": [
            "🔴 Вікно харчування закрилося. Холодильник переходить у режим «до завтра» 😄",
            "🌙 Їжу на сьогодні завершено. Чай без цукру та вода залишаються у грі.",
            "🔒 Кухню умовно зачинено. Не караємо себе — просто тримаємо обраний ритм.",
        ],
        "ru": [
            "🔴 Окно питания закрылось. Холодильник переходит в режим «до завтра» 😄",
            "🌙 Еду на сегодня закончили. Чай без сахара и вода остаются в игре.",
            "🔒 Кухня условно закрыта. Не наказываем себя — просто держим выбранный ритм.",
        ],
    }

    for profile in await db.all_complete_profiles():
        try:
            if not access_active(profile):
                continue
            local_now = datetime.now(ZoneInfo(profile["timezone"]))
            local_date = local_now.date().isoformat()
            current_time = local_now.strftime("%H:%M")
            language = profile["language"]

            for slot in await db.get_meal_schedule(profile["user_id"]):
                if slot["meal_time"] != current_time:
                    continue
                key = f"meal:{slot['slot_number']}:{slot['meal_time']}"
                if not await db.mark_notification_sent(profile["user_id"], key, local_date):
                    continue
                text = (
                    f"🍽 Орієнтовний час для «{slot['meal_name']}».\n\n"
                    "Це м'яке нагадування, а не команда. Якщо ви голодні — "
                    "оберіть звичайну порцію. Якщо ще не хочеться їсти, "
                    "відкладіть повідомлення або пропустіть його без почуття провини."
                    if language == "uk" else
                    f"🍽 Ориентировочное время для «{slot['meal_name']}».\n\n"
                    "Это мягкое напоминание, а не команда. Если вы голодны — "
                    "выберите обычную порцию. Если есть пока не хочется, "
                    "отложите уведомление или пропустите его без чувства вины."
                )
                await bot.send_message(
                    profile["user_id"],
                    text,
                    reply_markup=meal_reminder_action_keyboard(
                        int(slot["slot_number"]),
                        language,
                    ),
                )

            for snooze in await db.due_meal_snoozes(
                profile["user_id"],
                int(time.time()),
            ):
                snooze_text = (
                    f"⏰ Минуло 30 хвилин після нагадування про "
                    f"«{snooze['meal_name']}».\n\n"
                    "Перевірте фізичний голод. Якщо їсти вже хочеться — "
                    "запишіть звичайну порцію. Якщо ні, повідомлення можна "
                    "спокійно пропустити."
                    if language == "uk" else
                    f"⏰ Прошло 30 минут после напоминания про "
                    f"«{snooze['meal_name']}».\n\n"
                    "Проверьте физический голод. Если есть уже хочется — "
                    "запишите обычную порцию. Если нет, уведомление можно "
                    "спокойно пропустить."
                )
                await bot.send_message(
                    profile["user_id"],
                    snooze_text,
                    reply_markup=meal_reminder_action_keyboard(
                        int(snooze["slot_number"]),
                        language,
                    ),
                )
                await db.mark_meal_snooze_sent(int(snooze["id"]))

            if profile.get("fasting_mode"):
                start = profile.get("fasting_start")
                end = profile.get("fasting_end")
                index = (profile["user_id"] + local_now.toordinal()) % 3

                if start and current_time == start:
                    key = f"fasting_open:{start}"
                    if await db.mark_notification_sent(profile["user_id"], key, local_date):
                        await bot.send_message(
                            profile["user_id"],
                            open_jokes[language][index],
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(
                                    text="🕐 Статус",
                                    callback_data="fasting_status",
                                    style=ButtonStyle.PRIMARY,
                                )
                            ]]),
                        )

                if end:
                    warning = time_from_minutes(minutes_from_time(end) - 30)
                    if current_time == warning:
                        key = f"fasting_warning:{end}"
                        if await db.mark_notification_sent(profile["user_id"], key, local_date):
                            await bot.send_message(
                                profile["user_id"],
                                "🌙 До закриття вікна 30 хвилин. Не треба терміново доїдати всю кухню 😄"
                                if language == "uk" else
                                "🌙 До закрытия окна 30 минут. Не надо срочно доедать всю кухню 😄",
                            )
                    if current_time == end:
                        key = f"fasting_closed:{end}"
                        if await db.mark_notification_sent(profile["user_id"], key, local_date):
                            await bot.send_message(
                                profile["user_id"],
                                close_jokes[language][index],
                                reply_markup=InlineKeyboardMarkup(
                                    inline_keyboard=[
                                        [
                                            InlineKeyboardButton(
                                                text=(
                                                    "✅ Дотримано"
                                                    if language == "uk"
                                                    else "✅ Соблюдено"
                                                ),
                                                callback_data=(
                                                    f"fasting_day:success:{local_date}"
                                                ),
                                                style=ButtonStyle.SUCCESS,
                                            ),
                                            InlineKeyboardButton(
                                                text=(
                                                    "🔸 Не вийшло"
                                                    if language == "uk"
                                                    else "🔸 Не получилось"
                                                ),
                                                callback_data=(
                                                    f"fasting_day:missed:{local_date}"
                                                ),
                                                style=ButtonStyle.DANGER,
                                            ),
                                        ],
                                        [
                                            InlineKeyboardButton(
                                                text="🕐 Статус",
                                                callback_data="fasting_status",
                                                style=ButtonStyle.PRIMARY,
                                            )
                                        ],
                                    ]
                                ),
                            )

            # Нагадування про вагу та об'єми — не частіше разу на два дні.
            body_prefs = await db.get_reminder_preferences(profile["user_id"])
            if (
                body_prefs.get("body_enabled")
                and current_time == body_prefs.get("body_time", "09:00")
            ):
                latest_weight_text = await db.latest_weight_date(profile["user_id"])
                latest_notice_text = await db.latest_notification_date(
                    profile["user_id"],
                    "body_checkin_2days",
                )
                dates = []
                for value in (latest_weight_text, latest_notice_text):
                    if value:
                        try:
                            dates.append(date.fromisoformat(value))
                        except ValueError:
                            pass
                last_activity = max(dates) if dates else None
                due = (
                    last_activity is None
                    or (local_now.date() - last_activity).days >= 2
                )
                if due:
                    key = "body_checkin_2days"
                    if await db.mark_notification_sent(
                        profile["user_id"],
                        key,
                        local_date,
                    ):
                        text = (
                            "⚖️ Час спокійної перевірки прогресу.\n\n"
                            "Запишіть вагу, а талію, стегна й груди — лише "
                            "за бажанням. Для порівняння краще використовувати "
                            "схожі умови зважування. Один результат не визначає "
                            "прогрес — дивимося на тенденцію за кілька тижнів."
                            if language == "uk" else
                            "⚖️ Время спокойной проверки прогресса.\n\n"
                            "Запишите вес, а талию, бёдра и грудь — только "
                            "по желанию. Для сравнения лучше использовать "
                            "похожие условия взвешивания. Один результат не "
                            "определяет прогресс — смотрим на тенденцию за несколько недель."
                        )
                        await bot.send_message(
                            profile["user_id"],
                            text,
                            reply_markup=body_log_keyboard(language),
                        )
        except Exception:
            logging.exception("Notification error for user_id=%s", profile.get("user_id"))


async def scheduler_loop(bot: Bot) -> None:
    while True:
        try:
            await db.release_expired_bonus_reservations()
            await process_notifications(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Scheduler error")
        await asyncio.sleep(30)


def _create_consistent_sqlite_backup(source_path: str, destination_path: str) -> None:
    """Create and verify a consistent SQLite snapshot while the bot is running."""
    source = Path(source_path)
    if not source.is_file():
        raise FileNotFoundError(f"Database file not found: {source}")

    destination = Path(destination_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    source_connection = sqlite3.connect(source_uri, uri=True, timeout=30)
    destination_connection = sqlite3.connect(destination, timeout=30)
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()

        result = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            raise RuntimeError("SQLite integrity check failed")
    finally:
        destination_connection.close()
        source_connection.close()


@router.message(Command("backup_db"), F.from_user.id == settings.admin_id)
async def backup_database_handler(message: Message) -> None:
    """Send the administrator a verified snapshot of the live database."""
    await message.answer("⏳ Создаю резервную копию базы…")

    timestamp = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d_%H-%M-%S_UTC")
    filename = f"minus_kg_backup_{timestamp}.sqlite3"

    try:
        with tempfile.TemporaryDirectory(prefix="minus_kg_backup_") as temp_dir:
            backup_path = Path(temp_dir) / filename
            await asyncio.to_thread(
                _create_consistent_sqlite_backup,
                settings.database_path,
                str(backup_path),
            )

            size_mb = backup_path.stat().st_size / (1024 * 1024)
            await message.answer_document(
                document=FSInputFile(backup_path, filename=filename),
                caption=(
                    "✅ Резервная копия базы готова.\n"
                    f"Размер: {size_mb:.2f} МБ\n\n"
                    "Сохраните этот файл в безопасном месте. "
                    "Он содержит анкеты и записи пользователей."
                ),
            )
    except Exception:
        logging.exception("Database backup failed")
        await message.answer(
            "❌ Не удалось создать резервную копию. "
            "Откройте Railway → worker → Deployments → View logs "
            "и пришлите последние строки ошибки."
        )


@router.message(Command("stats"), F.from_user.id == settings.admin_id)
async def stats_handler(message: Message) -> None:
    stats = await db.stats(int(time.time()))
    await message.answer(
        "📊 minus_kg\n\n"
        f"Користувачів: {stats['users']}\n"
        f"З активним доступом: {stats['active']}\n"
        f"Платежів: {stats['payments']}\n"
        f"Stars: {stats['stars']}"
    )


@router.message(Command("menu"))
async def menu_command_handler(
    message: Message,
    bot: Bot,
    state: FSMContext,
) -> None:
    if not message.from_user:
        return
    await bot.set_chat_menu_button(
        chat_id=message.chat.id,
        menu_button=MenuButtonDefault(),
    )
    await state.clear()
    profile = await db.get_profile(message.from_user.id)
    if not profile_complete(profile):
        await message.answer("Сначала завершите анкету командой /start.")
        return
    language = profile["language"]

    if not access_active(profile):
        await send_access_expired(message, language)
        return

    await message.answer(
        "🏠 Кнопку меню закріплено."
        if language == "uk"
        else "🏠 Кнопка меню закреплена.",
        reply_markup=persistent_menu_keyboard(language),
    )
    await message.answer(
        "Головне меню:" if language == "uk" else "Главное меню:",
        reply_markup=main_menu(language),
    )



@router.message(Command("partner"))
async def partner_command_handler(
    message: Message,
    bot: Bot,
) -> None:
    if not message.from_user:
        return
    await send_referral_panel(
        message,
        message.from_user.id,
        bot,
    )


@router.message(Command("settings"))
async def settings_command_handler(message: Message) -> None:
    if not message.from_user:
        return
    profile = await db.get_profile(message.from_user.id)
    language = profile.get("language") if profile else "ru"
    await message.answer(
        "⚙️ Налаштування"
        if language == "uk"
        else "⚙️ Настройки",
        reply_markup=settings_keyboard(language),
    )


@router.message(Command("reset_profile"))
async def reset_profile_handler(message: Message, state: FSMContext) -> None:
    await begin_onboarding(message, state)


async def setup_commands(bot: Bot) -> None:
    # Use only the large persistent reply button under the input field.
    await bot.delete_my_commands()
    await bot.set_chat_menu_button(
        menu_button=MenuButtonDefault()
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    await db.init()
    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    await setup_commands(bot)
    scheduler = asyncio.create_task(scheduler_loop(bot))
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        scheduler.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
