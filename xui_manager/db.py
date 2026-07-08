from __future__ import annotations

import json
import base64
import hashlib
import hmac
import random
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .auth import hash_password, verify_password
from .billing import bytes_from_gb
from .vless import parse_vless_template, positive_finite_float, positive_int, validate_target_nodes


SUPPORTED_PRODUCT_TYPES = {"subscription", "traffic_pack", "time_pack", "reset_pack"}

PRODUCT_TRANSACTION_LABELS = {
    "subscription": "购买套餐",
    "traffic_pack": "购买流量包",
    "time_pack": "购买时长包",
    "reset_pack": "购买流量重置包",
}


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys=on")
        return conn

    @contextmanager
    def session(self):
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.session() as conn:
            conn.executescript(
                """
                create table if not exists plans (
                    id integer primary key autoincrement,
                    name text not null,
                    quota_bytes integer not null,
                    duration_days integer not null,
                    allowed_tags text not null default '[]',
                    require_approval integer not null default 1,
                    price_cents integer not null default 0,
                    product_type text not null default 'subscription',
                    category text not null default '套餐',
                    description text not null default '',
                    purchase_notice text not null default '',
                    enabled integer not null default 1,
                    created_at integer not null
                );

                create table if not exists users (
                    id integer primary key autoincrement,
                    email text not null unique,
                    password_hash text not null,
                    role text not null default 'user',
                    status text not null default 'pending',
                    plan_id integer,
                    token text not null unique,
                    quota_bytes integer not null default 0,
                    expire_at integer not null default 0,
                    created_at integer not null,
                    approved_at integer not null default 0,
                    balance_cents integer not null default 0,
                    admin_note text not null default '',
                    is_priority integer not null default 0,
                    foreign key(plan_id) references plans(id)
                );

                create table if not exists recharge_cards (
                    id integer primary key autoincrement,
                    code_hash text not null unique,
                    code_suffix text not null,
                    encrypted_code text,
                    amount_cents integer not null,
                    status text not null default 'unused',
                    created_by integer,
                    redeemed_by integer,
                    created_at integer not null,
                    redeemed_at integer not null default 0,
                    foreign key(created_by) references users(id) on delete set null,
                    foreign key(redeemed_by) references users(id) on delete set null
                );

                create table if not exists balance_transactions (
                    id integer primary key autoincrement,
                    user_id integer not null,
                    amount_cents integer not null,
                    balance_after_cents integer not null,
                    kind text not null,
                    note text not null default '',
                    recharge_card_id integer,
                    created_by integer,
                    created_at integer not null,
                    foreign key(user_id) references users(id) on delete cascade,
                    foreign key(recharge_card_id) references recharge_cards(id) on delete set null,
                    foreign key(created_by) references users(id) on delete set null
                );

                create table if not exists panels (
                    id integer primary key autoincrement,
                    name text not null,
                    base_url text not null,
                    username text not null,
                    password text not null,
                    subscription_url text not null default '',
                    verify_tls integer not null default 1,
                    enabled integer not null default 1,
                    created_at integer not null
                );

                create table if not exists nodes (
                    id integer primary key autoincrement,
                    name text not null,
                    panel_id integer,
                    inbound_id integer not null default 0,
                    source_url text not null,
                    rate real not null default 1,
                    tags text not null default '[]',
                    enabled integer not null default 1,
                    latency_ms integer,
                    status text not null default 'unknown',
                    last_checked_at integer not null default 0,
                    created_at integer not null,
                    foreign key(panel_id) references panels(id)
                );

                create table if not exists checkin_records (
                    id integer primary key autoincrement,
                    user_id integer not null,
                    checkin_date text not null,
                    reward_bytes integer not null,
                    created_at integer not null,
                    unique(user_id, checkin_date),
                    foreign key(user_id) references users(id) on delete cascade
                );

                create table if not exists tickets (
                    id integer primary key autoincrement,
                    user_id integer not null,
                    subject text not null,
                    message text not null,
                    status text not null default 'open',
                    created_at integer not null,
                    updated_at integer not null,
                    foreign key(user_id) references users(id) on delete cascade
                );

                create table if not exists ticket_replies (
                    id integer primary key autoincrement,
                    ticket_id integer not null,
                    user_id integer,
                    role text not null,
                    message text not null,
                    created_at integer not null,
                    foreign key(ticket_id) references tickets(id) on delete cascade,
                    foreign key(user_id) references users(id) on delete set null
                );

                create table if not exists tutorials (
                    id integer primary key autoincrement,
                    platform text not null default '通用',
                    title text not null,
                    content text not null,
                    image_url text not null default '',
                    enabled integer not null default 1,
                    sort_order integer not null default 0,
                    created_at integer not null,
                    updated_at integer not null
                );

                create table if not exists usage_records (
                    id integer primary key autoincrement,
                    user_id integer not null,
                    node_id integer not null,
                    upload integer not null default 0,
                    download integer not null default 0,
                    updated_at integer not null,
                    unique(user_id, node_id),
                    foreign key(user_id) references users(id),
                    foreign key(node_id) references nodes(id)
                );

                create table if not exists sessions (
                    token text primary key,
                    user_id integer not null,
                    created_at integer not null,
                    foreign key(user_id) references users(id)
                );

                create table if not exists managed_clients (
                    id integer primary key autoincrement,
                    user_id integer not null,
                    panel_id integer not null,
                    inbound_id integer not null,
                    protocol text not null default 'vless',
                    client_uuid text not null,
                    remote_email text not null,
                    flow text not null default '',
                    rate real not null default 1,
                    desired_expire_at integer not null default 0,
                    desired_enabled integer not null default 1,
                    state text not null default 'pending',
                    remote_enabled integer not null default 0,
                    last_error text not null default '',
                    attempt_count integer not null default 0,
                    last_attempt_at integer not null default 0,
                    last_synced_at integer not null default 0,
                    created_at integer not null,
                    updated_at integer not null,
                    unique(user_id, panel_id, inbound_id),
                    foreign key(user_id) references users(id),
                    foreign key(panel_id) references panels(id)
                );

                create table if not exists usage_ledgers (
                    managed_client_id integer primary key,
                    last_remote_up integer not null default 0,
                    last_remote_down integer not null default 0,
                    raw_up integer not null default 0,
                    raw_down integer not null default 0,
                    weighted_up integer not null default 0,
                    weighted_down integer not null default 0,
                    rate real not null default 1,
                    reset_pending integer not null default 0,
                    updated_at integer not null default 0,
                    foreign key(managed_client_id) references managed_clients(id) on delete cascade
                );

                create table if not exists app_settings (
                    key text primary key,
                    value text not null
                );

                create index if not exists idx_managed_clients_user_state
                on managed_clients(user_id, state);

                create index if not exists idx_managed_clients_panel_inbound
                on managed_clients(panel_id, inbound_id);

                create index if not exists idx_balance_transactions_user_created
                on balance_transactions(user_id, created_at desc, id desc);

                create index if not exists idx_recharge_cards_status_created
                on recharge_cards(status, created_at desc, id desc);

                create index if not exists idx_checkin_records_user_date
                on checkin_records(user_id, checkin_date desc);

                create index if not exists idx_tickets_user_updated
                on tickets(user_id, updated_at desc, id desc);

                create index if not exists idx_ticket_replies_ticket_created
                on ticket_replies(ticket_id, created_at, id);

                create index if not exists idx_tutorials_platform_sort
                on tutorials(enabled, platform, sort_order, id);
                """
            )
            self._ensure_column(conn, "plans", "price_cents", "integer not null default 0")
            self._ensure_column(conn, "plans", "product_type", "text not null default 'subscription'")
            self._ensure_column(conn, "plans", "category", "text not null default '套餐'")
            self._ensure_column(conn, "plans", "description", "text not null default ''")
            self._ensure_column(conn, "plans", "purchase_notice", "text not null default ''")
            self._ensure_column(conn, "users", "balance_cents", "integer not null default 0")
            self._ensure_column(conn, "users", "admin_note", "text not null default ''")
            self._ensure_column(conn, "users", "is_priority", "integer not null default 0")
            self._ensure_column(conn, "recharge_cards", "encrypted_code", "text")
            self._ensure_column(conn, "nodes", "inbound_id", "integer not null default 0")
            self._ensure_column(conn, "nodes", "mode", "text not null default 'static'")
            self._ensure_column(conn, "nodes", "latency_ms", "integer")
            self._ensure_column(conn, "nodes", "status", "text not null default 'unknown'")
            self._ensure_column(conn, "nodes", "last_checked_at", "integer not null default 0")
            self._ensure_column(conn, "usage_ledgers", "reset_pending", "integer not null default 0")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = [row["name"] for row in conn.execute(f"pragma table_info({table})")]
        if column not in columns:
            conn.execute(f"alter table {table} add column {column} {definition}")

    def seed_admin(self, email: str, password: str) -> dict[str, Any]:
        existing = self.get_user_by_email(email)
        if existing:
            return existing
        now = int(time.time())
        with self.session() as conn:
            cur = conn.execute(
                """
                insert into users(email, password_hash, role, status, token, created_at, approved_at)
                values (?, ?, 'admin', 'active', ?, ?, ?)
                """,
                (email, hash_password(password), secrets.token_urlsafe(24), now, now),
            )
            user_id = int(cur.lastrowid)
        return self.get_user(user_id)

    def create_plan(
        self,
        name: str,
        quota_gb: float,
        duration_days: int,
        allowed_tags: list[str],
        require_approval: bool,
        enabled: bool = True,
        price_cents: int = 0,
        product_type: str = "subscription",
        category: str = "套餐",
        description: str = "",
        purchase_notice: str = "",
    ) -> int:
        name = name.strip()
        if not name:
            raise ValueError("plan name is required")
        product_type = self._normalize_product_type(product_type)
        category = self._normalize_text(category, "套餐", 40)
        description = self._normalize_text(description, "", 1000)
        purchase_notice = self._normalize_text(purchase_notice, "", 1000)
        with self.session() as conn:
            conn.execute("begin immediate")
            existing = conn.execute("select id from plans where lower(name)=lower(?)", (name,)).fetchone()
            if existing:
                raise ValueError("plan name already exists")
            cur = conn.execute(
                """
                insert into plans(
                    name, quota_bytes, duration_days, allowed_tags, require_approval,
                    price_cents, product_type, category, description, purchase_notice, enabled, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    bytes_from_gb(quota_gb),
                    int(duration_days),
                    json.dumps(allowed_tags),
                    int(require_approval),
                    max(int(price_cents), 0),
                    product_type,
                    category,
                    description,
                    purchase_notice,
                    int(enabled),
                    int(time.time()),
                ),
            )
            return int(cur.lastrowid)

    def update_plan(
        self,
        plan_id: int,
        name: str,
        quota_gb: float,
        duration_days: int,
        allowed_tags: list[str],
        require_approval: bool,
        enabled: bool = True,
        price_cents: int = 0,
        product_type: str = "subscription",
        category: str = "套餐",
        description: str = "",
        purchase_notice: str = "",
    ) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("plan name is required")
        product_type = self._normalize_product_type(product_type)
        category = self._normalize_text(category, "套餐", 40)
        description = self._normalize_text(description, "", 1000)
        purchase_notice = self._normalize_text(purchase_notice, "", 1000)
        with self.session() as conn:
            existing = conn.execute(
                "select id from plans where lower(name)=lower(?) and id<>?",
                (name, int(plan_id)),
            ).fetchone()
            if existing:
                raise ValueError("plan name already exists")
            conn.execute(
                """
                update plans
                set name=?, quota_bytes=?, duration_days=?, allowed_tags=?, require_approval=?,
                    price_cents=?, product_type=?, category=?, description=?, purchase_notice=?, enabled=?
                where id=?
                """,
                (
                    name,
                    bytes_from_gb(quota_gb),
                    int(duration_days),
                    json.dumps(allowed_tags),
                    int(require_approval),
                    max(int(price_cents), 0),
                    product_type,
                    category,
                    description,
                    purchase_notice,
                    int(enabled),
                    int(plan_id),
                ),
            )
        plan = self.get_plan(plan_id)
        if not plan:
            raise ValueError("plan not found")
        return plan

    def delete_plan(self, plan_id: int) -> None:
        with self.session() as conn:
            in_use = conn.execute("select 1 from users where plan_id=? limit 1", (int(plan_id),)).fetchone()
            if in_use:
                raise ValueError("plan is in use")
            result = conn.execute("delete from plans where id=?", (int(plan_id),))
            if result.rowcount == 0:
                raise ValueError("plan not found")

    def list_plans(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "select * from plans"
        params: tuple[Any, ...] = ()
        if enabled_only:
            sql += " where enabled=1"
        sql += " order by id"
        with self.session() as conn:
            return [self._decode_plan(row) for row in conn.execute(sql, params)]

    def get_plan(self, plan_id: int) -> dict[str, Any] | None:
        with self.session() as conn:
            row = conn.execute("select * from plans where id=?", (plan_id,)).fetchone()
            return self._decode_plan(row) if row else None

    def register_user(self, email: str, password: str, plan_id: int | None = None) -> dict[str, Any]:
        email = email.strip().lower()
        if "@" not in email:
            raise ValueError("invalid email")
        if len(password) < 6:
            raise ValueError("password too short")
        now = int(time.time())
        plan = self.get_plan(plan_id) if plan_id is not None else None
        if plan_id is not None and (not plan or not plan["enabled"]):
            raise ValueError("plan not found")
        status = "unsubscribed"
        expire_at = 0
        quota_bytes = 0
        approved_at = 0
        if plan:
            status = "pending" if plan["require_approval"] else "active"
            expire_at = now + plan["duration_days"] * 86400 if status == "active" else 0
            quota_bytes = plan["quota_bytes"] if status == "active" else 0
            approved_at = now if status == "active" else 0
        with self.session() as conn:
            cur = conn.execute(
                """
                insert into users(email, password_hash, status, plan_id, token, quota_bytes, expire_at, created_at, approved_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    email,
                    hash_password(password),
                    status,
                    plan_id,
                    secrets.token_urlsafe(24),
                    quota_bytes,
                    expire_at,
                    now,
                    approved_at,
                ),
            )
            user_id = int(cur.lastrowid)
        return self.get_user(user_id)

    def apply_plan(self, user_id: int, plan_id: int) -> dict[str, Any]:
        now = int(time.time())
        with self.session() as conn:
            conn.execute("begin immediate")
            user_row = conn.execute("select * from users where id=?", (int(user_id),)).fetchone()
            if not user_row:
                raise ValueError("user not found")
            user = self._decode_user(user_row)
            if user.get("role") != "user":
                raise ValueError("管理员账号不能申请套餐")
            if user.get("status") != "unsubscribed" or user.get("plan_id") is not None:
                raise ValueError("已有套餐或申请，不能重复提交")
            plan_row = conn.execute("select * from plans where id=? and enabled=1", (int(plan_id),)).fetchone()
            if not plan_row:
                raise ValueError("套餐不存在或已停售")
            plan = self._decode_plan(plan_row)
            status = "pending" if plan["require_approval"] else "active"
            quota_bytes = plan["quota_bytes"] if status == "active" else 0
            expire_at = now + plan["duration_days"] * 86400 if status == "active" else 0
            approved_at = now if status == "active" else 0
            conn.execute(
                """
                update users
                set status=?, plan_id=?, quota_bytes=?, expire_at=?, approved_at=?
                where id=?
                """,
                (status, int(plan_id), quota_bytes, expire_at, approved_at, int(user_id)),
            )
        return self.get_user(user_id)

    def approve_user(self, user_id: int) -> dict[str, Any]:
        user = self.get_user(user_id)
        if not user:
            raise ValueError("user not found")
        plan = self.get_plan(user["plan_id"])
        if not plan:
            raise ValueError("plan not found")
        now = int(time.time())
        expire_at = now + plan["duration_days"] * 86400
        with self.session() as conn:
            conn.execute(
                """
                update users
                set status='active', quota_bytes=?, expire_at=?, approved_at=?
                where id=?
                """,
                (plan["quota_bytes"], expire_at, now, user_id),
            )
        return self.get_user(user_id)

    def update_user_status(self, user_id: int, status: str) -> dict[str, Any]:
        if status not in {"unsubscribed", "pending", "active", "disabled"}:
            raise ValueError("invalid status")
        with self.session() as conn:
            conn.execute("update users set status=? where id=?", (status, user_id))
        return self.get_user(user_id)

    def authenticate(self, email: str, password: str) -> dict[str, Any] | None:
        user = self.get_user_by_email(email.strip().lower())
        if user and verify_password(password, user["password_hash"]):
            return user
        return None

    def update_password(self, user_id: int, current_password: str, new_password: str) -> dict[str, Any]:
        if len(new_password) < 6:
            raise ValueError("password too short")
        with self.session() as conn:
            row = conn.execute("select * from users where id=?", (int(user_id),)).fetchone()
            if not row:
                raise ValueError("user not found")
            user = self._decode_user(row)
            if not verify_password(current_password, user["password_hash"]):
                raise ValueError("当前密码不正确")
            conn.execute("update users set password_hash=? where id=?", (hash_password(new_password), int(user_id)))
        return self.get_user(user_id)

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        with self.session() as conn:
            conn.execute(
                "insert into sessions(token, user_id, created_at) values (?, ?, ?)",
                (token, user_id, int(time.time())),
            )
        return token

    def delete_session(self, token: str) -> None:
        if not token:
            return
        with self.session() as conn:
            conn.execute("delete from sessions where token=?", (token,))

    def get_session_user(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        with self.session() as conn:
            row = conn.execute(
                "select users.* from sessions join users on users.id=sessions.user_id where sessions.token=?",
                (token,),
            ).fetchone()
            return self._decode_user(row) if row else None

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self.session() as conn:
            row = conn.execute("select * from users where id=?", (user_id,)).fetchone()
            return self._decode_user(row) if row else None

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        with self.session() as conn:
            row = conn.execute("select * from users where email=?", (email,)).fetchone()
            return self._decode_user(row) if row else None

    def get_user_by_token(self, token: str) -> dict[str, Any] | None:
        with self.session() as conn:
            row = conn.execute("select * from users where token=?", (token,)).fetchone()
            return self._decode_user(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        with self.session() as conn:
            return [self._decode_user(row) for row in conn.execute("select * from users order by id")]

    def purchase_plan(self, user_id: int, plan_id: int) -> dict[str, Any]:
        now = int(time.time())
        with self.session() as conn:
            conn.execute("begin immediate")
            user = conn.execute("select * from users where id=?", (int(user_id),)).fetchone()
            if not user:
                raise ValueError("用户不存在")
            if user["role"] != "user":
                raise ValueError("管理员账号不能购买套餐")
            if user["status"] == "disabled":
                raise ValueError("账号已停用")
            plan = conn.execute("select * from plans where id=? and enabled=1", (int(plan_id),)).fetchone()
            if not plan:
                raise ValueError("套餐不存在或已停售")
            plan = self._decode_plan(plan)
            product_type = plan["product_type"]
            has_active_subscription = (
                user["status"] == "active"
                and user["plan_id"] is not None
                and int(user["expire_at"] or 0) > now
            )
            if product_type != "subscription" and not has_active_subscription:
                raise ValueError("需要先开通有效套餐后才能购买此商品")
            price_cents = int(plan["price_cents"] or 0)
            balance_cents = int(user["balance_cents"] or 0)
            if balance_cents < price_cents:
                raise ValueError("余额不足，请先使用充值卡充值")
            new_balance = balance_cents - price_cents
            if product_type == "subscription":
                expire_at = now + int(plan["duration_days"]) * 86400
                conn.execute(
                    """
                    update users
                    set balance_cents=?, status='active', plan_id=?, quota_bytes=?, expire_at=?, approved_at=?
                    where id=?
                    """,
                    (new_balance, int(plan_id), int(plan["quota_bytes"]), expire_at, now, int(user_id)),
                )
                self._reset_user_usage(conn, int(user_id), now)
            elif product_type == "traffic_pack":
                conn.execute(
                    """
                    update users
                    set balance_cents=?, quota_bytes=?
                    where id=?
                    """,
                    (new_balance, int(user["quota_bytes"] or 0) + int(plan["quota_bytes"] or 0), int(user_id)),
                )
            elif product_type == "time_pack":
                base_expire_at = max(int(user["expire_at"] or 0), now)
                conn.execute(
                    """
                    update users
                    set balance_cents=?, expire_at=?
                    where id=?
                    """,
                    (new_balance, base_expire_at + int(plan["duration_days"]) * 86400, int(user_id)),
                )
            elif product_type == "reset_pack":
                conn.execute("update users set balance_cents=? where id=?", (new_balance, int(user_id)))
                self._reset_user_usage(conn, int(user_id), now)
            self._insert_balance_transaction(
                conn,
                int(user_id),
                -price_cents,
                new_balance,
                "purchase",
                f"{PRODUCT_TRANSACTION_LABELS[product_type]}：{plan['name']}",
                None,
                int(user_id),
                now,
            )
        return self.get_user(user_id)

    def adjust_user_balance(self, user_id: int, amount_cents: int, note: str, created_by: int) -> dict[str, Any]:
        amount_cents = int(amount_cents)
        note = str(note or "").strip()
        if amount_cents == 0:
            raise ValueError("调整金额不能为 0")
        if not note:
            raise ValueError("请填写余额调整原因")
        now = int(time.time())
        with self.session() as conn:
            conn.execute("begin immediate")
            user = conn.execute("select balance_cents from users where id=?", (int(user_id),)).fetchone()
            if not user:
                raise ValueError("用户不存在")
            new_balance = int(user["balance_cents"] or 0) + amount_cents
            if new_balance < 0:
                raise ValueError("余额不能为负数")
            conn.execute("update users set balance_cents=? where id=?", (new_balance, int(user_id)))
            self._insert_balance_transaction(
                conn, int(user_id), amount_cents, new_balance, "admin_adjustment", note, None, int(created_by), now
            )
        return self.get_user(user_id)

    def update_user_note(self, user_id: int, note: str, is_priority: bool) -> dict[str, Any]:
        with self.session() as conn:
            result = conn.execute(
                "update users set admin_note=?, is_priority=? where id=?",
                (str(note or "").strip()[:500], int(bool(is_priority)), int(user_id)),
            )
            if result.rowcount == 0:
                raise ValueError("用户不存在")
        return self.get_user(user_id)

    def create_recharge_cards(
        self,
        amount_cents: int,
        count: int,
        created_by: int,
        encryption_secret: str | None = None,
    ) -> list[dict[str, Any]]:
        amount_cents = int(amount_cents)
        count = int(count)
        if amount_cents <= 0:
            raise ValueError("充值卡金额必须大于 0")
        if count < 1 or count > 100:
            raise ValueError("单次生成数量必须在 1 到 100 之间")
        now = int(time.time())
        cards: list[dict[str, Any]] = []
        normalized_secret = str(encryption_secret or "").strip()
        with self.session() as conn:
            conn.execute("begin immediate")
            for _ in range(count):
                raw = secrets.token_hex(12).upper()
                code = "HXY-" + "-".join(raw[index:index + 4] for index in range(0, len(raw), 4))
                digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
                encrypted_code = encrypt_text(code, normalized_secret) if normalized_secret else None
                cur = conn.execute(
                    """
                    insert into recharge_cards(code_hash, code_suffix, encrypted_code, amount_cents, created_by, created_at)
                    values (?, ?, ?, ?, ?, ?)
                    """,
                    (digest, code[-4:], encrypted_code, amount_cents, int(created_by), now),
                )
                cards.append({"id": int(cur.lastrowid), "code": code, "amount_cents": amount_cents})
        return cards

    def reveal_recharge_card(self, card_id: int, encryption_secret: str) -> dict[str, Any]:
        if not str(encryption_secret or "").strip():
            raise ValueError("RECHARGE_CARD_SECRET 未配置，无法查看完整卡密")
        with self.session() as conn:
            row = conn.execute(
                "select id, encrypted_code, code_suffix, amount_cents, status from recharge_cards where id=?",
                (int(card_id),),
            ).fetchone()
            if not row:
                raise ValueError("充值卡不存在")
            if not row["encrypted_code"]:
                raise ValueError("旧充值卡无法查看完整卡密")
            code = decrypt_text(str(row["encrypted_code"]), str(encryption_secret).strip())
            return {
                "id": int(row["id"]),
                "code": code,
                "amount_cents": int(row["amount_cents"]),
                "status": row["status"],
                "masked_code": f"HXY-****-****-{row['code_suffix']}",
            }

    def redeem_recharge_card(self, user_id: int, code: str) -> dict[str, Any]:
        normalized = str(code or "").strip().upper()
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        now = int(time.time())
        with self.session() as conn:
            conn.execute("begin immediate")
            user = conn.execute("select balance_cents from users where id=?", (int(user_id),)).fetchone()
            if not user:
                raise ValueError("用户不存在")
            card = conn.execute(
                "select * from recharge_cards where code_hash=? and status='unused'", (digest,)
            ).fetchone()
            if not card:
                raise ValueError("充值卡无效或已使用")
            new_balance = int(user["balance_cents"] or 0) + int(card["amount_cents"])
            updated = conn.execute(
                """
                update recharge_cards set status='used', redeemed_by=?, redeemed_at=?
                where id=? and status='unused'
                """,
                (int(user_id), now, int(card["id"])),
            )
            if updated.rowcount != 1:
                raise ValueError("充值卡无效或已使用")
            conn.execute("update users set balance_cents=? where id=?", (new_balance, int(user_id)))
            self._insert_balance_transaction(
                conn,
                int(user_id),
                int(card["amount_cents"]),
                new_balance,
                "recharge_card",
                f"充值卡 ****{card['code_suffix']}",
                int(card["id"]),
                int(user_id),
                now,
            )
        return self.get_user(user_id)

    def list_balance_transactions(self, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute(
                """
                select * from balance_transactions where user_id=?
                order by created_at desc, id desc limit ?
                """,
                (int(user_id), max(1, min(int(limit), 200))),
            )
            return [dict(row) for row in rows]

    def list_recharge_cards(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute(
                """
                select recharge_cards.id, recharge_cards.code_suffix, recharge_cards.encrypted_code, recharge_cards.amount_cents,
                       recharge_cards.status, recharge_cards.created_at, recharge_cards.redeemed_at,
                       creator.email as created_by_email, redeemer.email as redeemed_by_email
                from recharge_cards
                left join users creator on creator.id=recharge_cards.created_by
                left join users redeemer on redeemer.id=recharge_cards.redeemed_by
                order by recharge_cards.created_at desc, recharge_cards.id desc limit ?
                """,
                (max(1, min(int(limit), 500)),),
            )
            cards: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                encrypted_code = item.pop("encrypted_code", None)
                item["masked_code"] = f"HXY-****-****-{row['code_suffix']}"
                item["can_reveal"] = bool(encrypted_code)
                cards.append(item)
            return cards

    def _insert_balance_transaction(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        amount_cents: int,
        balance_after_cents: int,
        kind: str,
        note: str,
        recharge_card_id: int | None,
        created_by: int | None,
        created_at: int,
    ) -> None:
        conn.execute(
            """
            insert into balance_transactions(
                user_id, amount_cents, balance_after_cents, kind, note,
                recharge_card_id, created_by, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                amount_cents,
                balance_after_cents,
                kind,
                note,
                recharge_card_id,
                created_by,
                created_at,
            ),
        )

    def delete_user(self, user_id: int) -> None:
        user_id = int(user_id)
        with self.session() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select role, status from users where id=?", (user_id,)).fetchone()
            if not row:
                raise ValueError("user not found")
            if row["role"] == "admin" or row["status"] != "disabled":
                raise ValueError("only disabled users can be deleted")
            conn.execute(
                "delete from usage_ledgers where managed_client_id in (select id from managed_clients where user_id=?)",
                (user_id,),
            )
            conn.execute("delete from managed_clients where user_id=?", (user_id,))
            conn.execute("delete from usage_records where user_id=?", (user_id,))
            conn.execute("delete from sessions where user_id=?", (user_id,))
            conn.execute("delete from users where id=?", (user_id,))

    def create_panel(
        self,
        name: str,
        base_url: str,
        username: str,
        password: str,
        subscription_url: str = "",
        verify_tls: bool = True,
        enabled: bool = True,
    ) -> int:
        base_url = normalize_panel_url(base_url)
        with self.session() as conn:
            conn.execute("begin immediate")
            existing = conn.execute("select id from panels where lower(base_url)=lower(?)", (base_url,)).fetchone()
            if existing:
                raise ValueError("panel address already exists")
            cur = conn.execute(
                """
                insert into panels(name, base_url, username, password, subscription_url, verify_tls, enabled, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (name, base_url, username, password, subscription_url, int(verify_tls), int(enabled), int(time.time())),
            )
            return int(cur.lastrowid)

    def update_panel(
        self,
        panel_id: int,
        name: str,
        base_url: str,
        username: str,
        password: str,
        subscription_url: str = "",
        verify_tls: bool = True,
        enabled: bool = True,
    ) -> dict[str, Any]:
        base_url = normalize_panel_url(base_url)
        with self.session() as conn:
            existing = conn.execute(
                "select id from panels where lower(base_url)=lower(?) and id<>?",
                (base_url, int(panel_id)),
            ).fetchone()
            if existing:
                raise ValueError("panel address already exists")
            conn.execute(
                """
                update panels
                set name=?, base_url=?, username=?, password=?, subscription_url=?, verify_tls=?, enabled=?
                where id=?
                """,
                (
                    name,
                    base_url,
                    username,
                    password,
                    subscription_url,
                    int(verify_tls),
                    int(enabled),
                    int(panel_id),
                ),
            )
        with self.session() as conn:
            row = conn.execute("select * from panels where id=?", (panel_id,)).fetchone()
            if not row:
                raise ValueError("panel not found")
            return dict(row)

    def delete_panel(self, panel_id: int) -> None:
        with self.session() as conn:
            in_use = conn.execute("select 1 from nodes where panel_id=? limit 1", (int(panel_id),)).fetchone()
            managed_in_use = conn.execute(
                "select 1 from managed_clients where panel_id=? limit 1",
                (int(panel_id),),
            ).fetchone()
            if in_use or managed_in_use:
                raise ValueError("panel is in use")
            result = conn.execute("delete from panels where id=?", (int(panel_id),))
            if result.rowcount == 0:
                raise ValueError("panel not found")

    def list_panels(self) -> list[dict[str, Any]]:
        with self.session() as conn:
            return [dict(row) for row in conn.execute("select * from panels order by id")]

    def create_node(
        self,
        name: str,
        source_url: str,
        rate: float,
        tags: list[str],
        enabled: bool = True,
        panel_id: int | None = None,
        inbound_id: int = 0,
        mode: str = "static",
    ) -> int:
        with self.session() as conn:
            mode, panel_id, inbound_id, rate = self._validate_node_input(
                conn,
                name=name,
                source_url=source_url,
                rate=rate,
                tags=tags,
                enabled=enabled,
                panel_id=panel_id,
                inbound_id=inbound_id,
                mode=mode,
            )
            cur = conn.execute(
                """
                insert into nodes(name, panel_id, inbound_id, mode, source_url, rate, tags, enabled, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    panel_id,
                    inbound_id,
                    mode,
                    source_url,
                    rate,
                    json.dumps(tags),
                    int(enabled),
                    int(time.time()),
                ),
            )
            return int(cur.lastrowid)

    def update_node(
        self,
        node_id: int,
        name: str,
        source_url: str,
        rate: float,
        tags: list[str],
        enabled: bool = True,
        panel_id: int | None = None,
        inbound_id: int = 0,
        mode: str = "static",
    ) -> dict[str, Any]:
        with self.session() as conn:
            existing = conn.execute("select id from nodes where id=?", (int(node_id),)).fetchone()
            if not existing:
                raise ValueError("node not found")
            mode, panel_id, inbound_id, rate = self._validate_node_input(
                conn,
                name=name,
                source_url=source_url,
                rate=rate,
                tags=tags,
                enabled=enabled,
                panel_id=panel_id,
                inbound_id=inbound_id,
                mode=mode,
                exclude_node_id=int(node_id),
            )
            result = conn.execute(
                """
                update nodes
                set name=?, panel_id=?, inbound_id=?, mode=?, source_url=?, rate=?, tags=?, enabled=?
                where id=?
                """,
                (
                    name,
                    panel_id,
                    inbound_id,
                    mode,
                    source_url,
                    rate,
                    json.dumps(tags),
                    int(enabled),
                    int(node_id),
                ),
            )
            if result.rowcount == 0:
                raise ValueError("node not found")
            row = conn.execute("select * from nodes where id=?", (node_id,)).fetchone()
            return self._decode_node(row)

    def list_nodes(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "select * from nodes"
        if enabled_only:
            sql += " where enabled=1"
        sql += " order by id"
        with self.session() as conn:
            return [self._decode_node(row) for row in conn.execute(sql)]

    def update_node_status(self, node_id: int, status: str, latency_ms: int | None = None) -> dict[str, Any]:
        status = str(status or "unknown").strip().lower()
        if status not in {"online", "offline", "degraded", "unknown", "maintenance"}:
            raise ValueError("invalid node status")
        latency_value = None if latency_ms is None or latency_ms == "" else max(int(latency_ms), 0)
        now = int(time.time())
        with self.session() as conn:
            result = conn.execute(
                "update nodes set status=?, latency_ms=?, last_checked_at=? where id=?",
                (status, latency_value, now, int(node_id)),
            )
            if result.rowcount == 0:
                raise ValueError("node not found")
            row = conn.execute("select * from nodes where id=?", (int(node_id),)).fetchone()
            return self._decode_node(row)

    def delete_node(self, node_id: int) -> None:
        with self.session() as conn:
            conn.execute("delete from usage_records where node_id=?", (int(node_id),))
            result = conn.execute("delete from nodes where id=?", (int(node_id),))
            if result.rowcount == 0:
                raise ValueError("node not found")

    def record_usage(self, user_id: int, node_id: int, upload: int, download: int) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                """
                insert into usage_records(user_id, node_id, upload, download, updated_at)
                values (?, ?, ?, ?, ?)
                on conflict(user_id, node_id) do update set
                    upload=excluded.upload,
                    download=excluded.download,
                    updated_at=excluded.updated_at
                """,
                (user_id, node_id, int(upload), int(download), now),
            )

    def usage_for_user(self, user_id: int) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute(
                """
                select usage_records.*, nodes.rate, nodes.name as node_name
                    , nodes.mode
                from usage_records
                join nodes on nodes.id=usage_records.node_id
                where usage_records.user_id=?
                """,
                (user_id,),
            )
            return [dict(row) for row in rows]

    def ensure_managed_client(
        self,
        user_id: int,
        panel_id: int,
        inbound_id: int,
        protocol: str,
        flow: str,
        rate: float,
        expire_at: int,
    ) -> dict[str, Any]:
        client_uuid = str(uuid.uuid4())
        remote_email = f"xum-u{int(user_id)}-p{int(panel_id)}-i{int(inbound_id)}"
        now = int(time.time())
        with self.session() as conn:
            conn.execute("begin immediate")
            conn.execute(
                """
                insert into managed_clients(
                    user_id, panel_id, inbound_id, protocol, client_uuid, remote_email,
                    flow, rate, desired_expire_at, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(user_id, panel_id, inbound_id) do nothing
                """,
                (
                    int(user_id),
                    int(panel_id),
                    int(inbound_id),
                    protocol,
                    client_uuid,
                    remote_email,
                    flow,
                    float(rate),
                    int(expire_at),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                select * from managed_clients
                where user_id=? and panel_id=? and inbound_id=?
                """,
                (int(user_id), int(panel_id), int(inbound_id)),
            ).fetchone()
            return self._decode_managed_client(row)

    def get_managed_client(self, client_id: int) -> dict[str, Any] | None:
        with self.session() as conn:
            row = conn.execute("select * from managed_clients where id=?", (int(client_id),)).fetchone()
            return self._decode_managed_client(row) if row else None

    def get_managed_client_for_target(
        self, user_id: int, panel_id: int, inbound_id: int
    ) -> dict[str, Any] | None:
        with self.session() as conn:
            row = conn.execute(
                """
                select * from managed_clients
                where user_id=? and panel_id=? and inbound_id=?
                """,
                (int(user_id), int(panel_id), int(inbound_id)),
            ).fetchone()
            return self._decode_managed_client(row) if row else None

    def list_managed_clients(
        self, user_id: int | None = None, states: list[str] | tuple[str, ...] | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(int(user_id))
        if states is not None:
            if not states:
                return []
            clauses.append(f"state in ({','.join('?' for _ in states)})")
            params.extend(states)

        sql = "select * from managed_clients"
        if clauses:
            sql += " where " + " and ".join(clauses)
        sql += " order by id"
        with self.session() as conn:
            return [self._decode_managed_client(row) for row in conn.execute(sql, params)]

    def update_managed_client_result(
        self,
        client_id: int,
        *,
        state: str,
        remote_enabled: bool,
        error: str,
    ) -> None:
        now = int(time.time())
        with self.session() as conn:
            result = conn.execute(
                """
                update managed_clients
                set state=?, remote_enabled=?, last_error=?,
                    attempt_count=attempt_count+1, last_attempt_at=?, updated_at=?
                where id=?
                """,
                (state, int(remote_enabled), error, now, now, int(client_id)),
            )
            if result.rowcount == 0:
                raise ValueError("managed client not found")

    def set_managed_client_desired(
        self, client_id: int, *, enabled: bool, expire_at: int
    ) -> None:
        with self.session() as conn:
            result = conn.execute(
                """
                update managed_clients
                set desired_enabled=?, desired_expire_at=?, updated_at=?
                where id=?
                """,
                (int(enabled), int(expire_at), int(time.time()), int(client_id)),
            )
            if result.rowcount == 0:
                raise ValueError("managed client not found")

    def set_managed_client_rate(self, client_id: int, rate: float) -> None:
        with self.session() as conn:
            result = conn.execute(
                "update managed_clients set rate=?, updated_at=? where id=?",
                (float(rate), int(time.time()), int(client_id)),
            )
            if result.rowcount == 0:
                raise ValueError("managed client not found")

    def get_usage_ledger(self, managed_client_id: int) -> dict[str, Any] | None:
        with self.session() as conn:
            row = conn.execute(
                "select * from usage_ledgers where managed_client_id=?",
                (int(managed_client_id),),
            ).fetchone()
            return dict(row) if row else None

    def advance_usage_ledger(
        self, managed_client_id: int, remote_up: int, remote_down: int, rate: float
    ) -> dict[str, Any]:
        remote_up = int(remote_up)
        remote_down = int(remote_down)
        rate = float(rate)
        now = int(time.time())
        with self.session() as conn:
            conn.execute("begin immediate")
            previous = conn.execute(
                "select * from usage_ledgers where managed_client_id=?",
                (int(managed_client_id),),
            ).fetchone()
            previous_up = int(previous["last_remote_up"]) if previous else 0
            previous_down = int(previous["last_remote_down"]) if previous else 0
            reset_pending = bool(previous["reset_pending"]) if previous else False
            if reset_pending:
                delta_up = 0
                delta_down = 0
            else:
                delta_up = remote_up - previous_up if remote_up >= previous_up else remote_up
                delta_down = remote_down - previous_down if remote_down >= previous_down else remote_down
            raw_up = (int(previous["raw_up"]) if previous else 0) + delta_up
            raw_down = (int(previous["raw_down"]) if previous else 0) + delta_down
            weighted_up = (int(previous["weighted_up"]) if previous else 0) + int(delta_up * rate)
            weighted_down = (int(previous["weighted_down"]) if previous else 0) + int(delta_down * rate)

            conn.execute(
                """
                insert into usage_ledgers(
                    managed_client_id, last_remote_up, last_remote_down, raw_up, raw_down,
                    weighted_up, weighted_down, rate, reset_pending, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(managed_client_id) do update set
                    last_remote_up=excluded.last_remote_up,
                    last_remote_down=excluded.last_remote_down,
                    raw_up=excluded.raw_up,
                    raw_down=excluded.raw_down,
                    weighted_up=excluded.weighted_up,
                    weighted_down=excluded.weighted_down,
                    rate=excluded.rate,
                    reset_pending=excluded.reset_pending,
                    updated_at=excluded.updated_at
                """,
                (
                    int(managed_client_id),
                    remote_up,
                    remote_down,
                    raw_up,
                    raw_down,
                    weighted_up,
                    weighted_down,
                    rate,
                    0,
                    now,
                ),
            )
            conn.execute(
                "update managed_clients set last_synced_at=?, updated_at=? where id=?",
                (now, now, int(managed_client_id)),
            )
            row = conn.execute(
                "select * from usage_ledgers where managed_client_id=?",
                (int(managed_client_id),),
            ).fetchone()
            return dict(row)

    def managed_usage_totals(self, user_id: int) -> dict[str, int]:
        with self.session() as conn:
            row = conn.execute(
                """
                select
                    coalesce(sum(usage_ledgers.weighted_up), 0) as upload,
                    coalesce(sum(usage_ledgers.weighted_down), 0) as download
                from managed_clients
                left join usage_ledgers on usage_ledgers.managed_client_id=managed_clients.id
                where managed_clients.user_id=?
                """,
                (int(user_id),),
            ).fetchone()
            return {"upload": int(row["upload"]), "download": int(row["download"])}

    def reset_managed_usage(self, user_id: int) -> None:
        now = int(time.time())
        with self.session() as conn:
            self._reset_user_usage(conn, int(user_id), now)

    def list_tutorials(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "select * from tutorials"
        params: tuple[Any, ...] = ()
        if enabled_only:
            sql += " where enabled=1"
        sql += " order by sort_order, platform, id"
        with self.session() as conn:
            return [self._decode_tutorial(row) for row in conn.execute(sql, params)]

    def save_tutorial(
        self,
        title: str,
        platform: str,
        content: str,
        image_url: str = "",
        enabled: bool = True,
        sort_order: int = 0,
        tutorial_id: int | None = None,
    ) -> dict[str, Any]:
        platform = self._normalize_text(platform, "通用", 40)
        title = self._normalize_text(title, "", 120)
        content = self._normalize_text(content, "", 8000)
        image_url = self._normalize_text(image_url, "", 200000)
        if not title:
            raise ValueError("教程标题不能为空")
        if not content:
            raise ValueError("教程内容不能为空")
        now = int(time.time())
        with self.session() as conn:
            if tutorial_id:
                result = conn.execute(
                    """
                    update tutorials
                    set platform=?, title=?, content=?, image_url=?, enabled=?, sort_order=?, updated_at=?
                    where id=?
                    """,
                    (platform, title, content, image_url, int(bool(enabled)), int(sort_order), now, int(tutorial_id)),
                )
                if result.rowcount == 0:
                    raise ValueError("教程不存在")
                row_id = int(tutorial_id)
            else:
                cur = conn.execute(
                    """
                    insert into tutorials(platform, title, content, image_url, enabled, sort_order, created_at, updated_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (platform, title, content, image_url, int(bool(enabled)), int(sort_order), now, now),
                )
                row_id = int(cur.lastrowid)
            row = conn.execute("select * from tutorials where id=?", (row_id,)).fetchone()
            return self._decode_tutorial(row)

    def delete_tutorial(self, tutorial_id: int) -> None:
        with self.session() as conn:
            result = conn.execute("delete from tutorials where id=?", (int(tutorial_id),))
            if result.rowcount == 0:
                raise ValueError("教程不存在")

    def checkin_settings(self) -> dict[str, Any]:
        enabled = str(self.get_setting("checkin_enabled", "true")).lower() in {"1", "true", "yes", "on"}
        mode = str(self.get_setting("checkin_mode", "fixed") or "fixed").strip().lower()
        active_plan_only = str(self.get_setting("checkin_active_plan_only", "true")).lower() in {"1", "true", "yes", "on"}
        if mode not in {"fixed", "random"}:
            mode = "fixed"
        return {
            "enabled": enabled,
            "mode": mode,
            "fixed_gb": float(self.get_setting("checkin_fixed_gb", "1") or 1),
            "min_gb": float(self.get_setting("checkin_min_gb", "0.5") or 0.5),
            "max_gb": float(self.get_setting("checkin_max_gb", "1") or 1),
            "active_plan_only": active_plan_only,
        }

    def save_checkin_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        enabled = bool(payload.get("enabled", False))
        mode = str(payload.get("mode", "fixed") or "fixed").strip().lower()
        if mode not in {"fixed", "random"}:
            raise ValueError("invalid checkin mode")
        fixed_gb = max(float(payload.get("fixed_gb", 1) or 0), 0)
        min_gb = max(float(payload.get("min_gb", 0.5) or 0), 0)
        max_gb = max(float(payload.get("max_gb", 1) or 0), 0)
        if mode == "fixed" and fixed_gb <= 0:
            raise ValueError("固定签到流量必须大于 0")
        if mode == "random" and (min_gb <= 0 or max_gb <= 0 or max_gb < min_gb):
            raise ValueError("随机签到范围不正确")
        with self.session() as conn:
            for key, value in {
                "checkin_enabled": "true" if enabled else "false",
                "checkin_mode": mode,
                "checkin_fixed_gb": fixed_gb,
                "checkin_min_gb": min_gb,
                "checkin_max_gb": max_gb,
                "checkin_active_plan_only": "true" if payload.get("active_plan_only", True) else "false",
            }.items():
                conn.execute(
                    """
                    insert into app_settings(key, value) values (?, ?)
                    on conflict(key) do update set value=excluded.value
                    """,
                    (key, str(value)),
                )
        return self.checkin_settings()

    def checkin_status(self, user_id: int) -> dict[str, Any]:
        now = int(time.time())
        today = current_date_key(now)
        settings = self.checkin_settings()
        with self.session() as conn:
            user_row = conn.execute("select * from users where id=?", (int(user_id),)).fetchone()
            if not user_row:
                raise ValueError("用户不存在")
            user = self._decode_user(user_row)
            checked = conn.execute(
                "select * from checkin_records where user_id=? and checkin_date=?",
                (int(user_id), today),
            ).fetchone()
            rows = conn.execute(
                """
                select id, checkin_date, reward_bytes, created_at
                from checkin_records
                where user_id=?
                order by checkin_date desc, id desc limit 7
                """,
                (int(user_id),),
            )
            recent = [dict(row) for row in rows]
        return {
            "settings": settings,
            "enabled": bool(settings["enabled"]),
            "eligible": self._user_can_checkin(user, now, bool(settings["active_plan_only"])),
            "checked_in_today": bool(checked),
            "today": today,
            "last_reward_bytes": int(checked["reward_bytes"]) if checked else 0,
            "recent": recent,
        }

    def perform_checkin(self, user_id: int) -> dict[str, Any]:
        settings = self.checkin_settings()
        if not settings["enabled"]:
            raise ValueError("签到功能未开启")
        now = int(time.time())
        today = current_date_key(now)
        if settings["mode"] == "random":
            reward_gb = random.uniform(float(settings["min_gb"]), float(settings["max_gb"]))
        else:
            reward_gb = float(settings["fixed_gb"])
        reward_bytes = bytes_from_gb(reward_gb)
        if reward_bytes <= 0:
            raise ValueError("签到奖励流量必须大于 0")
        with self.session() as conn:
            conn.execute("begin immediate")
            user_row = conn.execute("select * from users where id=?", (int(user_id),)).fetchone()
            if not user_row:
                raise ValueError("用户不存在")
            user = self._decode_user(user_row)
            if not self._user_can_checkin(user, now, bool(settings["active_plan_only"])):
                raise ValueError("仅限已开通套餐的用户签到")
            existing = conn.execute(
                "select id from checkin_records where user_id=? and checkin_date=?",
                (int(user_id), today),
            ).fetchone()
            if existing:
                raise ValueError("今日已签到")
            new_quota = int(user["quota_bytes"] or 0) + int(reward_bytes)
            conn.execute("update users set quota_bytes=? where id=?", (new_quota, int(user_id)))
            cur = conn.execute(
                """
                insert into checkin_records(user_id, checkin_date, reward_bytes, created_at)
                values (?, ?, ?, ?)
                """,
                (int(user_id), today, int(reward_bytes), now),
            )
            record = {
                "id": int(cur.lastrowid),
                "user_id": int(user_id),
                "checkin_date": today,
                "reward_bytes": int(reward_bytes),
                "created_at": now,
            }
        return {"record": record, "user": self.get_user(user_id), "checkin": self.checkin_status(user_id)}

    def _user_can_checkin(self, user: dict[str, Any], now: int | None = None, active_plan_only: bool = True) -> bool:
        now = int(time.time()) if now is None else int(now)
        if user.get("role") != "user" or user.get("status") != "active":
            return False
        if not active_plan_only:
            return True
        return user.get("plan_id") is not None and int(user.get("expire_at") or 0) > now

    def create_ticket(self, user_id: int, subject: str, message: str) -> dict[str, Any]:
        subject = str(subject or "").strip()
        message = str(message or "").strip()
        if not subject:
            raise ValueError("工单标题不能为空")
        if not message:
            raise ValueError("工单内容不能为空")
        now = int(time.time())
        with self.session() as conn:
            user = conn.execute("select id from users where id=?", (int(user_id),)).fetchone()
            if not user:
                raise ValueError("用户不存在")
            cur = conn.execute(
                """
                insert into tickets(user_id, subject, message, status, created_at, updated_at)
                values (?, ?, ?, 'open', ?, ?)
                """,
                (int(user_id), subject[:120], message[:2000], now, now),
            )
            ticket_id = int(cur.lastrowid)
        return self.get_ticket(ticket_id)

    def get_ticket(self, ticket_id: int) -> dict[str, Any]:
        with self.session() as conn:
            row = conn.execute(
                """
                select tickets.*, users.email as user_email,
                       (select count(*) from ticket_replies where ticket_replies.ticket_id=tickets.id) as reply_count
                from tickets join users on users.id=tickets.user_id
                where tickets.id=?
                """,
                (int(ticket_id),),
            ).fetchone()
            if not row:
                raise ValueError("工单不存在")
            return self._decode_ticket(conn, row, include_replies=True)

    def list_user_tickets(self, user_id: int) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute(
                """
                select tickets.*, users.email as user_email,
                       (select count(*) from ticket_replies where ticket_replies.ticket_id=tickets.id) as reply_count
                from tickets join users on users.id=tickets.user_id
                where tickets.user_id=?
                order by tickets.updated_at desc, tickets.id desc
                """,
                (int(user_id),),
            )
            return [self._decode_ticket(conn, row, include_replies=True) for row in rows]

    def list_admin_tickets(self) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute(
                """
                select tickets.*, users.email as user_email,
                       (select count(*) from ticket_replies where ticket_replies.ticket_id=tickets.id) as reply_count
                from tickets join users on users.id=tickets.user_id
                order by tickets.updated_at desc, tickets.id desc
                """
            )
            return [self._decode_ticket(conn, row, include_replies=True) for row in rows]

    def reply_ticket(self, ticket_id: int, user_id: int | None, message: str, status: str | None = None) -> dict[str, Any]:
        message = str(message or "").strip()
        if not message:
            raise ValueError("回复内容不能为空")
        next_status = str(status or "open").strip().lower()
        if next_status not in {"open", "pending", "closed"}:
            raise ValueError("invalid ticket status")
        now = int(time.time())
        role = "admin"
        if user_id is not None:
            user = self.get_user(int(user_id))
            role = str(user.get("role", "user")) if user else "admin"
        with self.session() as conn:
            ticket = conn.execute("select id from tickets where id=?", (int(ticket_id),)).fetchone()
            if not ticket:
                raise ValueError("工单不存在")
            conn.execute(
                "insert into ticket_replies(ticket_id, user_id, role, message, created_at) values (?, ?, ?, ?, ?)",
                (int(ticket_id), int(user_id) if user_id is not None else None, role, message[:2000], now),
            )
            conn.execute(
                "update tickets set status=?, updated_at=? where id=?",
                (next_status, now, int(ticket_id)),
            )
        return self.get_ticket(ticket_id)

    def reply_user_ticket(self, ticket_id: int, user_id: int, message: str) -> dict[str, Any]:
        message = str(message or "").strip()
        if not message:
            raise ValueError("回复内容不能为空")
        now = int(time.time())
        with self.session() as conn:
            ticket = conn.execute(
                "select id, user_id from tickets where id=?",
                (int(ticket_id),),
            ).fetchone()
            if not ticket:
                raise ValueError("工单不存在")
            if int(ticket["user_id"]) != int(user_id):
                raise PermissionError("无权回复该工单")
            conn.execute(
                "insert into ticket_replies(ticket_id, user_id, role, message, created_at) values (?, ?, ?, ?, ?)",
                (int(ticket_id), int(user_id), "user", message[:2000], now),
            )
            conn.execute(
                "update tickets set status=?, updated_at=? where id=?",
                ("open", now, int(ticket_id)),
            )
        return self.get_ticket(ticket_id)

    def _decode_ticket(self, conn: sqlite3.Connection, row: sqlite3.Row, include_replies: bool = False) -> dict[str, Any]:
        ticket = dict(row)
        ticket["reply_count"] = int(ticket.get("reply_count") or 0)
        if include_replies:
            replies = conn.execute(
                "select id, ticket_id, user_id, role, message, created_at from ticket_replies where ticket_id=? order by created_at, id",
                (int(ticket["id"]),),
            )
            ticket["replies"] = [dict(reply) for reply in replies]
        return ticket

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.session() as conn:
            row = conn.execute("select value from app_settings where key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self.session() as conn:
            conn.execute(
                """
                insert into app_settings(key, value) values (?, ?)
                on conflict(key) do update set value=excluded.value
                """,
                (key, str(value)),
            )

    def _decode_plan(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["allowed_tags"] = json.loads(data.get("allowed_tags") or "[]")
        data["require_approval"] = bool(data["require_approval"])
        data["enabled"] = bool(data["enabled"])
        data["product_type"] = data.get("product_type") or "subscription"
        if data["product_type"] not in SUPPORTED_PRODUCT_TYPES:
            data["product_type"] = "subscription"
        data["category"] = data.get("category") or "套餐"
        data["description"] = data.get("description") or ""
        data["purchase_notice"] = data.get("purchase_notice") or ""
        return data

    def _decode_tutorial(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        return data

    def _normalize_product_type(self, product_type: str) -> str:
        normalized = str(product_type or "subscription").strip().lower()
        if normalized not in SUPPORTED_PRODUCT_TYPES:
            raise ValueError("invalid product type")
        return normalized

    def _normalize_text(self, value: Any, default: str, limit: int) -> str:
        text = str(value if value is not None else default).strip()
        if not text:
            text = default
        return text[:limit]

    def _reset_user_usage(self, conn: sqlite3.Connection, user_id: int, now: int) -> None:
        conn.execute("delete from usage_records where user_id=?", (int(user_id),))
        conn.execute(
            """
            update usage_ledgers
            set raw_up=0, raw_down=0, weighted_up=0, weighted_down=0, updated_at=?
            where managed_client_id in (select id from managed_clients where user_id=?)
            """,
            (now, int(user_id)),
        )

        conn.execute(
            """
            insert or ignore into usage_ledgers(managed_client_id, rate, reset_pending, updated_at)
            select id, rate, 1, ? from managed_clients where user_id=?
            """,
            (now, int(user_id)),
        )
    def _decode_node(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["tags"] = json.loads(data.get("tags") or "[]")
        data["enabled"] = bool(data["enabled"])
        if "latency_ms" not in data:
            data["latency_ms"] = None
        if "status" not in data or not data.get("status"):
            data["status"] = "unknown"
        if "last_checked_at" not in data:
            data["last_checked_at"] = 0
        return data

    def _validate_node_input(
        self,
        conn: sqlite3.Connection,
        *,
        name: str,
        source_url: str,
        rate: float,
        tags: list[str],
        enabled: bool,
        panel_id: int | None,
        inbound_id: int,
        mode: str,
        exclude_node_id: int | None = None,
    ) -> tuple[str, int | None, int, float]:
        mode = (mode or "static").strip().lower()
        if mode not in {"static", "managed"}:
            raise ValueError("invalid node mode")
        if mode == "static":
            return mode, panel_id, int(inbound_id), float(rate)

        if panel_id is None:
            raise ValueError("panel_id is required")
        panel_id = positive_int(panel_id, "panel_id")
        inbound_id = positive_int(inbound_id, "inbound_id")
        rate = positive_finite_float(rate, "rate")

        parse_vless_template(source_url)
        if enabled:
            siblings = self._enabled_managed_siblings(conn, panel_id, inbound_id, exclude_node_id)
            candidate = {
                "name": name,
                "mode": mode,
                "panel_id": panel_id,
                "inbound_id": inbound_id,
                "source_url": source_url,
                "rate": rate,
                "tags": tags,
                "enabled": enabled,
            }
            validate_target_nodes([*siblings, candidate])
        return mode, panel_id, inbound_id, rate

    def _enabled_managed_siblings(
        self,
        conn: sqlite3.Connection,
        panel_id: int,
        inbound_id: int,
        exclude_node_id: int | None,
    ) -> list[dict[str, Any]]:
        sql = """
            select * from nodes
            where mode='managed' and enabled=1 and panel_id=? and inbound_id=?
        """
        params: list[Any] = [panel_id, inbound_id]
        if exclude_node_id is not None:
            sql += " and id<>?"
            params.append(exclude_node_id)
        return [self._decode_node(row) for row in conn.execute(sql, params)]

    def _decode_managed_client(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["desired_enabled"] = bool(data["desired_enabled"])
        data["remote_enabled"] = bool(data["remote_enabled"])
        return data

    def _decode_user(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["is_priority"] = bool(data.get("is_priority", 0))
        return data


def normalize_panel_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("panel address is required")
    return value.rstrip("/") + "/"


def current_date_key(timestamp: int | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(int(timestamp or time.time())))


def _secret_key(secret: str) -> bytes:
    return hashlib.sha256(str(secret).encode("utf-8")).digest()


def encrypt_text(value: str, secret: str) -> str:
    if not secret:
        raise ValueError("RECHARGE_CARD_SECRET 未配置")
    nonce = secrets.token_bytes(16)
    key = _secret_key(secret)
    plain = value.encode("utf-8")
    stream = hmac.new(key, nonce + b"stream", hashlib.sha256).digest()
    cipher = bytes(byte ^ stream[index % len(stream)] for index, byte in enumerate(plain))
    tag = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + tag + cipher).decode("ascii")


def decrypt_text(value: str, secret: str) -> str:
    if not secret:
        raise ValueError("RECHARGE_CARD_SECRET 未配置")
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("完整卡密解密失败") from exc
    if len(raw) <= 48:
        raise ValueError("完整卡密解密失败")
    nonce = raw[:16]
    tag = raw[16:48]
    cipher = raw[48:]
    key = _secret_key(secret)
    expected = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("完整卡密解密失败")
    stream = hmac.new(key, nonce + b"stream", hashlib.sha256).digest()
    plain = bytes(byte ^ stream[index % len(stream)] for index, byte in enumerate(cipher))
    return plain.decode("utf-8")
