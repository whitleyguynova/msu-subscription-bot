import time

import aiosqlite

conn: aiosqlite.Connection

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    created_at INTEGER,
    source TEXT,                 -- откуда пришёл: метка src_, промокод p_ или ref
    referrer_id INTEGER,
    ref_rewarded INTEGER DEFAULT 0,
    trial_used INTEGER DEFAULT 0,
    pending_promo TEXT,          -- промокод, который применится к следующей оплате
    pay_bank TEXT,               -- банк, на который этот человек переводит
    fio TEXT,                    -- ФИО, которое человек ввёл перед оплатой
    study_group TEXT             -- учебная группа
);
CREATE TABLE IF NOT EXISTS subs(
    user_id INTEGER PRIMARY KEY,
    status TEXT,                 -- active / expired
    tariff TEXT,                 -- m1 / m3 / sem / trial / manual / gift / bonus
    method TEXT,                 -- test / trial / manual / gift / ref (потом yk / stars)
    started_at INTEGER,
    expires_at INTEGER,
    autorenew INTEGER DEFAULT 0,
    reminded3 INTEGER DEFAULT 0,
    reminded1 INTEGER DEFAULT 0,
    invite_link TEXT
);
CREATE TABLE IF NOT EXISTS payments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    provider TEXT,               -- test (потом yk / stars)
    ext_id TEXT UNIQUE,
    amount INTEGER,
    currency TEXT,
    tariff TEXT,
    kind TEXT,                   -- sub / renew / gift
    promo TEXT,
    status TEXT,                 -- pending / succeeded / canceled / expired
    created_at INTEGER,
    paid_at INTEGER,
    kop INTEGER DEFAULT 0,       -- не используется (осталось от режима с копейками)
    admin_msg_id INTEGER,        -- не используется
    bank TEXT                    -- банк, на который переводили
);
CREATE TABLE IF NOT EXISTS promos(
    code TEXT PRIMARY KEY,
    percent INTEGER,
    max_uses INTEGER,
    uses INTEGER DEFAULT 0,
    expires_at INTEGER,
    user_id INTEGER,             -- если задан, код личный (скидка «вернись»)
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS gifts(
    code TEXT PRIMARY KEY,
    tariff TEXT,
    buyer_id INTEGER,
    created_at INTEGER,
    used_by INTEGER,
    used_at INTEGER
);
CREATE TABLE IF NOT EXISTS subjects(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    position INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS materials(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id INTEGER,
    topic TEXT,                  -- тема, в которую ляжет файл
    file_id TEXT,
    file_type TEXT,              -- document / video / audio / photo / voice
    file_name TEXT,
    position INTEGER DEFAULT 0,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS opens(user_id INTEGER, subject_id INTEGER, at INTEGER);
CREATE TABLE IF NOT EXISTS support(admin_msg_id INTEGER PRIMARY KEY, user_id INTEGER);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    type TEXT,
    note TEXT,
    at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_pay_user ON payments(user_id, status);
CREATE INDEX IF NOT EXISTS ix_subs_exp ON subs(status, expires_at);
CREATE INDEX IF NOT EXISTS ix_mat_subj ON materials(subject_id, position);
CREATE INDEX IF NOT EXISTS ix_events ON events(type, at);
CREATE INDEX IF NOT EXISTS ix_opens ON opens(user_id, subject_id);
"""


def now() -> int:
    return int(time.time())


async def init(path: str):
    global conn
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.executescript(SCHEMA)
    # миграции для базы, созданной прошлой версией
    for table, col, typ in (("payments", "kop", "INTEGER DEFAULT 0"), ("payments", "admin_msg_id", "INTEGER"),
                             ("payments", "bank", "TEXT"), ("users", "pay_bank", "TEXT"),
                             ("users", "fio", "TEXT"), ("users", "study_group", "TEXT")):
        cols = {r[1] for r in await (await conn.execute(f"PRAGMA table_info({table})")).fetchall()}
        if col not in cols:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    await conn.commit()


async def fetch(sql, *args):
    async with conn.execute(sql, args) as cur:
        return await cur.fetchall()


async def fetchone(sql, *args):
    async with conn.execute(sql, args) as cur:
        return await cur.fetchone()


async def scalar(sql, *args):
    row = await fetchone(sql, *args)
    return row[0] if row else None


async def execute(sql, *args):
    cur = await conn.execute(sql, args)
    await conn.commit()
    return cur


async def event(user_id, type_, note=None):
    await execute("INSERT INTO events(user_id,type,note,at) VALUES(?,?,?,?)", user_id, type_, note, now())


async def get_user(uid):
    return await fetchone("SELECT * FROM users WHERE user_id=?", uid)


async def get_sub(uid):
    return await fetchone("SELECT * FROM subs WHERE user_id=?", uid)


async def get_setting(key, default=None):
    v = await scalar("SELECT value FROM settings WHERE key=?", key)
    return default if v is None else v


async def set_setting(key, value):
    await execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", key, None if value is None else str(value))
