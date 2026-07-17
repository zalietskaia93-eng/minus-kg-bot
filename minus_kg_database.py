from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite


class MinusKgDatabase:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS profiles (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    telegram_first_name TEXT,
                    language TEXT,
                    age_confirmed INTEGER NOT NULL DEFAULT 0,
                    display_name TEXT,
                    sex TEXT,
                    birth_date TEXT,
                    height_cm REAL,
                    start_weight_kg REAL,
                    current_weight_kg REAL,
                    target_weight_kg REAL,
                    last_target_period TEXT,
                    activity TEXT,
                    sport_mode TEXT,
                    wake_time TEXT,
                    sleep_time TEXT,
                    meals_count INTEGER,
                    timezone TEXT,
                    safety_restricted INTEGER NOT NULL DEFAULT 0,
                    bmr REAL,
                    tdee REAL,
                    calorie_target INTEGER,
                    protein_g INTEGER,
                    fat_g INTEGER,
                    carbs_g INTEGER,
                    trial_expires_at INTEGER,
                    subscription_expires_at INTEGER,
                    plan_code TEXT,
                    fasting_mode TEXT,
                    fasting_start TEXT,
                    fasting_end TEXT,
                    registered_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS food_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    description TEXT NOT NULL,
                    calories_min REAL NOT NULL,
                    calories_max REAL NOT NULL,
                    protein_min REAL NOT NULL,
                    protein_max REAL NOT NULL,
                    fat_min REAL NOT NULL,
                    fat_max REAL NOT NULL,
                    carbs_min REAL NOT NULL,
                    carbs_max REAL NOT NULL,
                    logged_at INTEGER NOT NULL,
                    local_date TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_food_logs_user_date
                    ON food_logs(user_id, local_date);
                CREATE TABLE IF NOT EXISTS drink_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    drink_code TEXT NOT NULL,
                    drink_name TEXT NOT NULL,
                    volume_ml INTEGER NOT NULL,
                    calories REAL NOT NULL DEFAULT 0,
                    protein REAL NOT NULL DEFAULT 0,
                    fat REAL NOT NULL DEFAULT 0,
                    carbs REAL NOT NULL DEFAULT 0,
                    counts_as_water INTEGER NOT NULL DEFAULT 0,
                    logged_at INTEGER NOT NULL,
                    local_date TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_drink_logs_user_date
                    ON drink_logs(user_id, local_date, logged_at);
                CREATE TABLE IF NOT EXISTS photo_analyses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    file_unique_id TEXT,
                    summary TEXT NOT NULL,
                    calories_min REAL NOT NULL,
                    calories_max REAL NOT NULL,
                    protein_min REAL NOT NULL,
                    protein_max REAL NOT NULL,
                    fat_min REAL NOT NULL,
                    fat_max REAL NOT NULL,
                    carbs_min REAL NOT NULL,
                    carbs_max REAL NOT NULL,
                    created_at INTEGER NOT NULL,
                    local_date TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_photo_analyses_user
                    ON photo_analyses(user_id);
                CREATE TABLE IF NOT EXISTS ai_chat_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    local_date TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ai_chat_logs_user_date
                    ON ai_chat_logs(user_id, local_date, created_at);
                CREATE TABLE IF NOT EXISTS weight_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    weight_kg REAL NOT NULL,
                    logged_at INTEGER NOT NULL,
                    local_date TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS body_measurements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    weight_kg REAL NOT NULL,
                    waist_cm REAL,
                    hips_cm REAL,
                    chest_cm REAL,
                    logged_at INTEGER NOT NULL,
                    local_date TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_body_measurements_user_date
                    ON body_measurements(user_id, local_date, logged_at);
                CREATE TABLE IF NOT EXISTS meal_schedule (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    slot_number INTEGER NOT NULL,
                    meal_name TEXT NOT NULL,
                    meal_time TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(user_id, slot_number)
                );
                CREATE TABLE IF NOT EXISTS sent_notifications (
                    user_id INTEGER NOT NULL,
                    notification_key TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    sent_at INTEGER NOT NULL,
                    PRIMARY KEY(user_id, notification_key, local_date)
                );
                CREATE TABLE IF NOT EXISTS reminder_preferences (
                    user_id INTEGER PRIMARY KEY,
                    body_enabled INTEGER NOT NULL DEFAULT 1,
                    body_time TEXT NOT NULL DEFAULT '09:00',
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reminder_snoozes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    slot_number INTEGER NOT NULL,
                    meal_name TEXT NOT NULL,
                    due_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    sent_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_reminder_snoozes_due
                    ON reminder_snoozes(user_id, sent_at, due_at);
                CREATE TABLE IF NOT EXISTS payments (
                    charge_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    plan_code TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS menu_sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    products_text TEXT NOT NULL,
                    menu_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    local_date TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_menu_sessions_user_created
                    ON menu_sessions(user_id, created_at);
                CREATE TABLE IF NOT EXISTS recipe_choices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    recipe_index INTEGER NOT NULL,
                    recipe_title TEXT NOT NULL,
                    recipe_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'planned',
                    chosen_at INTEGER NOT NULL,
                    eaten_at INTEGER,
                    local_date TEXT NOT NULL,
                    UNIQUE(user_id, session_id, recipe_index)
                );
                CREATE INDEX IF NOT EXISTS idx_recipe_choices_user_date
                    ON recipe_choices(user_id, local_date, status);
                CREATE TABLE IF NOT EXISTS coach_applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_number INTEGER UNIQUE,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new',
                    focus TEXT NOT NULL,
                    support_format TEXT NOT NULL,
                    sport_preference TEXT NOT NULL,
                    food_notes TEXT NOT NULL,
                    exclusions TEXT NOT NULL DEFAULT '',
                    contact_time TEXT NOT NULL,
                    comment TEXT,
                    cancelled_by TEXT,
                    cancellation_reason TEXT,
                    cancelled_at INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_coach_applications_user_created
                    ON coach_applications(user_id, created_at);
                CREATE TABLE IF NOT EXISTS fasting_daily_logs (
                    user_id INTEGER NOT NULL,
                    local_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(user_id, local_date)
                );
                CREATE INDEX IF NOT EXISTS idx_fasting_daily_logs_user_date
                    ON fasting_daily_logs(user_id, local_date);
                CREATE TABLE IF NOT EXISTS referrals (
                    invited_user_id INTEGER PRIMARY KEY,
                    inviter_user_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    qualified_at INTEGER,
                    free_day_awarded_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_referrals_inviter
                    ON referrals(inviter_user_id, qualified_at);

                CREATE TABLE IF NOT EXISTS bonus_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    reference TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_bonus_ledger_user
                    ON bonus_ledger(user_id, created_at);

                CREATE TABLE IF NOT EXISTS pending_invoices (
                    invoice_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    plan_code TEXT NOT NULL,
                    original_amount INTEGER NOT NULL,
                    bonus_used INTEGER NOT NULL DEFAULT 0,
                    payable_amount INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    telegram_charge_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_pending_invoices_user
                    ON pending_invoices(user_id, status, expires_at);

                CREATE TABLE IF NOT EXISTS referral_rewards (
                    source_charge_id TEXT PRIMARY KEY,
                    inviter_user_id INTEGER NOT NULL,
                    invited_user_id INTEGER NOT NULL,
                    paid_amount INTEGER NOT NULL,
                    bonus_amount INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_referral_rewards_inviter
                    ON referral_rewards(inviter_user_id, created_at);
                """
            )

            # Migration for databases created before the dedicated
            # «what to exclude» field was added.
            columns_cursor = await db.execute(
                "PRAGMA table_info(coach_applications)"
            )
            coach_columns = {
                row[1]
                for row in await columns_cursor.fetchall()
            }
            if "exclusions" not in coach_columns:
                await db.execute(
                    '''
                    ALTER TABLE coach_applications
                    ADD COLUMN exclusions TEXT NOT NULL DEFAULT ''
                    '''
                )

            if "public_number" not in coach_columns:
                await db.execute(
                    '''
                    ALTER TABLE coach_applications
                    ADD COLUMN public_number INTEGER
                    '''
                )
            if "cancelled_by" not in coach_columns:
                await db.execute(
                    '''
                    ALTER TABLE coach_applications
                    ADD COLUMN cancelled_by TEXT
                    '''
                )
            if "cancellation_reason" not in coach_columns:
                await db.execute(
                    '''
                    ALTER TABLE coach_applications
                    ADD COLUMN cancellation_reason TEXT
                    '''
                )
            if "cancelled_at" not in coach_columns:
                await db.execute(
                    '''
                    ALTER TABLE coach_applications
                    ADD COLUMN cancelled_at INTEGER
                    '''
                )

            await db.execute(
                '''
                CREATE UNIQUE INDEX IF NOT EXISTS
                idx_coach_applications_public_number
                ON coach_applications(public_number)
                WHERE public_number IS NOT NULL
                '''
            )

            await db.commit()

    async def touch_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO profiles (user_id, username, telegram_first_name, registered_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    telegram_first_name = excluded.telegram_first_name,
                    updated_at = excluded.updated_at
                """,
                (user_id, username, first_name, now, now),
            )
            await db.commit()

    async def get_profile(self, user_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def save_profile(self, user_id: int, values: dict[str, Any]) -> None:
        allowed = {
            "language", "age_confirmed", "display_name", "sex", "birth_date",
            "height_cm", "start_weight_kg", "current_weight_kg", "target_weight_kg",
            "last_target_period", "activity", "sport_mode", "wake_time", "sleep_time",
            "meals_count", "timezone", "safety_restricted", "bmr", "tdee",
            "calorie_target", "protein_g", "fat_g", "carbs_g", "trial_expires_at",
            "subscription_expires_at", "plan_code", "fasting_mode", "fasting_start",
            "fasting_end",
        }
        clean = {k: v for k, v in values.items() if k in allowed}
        if not clean:
            return
        clean["updated_at"] = int(time.time())
        sql = ", ".join(f"{k} = ?" for k in clean)
        params = list(clean.values()) + [user_id]
        async with aiosqlite.connect(self.path) as db:
            await db.execute(f"UPDATE profiles SET {sql} WHERE user_id = ?", params)
            await db.commit()

    async def set_meal_schedule(self, user_id: int, schedule: list[tuple[int, str, str]]) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM meal_schedule WHERE user_id = ?", (user_id,))
            await db.executemany(
                "INSERT INTO meal_schedule (user_id, slot_number, meal_name, meal_time) VALUES (?, ?, ?, ?)",
                [(user_id, n, name, meal_time) for n, name, meal_time in schedule],
            )
            await db.commit()


    async def update_meal_names(
        self,
        user_id: int,
        names: list[str],
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            for slot_number, name in enumerate(names, start=1):
                await db.execute(
                    """
                    UPDATE meal_schedule
                    SET meal_name = ?
                    WHERE user_id = ? AND slot_number = ?
                    """,
                    (name, user_id, slot_number),
                )
            await db.commit()

    async def get_meal_schedule(self, user_id: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM meal_schedule WHERE user_id = ? AND enabled = 1 ORDER BY slot_number",
                (user_id,),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def get_all_meal_schedule(self, user_id: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM meal_schedule WHERE user_id = ? ORDER BY slot_number",
                (user_id,),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def get_meal_slot(
        self,
        user_id: int,
        slot_number: int,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM meal_schedule WHERE user_id = ? AND slot_number = ?",
                (user_id, slot_number),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_meal_time(
        self,
        user_id: int,
        slot_number: int,
        meal_time: str,
    ) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "UPDATE meal_schedule SET meal_time = ? WHERE user_id = ? AND slot_number = ?",
                (meal_time, user_id, slot_number),
            )
            await db.commit()
            return cur.rowcount > 0

    async def toggle_meal_reminder(
        self,
        user_id: int,
        slot_number: int,
    ) -> bool | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT enabled FROM meal_schedule WHERE user_id = ? AND slot_number = ?",
                (user_id, slot_number),
            )
            row = await cur.fetchone()
            if not row:
                return None
            enabled = 0 if int(row[0]) else 1
            await db.execute(
                "UPDATE meal_schedule SET enabled = ? WHERE user_id = ? AND slot_number = ?",
                (enabled, user_id, slot_number),
            )
            await db.commit()
            return bool(enabled)

    async def get_reminder_preferences(self, user_id: int) -> dict[str, Any]:
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO reminder_preferences (
                    user_id, body_enabled, body_time, updated_at
                ) VALUES (?, 1, '09:00', ?)
                """,
                (user_id, now),
            )
            await db.commit()
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM reminder_preferences WHERE user_id = ?",
                (user_id,),
            )
            row = await cur.fetchone()
            return dict(row)

    async def toggle_body_reminder(self, user_id: int) -> bool:
        prefs = await self.get_reminder_preferences(user_id)
        enabled = 0 if int(prefs.get("body_enabled", 1)) else 1
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE reminder_preferences SET body_enabled = ?, updated_at = ? WHERE user_id = ?",
                (enabled, int(time.time()), user_id),
            )
            await db.commit()
        return bool(enabled)

    async def update_body_reminder_time(
        self,
        user_id: int,
        body_time: str,
    ) -> None:
        await self.get_reminder_preferences(user_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE reminder_preferences SET body_time = ?, updated_at = ? WHERE user_id = ?",
                (body_time, int(time.time()), user_id),
            )
            await db.commit()

    async def create_meal_snooze(
        self,
        user_id: int,
        slot_number: int,
        meal_name: str,
        due_at: int,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO reminder_snoozes (
                    user_id, slot_number, meal_name, due_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, slot_number, meal_name, due_at, int(time.time())),
            )
            await db.commit()

    async def due_meal_snoozes(
        self,
        user_id: int,
        now_ts: int,
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT * FROM reminder_snoozes
                WHERE user_id = ? AND sent_at IS NULL AND due_at <= ?
                ORDER BY due_at
                LIMIT 10
                """,
                (user_id, now_ts),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def mark_meal_snooze_sent(self, snooze_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE reminder_snoozes SET sent_at = ? WHERE id = ? AND sent_at IS NULL",
                (int(time.time()), snooze_id),
            )
            await db.commit()

    async def latest_notification_date(
        self,
        user_id: int,
        notification_key: str,
    ) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                SELECT MAX(local_date) FROM sent_notifications
                WHERE user_id = ? AND notification_key = ?
                """,
                (user_id, notification_key),
            )
            row = await cur.fetchone()
            return row[0] if row and row[0] else None

    async def add_weight(self, user_id: int, weight_kg: float, local_date: str) -> None:
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO weight_logs (user_id, weight_kg, logged_at, local_date) VALUES (?, ?, ?, ?)",
                (user_id, weight_kg, now, local_date),
            )
            await db.execute(
                "UPDATE profiles SET current_weight_kg = ?, updated_at = ? WHERE user_id = ?",
                (weight_kg, now, user_id),
            )
            await db.commit()

    async def recent_weights(self, user_id: int, limit: int = 14) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM weight_logs WHERE user_id = ? ORDER BY logged_at DESC LIMIT ?",
                (user_id, limit),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def add_body_measurement(
        self,
        user_id: int,
        weight_kg: float,
        waist_cm: float | None,
        hips_cm: float | None,
        chest_cm: float | None,
        local_date: str,
    ) -> None:
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN")
            await db.execute(
                """
                INSERT INTO body_measurements (
                    user_id, weight_kg, waist_cm, hips_cm, chest_cm,
                    logged_at, local_date
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, weight_kg, waist_cm, hips_cm, chest_cm,
                    now, local_date,
                ),
            )
            await db.execute(
                """
                INSERT INTO weight_logs (user_id, weight_kg, logged_at, local_date)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, weight_kg, now, local_date),
            )
            await db.execute(
                """
                UPDATE profiles
                SET current_weight_kg = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (weight_kg, now, user_id),
            )
            await db.commit()

    async def recent_body_measurements(
        self,
        user_id: int,
        limit: int = 14,
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT weight_kg, waist_cm, hips_cm, chest_cm,
                       logged_at, local_date
                FROM body_measurements
                WHERE user_id = ?
                ORDER BY logged_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            if rows:
                return rows

            # Для старих профілів показуємо попередні записи ваги.
            cursor = await db.execute(
                """
                SELECT weight_kg, NULL AS waist_cm, NULL AS hips_cm,
                       NULL AS chest_cm, logged_at, local_date
                FROM weight_logs
                WHERE user_id = ?
                ORDER BY logged_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def latest_weight_date(self, user_id: int) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT local_date
                FROM weight_logs
                WHERE user_id = ?
                ORDER BY logged_at DESC
                LIMIT 1
                """,
                (user_id,),
            )
            row = await cursor.fetchone()
            return str(row[0]) if row else None

    async def add_food_log(
        self,
        user_id: int,
        description: str,
        calories_min: float,
        calories_max: float,
        protein_min: float,
        protein_max: float,
        fat_min: float,
        fat_max: float,
        carbs_min: float,
        carbs_max: float,
        local_date: str,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO food_logs (
                    user_id, description,
                    calories_min, calories_max,
                    protein_min, protein_max,
                    fat_min, fat_max,
                    carbs_min, carbs_max,
                    logged_at, local_date
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, description,
                    calories_min, calories_max,
                    protein_min, protein_max,
                    fat_min, fat_max,
                    carbs_min, carbs_max,
                    int(time.time()), local_date,
                ),
            )
            await db.commit()

    async def add_drink_log(
        self,
        user_id: int,
        drink_code: str,
        drink_name: str,
        volume_ml: int,
        calories: float,
        protein: float,
        fat: float,
        carbs: float,
        counts_as_water: bool,
        local_date: str,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO drink_logs (
                    user_id, drink_code, drink_name, volume_ml,
                    calories, protein, fat, carbs, counts_as_water,
                    logged_at, local_date
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    drink_code,
                    drink_name,
                    int(volume_ml),
                    max(0.0, float(calories)),
                    max(0.0, float(protein)),
                    max(0.0, float(fat)),
                    max(0.0, float(carbs)),
                    1 if counts_as_water else 0,
                    int(time.time()),
                    local_date,
                ),
            )
            await db.commit()

    async def daily_drink_totals(
        self,
        user_id: int,
        local_date: str,
    ) -> dict[str, float]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT
                    COALESCE(SUM(volume_ml), 0),
                    COALESCE(SUM(CASE WHEN counts_as_water = 1 THEN volume_ml ELSE 0 END), 0),
                    COALESCE(SUM(calories), 0),
                    COALESCE(SUM(protein), 0),
                    COALESCE(SUM(fat), 0),
                    COALESCE(SUM(carbs), 0),
                    COUNT(*)
                FROM drink_logs
                WHERE user_id = ? AND local_date = ?
                """,
                (user_id, local_date),
            )
            row = await cursor.fetchone()
            keys = (
                "fluid_ml", "water_ml", "drink_calories",
                "drink_protein", "drink_fat", "drink_carbs",
                "drink_count",
            )
            return {key: float(value or 0) for key, value in zip(keys, row)}

    async def daily_food_totals(self, user_id: int, local_date: str) -> dict[str, float]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT
                    COALESCE(SUM(calories_min), 0),
                    COALESCE(SUM(calories_max), 0),
                    COALESCE(SUM(protein_min), 0),
                    COALESCE(SUM(protein_max), 0),
                    COALESCE(SUM(fat_min), 0),
                    COALESCE(SUM(fat_max), 0),
                    COALESCE(SUM(carbs_min), 0),
                    COALESCE(SUM(carbs_max), 0),
                    COUNT(*)
                FROM food_logs
                WHERE user_id = ? AND local_date = ?
                """,
                (user_id, local_date),
            )
            row = await cursor.fetchone()
            keys = (
                "calories_min", "calories_max",
                "protein_min", "protein_max",
                "fat_min", "fat_max",
                "carbs_min", "carbs_max",
                "food_count",
            )
            totals = {
                key: float(value or 0)
                for key, value in zip(keys, row)
            }

            cursor = await db.execute(
                """
                SELECT
                    COALESCE(SUM(volume_ml), 0),
                    COALESCE(SUM(CASE WHEN counts_as_water = 1 THEN volume_ml ELSE 0 END), 0),
                    COALESCE(SUM(calories), 0),
                    COALESCE(SUM(protein), 0),
                    COALESCE(SUM(fat), 0),
                    COALESCE(SUM(carbs), 0),
                    COUNT(*)
                FROM drink_logs
                WHERE user_id = ? AND local_date = ?
                """,
                (user_id, local_date),
            )
            drink_row = await cursor.fetchone()
            fluid_ml, water_ml, calories, protein, fat, carbs, count = [
                float(value or 0) for value in drink_row
            ]
            totals["calories_min"] += calories
            totals["calories_max"] += calories
            totals["protein_min"] += protein
            totals["protein_max"] += protein
            totals["fat_min"] += fat
            totals["fat_max"] += fat
            totals["carbs_min"] += carbs
            totals["carbs_max"] += carbs
            totals.update(
                {
                    "fluid_ml": fluid_ml,
                    "water_ml": water_ml,
                    "drink_calories": calories,
                    "drink_count": count,
                }
            )
            return totals

    async def photo_analysis_count(self, user_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM photo_analyses WHERE user_id = ?",
                (user_id,),
            )
            return int((await cursor.fetchone())[0])

    async def record_photo_analysis(
        self,
        user_id: int,
        file_unique_id: str | None,
        summary: str,
        calories_min: float,
        calories_max: float,
        protein_min: float,
        protein_max: float,
        fat_min: float,
        fat_max: float,
        carbs_min: float,
        carbs_max: float,
        local_date: str,
    ) -> None:
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN")
            await db.execute(
                """
                INSERT INTO photo_analyses (
                    user_id, file_unique_id, summary,
                    calories_min, calories_max,
                    protein_min, protein_max,
                    fat_min, fat_max,
                    carbs_min, carbs_max,
                    created_at, local_date
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, file_unique_id, summary,
                    calories_min, calories_max,
                    protein_min, protein_max,
                    fat_min, fat_max,
                    carbs_min, carbs_max,
                    now, local_date,
                ),
            )
            await db.execute(
                """
                INSERT INTO food_logs (
                    user_id, description,
                    calories_min, calories_max,
                    protein_min, protein_max,
                    fat_min, fat_max,
                    carbs_min, carbs_max,
                    logged_at, local_date
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, f"Фото: {summary}",
                    calories_min, calories_max,
                    protein_min, protein_max,
                    fat_min, fat_max,
                    carbs_min, carbs_max,
                    now, local_date,
                ),
            )
            await db.commit()

    async def ai_questions_today(self, user_id: int, local_date: str) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM ai_chat_logs
                WHERE user_id = ? AND local_date = ? AND role = 'user'
                """,
                (user_id, local_date),
            )
            return int((await cursor.fetchone())[0])

    async def recent_ai_messages(
        self,
        user_id: int,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT role, content, created_at
                FROM ai_chat_logs
                WHERE user_id = ?
                  AND role IN ('user', 'assistant')
                  AND id > COALESCE(
                      (
                          SELECT MAX(id)
                          FROM ai_chat_logs
                          WHERE user_id = ?
                            AND role = 'reset'
                      ),
                      0
                  )
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, user_id, limit),
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            rows.reverse()
            return rows

    async def clear_ai_history(self, user_id: int) -> None:
        """Start a fresh AI conversation without resetting today's quota."""
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO ai_chat_logs (
                    user_id,
                    role,
                    content,
                    created_at,
                    local_date
                )
                VALUES (?, 'reset', '', ?, ?)
                """,
                (
                    user_id,
                    now,
                    time.strftime("%Y-%m-%d"),
                ),
            )
            await db.commit()

    async def add_ai_exchange(
        self,
        user_id: int,
        question: str,
        answer: str,
        local_date: str,
    ) -> None:
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN")
            await db.execute(
                """
                INSERT INTO ai_chat_logs (
                    user_id, role, content, created_at, local_date
                )
                VALUES (?, 'user', ?, ?, ?)
                """,
                (user_id, question[:2500], now, local_date),
            )
            await db.execute(
                """
                INSERT INTO ai_chat_logs (
                    user_id, role, content, created_at, local_date
                )
                VALUES (?, 'assistant', ?, ?, ?)
                """,
                (user_id, answer[:4096], now + 1, local_date),
            )
            await db.commit()


    async def save_menu_session(
        self,
        session_id: str,
        user_id: int,
        products_text: str,
        menu_data: dict[str, Any],
        local_date: str,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO menu_sessions (
                    session_id, user_id, products_text,
                    menu_json, created_at, local_date
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    user_id,
                    products_text[:3000],
                    json.dumps(menu_data, ensure_ascii=False),
                    int(time.time()),
                    local_date,
                ),
            )
            await db.commit()


    async def get_latest_menu_session_by_products(
        self,
        user_id: int,
        products_text: str,
        local_date: str,
        max_age_seconds: int = 21600,
    ) -> dict[str, Any] | None:
        cutoff = int(time.time()) - max(60, max_age_seconds)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM menu_sessions
                WHERE user_id = ?
                  AND products_text = ?
                  AND local_date = ?
                  AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id, products_text[:3000], local_date, cutoff),
            )
            row = await cursor.fetchone()
            if not row:
                return None

            result = dict(row)
            try:
                result["menu_data"] = json.loads(result.pop("menu_json"))
            except (json.JSONDecodeError, TypeError):
                return None
            return result

    async def get_menu_session(
        self,
        session_id: str,
        user_id: int,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM menu_sessions
                WHERE session_id = ? AND user_id = ?
                """,
                (session_id, user_id),
            )
            row = await cursor.fetchone()
            if not row:
                return None

            result = dict(row)
            try:
                result["menu_data"] = json.loads(result.pop("menu_json"))
            except (json.JSONDecodeError, TypeError):
                return None
            return result



    async def save_recipe_choice(
        self,
        *,
        user_id: int,
        session_id: str,
        recipe_index: int,
        recipe: dict[str, Any],
        local_date: str,
    ) -> int:
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO recipe_choices (
                    user_id, session_id, recipe_index,
                    recipe_title, recipe_json, status,
                    chosen_at, local_date
                )
                VALUES (?, ?, ?, ?, ?, 'planned', ?, ?)
                ON CONFLICT(user_id, session_id, recipe_index)
                DO UPDATE SET
                    recipe_title = excluded.recipe_title,
                    recipe_json = excluded.recipe_json,
                    chosen_at = excluded.chosen_at,
                    local_date = excluded.local_date
                """,
                (
                    user_id,
                    session_id,
                    recipe_index,
                    str(recipe.get("title") or "Рецепт")[:300],
                    json.dumps(recipe, ensure_ascii=False),
                    now,
                    local_date,
                ),
            )
            await db.commit()
            row = await (
                await db.execute(
                    """
                    SELECT id
                    FROM recipe_choices
                    WHERE user_id = ?
                      AND session_id = ?
                      AND recipe_index = ?
                    """,
                    (user_id, session_id, recipe_index),
                )
            ).fetchone()
            return int(row[0])

    async def mark_recipe_choice_eaten(
        self,
        *,
        user_id: int,
        session_id: str,
        recipe_index: int,
        local_date: str,
    ) -> dict[str, Any] | None:
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")

            cursor = await db.execute(
                """
                SELECT *
                FROM recipe_choices
                WHERE user_id = ?
                  AND session_id = ?
                  AND recipe_index = ?
                """,
                (user_id, session_id, recipe_index),
            )
            row = await cursor.fetchone()
            if not row:
                await db.rollback()
                return None

            result = dict(row)
            try:
                recipe = json.loads(result["recipe_json"])
            except (json.JSONDecodeError, TypeError):
                await db.rollback()
                return None

            if result["status"] == "eaten":
                await db.rollback()
                result["recipe"] = recipe
                result["already_eaten"] = True
                return result

            calories = max(0.0, float(recipe.get("calories") or 0))
            protein = max(0.0, float(recipe.get("protein") or 0))
            fat = max(0.0, float(recipe.get("fat") or 0))
            carbs = max(0.0, float(recipe.get("carbs") or 0))

            await db.execute(
                """
                INSERT INTO food_logs (
                    user_id, description,
                    calories_min, calories_max,
                    protein_min, protein_max,
                    fat_min, fat_max,
                    carbs_min, carbs_max,
                    logged_at, local_date
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    f"Рецепт: {recipe.get('title') or 'Блюдо'}",
                    calories,
                    calories,
                    protein,
                    protein,
                    fat,
                    fat,
                    carbs,
                    carbs,
                    now,
                    local_date,
                ),
            )
            await db.execute(
                """
                UPDATE recipe_choices
                SET status = 'eaten',
                    eaten_at = ?,
                    local_date = ?
                WHERE id = ?
                """,
                (now, local_date, int(row["id"])),
            )
            await db.commit()

            result["recipe"] = recipe
            result["already_eaten"] = False
            result["status"] = "eaten"
            result["eaten_at"] = now
            return result

    async def create_coach_application(
        self,
        *,
        user_id: int,
        focus: str,
        support_format: str,
        sport_preference: str,
        food_notes: str,
        exclusions: str,
        contact_time: str,
        comment: str,
    ) -> dict[str, int]:
        """
        Create an application with a separate public number.

        Public numbering starts at 1022 and increases:
        1022, 1023, 1024...
        Existing historical applications remain unchanged.
        """
        now = int(time.time())

        async with aiosqlite.connect(self.path) as db:
            # This lock prevents duplicate numbers if two applications
            # are sent at nearly the same moment.
            await db.execute("BEGIN IMMEDIATE")

            cursor = await db.execute(
                """
                SELECT MAX(public_number)
                FROM coach_applications
                WHERE public_number IS NOT NULL
                """
            )
            row = await cursor.fetchone()
            last_public_number = (
                int(row[0])
                if row and row[0] is not None
                else 1021
            )
            public_number = max(
                1022,
                last_public_number + 1,
            )

            cursor = await db.execute(
                """
                INSERT INTO coach_applications (
                    public_number, user_id, status,
                    focus, support_format, sport_preference,
                    food_notes, exclusions, contact_time,
                    comment, created_at, updated_at
                )
                VALUES (?, ?, 'new', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    public_number,
                    user_id,
                    focus,
                    support_format,
                    sport_preference,
                    food_notes[:1000],
                    exclusions[:1000],
                    contact_time,
                    comment[:1500],
                    now,
                    now,
                ),
            )
            internal_id = int(cursor.lastrowid)
            await db.commit()

            return {
                "id": internal_id,
                "public_number": public_number,
            }

    async def latest_coach_application(
        self,
        user_id: int,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM coach_applications
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_coach_application(
        self,
        application_id: int,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM coach_applications WHERE id = ?",
                (application_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def cancel_coach_application(
        self,
        application_id: int,
        *,
        cancelled_by: str,
        reason: str,
    ) -> bool:
        if cancelled_by not in {"manager", "client"}:
            return False

        clean_reason = reason.strip()[:1000]
        if len(clean_reason) < 3:
            return False

        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE coach_applications
                SET status = 'cancelled',
                    cancelled_by = ?,
                    cancellation_reason = ?,
                    cancelled_at = ?,
                    updated_at = ?
                WHERE id = ?
                  AND status IN ('new', 'in_progress')
                """,
                (
                    cancelled_by,
                    clean_reason,
                    now,
                    now,
                    application_id,
                ),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def update_coach_application_status(
        self,
        application_id: int,
        status: str,
    ) -> bool:
        allowed = {"new", "in_progress", "closed", "cancelled"}
        if status not in allowed:
            return False
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE coach_applications
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, int(time.time()), application_id),
            )
            await db.commit()
            return cursor.rowcount > 0


    async def set_fasting_day_status(
        self,
        user_id: int,
        local_date: str,
        status: str,
    ) -> None:
        if status not in {"success", "missed"}:
            raise ValueError("Unsupported fasting status")
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO fasting_daily_logs (
                    user_id, local_date, status, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, local_date) DO UPDATE SET
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (user_id, local_date, status, int(time.time())),
            )
            await db.commit()

    async def calendar_month_data(
        self,
        user_id: int,
        start_date: str,
        end_date: str,
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}

        def item(day: str) -> dict[str, Any]:
            return result.setdefault(
                day,
                {
                    "local_date": day,
                    "weight_kg": None,
                    "waist_cm": None,
                    "hips_cm": None,
                    "chest_cm": None,
                    "calories_min": 0.0,
                    "calories_max": 0.0,
                    "protein_min": 0.0,
                    "protein_max": 0.0,
                    "fat_min": 0.0,
                    "fat_max": 0.0,
                    "carbs_min": 0.0,
                    "carbs_max": 0.0,
                    "food_count": 0,
                    "drink_count": 0,
                    "fluid_ml": 0.0,
                    "water_ml": 0.0,
                    "drink_calories": 0.0,
                    "fasting_status": None,
                },
            )

        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute(
                """
                SELECT local_date, weight_kg, waist_cm, hips_cm, chest_cm,
                       logged_at
                FROM body_measurements
                WHERE user_id = ? AND local_date BETWEEN ? AND ?
                ORDER BY local_date, logged_at
                """,
                (user_id, start_date, end_date),
            )
            for row in await cursor.fetchall():
                target = item(row["local_date"])
                target.update(
                    {
                        "weight_kg": float(row["weight_kg"]),
                        "waist_cm": (
                            float(row["waist_cm"])
                            if row["waist_cm"] is not None else None
                        ),
                        "hips_cm": (
                            float(row["hips_cm"])
                            if row["hips_cm"] is not None else None
                        ),
                        "chest_cm": (
                            float(row["chest_cm"])
                            if row["chest_cm"] is not None else None
                        ),
                    }
                )

            cursor = await db.execute(
                """
                SELECT local_date, weight_kg, logged_at
                FROM weight_logs
                WHERE user_id = ? AND local_date BETWEEN ? AND ?
                ORDER BY local_date, logged_at
                """,
                (user_id, start_date, end_date),
            )
            for row in await cursor.fetchall():
                target = item(row["local_date"])
                target["weight_kg"] = float(row["weight_kg"])

            cursor = await db.execute(
                """
                SELECT local_date,
                       COALESCE(SUM(calories_min), 0) AS calories_min,
                       COALESCE(SUM(calories_max), 0) AS calories_max,
                       COALESCE(SUM(protein_min), 0) AS protein_min,
                       COALESCE(SUM(protein_max), 0) AS protein_max,
                       COALESCE(SUM(fat_min), 0) AS fat_min,
                       COALESCE(SUM(fat_max), 0) AS fat_max,
                       COALESCE(SUM(carbs_min), 0) AS carbs_min,
                       COALESCE(SUM(carbs_max), 0) AS carbs_max,
                       COUNT(*) AS food_count
                FROM food_logs
                WHERE user_id = ? AND local_date BETWEEN ? AND ?
                GROUP BY local_date
                """,
                (user_id, start_date, end_date),
            )
            for row in await cursor.fetchall():
                target = item(row["local_date"])
                for key in (
                    "calories_min", "calories_max",
                    "protein_min", "protein_max",
                    "fat_min", "fat_max",
                    "carbs_min", "carbs_max",
                ):
                    target[key] = float(row[key] or 0)
                target["food_count"] = int(row["food_count"] or 0)

            cursor = await db.execute(
                """
                SELECT local_date,
                       COALESCE(SUM(volume_ml), 0) AS fluid_ml,
                       COALESCE(SUM(CASE WHEN counts_as_water = 1 THEN volume_ml ELSE 0 END), 0) AS water_ml,
                       COALESCE(SUM(calories), 0) AS calories,
                       COALESCE(SUM(protein), 0) AS protein,
                       COALESCE(SUM(fat), 0) AS fat,
                       COALESCE(SUM(carbs), 0) AS carbs,
                       COUNT(*) AS drink_count
                FROM drink_logs
                WHERE user_id = ? AND local_date BETWEEN ? AND ?
                GROUP BY local_date
                """,
                (user_id, start_date, end_date),
            )
            for row in await cursor.fetchall():
                target = item(row["local_date"])
                calories = float(row["calories"] or 0)
                protein = float(row["protein"] or 0)
                fat = float(row["fat"] or 0)
                carbs = float(row["carbs"] or 0)
                target["calories_min"] += calories
                target["calories_max"] += calories
                target["protein_min"] += protein
                target["protein_max"] += protein
                target["fat_min"] += fat
                target["fat_max"] += fat
                target["carbs_min"] += carbs
                target["carbs_max"] += carbs
                target["drink_count"] = int(row["drink_count"] or 0)
                target["fluid_ml"] = float(row["fluid_ml"] or 0)
                target["water_ml"] = float(row["water_ml"] or 0)
                target["drink_calories"] = calories

            cursor = await db.execute(
                """
                SELECT local_date, status
                FROM fasting_daily_logs
                WHERE user_id = ? AND local_date BETWEEN ? AND ?
                """,
                (user_id, start_date, end_date),
            )
            for row in await cursor.fetchall():
                item(row["local_date"])["fasting_status"] = row["status"]

        return result

    async def calendar_day_details(
        self,
        user_id: int,
        local_date: str,
    ) -> dict[str, Any]:
        month_data = await self.calendar_month_data(
            user_id,
            local_date,
            local_date,
        )
        result = month_data.get(
            local_date,
            {
                "local_date": local_date,
                "weight_kg": None,
                "waist_cm": None,
                "hips_cm": None,
                "chest_cm": None,
                "calories_min": 0.0,
                "calories_max": 0.0,
                "protein_min": 0.0,
                "protein_max": 0.0,
                "fat_min": 0.0,
                "fat_max": 0.0,
                "carbs_min": 0.0,
                "carbs_max": 0.0,
                "food_count": 0,
                "drink_count": 0,
                "fluid_ml": 0.0,
                "water_ml": 0.0,
                "drink_calories": 0.0,
                "fasting_status": None,
            },
        )

        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT description, calories_min, calories_max,
                       protein_min, protein_max, fat_min, fat_max,
                       carbs_min, carbs_max, logged_at
                FROM food_logs
                WHERE user_id = ? AND local_date = ?
                ORDER BY logged_at
                LIMIT 20
                """,
                (user_id, local_date),
            )
            result["foods"] = [dict(row) for row in await cursor.fetchall()]

            cursor = await db.execute(
                """
                SELECT drink_code, drink_name, volume_ml, calories,
                       protein, fat, carbs, counts_as_water, logged_at
                FROM drink_logs
                WHERE user_id = ? AND local_date = ?
                ORDER BY logged_at
                LIMIT 30
                """,
                (user_id, local_date),
            )
            result["drinks"] = [dict(row) for row in await cursor.fetchall()]
        return result

    async def weight_history(
        self,
        user_id: int,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 180,
    ) -> list[dict[str, Any]]:
        conditions = ["user_id = ?"]
        params: list[Any] = [user_id]
        if start_date:
            conditions.append("local_date >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("local_date <= ?")
            params.append(end_date)
        params.append(limit)

        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT local_date, weight_kg, logged_at
                FROM weight_logs
                WHERE {' AND '.join(conditions)}
                ORDER BY logged_at DESC
                LIMIT ?
                """,
                params,
            )
            rows = [dict(row) for row in await cursor.fetchall()]

        latest_by_day: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest_by_day.setdefault(row["local_date"], row)
        return sorted(
            latest_by_day.values(),
            key=lambda row: row["local_date"],
        )

    async def body_history(
        self,
        user_id: int,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT local_date, weight_kg, waist_cm, hips_cm, chest_cm,
                       logged_at
                FROM body_measurements
                WHERE user_id = ? AND local_date BETWEEN ? AND ?
                ORDER BY logged_at
                """,
                (user_id, start_date, end_date),
            )
            rows = [dict(row) for row in await cursor.fetchall()]

        latest_by_day: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest_by_day[row["local_date"]] = row
        return [
            latest_by_day[key]
            for key in sorted(latest_by_day)
        ]


    async def register_referral(
        self,
        invited_user_id: int,
        inviter_user_id: int,
    ) -> bool:
        if invited_user_id == inviter_user_id:
            return False

        async with aiosqlite.connect(self.path) as db:
            inviter = await (
                await db.execute(
                    """
                    SELECT user_id
                    FROM profiles
                    WHERE user_id = ?
                      AND display_name IS NOT NULL
                      AND calorie_target IS NOT NULL
                    """,
                    (inviter_user_id,),
                )
            ).fetchone()
            if not inviter:
                return False

            try:
                await db.execute(
                    """
                    INSERT INTO referrals (
                        invited_user_id, inviter_user_id, created_at
                    )
                    VALUES (?, ?, ?)
                    """,
                    (
                        invited_user_id,
                        inviter_user_id,
                        int(time.time()),
                    ),
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def qualify_referral_and_award_day(
        self,
        invited_user_id: int,
    ) -> dict[str, Any] | None:
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")

            cursor = await db.execute(
                """
                SELECT r.*, p.trial_expires_at, p.subscription_expires_at
                FROM referrals AS r
                JOIN profiles AS p ON p.user_id = r.inviter_user_id
                WHERE r.invited_user_id = ?
                  AND r.qualified_at IS NULL
                """,
                (invited_user_id,),
            )
            row = await cursor.fetchone()
            if not row:
                await db.rollback()
                return None

            base = max(
                now,
                int(row["trial_expires_at"] or 0),
                int(row["subscription_expires_at"] or 0),
            )
            new_expiry = base + 24 * 60 * 60

            await db.execute(
                """
                UPDATE profiles
                SET subscription_expires_at = ?,
                    plan_code = CASE
                        WHEN plan_code IS NULL OR plan_code = ''
                        THEN 'referral_day'
                        ELSE plan_code
                    END,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (new_expiry, now, int(row["inviter_user_id"])),
            )
            await db.execute(
                """
                UPDATE referrals
                SET qualified_at = ?,
                    free_day_awarded_at = ?
                WHERE invited_user_id = ?
                  AND qualified_at IS NULL
                """,
                (now, now, invited_user_id),
            )
            await db.commit()

            return {
                "inviter_user_id": int(row["inviter_user_id"]),
                "new_expiry": new_expiry,
            }

    async def get_bonus_balance(self, user_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            row = await (
                await db.execute(
                    """
                    SELECT COALESCE(SUM(amount), 0)
                    FROM bonus_ledger
                    WHERE user_id = ?
                    """,
                    (user_id,),
                )
            ).fetchone()
            return max(0, int(row[0] or 0))

    async def referral_stats(self, user_id: int) -> dict[str, int]:
        async with aiosqlite.connect(self.path) as db:
            invited, qualified, free_days = await (
                await db.execute(
                    """
                    SELECT
                        COUNT(*),
                        COALESCE(SUM(CASE WHEN qualified_at IS NOT NULL THEN 1 ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN free_day_awarded_at IS NOT NULL THEN 1 ELSE 0 END), 0)
                    FROM referrals
                    WHERE inviter_user_id = ?
                    """,
                    (user_id,),
                )
            ).fetchone()

            paid_people, paid_orders, earned = await (
                await db.execute(
                    """
                    SELECT
                        COUNT(DISTINCT invited_user_id),
                        COUNT(*),
                        COALESCE(SUM(bonus_amount), 0)
                    FROM referral_rewards
                    WHERE inviter_user_id = ?
                    """,
                    (user_id,),
                )
            ).fetchone()

            balance = await (
                await db.execute(
                    """
                    SELECT COALESCE(SUM(amount), 0)
                    FROM bonus_ledger
                    WHERE user_id = ?
                    """,
                    (user_id,),
                )
            ).fetchone()

            return {
                "invited": int(invited or 0),
                "qualified": int(qualified or 0),
                "free_days": int(free_days or 0),
                "paid_people": int(paid_people or 0),
                "paid_orders": int(paid_orders or 0),
                "earned_bonus": int(earned or 0),
                "bonus_balance": max(0, int(balance[0] or 0)),
            }

    async def _release_pending_for_user(
        self,
        db: aiosqlite.Connection,
        user_id: int,
        *,
        status: str = "cancelled",
    ) -> None:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT invoice_id, bonus_used
            FROM pending_invoices
            WHERE user_id = ? AND status = 'pending'
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        now = int(time.time())

        for row in rows:
            bonus_used = int(row["bonus_used"] or 0)
            if bonus_used > 0:
                try:
                    await db.execute(
                        """
                        INSERT INTO bonus_ledger (
                            user_id, amount, kind, reference, created_at
                        )
                        VALUES (?, ?, 'invoice_release', ?, ?)
                        """,
                        (
                            user_id,
                            bonus_used,
                            f"invoice_release:{row['invoice_id']}",
                            now,
                        ),
                    )
                except aiosqlite.IntegrityError:
                    pass

            await db.execute(
                """
                UPDATE pending_invoices
                SET status = ?
                WHERE invoice_id = ? AND status = 'pending'
                """,
                (status, row["invoice_id"]),
            )

    async def release_expired_bonus_reservations(self) -> int:
        now = int(time.time())
        released = 0

        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT invoice_id, user_id, bonus_used
                FROM pending_invoices
                WHERE status = 'pending' AND expires_at <= ?
                """,
                (now,),
            )
            rows = await cursor.fetchall()

            for row in rows:
                bonus_used = int(row["bonus_used"] or 0)
                if bonus_used > 0:
                    try:
                        await db.execute(
                            """
                            INSERT INTO bonus_ledger (
                                user_id, amount, kind, reference, created_at
                            )
                            VALUES (?, ?, 'invoice_release', ?, ?)
                            """,
                            (
                                int(row["user_id"]),
                                bonus_used,
                                f"invoice_release:{row['invoice_id']}",
                                now,
                            ),
                        )
                        released += bonus_used
                    except aiosqlite.IntegrityError:
                        pass

                await db.execute(
                    """
                    UPDATE pending_invoices
                    SET status = 'expired'
                    WHERE invoice_id = ? AND status = 'pending'
                    """,
                    (row["invoice_id"],),
                )

            await db.commit()
        return released

    async def create_pending_invoice(
        self,
        *,
        invoice_id: str,
        user_id: int,
        plan_code: str,
        original_amount: int,
        use_bonus: bool,
        lifetime_seconds: int = 30 * 60,
    ) -> dict[str, Any]:
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")

            await self._release_pending_for_user(
                db,
                user_id,
                status="cancelled",
            )

            balance_row = await (
                await db.execute(
                    """
                    SELECT COALESCE(SUM(amount), 0)
                    FROM bonus_ledger
                    WHERE user_id = ?
                    """,
                    (user_id,),
                )
            ).fetchone()
            balance = max(0, int(balance_row[0] or 0))
            bonus_used = (
                min(balance, max(0, original_amount - 1))
                if use_bonus else 0
            )
            payable = max(1, original_amount - bonus_used)

            if bonus_used > 0:
                await db.execute(
                    """
                    INSERT INTO bonus_ledger (
                        user_id, amount, kind, reference, created_at
                    )
                    VALUES (?, ?, 'invoice_reserve', ?, ?)
                    """,
                    (
                        user_id,
                        -bonus_used,
                        f"invoice_reserve:{invoice_id}",
                        now,
                    ),
                )

            expires_at = now + max(300, lifetime_seconds)
            await db.execute(
                """
                INSERT INTO pending_invoices (
                    invoice_id, user_id, plan_code, original_amount,
                    bonus_used, payable_amount, status,
                    created_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    invoice_id,
                    user_id,
                    plan_code,
                    original_amount,
                    bonus_used,
                    payable,
                    now,
                    expires_at,
                ),
            )
            await db.commit()

            return {
                "invoice_id": invoice_id,
                "user_id": user_id,
                "plan_code": plan_code,
                "original_amount": original_amount,
                "bonus_used": bonus_used,
                "payable_amount": payable,
                "status": "pending",
                "created_at": now,
                "expires_at": expires_at,
            }

    async def get_pending_invoice(
        self,
        invoice_id: str,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT *
                FROM pending_invoices
                WHERE invoice_id = ?
                """,
                (invoice_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def validate_pending_invoice(
        self,
        *,
        invoice_id: str,
        user_id: int,
        amount: int,
        currency: str,
    ) -> bool:
        invoice = await self.get_pending_invoice(invoice_id)
        return bool(
            invoice
            and invoice["status"] == "pending"
            and int(invoice["expires_at"]) > int(time.time())
            and int(invoice["user_id"]) == user_id
            and int(invoice["payable_amount"]) == amount
            and currency == "XTR"
        )

    async def complete_pending_invoice(
        self,
        *,
        invoice_id: str,
        charge_id: str,
        user_id: int,
        amount: int,
        currency: str,
        expires_at: int,
    ) -> dict[str, Any] | None:
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")

            cursor = await db.execute(
                """
                SELECT *
                FROM pending_invoices
                WHERE invoice_id = ?
                """,
                (invoice_id,),
            )
            invoice = await cursor.fetchone()
            if not invoice:
                await db.rollback()
                return None

            if not (
                invoice["status"] == "pending"
                and int(invoice["expires_at"]) > now
                and int(invoice["user_id"]) == user_id
                and int(invoice["payable_amount"]) == amount
                and currency == "XTR"
            ):
                await db.rollback()
                return None

            try:
                await db.execute(
                    """
                    INSERT INTO payments (
                        charge_id, user_id, plan_code, amount,
                        currency, expires_at, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        charge_id,
                        user_id,
                        invoice["plan_code"],
                        amount,
                        currency,
                        expires_at,
                        now,
                    ),
                )
            except aiosqlite.IntegrityError:
                await db.rollback()
                return None

            await db.execute(
                """
                UPDATE profiles
                SET subscription_expires_at = ?,
                    plan_code = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    expires_at,
                    invoice["plan_code"],
                    now,
                    user_id,
                ),
            )
            await db.execute(
                """
                UPDATE pending_invoices
                SET status = 'paid',
                    telegram_charge_id = ?
                WHERE invoice_id = ?
                """,
                (charge_id, invoice_id),
            )

            referral_cursor = await db.execute(
                """
                SELECT inviter_user_id
                FROM referrals
                WHERE invited_user_id = ?
                  AND qualified_at IS NOT NULL
                """,
                (user_id,),
            )
            referral = await referral_cursor.fetchone()
            inviter_id: int | None = None
            referral_bonus = 0

            if referral:
                inviter_id = int(referral["inviter_user_id"])
                # Round 5% to the nearest whole internal bonus Star.
                referral_bonus = max(1, (amount * 5 + 50) // 100)
                try:
                    await db.execute(
                        """
                        INSERT INTO referral_rewards (
                            source_charge_id, inviter_user_id,
                            invited_user_id, paid_amount,
                            bonus_amount, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            charge_id,
                            inviter_id,
                            user_id,
                            amount,
                            referral_bonus,
                            now,
                        ),
                    )
                    await db.execute(
                        """
                        INSERT INTO bonus_ledger (
                            user_id, amount, kind, reference, created_at
                        )
                        VALUES (?, ?, 'referral_payment', ?, ?)
                        """,
                        (
                            inviter_id,
                            referral_bonus,
                            f"referral_payment:{charge_id}",
                            now,
                        ),
                    )
                except aiosqlite.IntegrityError:
                    inviter_id = None
                    referral_bonus = 0

            await db.commit()
            return {
                "plan_code": str(invoice["plan_code"]),
                "bonus_used": int(invoice["bonus_used"] or 0),
                "payable_amount": int(invoice["payable_amount"]),
                "inviter_user_id": inviter_id,
                "referral_bonus": referral_bonus,
            }

    async def delete_user_data(self, user_id: int) -> None:
        tables_by_user = [
            "food_logs",
            "drink_logs",
            "photo_analyses",
            "ai_chat_logs",
            "weight_logs",
            "body_measurements",
            "meal_schedule",
            "sent_notifications",
            "reminder_preferences",
            "reminder_snoozes",
            "payments",
            "menu_sessions",
            "recipe_choices",
            "coach_applications",
            "fasting_daily_logs",
            "bonus_ledger",
            "pending_invoices",
        ]

        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")

            for table in tables_by_user:
                await db.execute(
                    f"DELETE FROM {table} WHERE user_id = ?",
                    (user_id,),
                )

            await db.execute(
                """
                DELETE FROM referral_rewards
                WHERE inviter_user_id = ? OR invited_user_id = ?
                """,
                (user_id, user_id),
            )
            await db.execute(
                """
                DELETE FROM referrals
                WHERE inviter_user_id = ? OR invited_user_id = ?
                """,
                (user_id, user_id),
            )
            await db.execute(
                "DELETE FROM profiles WHERE user_id = ?",
                (user_id,),
            )
            await db.commit()

    async def all_complete_profiles(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT * FROM profiles
                WHERE language IS NOT NULL AND display_name IS NOT NULL
                  AND timezone IS NOT NULL AND calorie_target IS NOT NULL
                """
            )
            return [dict(row) for row in await cur.fetchall()]

    async def mark_notification_sent(self, user_id: int, key: str, local_date: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            try:
                await db.execute(
                    "INSERT INTO sent_notifications (user_id, notification_key, local_date, sent_at) VALUES (?, ?, ?, ?)",
                    (user_id, key, local_date, int(time.time())),
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def activate_subscription(
        self,
        user_id: int,
        charge_id: str,
        plan_code: str,
        amount: int,
        currency: str,
        expires_at: int,
    ) -> bool:
        async with aiosqlite.connect(self.path) as db:
            try:
                await db.execute("BEGIN")
                await db.execute(
                    "INSERT INTO payments (charge_id, user_id, plan_code, amount, currency, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (charge_id, user_id, plan_code, amount, currency, expires_at, int(time.time())),
                )
                await db.execute(
                    "UPDATE profiles SET subscription_expires_at = ?, plan_code = ?, updated_at = ? WHERE user_id = ?",
                    (expires_at, plan_code, int(time.time()), user_id),
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                await db.rollback()
                return False

    async def stats(self, now: int) -> dict[str, int]:
        async with aiosqlite.connect(self.path) as db:
            users = int((await (await db.execute("SELECT COUNT(*) FROM profiles")).fetchone())[0])
            active = int((await (await db.execute(
                "SELECT COUNT(*) FROM profiles WHERE trial_expires_at > ? OR subscription_expires_at > ?",
                (now, now),
            )).fetchone())[0])
            payments, stars = await (await db.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM payments"
            )).fetchone()
            return {"users": users, "active": active, "payments": int(payments), "stars": int(stars)}
