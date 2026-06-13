from __future__ import annotations

import asyncio
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp


def load_env_file(path: str | os.PathLike[str] = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fmt_amount(value: float) -> str:
    normalized = f"{value:.8f}".rstrip("0").rstrip(".")
    return normalized or "0"


def parse_admin_ids(raw: str) -> list[int]:
    result: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            result.append(int(chunk))
        except ValueError:
            continue
    return result


@dataclass(slots=True)
class Config:
    bot_token: str
    crypto_pay_token: str
    crypto_pay_base_url: str
    admin_ids: list[int]
    db_path: str
    log_chat_id: int | None
    force_sub_chat_id: str | None
    force_sub_chat_username: str | None
    invoice_poll_interval: int
    bot_asset: str
    min_room_amount: float
    min_deposit_amount: float
    min_withdraw_amount: float
    house_commission_percent: float
    referral_percent: float

    @classmethod
    def from_env(cls) -> "Config":
        load_env_file()
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        if not bot_token:
            raise RuntimeError("BOT_TOKEN is required")
        return cls(
            bot_token=bot_token,
            crypto_pay_token=os.getenv("CRYPTO_PAY_TOKEN", "").strip(),
            crypto_pay_base_url=os.getenv("CRYPTO_PAY_BASE_URL", "https://pay.crypt.bot/api/").rstrip("/") + "/",
            admin_ids=parse_admin_ids(os.getenv("ADMIN_IDS", "")),
            db_path=os.getenv("DB_PATH", "bot_data.sqlite3").strip() or "bot_data.sqlite3",
            log_chat_id=int(os.getenv("LOG_CHAT_ID")) if os.getenv("LOG_CHAT_ID", "").strip() else None,
            force_sub_chat_id=os.getenv("FORCE_SUB_CHAT_ID", "").strip() or None,
            force_sub_chat_username=os.getenv("FORCE_SUB_CHAT_USERNAME", "").strip() or None,
            invoice_poll_interval=max(5, int(os.getenv("INVOICE_POLL_INTERVAL", "15"))),
            bot_asset=os.getenv("BOT_ASSET", "USDT").strip().upper() or "USDT",
            min_room_amount=float(os.getenv("MIN_ROOM_AMOUNT", "0.05")),
            min_deposit_amount=float(os.getenv("MIN_DEPOSIT_AMOUNT", "0.05")),
            min_withdraw_amount=float(os.getenv("MIN_WITHDRAW_AMOUNT", "0.05")),
            house_commission_percent=float(os.getenv("HOUSE_COMMISSION_PERCENT", "1")),
            referral_percent=float(os.getenv("REFERRAL_PERCENT", "0.01")),
        )


class CryptoPayError(RuntimeError):
    pass


class CryptoBotClient:
    def __init__(self, token: str, base_url: str) -> None:
        self.token = token
        self.base_url = base_url
        self.session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=25)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def _request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.token:
            raise CryptoPayError("CRYPTO_PAY_TOKEN is not set")
        session = await self._ensure_session()
        headers = {"Crypto-Pay-API-Token": self.token}
        request_kwargs: dict[str, Any] = {"headers": headers}
        if payload is not None:
            request_kwargs["json"] = payload
        if query is not None:
            request_kwargs["params"] = query
        async with session.request(method, self.base_url + endpoint, **request_kwargs) as response:
            data = await response.json(content_type=None)
        if not data.get("ok"):
            raise CryptoPayError(str(data.get("error") or data))
        result = data.get("result")
        if isinstance(result, dict):
            return result
        return {"items": result}

    async def create_invoice(self, amount: float, asset: str, payload: str) -> dict[str, Any]:
        body = {
            "asset": asset,
            "amount": fmt_amount(amount),
            "description": f"Пополнение игрового баланса {asset}",
            "payload": payload,
        }
        return await self._request("POST", "createInvoice", body)

    async def get_invoices(self, invoice_ids: list[str | int]) -> list[dict[str, Any]]:
        if not invoice_ids:
            return []
        query = {"invoice_ids": ",".join(str(item) for item in invoice_ids)}
        data = await self._request("GET", "getInvoices", query=query)
        return list(data.get("items", []))

    async def create_check(self, amount: float, asset: str) -> dict[str, Any]:
        body = {
            "asset": asset,
            "amount": fmt_amount(amount),
        }
        return await self._request("POST", "createCheck", body)


class Database:
    def __init__(self, path: str, config: Config) -> None:
        self.path = path
        self.config = config
        self.lock = asyncio.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    async def init(self) -> None:
        async with self.lock:
            self.conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT NOT NULL,
                    balance REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    invited_by INTEGER,
                    referral_earnings REAL NOT NULL DEFAULT 0,
                    total_deposit REAL NOT NULL DEFAULT 0,
                    total_withdraw REAL NOT NULL DEFAULT 0,
                    total_wager REAL NOT NULL DEFAULT 0,
                    total_win REAL NOT NULL DEFAULT 0,
                    promo_wager_remaining REAL NOT NULL DEFAULT 0,
                    auto_withdraw_enabled INTEGER NOT NULL DEFAULT 1,
                    default_room_amount REAL NOT NULL DEFAULT 0.05
                );
                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY,
                    added_by INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rooms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    creator_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL,
                    opponent_id INTEGER,
                    creator_roll INTEGER,
                    opponent_roll INTEGER,
                    winner_id INTEGER,
                    rerolls INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    amount REAL NOT NULL,
                    meta TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS invoices (
                    invoice_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    asset TEXT NOT NULL,
                    status TEXT NOT NULL,
                    pay_url TEXT,
                    payload TEXT,
                    created_at TEXT NOT NULL,
                    paid_at TEXT
                );
                CREATE TABLE IF NOT EXISTS withdrawals (
                    check_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    asset TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provider_check_id TEXT,
                    check_url TEXT,
                    processed_by INTEGER,
                    created_at TEXT NOT NULL,
                    processed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS promocodes (
                    code TEXT PRIMARY KEY,
                    amount REAL NOT NULL,
                    wager_requirement REAL NOT NULL DEFAULT 0,
                    activations_left INTEGER NOT NULL,
                    created_by INTEGER NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS promo_activations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(code, user_id)
                );
                CREATE TABLE IF NOT EXISTS referral_rewards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    inviter_id INTEGER NOT NULL,
                    referred_user_id INTEGER NOT NULL,
                    room_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gift_checks (
                    token TEXT PRIMARY KEY,
                    amount REAL NOT NULL,
                    activations_total INTEGER NOT NULL,
                    activations_left INTEGER NOT NULL,
                    required_deposit REAL NOT NULL DEFAULT 0,
                    required_channels TEXT NOT NULL DEFAULT '',
                    password TEXT,
                    description TEXT,
                    photo_file_id TEXT,
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS gift_check_claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    claimed_at TEXT NOT NULL,
                    UNIQUE(token, user_id)
                );
                """
            )
            self._ensure_column("users", "promo_wager_remaining", "REAL NOT NULL DEFAULT 0")
            self._ensure_column("promocodes", "wager_requirement", "REAL NOT NULL DEFAULT 0")
            self._ensure_column("gift_checks", "required_deposit", "REAL NOT NULL DEFAULT 0")
            self._ensure_column("gift_checks", "required_channels", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("withdrawals", "provider_check_id", "TEXT")
            self._ensure_column("withdrawals", "processed_by", "INTEGER")
            self._ensure_column("withdrawals", "processed_at", "TEXT")
            defaults = {
                "min_room_amount": fmt_amount(self.config.min_room_amount),
                "min_deposit_amount": fmt_amount(self.config.min_deposit_amount),
                "min_withdraw_amount": fmt_amount(self.config.min_withdraw_amount),
                "house_commission_percent": fmt_amount(self.config.house_commission_percent),
                "referral_percent": fmt_amount(self.config.referral_percent),
                "withdraw_auto_enabled": "1",
                "force_sub_chat_id": self.config.force_sub_chat_id or "",
                "force_sub_chat_username": self.config.force_sub_chat_username or "",
                "log_chat_id": str(self.config.log_chat_id or ""),
                "bot_asset": self.config.bot_asset,
            }
            for key, value in defaults.items():
                self.conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                    (key, value),
                )
            for admin_id in self.config.admin_ids:
                self.conn.execute(
                    "INSERT OR IGNORE INTO admins(user_id, added_by, created_at) VALUES(?, ?, ?)",
                    (admin_id, admin_id, now_iso()),
                )
            self.conn.commit()

    def _ensure_column(self, table_name: str, column_name: str, definition: str) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name not in columns:
            self.conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    async def close(self) -> None:
        async with self.lock:
            self.conn.commit()
            self.conn.close()

    async def get_setting(self, key: str, default: str = "") -> str:
        async with self.lock:
            row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    async def get_setting_float(self, key: str, default: float) -> float:
        try:
            return float(await self.get_setting(key, fmt_amount(default)))
        except ValueError:
            return default

    async def set_setting(self, key: str, value: str) -> None:
        async with self.lock:
            self.conn.execute(
                """
                INSERT INTO settings(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            self.conn.commit()

    async def is_admin(self, user_id: int) -> bool:
        async with self.lock:
            row = self.conn.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone()
        return bool(row)

    async def add_admin(self, admin_user_id: int, added_by: int) -> None:
        async with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO admins(user_id, added_by, created_at) VALUES(?, ?, ?)",
                (admin_user_id, added_by, now_iso()),
            )
            self.conn.commit()

    async def list_admins(self) -> list[sqlite3.Row]:
        async with self.lock:
            rows = self.conn.execute(
                """
                SELECT a.user_id, u.username, u.full_name, a.created_at
                FROM admins a
                LEFT JOIN users u ON u.user_id = a.user_id
                ORDER BY a.created_at ASC
                """
            ).fetchall()
        return rows

    async def upsert_user(self, tg_user: Any, invited_by: int | None = None) -> sqlite3.Row:
        username = tg_user.username.lower() if getattr(tg_user, "username", None) else None
        full_name = (getattr(tg_user, "full_name", "") or getattr(tg_user, "first_name", "") or "Пользователь").strip()
        async with self.lock:
            existing = self.conn.execute(
                "SELECT invited_by FROM users WHERE user_id = ?",
                (tg_user.id,),
            ).fetchone()
            final_invited_by = existing["invited_by"] if existing else None
            if final_invited_by is None and invited_by and invited_by != tg_user.id:
                final_invited_by = invited_by
            self.conn.execute(
                """
                INSERT INTO users(user_id, username, full_name, created_at, invited_by, default_room_amount)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name,
                    invited_by = COALESCE(users.invited_by, excluded.invited_by)
                """,
                (
                    tg_user.id,
                    username,
                    full_name,
                    now_iso(),
                    final_invited_by,
                    self.config.min_room_amount,
                ),
            )
            row = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (tg_user.id,)).fetchone()
            self.conn.commit()
        return row

    async def get_user(self, user_id: int) -> sqlite3.Row | None:
        async with self.lock:
            row = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row

    async def get_user_by_username(self, username: str) -> sqlite3.Row | None:
        normalized = username.strip().lower().lstrip("@")
        async with self.lock:
            row = self.conn.execute("SELECT * FROM users WHERE username = ?", (normalized,)).fetchone()
        return row

    async def get_user_by_id_or_username(self, value: str) -> sqlite3.Row | None:
        cleaned = value.strip()
        if cleaned.lstrip("-").isdigit():
            return await self.get_user(int(cleaned))
        return await self.get_user_by_username(cleaned)

    async def set_default_room_amount(self, user_id: int, amount: float) -> None:
        async with self.lock:
            self.conn.execute(
                "UPDATE users SET default_room_amount = ? WHERE user_id = ?",
                (round(amount, 8), user_id),
            )
            self.conn.commit()

    async def toggle_auto_withdraw(self, user_id: int) -> sqlite3.Row | None:
        async with self.lock:
            self.conn.execute(
                """
                UPDATE users
                SET auto_withdraw_enabled = CASE auto_withdraw_enabled WHEN 1 THEN 0 ELSE 1 END
                WHERE user_id = ?
                """,
                (user_id,),
            )
            row = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            self.conn.commit()
        return row

    async def _change_balance(
        self,
        user_id: int,
        delta: float,
        tx_type: str,
        meta: str,
        *,
        deposit_delta: float = 0.0,
        withdraw_delta: float = 0.0,
        referral_delta: float = 0.0,
        total_win_delta: float = 0.0,
        total_wager_delta: float = 0.0,
    ) -> sqlite3.Row:
        async with self.lock:
            user = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if not user:
                raise ValueError("User not found")
            new_balance = round(float(user["balance"]) + delta, 8)
            if new_balance < -1e-9:
                raise ValueError("Insufficient balance")
            self.conn.execute(
                """
                UPDATE users
                SET balance = ?,
                    total_deposit = total_deposit + ?,
                    total_withdraw = total_withdraw + ?,
                    referral_earnings = referral_earnings + ?,
                    total_win = total_win + ?,
                    total_wager = total_wager + ?
                WHERE user_id = ?
                """,
                (
                    new_balance,
                    round(deposit_delta, 8),
                    round(withdraw_delta, 8),
                    round(referral_delta, 8),
                    round(total_win_delta, 8),
                    round(total_wager_delta, 8),
                    user_id,
                ),
            )
            self.conn.execute(
                "INSERT INTO transactions(user_id, type, amount, meta, created_at) VALUES(?, ?, ?, ?, ?)",
                (user_id, tx_type, round(delta, 8), meta, now_iso()),
            )
            updated = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            self.conn.commit()
        return updated

    async def add_balance(self, user_id: int, amount: float, tx_type: str, meta: str) -> sqlite3.Row:
        return await self._change_balance(user_id, abs(amount), tx_type, meta)

    async def subtract_balance(self, user_id: int, amount: float, tx_type: str, meta: str) -> sqlite3.Row:
        return await self._change_balance(user_id, -abs(amount), tx_type, meta)

    async def create_invoice(self, invoice_id: str, user_id: int, amount: float, asset: str, pay_url: str, payload: str) -> None:
        async with self.lock:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO invoices(invoice_id, user_id, amount, asset, status, pay_url, payload, created_at, paid_at)
                VALUES(?, ?, ?, ?, 'active', ?, ?, ?, NULL)
                """,
                (str(invoice_id), user_id, round(amount, 8), asset, pay_url, payload, now_iso()),
            )
            self.conn.commit()

    async def get_pending_invoices(self, limit: int = 40) -> list[sqlite3.Row]:
        async with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM invoices WHERE status = 'active' ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return rows

    async def complete_invoice(self, invoice_id: str) -> sqlite3.Row | None:
        async with self.lock:
            invoice = self.conn.execute("SELECT * FROM invoices WHERE invoice_id = ?", (str(invoice_id),)).fetchone()
            if not invoice or invoice["status"] != "active":
                return None
            self.conn.execute(
                "UPDATE invoices SET status = 'paid', paid_at = ? WHERE invoice_id = ?",
                (now_iso(), str(invoice_id)),
            )
            user = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (invoice["user_id"],)).fetchone()
            new_balance = round(float(user["balance"]) + float(invoice["amount"]), 8)
            self.conn.execute(
                """
                UPDATE users
                SET balance = ?, total_deposit = total_deposit + ?
                WHERE user_id = ?
                """,
                (new_balance, round(float(invoice["amount"]), 8), invoice["user_id"]),
            )
            self.conn.execute(
                "INSERT INTO transactions(user_id, type, amount, meta, created_at) VALUES(?, 'deposit', ?, ?, ?)",
                (
                    invoice["user_id"],
                    round(float(invoice["amount"]), 8),
                    f"invoice:{invoice['invoice_id']}",
                    now_iso(),
                ),
            )
            updated_user = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (invoice["user_id"],)).fetchone()
            self.conn.commit()
        return updated_user

    async def create_withdrawal(self, check_id: str, user_id: int, amount: float, asset: str, check_url: str) -> sqlite3.Row:
        async with self.lock:
            user = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if not user:
                raise ValueError("User not found")
            new_balance = round(float(user["balance"]) - abs(amount), 8)
            if new_balance < -1e-9:
                raise ValueError("Insufficient balance")
            self.conn.execute(
                """
                INSERT OR REPLACE INTO withdrawals(check_id, user_id, amount, asset, status, provider_check_id, check_url, processed_by, created_at, processed_at)
                VALUES(?, ?, ?, ?, 'created', ?, ?, NULL, ?, ?)
                """,
                (str(check_id), user_id, round(amount, 8), asset, str(check_id), check_url, now_iso(), now_iso()),
            )
            self.conn.execute(
                """
                UPDATE users
                SET balance = ?, total_withdraw = total_withdraw + ?
                WHERE user_id = ?
                """,
                (new_balance, round(amount, 8), user_id),
            )
            self.conn.execute(
                "INSERT INTO transactions(user_id, type, amount, meta, created_at) VALUES(?, 'withdraw', ?, ?, ?)",
                (user_id, -round(amount, 8), f"check:{check_id}", now_iso()),
            )
            updated = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            self.conn.commit()
        return updated

    async def create_withdraw_request(self, request_id: str, user_id: int, amount: float, asset: str) -> tuple[sqlite3.Row, sqlite3.Row]:
        async with self.lock:
            user = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if not user:
                raise ValueError("User not found")
            new_balance = round(float(user["balance"]) - abs(amount), 8)
            if new_balance < -1e-9:
                raise ValueError("Insufficient balance")
            self.conn.execute(
                """
                INSERT OR REPLACE INTO withdrawals(check_id, user_id, amount, asset, status, provider_check_id, check_url, processed_by, created_at, processed_at)
                VALUES(?, ?, ?, ?, 'pending', NULL, NULL, NULL, ?, NULL)
                """,
                (str(request_id), user_id, round(amount, 8), asset, now_iso()),
            )
            self.conn.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (new_balance, user_id),
            )
            self.conn.execute(
                "INSERT INTO transactions(user_id, type, amount, meta, created_at) VALUES(?, 'withdraw_request', ?, ?, ?)",
                (user_id, -round(amount, 8), f"withdraw_request:{request_id}", now_iso()),
            )
            updated_user = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            withdrawal = self.conn.execute("SELECT * FROM withdrawals WHERE check_id = ?", (str(request_id),)).fetchone()
            self.conn.commit()
        return updated_user, withdrawal

    async def get_withdrawal(self, request_id: str) -> sqlite3.Row | None:
        async with self.lock:
            row = self.conn.execute("SELECT * FROM withdrawals WHERE check_id = ?", (str(request_id),)).fetchone()
        return row

    async def approve_withdraw_request(self, request_id: str, provider_check_id: str, check_url: str, admin_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
        async with self.lock:
            withdrawal = self.conn.execute("SELECT * FROM withdrawals WHERE check_id = ?", (str(request_id),)).fetchone()
            if not withdrawal:
                raise ValueError("Заявка не найдена")
            if withdrawal["status"] != "pending":
                raise ValueError("Заявка уже обработана")
            self.conn.execute(
                """
                UPDATE withdrawals
                SET status = 'approved',
                    provider_check_id = ?,
                    check_url = ?,
                    processed_by = ?,
                    processed_at = ?
                WHERE check_id = ?
                """,
                (str(provider_check_id), check_url, admin_id, now_iso(), str(request_id)),
            )
            self.conn.execute(
                """
                UPDATE users
                SET total_withdraw = total_withdraw + ?
                WHERE user_id = ?
                """,
                (round(float(withdrawal["amount"]), 8), withdrawal["user_id"]),
            )
            self.conn.execute(
                "INSERT INTO transactions(user_id, type, amount, meta, created_at) VALUES(?, 'withdraw_approved', ?, ?, ?)",
                (withdrawal["user_id"], 0, f"withdraw_approved:{request_id}", now_iso()),
            )
            updated_user = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (withdrawal["user_id"],)).fetchone()
            updated_withdrawal = self.conn.execute("SELECT * FROM withdrawals WHERE check_id = ?", (str(request_id),)).fetchone()
            self.conn.commit()
        return updated_user, updated_withdrawal

    async def reject_withdraw_request(self, request_id: str, admin_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
        async with self.lock:
            withdrawal = self.conn.execute("SELECT * FROM withdrawals WHERE check_id = ?", (str(request_id),)).fetchone()
            if not withdrawal:
                raise ValueError("Заявка не найдена")
            if withdrawal["status"] != "pending":
                raise ValueError("Заявка уже обработана")
            user = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (withdrawal["user_id"],)).fetchone()
            if not user:
                raise ValueError("User not found")
            refunded_balance = round(float(user["balance"]) + float(withdrawal["amount"]), 8)
            self.conn.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (refunded_balance, withdrawal["user_id"]),
            )
            self.conn.execute(
                """
                UPDATE withdrawals
                SET status = 'rejected',
                    processed_by = ?,
                    processed_at = ?
                WHERE check_id = ?
                """,
                (admin_id, now_iso(), str(request_id)),
            )
            self.conn.execute(
                "INSERT INTO transactions(user_id, type, amount, meta, created_at) VALUES(?, 'withdraw_refund', ?, ?, ?)",
                (withdrawal["user_id"], round(float(withdrawal["amount"]), 8), f"withdraw_rejected:{request_id}", now_iso()),
            )
            updated_user = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (withdrawal["user_id"],)).fetchone()
            updated_withdrawal = self.conn.execute("SELECT * FROM withdrawals WHERE check_id = ?", (str(request_id),)).fetchone()
            self.conn.commit()
        return updated_user, updated_withdrawal

    async def create_room(self, creator_id: int, amount: float) -> sqlite3.Row:
        async with self.lock:
            user = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (creator_id,)).fetchone()
            if not user:
                raise ValueError("User not found")
            if float(user["balance"]) < amount:
                raise ValueError("Недостаточно средств")
            self.conn.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (round(float(user["balance"]) - amount, 8), creator_id),
            )
            self.conn.execute(
                "INSERT INTO transactions(user_id, type, amount, meta, created_at) VALUES(?, 'room_hold', ?, ?, ?)",
                (creator_id, -round(amount, 8), "Создание комнаты", now_iso()),
            )
            cursor = self.conn.execute(
                """
                INSERT INTO rooms(creator_id, amount, status, created_at)
                VALUES(?, ?, 'open', ?)
                """,
                (creator_id, round(amount, 8), now_iso()),
            )
            room_id = cursor.lastrowid
            room = self.conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
            self.conn.commit()
        return room

    async def list_open_rooms(self, limit: int = 25) -> list[sqlite3.Row]:
        async with self.lock:
            rows = self.conn.execute(
                """
                SELECT r.*, u.username, u.full_name
                FROM rooms r
                JOIN users u ON u.user_id = r.creator_id
                WHERE r.status = 'open'
                ORDER BY r.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return rows

    async def list_user_open_rooms(self, user_id: int, limit: int = 20) -> list[sqlite3.Row]:
        async with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM rooms WHERE creator_id = ? AND status = 'open' ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return rows

    async def cancel_room(self, room_id: int, requester_id: int) -> sqlite3.Row:
        async with self.lock:
            room = self.conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
            if not room or room["status"] != "open":
                raise ValueError("Комната уже недоступна")
            if room["creator_id"] != requester_id:
                raise ValueError("Можно отменить только свою комнату")
            user = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (requester_id,)).fetchone()
            self.conn.execute(
                "UPDATE rooms SET status = 'cancelled', finished_at = ? WHERE id = ?",
                (now_iso(), room_id),
            )
            self.conn.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (round(float(user["balance"]) + float(room["amount"]), 8), requester_id),
            )
            self.conn.execute(
                "INSERT INTO transactions(user_id, type, amount, meta, created_at) VALUES(?, 'room_refund', ?, ?, ?)",
                (requester_id, round(float(room["amount"]), 8), f"Отмена комнаты #{room_id}", now_iso()),
            )
            updated = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (requester_id,)).fetchone()
            self.conn.commit()
        return updated

    async def ensure_room_joinable(self, room_id: int, opponent_id: int) -> sqlite3.Row:
        async with self.lock:
            room = self.conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
            if not room or room["status"] != "open":
                raise ValueError("Комната недоступна")
            if room["creator_id"] == opponent_id:
                raise ValueError("Нельзя зайти в собственную комнату")
            opponent = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (opponent_id,)).fetchone()
            if not opponent:
                raise ValueError("Игрок не найден")
            amount = float(room["amount"])
            if float(opponent["balance"]) < amount:
                raise ValueError("Недостаточно средств для входа")
        return room

    async def join_room(
        self,
        room_id: int,
        opponent_id: int,
        creator_roll: int,
        opponent_roll: int,
        rerolls: int = 0,
    ) -> dict[str, Any]:
        async with self.lock:
            room = self.conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
            if not room or room["status"] != "open":
                raise ValueError("Комната недоступна")
            if room["creator_id"] == opponent_id:
                raise ValueError("Нельзя зайти в собственную комнату")
            if creator_roll == opponent_roll:
                raise ValueError("Броски не должны быть равны")

            creator = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (room["creator_id"],)).fetchone()
            opponent = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (opponent_id,)).fetchone()
            if not creator or not opponent:
                raise ValueError("Игрок не найден")

            amount = float(room["amount"])
            if float(opponent["balance"]) < amount:
                raise ValueError("Недостаточно средств для входа")

            self.conn.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (round(float(opponent["balance"]) - amount, 8), opponent_id),
            )
            self.conn.execute(
                "INSERT INTO transactions(user_id, type, amount, meta, created_at) VALUES(?, 'room_join', ?, ?, ?)",
                (opponent_id, -round(amount, 8), f"Вход в комнату #{room_id}", now_iso()),
            )

            total_bank = round(amount * 2, 8)
            commission_row = self.conn.execute(
                "SELECT value FROM settings WHERE key = 'house_commission_percent'"
            ).fetchone()
            referral_row = self.conn.execute(
                "SELECT value FROM settings WHERE key = 'referral_percent'"
            ).fetchone()
            commission_percent = float(commission_row["value"]) if commission_row else self.config.house_commission_percent
            referral_percent = float(referral_row["value"]) if referral_row else self.config.referral_percent
            commission = round(total_bank * commission_percent / 100, 8)
            winner_prize = round(total_bank - commission, 8)

            winner_id = room["creator_id"] if creator_roll > opponent_roll else opponent_id
            loser_id = opponent_id if winner_id == room["creator_id"] else room["creator_id"]
            referral_reward = 0.0

            loser = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (loser_id,)).fetchone()
            inviter_id = loser["invited_by"] if loser else None
            if inviter_id:
                referral_reward = round(min(commission, amount * referral_percent / 100), 8)
                if referral_reward > 0:
                    inviter = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (inviter_id,)).fetchone()
                    if inviter:
                        self.conn.execute(
                            """
                            UPDATE users
                            SET balance = ?, referral_earnings = referral_earnings + ?
                            WHERE user_id = ?
                            """,
                            (
                                round(float(inviter["balance"]) + referral_reward, 8),
                                referral_reward,
                                inviter_id,
                            ),
                        )
                        self.conn.execute(
                            "INSERT INTO transactions(user_id, type, amount, meta, created_at) VALUES(?, 'referral', ?, ?, ?)",
                            (inviter_id, referral_reward, f"Реферальная награда за комнату #{room_id}", now_iso()),
                        )
                        self.conn.execute(
                            """
                            INSERT INTO referral_rewards(inviter_id, referred_user_id, room_id, amount, created_at)
                            VALUES(?, ?, ?, ?, ?)
                            """,
                            (inviter_id, loser_id, room_id, referral_reward, now_iso()),
                        )

            winner = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (winner_id,)).fetchone()
            self.conn.execute(
                """
                UPDATE users
                SET balance = ?,
                    total_win = total_win + ?,
                    total_wager = total_wager + ?,
                    promo_wager_remaining = MAX(promo_wager_remaining - ?, 0)
                WHERE user_id = ?
                """,
                (
                    round(float(winner["balance"]) + winner_prize, 8),
                    winner_prize,
                    amount,
                    amount,
                    winner_id,
                ),
            )
            self.conn.execute(
                """
                UPDATE users
                SET total_wager = total_wager + ?,
                    promo_wager_remaining = MAX(promo_wager_remaining - ?, 0)
                WHERE user_id = ?
                """,
                (amount, amount, loser_id),
            )
            self.conn.execute(
                "INSERT INTO transactions(user_id, type, amount, meta, created_at) VALUES(?, 'room_win', ?, ?, ?)",
                (winner_id, winner_prize, f"Победа в комнате #{room_id}", now_iso()),
            )
            self.conn.execute(
                """
                UPDATE rooms
                SET status = 'finished',
                    opponent_id = ?,
                    creator_roll = ?,
                    opponent_roll = ?,
                    winner_id = ?,
                    rerolls = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (opponent_id, creator_roll, opponent_roll, winner_id, rerolls, now_iso(), room_id),
            )

            creator_updated = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (room["creator_id"],)).fetchone()
            opponent_updated = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (opponent_id,)).fetchone()
            room_updated = self.conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
            self.conn.commit()

        return {
            "room": room_updated,
            "creator": creator_updated,
            "opponent": opponent_updated,
            "winner_id": winner_id,
            "loser_id": loser_id,
            "winner_prize": winner_prize,
            "commission": commission,
            "referral_reward": referral_reward,
        }

    async def create_promocode(
        self,
        code: str,
        amount: float,
        activations: int,
        wager_requirement: float,
        created_by: int,
    ) -> None:
        code = code.strip().upper()
        async with self.lock:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO promocodes(code, amount, wager_requirement, activations_left, created_by, is_active, created_at)
                VALUES(?, ?, ?, ?, ?, 1, ?)
                """,
                (code, round(amount, 8), round(wager_requirement, 8), activations, created_by, now_iso()),
            )
            self.conn.commit()

    async def activate_promocode(self, code: str, user_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
        promo_code = code.strip().upper()
        async with self.lock:
            promo = self.conn.execute("SELECT * FROM promocodes WHERE code = ?", (promo_code,)).fetchone()
            if not promo or int(promo["is_active"]) != 1:
                raise ValueError("Промокод не найден")
            if int(promo["activations_left"]) <= 0:
                raise ValueError("Промокод закончился")
            already = self.conn.execute(
                "SELECT 1 FROM promo_activations WHERE code = ? AND user_id = ?",
                (promo_code, user_id),
            ).fetchone()
            if already:
                raise ValueError("Вы уже активировали этот промокод")
            user = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if not user:
                raise ValueError("Пользователь не найден")
            self.conn.execute(
                """
                UPDATE promocodes
                SET activations_left = activations_left - 1,
                    is_active = CASE WHEN activations_left - 1 <= 0 THEN 0 ELSE 1 END
                WHERE code = ?
                """,
                (promo_code,),
            )
            self.conn.execute(
                "INSERT INTO promo_activations(code, user_id, created_at) VALUES(?, ?, ?)",
                (promo_code, user_id, now_iso()),
            )
            self.conn.execute(
                """
                UPDATE users
                SET balance = ?,
                    promo_wager_remaining = promo_wager_remaining + ?
                WHERE user_id = ?
                """,
                (
                    round(float(user["balance"]) + float(promo["amount"]), 8),
                    round(float(promo["wager_requirement"]), 8),
                    user_id,
                ),
            )
            self.conn.execute(
                "INSERT INTO transactions(user_id, type, amount, meta, created_at) VALUES(?, 'promo', ?, ?, ?)",
                (user_id, float(promo["amount"]), promo_code, now_iso()),
            )
            updated_user = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            updated_promo = self.conn.execute("SELECT * FROM promocodes WHERE code = ?", (promo_code,)).fetchone()
            self.conn.commit()
        return updated_user, updated_promo

    async def create_gift_check(
        self,
        amount: float,
        activations: int,
        created_by: int,
        *,
        required_deposit: float = 0.0,
        required_channels: list[str] | None = None,
        password: str | None = None,
        description: str | None = None,
        photo_file_id: str | None = None,
    ) -> sqlite3.Row:
        async with self.lock:
            token = ""
            while True:
                token = secrets.token_hex(6).upper()
                existing = self.conn.execute("SELECT 1 FROM gift_checks WHERE token = ?", (token,)).fetchone()
                if not existing:
                    break
            self.conn.execute(
                """
                INSERT INTO gift_checks(
                    token, amount, activations_total, activations_left, required_deposit, required_channels, password, description, photo_file_id, created_by, created_at, is_active
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    token,
                    round(amount, 8),
                    activations,
                    activations,
                    round(required_deposit, 8),
                    "\n".join(required_channels or []),
                    password.strip() if password else None,
                    description.strip() if description else None,
                    photo_file_id,
                    created_by,
                    now_iso(),
                ),
            )
            row = self.conn.execute("SELECT * FROM gift_checks WHERE token = ?", (token,)).fetchone()
            self.conn.commit()
        return row

    async def get_gift_check(self, token: str) -> sqlite3.Row | None:
        async with self.lock:
            row = self.conn.execute("SELECT * FROM gift_checks WHERE token = ?", (token.strip().upper(),)).fetchone()
        return row

    async def list_gift_checks(self, limit: int = 20) -> list[sqlite3.Row]:
        async with self.lock:
            rows = self.conn.execute(
                """
                SELECT *
                FROM gift_checks
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return rows

    async def delete_gift_check(self, token: str) -> sqlite3.Row | None:
        normalized = token.strip().upper()
        async with self.lock:
            row = self.conn.execute("SELECT * FROM gift_checks WHERE token = ?", (normalized,)).fetchone()
            if not row:
                return None
            self.conn.execute("DELETE FROM gift_checks WHERE token = ?", (normalized,))
            self.conn.commit()
        return row

    async def claim_gift_check(self, token: str, user_id: int, password: str | None = None) -> tuple[sqlite3.Row, sqlite3.Row]:
        normalized = token.strip().upper()
        async with self.lock:
            gift_check = self.conn.execute("SELECT * FROM gift_checks WHERE token = ?", (normalized,)).fetchone()
            if not gift_check or int(gift_check["is_active"]) != 1:
                raise ValueError("Чек не найден")
            if int(gift_check["activations_left"]) <= 0:
                raise ValueError("Чек уже закончился")
            if gift_check["password"]:
                if not password or password.strip() != str(gift_check["password"]):
                    raise ValueError("Неверный пароль")
            already = self.conn.execute(
                "SELECT 1 FROM gift_check_claims WHERE token = ? AND user_id = ?",
                (normalized, user_id),
            ).fetchone()
            if already:
                raise ValueError("Вы уже активировали этот чек")
            user = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if not user:
                raise ValueError("Пользователь не найден")

            if float(user["total_deposit"]) < float(gift_check["required_deposit"]):
                raise ValueError(
                    f"Для активации этого чека нужно пополнить минимум на {fmt_amount(float(gift_check['required_deposit']))} {self.config.bot_asset}"
                )
            new_balance = round(float(user["balance"]) + float(gift_check["amount"]), 8)
            left_after = int(gift_check["activations_left"]) - 1
            self.conn.execute(
                """
                UPDATE gift_checks
                SET activations_left = ?,
                    is_active = CASE WHEN ? <= 0 THEN 0 ELSE 1 END
                WHERE token = ?
                """,
                (left_after, left_after, normalized),
            )
            self.conn.execute(
                "INSERT INTO gift_check_claims(token, user_id, claimed_at) VALUES(?, ?, ?)",
                (normalized, user_id, now_iso()),
            )
            self.conn.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (new_balance, user_id),
            )
            self.conn.execute(
                "INSERT INTO transactions(user_id, type, amount, meta, created_at) VALUES(?, 'gift_check', ?, ?, ?)",
                (user_id, round(float(gift_check["amount"]), 8), f"gift_check:{normalized}", now_iso()),
            )
            updated_user = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            updated_check = self.conn.execute("SELECT * FROM gift_checks WHERE token = ?", (normalized,)).fetchone()
            self.conn.commit()
        return updated_user, updated_check

    async def get_referral_summary(self, user_id: int) -> dict[str, Any]:
        async with self.lock:
            referred_count = self.conn.execute(
                "SELECT COUNT(*) AS total FROM users WHERE invited_by = ?",
                (user_id,),
            ).fetchone()["total"]
            rewards_count = self.conn.execute(
                "SELECT COUNT(*) AS total FROM referral_rewards WHERE inviter_id = ?",
                (user_id,),
            ).fetchone()["total"]
            rewards_sum = self.conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS total FROM referral_rewards WHERE inviter_id = ?",
                (user_id,),
            ).fetchone()["total"]
        return {
            "referred_count": int(referred_count or 0),
            "rewards_count": int(rewards_count or 0),
            "rewards_sum": float(rewards_sum or 0),
        }

    async def get_referrals_top(self, limit: int = 10) -> list[sqlite3.Row]:
        async with self.lock:
            rows = self.conn.execute(
                """
                SELECT
                    u.user_id,
                    u.username,
                    u.full_name,
                    COUNT(r.id) AS rewards_count,
                    COALESCE(SUM(r.amount), 0) AS rewards_sum,
                    (SELECT COUNT(*) FROM users invited WHERE invited.invited_by = u.user_id) AS referred_count
                FROM users u
                LEFT JOIN referral_rewards r ON r.inviter_id = u.user_id
                GROUP BY u.user_id, u.username, u.full_name
                HAVING referred_count > 0 OR rewards_count > 0
                ORDER BY rewards_sum DESC, referred_count DESC, rewards_count DESC, u.user_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return rows

    async def list_user_ids(self) -> list[int]:
        async with self.lock:
            rows = self.conn.execute("SELECT user_id FROM users ORDER BY user_id ASC").fetchall()
        return [int(row["user_id"]) for row in rows]

    async def get_stats(self) -> dict[str, Any]:
        async with self.lock:
            users = self.conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"]
            open_rooms = self.conn.execute("SELECT COUNT(*) AS total FROM rooms WHERE status = 'open'").fetchone()["total"]
            finished_rooms = self.conn.execute("SELECT COUNT(*) AS total FROM rooms WHERE status = 'finished'").fetchone()["total"]
            deposits = self.conn.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM invoices WHERE status = 'paid'").fetchone()["total"]
            withdrawals = self.conn.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM withdrawals WHERE status IN ('created', 'approved')").fetchone()["total"]
        return {
            "users": int(users or 0),
            "open_rooms": int(open_rooms or 0),
            "finished_rooms": int(finished_rooms or 0),
            "deposits": float(deposits or 0),
            "withdrawals": float(withdrawals or 0),
        }
