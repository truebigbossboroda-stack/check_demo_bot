import logging
import json
import os
import random
import http
from dataclasses import dataclass, field
from enum import Enum
from outbox import emit_event
from typing import Dict, Optional, Set, List, Any, Tuple
from telegram.error import Forbidden, BadRequest
from telegram.request import HTTPXRequest
from db import SessionLocal
from dotenv import load_dotenv
from repositories.game_repo import get_active_game_by_chat
from repositories.game_repo import lock_game_row 
from repositories.snapshot_repo import insert_snapshot
from repositories.audit_repo import audit_log
from pathlib import Path
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session
from repositories.game_sessions_repo import get_current_session
from services.game_service import handle_ready, try_next_phase
from telegram.constants import ParseMode, MessageEntityType
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    MessageHandler
)

# ---------------- НАСТРОЙКИ -----------------

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(env_path)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing. Create .env or set env var.")

EVENTS_POOL: List[dict] = [] 

logging.basicConfig(level=logging.INFO)
print(">>> Запуск бота NeMonopolia...")
logger = logging.getLogger(__name__)


# ---------------- МОДЕЛИ -----------------


class Phase(str, Enum):
    LOBBY = "lobby"
    INCOME = "income"
    EVENT = "event"
    WORLD_ARENA = "world_arena"
    NEGOTIATIONS = "negotiations"
    ORDERS = "orders"
    RESOLVE = "resolve"
    FINISHED = "finished"


@dataclass
class City:
    name: str
    life: int  # 0–100
    shield: bool = False
    invested: int = 0      # сколько денег реально вложено в улучшения/щит этого города (по 150)
    destroyed: bool = False


@dataclass
class Country:
    country_id: int
    name: str
    president_id: int

    treasury: int = 0
    country_life: int = 60  # уровень жизни страны
    cities: Dict[str, City] = field(default_factory=dict)

    has_nuclear_industry: bool = False
    nukes: int = 0

    s_tokens: int = 0  # социальные
    p_tokens: int = 0  # политические

    sanctions_from: Set[int] = field(default_factory=set)  # кто ввёл против нас
    sanctions_to: Set[int] = field(default_factory=set)    # против кого ввели мы
    trade_deals: Set[int] = field(default_factory=set)  # активные торговые договоры (id стран-партнёров)

    # username президента (нужен для /votum @username)
    username: Optional[str] = None

    # Черновик указов на текущий раунд
    orders: Dict[str, int] = field(default_factory=dict)
    # ✅ подтверждение пакета указов на текущий раунд
    orders_confirmed: bool = False

    def reset_orders(self):
        self.orders = {}
        self.planned_strikes.clear()
        self.orders_confirmed = False

    def income_cities(self) -> int:
        # сумма процентов городов == доход
        return sum(0 if getattr(city, "destroyed", False) else int(city.life or 0)
            for city in self.cities.values())

    def income_country(self) -> int:
        # n% * 110 у.е.
        return self.country_life * 110 // 100
    
    planned_strikes: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class VotumVote:
    target_country_id: int          # против кого вотум
    initiated_by: int               # кто инициировал
    votes: Dict[int, bool] = field(default_factory=dict)  # user_id -> True/False
    active: bool = True


@dataclass
class WorldState:
    chat_id: int
    phase: Phase = Phase.LOBBY
    round_num: int = 0
    ecology: int = 30  # 0–100
    countries: Dict[int, Country] = field(default_factory=dict)  # president_id -> Country
    current_event: Optional[dict] = None
    current_votum: Optional[VotumVote] = None
    pending_trade: Dict[int, int] = field(default_factory=dict)
    owner_id: Optional[int] = None  
    owner_name: Optional[str] = None 
    round_resolved: bool = False
    current_event: Optional[dict] = None
    taken_countries: Set[str] = field(default_factory=set)
    player_country_key: Dict[int, str] = field(default_factory=dict)
    event_choices: Dict[int, str] = field(default_factory=dict)  # user.id -> option_key    


    def num_countries(self) -> int:
        return len(self.countries)


# Хранилище игр: chat_id -> WorldState
GAMES: Dict[int, WorldState] = {}
USER_ACTIVE_GAME: dict[int, int] = {}


# ---------------- ВСПОМОГАТЕЛЬНОЕ -----------------


def get_game(chat_id: int) -> Optional[WorldState]:
    return GAMES.get(chat_id)

def on_ready_command(chat_id: int, tg_user_id: int):
    with SessionLocal() as session:
        with session.begin():
            res = handle_ready(session, chat_id, tg_user_id)
        return res.message

def on_next_phase_command(chat_id: int):
    with SessionLocal() as session:
        with session.begin():
            res = try_next_phase(session, chat_id)
        return res.message

async def error_handler(update, context):
    import traceback, logging
    logging.exception("Unhandled exception:", exc_info=context.error)

def require_game(func):
    """Декоратор: требует существующей игры в чате."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        game = get_game(chat_id)
        if not game:
            await update.effective_chat.send_message(
                "Игры в этом чате пока нет. Используй /startgame. /menu для запуска меню управления"
            )
            return
        return await func(update, context, game)
    return wrapper

def load_events(path: str = "events.json") -> None:
    """
    Загружает EVENTS из JSON и:
    - не падает от пустого/битого файла
    - нормализует ключи (name->title, description->flavor)
    - проверяет структуру и отбрасывает невалидные события
    """
    global EVENTS
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
            if not raw:
                logging.warning("events.json пустой — EVENTS = [].")
                EVENTS = []
                return
            data = json.loads(raw)
    except FileNotFoundError:
        logging.warning("events.json не найден — EVENTS = [].")
        EVENTS = []
        return
    except json.JSONDecodeError as e:
        logging.error(f"events.json битый JSON: {e}. EVENTS = [].")
        EVENTS = []
        return
    except Exception as e:
        logging.exception(f"Не удалось прочитать events.json: {e}. EVENTS = [].")
        EVENTS = []
        return

    if not isinstance(data, list):
        logging.error("events.json должен быть массивом объектов [ {...}, {...} ]. EVENTS = [].")
        EVENTS = []
        return

    def _norm_event(ev: dict) -> dict:
        # поддержка старых ключей
        if "title" not in ev and "name" in ev:
            ev["title"] = ev["name"]
        if "flavor" not in ev and "description" in ev:
            ev["flavor"] = ev["description"]

        # дефолты
        ev.setdefault("flavor", "")
        ev.setdefault("phase", "orders")
        ev.setdefault("options", [])

        # options нормализация
        if not isinstance(ev["options"], list):
            ev["options"] = []

        for opt in ev["options"]:
            if not isinstance(opt, dict):
                continue
            opt.setdefault("cost", 0)
            opt.setdefault("effects", {})
            # поддержка альтернативных ключей
            if "label" not in opt and "text" in opt:
                opt["label"] = opt["text"]

        return ev

    def _validate_event(ev: dict) -> tuple[bool, list[str]]:
        errs: list[str] = []
        if not isinstance(ev, dict):
            return False, ["event is not an object"]

        if not ev.get("id") or not isinstance(ev.get("id"), str):
            errs.append("нет поля 'id' (строка)")
        if not ev.get("title") or not isinstance(ev.get("title"), str):
            errs.append("нет поля 'title' (строка) (или 'name' для старого формата)")
        if "flavor" in ev and not isinstance(ev.get("flavor"), str):
            errs.append("'flavor' должен быть строкой")
        if not ev.get("phase") or not isinstance(ev.get("phase"), str):
            errs.append("нет поля 'phase' (строка)")

        opts = ev.get("options", [])
        if not isinstance(opts, list):
            errs.append("'options' должен быть списком")
        else:
            for i, opt in enumerate(opts):
                if not isinstance(opt, dict):
                    errs.append(f"options[{i}] не объект")
                    continue
                if not opt.get("key") or not isinstance(opt.get("key"), str):
                    errs.append(f"options[{i}]: нет 'key' (строка)")
                if not opt.get("label") or not isinstance(opt.get("label"), str):
                    errs.append(f"options[{i}]: нет 'label' (строка)")
                cost = opt.get("cost", 0)
                if not isinstance(cost, (int, float)):
                    errs.append(f"options[{i}]: 'cost' должен быть числом")
                eff = opt.get("effects", {})
                if not isinstance(eff, dict):
                    errs.append(f"options[{i}]: 'effects' должен быть объектом {{...}}")

        return len(errs) == 0, errs

    cleaned: list[dict] = []
    ids_seen: set[str] = set()

    for idx, ev in enumerate(data):
        if not isinstance(ev, dict):
            logging.warning(f"EVENTS[{idx}] пропущен: не объект.")
            continue

        ev = _norm_event(ev)

        ok, errs = _validate_event(ev)
        if not ok:
            logging.warning(f"EVENT '{ev.get('id','<no id>')}' пропущен: " + "; ".join(errs))
            continue

        # уникальность id
        if ev["id"] in ids_seen:
            logging.warning(f"EVENT '{ev['id']}' пропущен: duplicate id.")
            continue
        ids_seen.add(ev["id"])

        cleaned.append(ev)

    EVENTS = cleaned
    logging.info(f"EVENTS загружены: {len(EVENTS)} шт.")


def event_title(ev: dict) -> str:
    return ev.get("title") or ev.get("name") or "Без названия"

def compute_trade_income(game: WorldState, country: Country) -> int:
    """
    База: 50 за каждую страну кроме себя (N-1)
    Минус: -50 за каждую страну, которая ввела санкции против нас (sanctions_from)
    Плюс: +50 за каждый активный торговый договор (trade_deals)
    """
    total = game.num_countries()
    base = max(0, total - 1) * 50
    minus_sanctions = len(country.sanctions_from) * 50
    plus_deals = len(country.trade_deals) * 50
    return base - minus_sanctions + plus_deals


def compute_ecology_income(game: WorldState) -> int:
    # n% * 200
    return game.ecology * 200 // 100


def apply_orders_for_country(game: WorldState, country: Country) -> Tuple[List[str], List[str]]:
    """Транзакционно применяем указы. Возвращаем (changes, errors) для лога."""
    changes: List[str] = []
    errors: List[str] = []

    orders = country.orders or {}
    strikes = list(getattr(country, "planned_strikes", []))[:3]

    # Если нет ни заказов, ни ударов, ни выбора события — реально нечего применять
    has_event_choice = bool(getattr(game, "current_event", None)) and bool(
        getattr(game, "event_choices", {}).get(country.president_id)
    )

    if not orders and not strikes and not has_event_choice:
        return changes, errors

    # Валидация/стоимость/казна
    report = calc_orders_cost_and_validate(country, game, country.president_id)
    if not report["ok"]:
        errors.extend(report["errors"])
        return changes, errors

    total_cost = int(report["total_cost"] or 0)
    country.treasury -= total_cost
    changes.append(f"💸 Потрачено: {total_cost} у.е. (остаток: {country.treasury} у.е.)")

    # --- APPLY EVENT EFFECTS ---
    ev = game.current_event
    if ev:
        choice = game.event_choices.get(country.president_id)  # или user.id
        if choice:
            opt = next((o for o in ev.get("options", []) if o["key"] == choice), None)
            if opt:
                eff = opt.get("effects", {}) or {}

                if "s_tokens" in eff:
                    country.s_tokens += int(eff["s_tokens"])
                    changes.append(f"🌍 Событие: +{eff['s_tokens']} S-токен(ов)")

                if "p_tokens" in eff:
                    country.p_tokens += int(eff["p_tokens"])
                    changes.append(f"🌍 Событие: +{eff['p_tokens']} P-токен(ов)")

                if "ecology" in eff:
                    old = game.ecology
                    game.ecology = max(0, min(100, game.ecology + int(eff["ecology"])))
                    changes.append(f"🌍 Экология: {old}→{game.ecology} ({eff['ecology']:+d})")

    # --- Экология ---
    if int(orders.get("improve_ecology", 0) or 0) == 1:
        old_ec = game.ecology
        base_bonus = 5
        extra_bonus = 0
        if game.current_event and game.current_event.get("id") == "climate_summit":
            extra_bonus = int(game.current_event.get("effects", {}).get("extra_ecology_bonus", 0) or 0)
        game.ecology = min(100, int(game.ecology) + base_bonus + extra_bonus)
        country.s_tokens += 1
        changes.append(f"🌿 Экология: {old_ec}→{game.ecology} (+{base_bonus}+{extra_bonus})")

    # --- Ядерная промышленность ---
    if int(orders.get("build_nuclear_industry", 0) or 0) == 1:
        country.has_nuclear_industry = True
        changes.append("☢ Построена ядерная промышленность")

    # --- Боеголовки (покупка) ---
    nukes_to_build = int(orders.get("build_nukes", 0) or 0)
    if nukes_to_build > 0:
        country.nukes += nukes_to_build
        changes.append(f"💣 Боеголовки: +{nukes_to_build} (всего: {country.nukes})")

    # --- Улучшения городов ---
    for code in ("A", "B", "C", "CAP"):
        if int(orders.get(f"improve_city_{code}", 0) or 0) == 1:
            city = country.cities.get(code)
            if city:
                if getattr(city, "destroyed", False):
                    changes.append(f"⚠️ {code}: город разрушен, улучшение пропущено.")
                    continue
                old = city.life
                city.life = min(100, int(city.life) + 20)  # под твои правила
                city.invested = int(getattr(city, "invested", 0) or 0) + int(COSTS["improve_city"])
                changes.append(f"🏙 {code}: {old}→{city.life} (инвестировано: {city.invested})")

    # --- Щиты ---
    for code in ("A", "B", "C", "CAP"):
        if int(orders.get(f"build_shield_{code}", 0) or 0) == 1:
            city = country.cities.get(code)
            if city:
                if getattr(city, "destroyed", False):
                    changes.append(f"⚠️ {code}: город разрушен, щит ставить нельзя.")
                    continue
                if not city.shield:
                    city.shield = True
                    city.invested = int(getattr(city, "invested", 0) or 0) + int(COSTS["build_shield"])
                changes.append(f"🛡 Щит установлен на один из городов.")

    # --- Ядерные удары (planned_strikes) ---
    strikes = list(getattr(country, "planned_strikes", []))[:3]
    if strikes:
        for idx, (tid, ccode) in enumerate(strikes, start=1):
            # Трата ресурсов атакующего транзакционно
            if country.nukes <= 0:
                changes.append("☢️ Удары: закончились боеголовки.")
                break

            country.nukes -= 1
            country.p_tokens += 1
            country.s_tokens = max(0, int(country.s_tokens) - 2)

            target = game.countries.get(tid)
            if not target:
                changes.append(f"☢️ Удар {idx}: цель не найдена (ID={tid}).")
                continue

            tcity = target.cities.get(ccode)
            if not tcity:
                changes.append(f"☢️ Удар {idx}: {target.name}/{ccode} — города нет.")
                continue

            if tcity.shield:
                tcity.shield = False
                changes.append(f"☢️ Удар {idx}: {target.name}/{ccode} — 🛡 щит поглотил удар (щит снят).")
            else:
                # стоимость восстановления = все вложения в город + 150
                invested = int(getattr(tcity, "invested", 0) or 0)
                recovery_cost = invested + 150

                old_life = tcity.life
                tcity.life = 0
                tcity.destroyed = True
                tcity.shield = False  # на всякий случай

                old_ec = game.ecology
                game.ecology = max(0, int(game.ecology) - 5)

                changes.append(
                    f"☢️ Удар {idx}: {target.name}/{ccode} — 💥 разрушен ({old_life}→0), "
                    f"экология {old_ec}→{game.ecology}. 🛠 Восстановление: {recovery_cost} у.е."
                )
        country.planned_strikes.clear()

    # --- Восстановление разрушенных городов ---
    for code in ("A", "B", "C", "CAP"):
        if int(orders.get(f"recover_city_{code}", 0) or 0) == 1:
            city = country.cities.get(code)
            if not city or not getattr(city, "destroyed", False):
                changes.append(f"⚠️ {code}: восстановление пропущено (город не разрушен).")
                continue

            # После восстановления считаем, что прошлые улучшения “сгорели”, инвестирование обнуляем
            city.destroyed = False
            city.life = 15
            city.shield = False
            city.invested = 0
            changes.append(f"🛠 {code}: восстановлен (life=15%, щит снят, инвестиции сброшены).")

    # Чистим пакет
    country.orders.clear()
    country.orders_confirmed = False
    return changes, errors

ORDER_KEYS = {
    "eco": "improve_ecology",
    "nuc_ind": "build_nuclear_industry",
    "nukes": "build_nukes",
    "city": lambda code: f"improve_city_{code}",
    "shield": lambda code: f"build_shield_{code}",
}

COSTS = {
    "improve_city": 150,
    "build_shield": 150,
    "improve_ecology": 75,
    "build_nuclear_industry": 400,
    "build_nukes": 200,
}

COUNTRY_PRESETS = {
    "germany": {
        "name": "Германия",
        "cities": {"A": "Гамбург", "B": "Дрезден", "C": "Мюнхен", "CAP": "Берлин"},
    },
    "france": {
        "name": "Франция",
        "cities": {"A": "Лион", "B": "Марсель", "C": "Тулуза", "CAP": "Париж"},
    },
    "ukraine": {
        "name": "Украина",
        "cities": {"A": "Черкассы", "B": "Одесса", "C": "Днепр", "CAP": "Киев"},
    },
    "belarus": {
        "name": "Беларусь",
        "cities": {"A": "Гомель", "B": "Витебск", "C": "Гродно", "CAP": "Минск"},
    },
    "uk": {
        "name": "Великобритания",
        "cities": {"A": "Манчестер", "B": "Бирмингем", "C": "Глазго", "CAP": "Лондон"},
    },
    "usa": {
        "name": "США",
        "cities": {"A": "Лос-Анджелес", "B": "Чикаго", "C": "Хьюстон", "CAP": "Вашингтон"},
    },
    "turkey": {
        "name": "Турция",
        "cities": {"A": "Измир", "B": "Анкара", "C": "Бурса", "CAP": "Стамбул"},
    },
    "china": {
        "name": "Китай",
        "cities": {"A": "Шанхай", "B": "Шэньчжэнь", "C": "Гуанчжоу", "CAP": "Пекин"},
    },
    "iran": {
        "name": "Иран",
        "cities": {"A": "Исфахан", "B": "Шираз", "C": "Мешхед", "CAP": "Тегеран"},
    },
    "brazil": {
        "name": "Бразилия",
        "cities": {"A": "Сан-Паулу", "B": "Рио-де-Жанейро", "C": "Сальвадор", "CAP": "Бразилиа"},
    },
}

PHASE_UI = {
    "world_arena": {
        "title": "🌍 Мировая арена",
        "checklist": [
            "90 сек: общее обсуждение",
            "60 сек на страну: позиция/заявления",
            "60 сек: финальное общее обсуждение",
            "Можно объявить вотум недоверия (1 раз за собрание от 1 страны)",
        ],
        "tip": "Договоры и угрозы — публично. Детали сделок — в переговорах.",
    },
    "negotiations": {
        "title": "🤝 Переговоры",
        "checklist": [
            "15 минут на переговоры",
            "Можно посетить 1 страну за раунд (по условиям хозяев)",
            "Фиксируйте договорённости: санкции, поддержка ООН, обмен ресурсами (если есть)",
        ],
        "tip": "Не распыляйся: выбери 1–2 цели раунда и добивай их.",
    },
    "orders": {
        "title": "📦 Указы (личный кабинет)",
        "checklist": [
            "Введи /orders в чат и тебе бот напишет в личные сообщения",
            "Собери пакет указов кнопками",
            "Проверь стоимость и ошибки",
            "Обязательно нажми ✅ Подтвердить",
        ],
        "tip": "Без подтверждения пакет не применяется в RESOLVE.",
    },
    "resolve": {
        "title": "✅ Применение (RESOLVE)",
        "checklist": [
            "Бот применяет только подтверждённые пакеты",
            "Списывает деньги → применяет эффекты",
            "Публикует итоги раунда в чат",
        ],
        "tip": "После RESOLVE ведущий запускает следующий раунд.",
    },
}

def _orders_main_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🏙 Улучшить город", callback_data="ord:city_menu"),
            InlineKeyboardButton("🛡 Щит", callback_data="ord:shield_menu"),
        ],
        [
            InlineKeyboardButton("🌿 Экология", callback_data="ord:eco_toggle"),
            InlineKeyboardButton("☢ Ядерная промышленность", callback_data="ord:nuc_ind_toggle"),
        ],
        [
            InlineKeyboardButton("🚫 Санкции", callback_data="ord:sanctions_menu"),
            InlineKeyboardButton("🤝 Торговля", callback_data="ord:trade_menu"),
        ],
        [
            InlineKeyboardButton("💣 Боеголовки +1", callback_data="ord:nuke_plus"),
            InlineKeyboardButton("💣 Боеголовки -1", callback_data="ord:nuke_minus"),
        ],
        [
            InlineKeyboardButton("☢️ Ядерный удар", callback_data="ord:strike_menu"),
            InlineKeyboardButton("↩️ Удалить удар", callback_data="ord:strike_pop"),
        ],
        [
            InlineKeyboardButton("🛠 Восстановить город", callback_data="ord:recover_menu"),
            InlineKeyboardButton("🌍 Событие", callback_data="ord:event_menu"),
        ],
        [
            InlineKeyboardButton("✅ Подтвердить", callback_data="ord:confirm"),
            InlineKeyboardButton("🧹 Очистить", callback_data="ord:clear"),
        ],
        [
            InlineKeyboardButton("⬅ Назад/Обновить", callback_data="ord:refresh"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

def _orders_event_keyboard(game: WorldState) -> InlineKeyboardMarkup:
    ev = game.current_event
    rows = []
    if not ev:
        rows.append([InlineKeyboardButton("⬅ Назад", callback_data="ord:back")])
        return InlineKeyboardMarkup(rows)

    for opt in ev.get("options", []):
        rows.append([InlineKeyboardButton(opt["label"], callback_data=f"ord:event_pick:{opt['key']}")])

    rows.append([InlineKeyboardButton("⬅ Назад", callback_data="ord:back")])
    return InlineKeyboardMarkup(rows)

def _orders_city_keyboard(prefix: str, country: Country):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(city_label(country,"A"), callback_data=f"{prefix}:A")],
        [InlineKeyboardButton(city_label(country,"B"), callback_data=f"{prefix}:B")],
        [InlineKeyboardButton(city_label(country,"C"), callback_data=f"{prefix}:C")],
        [InlineKeyboardButton(city_label(country,"CAP"), callback_data=f"{prefix}:CAP")],
        [InlineKeyboardButton("⬅ Назад", callback_data="ord:refresh")],
    ])

# === Ядерные удары ===

def _orders_recover_keyboard(country: Country) -> InlineKeyboardMarkup:
    buttons = []
    for code in ("A", "B", "C", "CAP"):
        city = country.cities.get(code)
        if city and getattr(city, "destroyed", False):
            buttons.append(InlineKeyboardButton(code, callback_data=f"ord:recover_set:{code}"))

    rows = [buttons[i:i+4] for i in range(0, len(buttons), 4)] if buttons else []
    rows.append([InlineKeyboardButton("⬅ Назад", callback_data="ord:back")])
    return InlineKeyboardMarkup(rows)

def _orders_targets_keyboard(game: WorldState, self_id: int) -> InlineKeyboardMarkup:
    rows = []
    for pid, c in game.countries.items():
        if pid == self_id:
            continue
        rows.append([InlineKeyboardButton(f"🎯 {c.name}", callback_data=f"ord:strike_tgt:{pid}")])
    rows.append([InlineKeyboardButton("⬅ Назад", callback_data="ord:back")])
    return InlineKeyboardMarkup(rows)

def _orders_sanctions_keyboard(game: WorldState, self_id: int, country: Country) -> InlineKeyboardMarkup:
    rows = []
    for pid, c in game.countries.items():
        if pid == self_id:
            continue
        active = "✅" if pid in country.sanctions_to else "➕"
        rows.append([InlineKeyboardButton(f"{active} {c.name}", callback_data=f"ord:sanction_toggle:{pid}")])
    rows.append([InlineKeyboardButton("⬅ Назад", callback_data="ord:back")])
    return InlineKeyboardMarkup(rows)

def _orders_trade_keyboard(game: WorldState, self_id: int, country: Country) -> InlineKeyboardMarkup:
    rows = []
    for pid, c in game.countries.items():
        if pid == self_id:
            continue

        if pid in country.trade_deals:
            label = f"✅ {c.name} (активен)"
            cb = f"ord:trade_cancel:{pid}"
        else:
            label = f"➕ {c.name} (предложить)"
            cb = f"ord:trade_request:{pid}"

        rows.append([InlineKeyboardButton(label, callback_data=cb)])

    rows.append([InlineKeyboardButton("⬅ Назад", callback_data="ord:back")])
    return InlineKeyboardMarkup(rows)

def _trade_request_keyboard(requester_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Принять", callback_data=f"ord:trade_accept:{requester_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"ord:trade_decline:{requester_id}"),
        ]
    ])

def city_label(country, code: str) -> str:
    city = country.cities.get(code)
    if not city:
        return code
    # показываем имя + код, чтобы игрок понимал, что это именно тот "ID"
    return f"{city.name} ({code})"

# === Валидация + расчёт стоимости пакета указов ===
def calc_orders_cost_and_validate(country, game, user_id: int | None = None) -> Dict[str, Any]:
    if user_id is None:
        user_id = getattr(country, "country_id", None) or getattr(country, "president_id", None)

    """
    Анализирует country.orders и возвращает:
    {
      ok: bool,
      total_cost: int,
      breakdown: list[str],
      errors: list[str],
      warnings: list[str],
      treasury_after: int
    }

    Ожидания по структуре:
    - country.treasury: int
    - country.has_nuclear_industry: bool
    - country.orders: dict[str,int]
    - country.cities: dict[str, City] где ключи: "A","B","C","CAP"
      и у City есть хотя бы .shield (bool) и .life (int)
    """
    orders = country.orders or {}
    errors: List[str] = []
    warnings: List[str] = []
    breakdown: List[str] = []
    total = 0

    # --- helper ---
    def _add(cost: int, label: str):
        nonlocal total
        total += cost
        breakdown.append(f"• {label}: {cost} у.е.")

    # --- 1) Экология (раз в раунд) ---
    eco_key = "improve_ecology"
    eco_val = int(orders.get(eco_key, 0) or 0)
    if eco_val not in (0, 1):
        errors.append("Экология: можно выбрать только 0 или 1 раз за раунд.")
    elif eco_val == 1:
        _add(COSTS["improve_ecology"], "🌿 Улучшение экологии")

    # --- 2) Ядерная промышленность (раз за игру) ---
    ind_key = "build_nuclear_industry"
    ind_val = int(orders.get(ind_key, 0) or 0)
    if ind_val not in (0, 1):
        errors.append("Ядерная промышленность: можно выбрать только 0 или 1.")
    elif ind_val == 1:
        if getattr(country, "has_nuclear_industry", False):
            errors.append("Ядерная промышленность уже создана ранее (повторно нельзя).")
        else:
            _add(COSTS["build_nuclear_industry"], "☢ Ядерная промышленность")

    # --- 3) Боеголовки (0..3 за раунд, требуют промышленность) ---
    nukes_key = "build_nukes"
    nukes_val = int(orders.get(nukes_key, 0) or 0)
    if nukes_val < 0 or nukes_val > 3:
        errors.append("Боеголовки: можно выбрать от 0 до 3 за раунд.")
    elif nukes_val > 0:
        # промышленность может быть уже есть, либо строится в этом же пакете
        has_ind_now = getattr(country, "has_nuclear_industry", False) or ind_val == 1
        if not has_ind_now:
            errors.append("Боеголовки нельзя производить без ядерной промышленности.")
        else:
            _add(COSTS["build_nukes"] * nukes_val, f"💣 Боеголовки ×{nukes_val}")

    # --- 4) Улучшения городов (1 раз на город за раунд) ---
    # ожидаемые ключи: improve_city_A / improve_city_B / improve_city_C / improve_city_CAP
    for code in ("A", "B", "C", "CAP"):
        k = f"improve_city_{code}"
        v = int(orders.get(k, 0) or 0)
        if v not in (0, 1):
            errors.append(f"Улучшение города {code}: можно только 0 или 1 за раунд.")
        elif v == 1:
            _add(COSTS["improve_city"], f"🏙 Улучшить город {code}")

    # --- 5) Щиты (1 раз на город за раунд) ---
    for code in ("A", "B", "C", "CAP"):
        k = f"build_shield_{code}"
        v = int(orders.get(k, 0) or 0)
        if v not in (0, 1):
            errors.append(f"Щит {code}: можно только 0 или 1 за раунд.")
        elif v == 1:
            # если щит уже стоит — это не критическая ошибка, но бессмысленно
            city = (country.cities or {}).get(code)
            if city is not None and getattr(city, "shield", False):
                warnings.append(f"Щит уже стоит в городе {code}. Покупка в этом раунде может быть бессмысленной.")
            _add(COSTS["build_shield"], f"🛡 Щит для {code}")

   # --- 6) Ядерные удары (до 3) ---
    strikes = getattr(country, "planned_strikes", [])
    if len(strikes) > 3:
        errors.append("Ядерные удары: максимум 3 за раунд.")

    if strikes:
        need = len(strikes)

        # боеголовки
        if getattr(country, "nukes", 0) < need:
            errors.append(f"Не хватает боеголовок: нужно {need}, есть {country.nukes}.")

        # S-токены (2 за удар)
        s_now = int(getattr(country, "s_tokens", 0) or 0)
        s_need = 2 * need
        if s_now < s_need:
            errors.append(f"Не хватает S-токенов для ударов: нужно {s_need}, есть {s_now}.")

        seen = set()
        for (tid, ccode) in strikes:
            if tid == user_id:
                errors.append("Нельзя наносить удар по самому себе.")
                continue

            target = game.countries.get(tid)
            if not target:
                errors.append("Удар: выбрана несуществующая страна-цель.")
                continue

            if ccode not in ("A", "B", "C", "CAP"):
                errors.append("Удар: неверный код города.")
                continue

            tcity = target.cities.get(ccode)
            if not tcity:
                errors.append("Удар: у цели нет выбранного города.")
                continue

            if getattr(tcity, "destroyed", False):
                errors.append("Удар: выбран разрушенный город (бить нельзя).")
                continue

            if (tid, ccode) in seen:
                warnings.append("Удар: дубликат цели (один и тот же город выбран несколько раз).")
            seen.add((tid, ccode))


    # --- 7) Восстановление разрушенных городов ---
    for code in ("A", "B", "C", "CAP"):
        k = f"recover_city_{code}"
        v = int(orders.get(k, 0) or 0)
        if v not in (0, 1):
            errors.append(f"Восстановление {code}: только 0 или 1.")
        elif v == 1:
            city = country.cities.get(code)
            if not city or not getattr(city, "destroyed", False):
                errors.append(f"Восстановление {code}: город не разрушен.")
            else:
                rec_cost = int(getattr(city, "invested", 0) or 0) + 150
                _add(rec_cost, f"🛠 Восстановить {code} (стоимость {rec_cost})")
    

    # --- EVENT COST ---
    if game.current_event:
        choice = game.event_choices.get(country.president_id)  # или user.id страны
        if choice:
            opt = next((o for o in game.current_event.get("options", []) if o["key"] == choice), None)
            if opt:
                c = int(opt.get("cost", 0) or 0)
                total += c
                breakdown.append(f"🌍 {game.current_event['title']}: {opt['label']} — {c} у.е.")
    
    treasury = int(getattr(country, "treasury", 0) or 0)
    treasury_after = treasury - total

    if treasury_after < 0:
        errors.append(f"Недостаточно средств: нужно {total} у.е., в казне {treasury} у.е. (не хватает {abs(treasury_after)} у.е.).")

    ok = len(errors) == 0

    # если ничего не выбрано — это не ошибка, но подсветим
    has_any_action = bool(orders) or bool(strikes) or bool(choice)
    if total == 0 and not errors and not has_any_action:
        warnings.append("Пакет указов пустой — в этом раунде ты ничего не делаешь.")

    return {
        "ok": ok,
        "total_cost": total,
        "breakdown": breakdown,
        "errors": errors,
        "warnings": warnings,
        "treasury_after": treasury_after,
    }

def toggle_order_flag(country: "Country", key: str) -> bool:
        """
        Переключает флаг-ордер.
        True  -> включили (key=1)
        False -> выключили (key удалён)
        """
        if int(country.orders.get(key, 0) or 0) == 1:
            country.orders.pop(key, None)
            return False
        country.orders[key] = 1
        return True

def format_phase_message(game) -> str:
    key = game.phase.value
    info = PHASE_UI.get(key)

    if not info:
        # fallback, если не нашли описание
        return f"Фаза игры: {key}"

    title = info["title"]
    checklist = "\n".join([f"• {x}" for x in info["checklist"]])
    tip = info.get("tip")

    text = f"{title}\n\n📋 Чек-лист:\n{checklist}"
    if tip:
        text += f"\n\n💡 Подсказка: {tip}"

    # полезно показывать раунд
    text = f"Раунд {game.round_num} — {text}"
    return text


async def send_phase_intro(chat, game):
    await chat.send_message(format_phase_message(game))

def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))

def compute_life_score(country) -> int:
    total = 0
    for code in ("A", "B", "C", "CAP"):
        c = country.cities.get(code)
        if not c:
            continue
        life = 0 if getattr(c, "destroyed", False) else int(c.life or 0)
        total += life
    return round(total / 4)

def compute_scores(game) -> dict:
    max_treasury = max([int(c.treasury or 0) for c in game.countries.values()] + [1])

    res = {}
    for pid, c in game.countries.items():
        eco_score = clamp(round(100 * int(c.treasury or 0) / max_treasury), 0, 100)
        life_score = clamp(compute_life_score(c), 0, 100)
        social_score = clamp(int(c.s_tokens or 0) * 10, 0, 100)
        polit_score = clamp(int(c.p_tokens or 0) * 10, 0, 100)
        total = eco_score + life_score + social_score + polit_score
        res[pid] = {
            "economy": eco_score,
            "life": life_score,
            "social": social_score,
            "political": polit_score,
            "total": total,
        }
    return res

def render_orders_ui(country: Country, game: WorldState, user_id: int) -> str:
    """Текст для лички: текущие указы + стоимость + ошибки/предупреждения."""
    report = calc_orders_cost_and_validate(country, game, user_id)
    orders = country.orders or {}

    lines: List[str] = []
    lines.append("📦 **Твой пакет указов**")
    status = "✅ подтверждено" if getattr(country, "orders_confirmed", False) else "⏳ не подтверждено"
    lines.append(f"Статус: {status}")

    # --- Очки (приватно) ---
    scores = compute_scores(game)
    my = scores.get(user_id)
    if my:
        lines.append(
            f"🏆 Очки: {my['total']} / 400 "
            f"(💰{my['economy']} 🏙{my['life']} 👥{my['social']} 🏛{my['political']})"
        )
    lines.append("")

    # --- EVENT ---
    if game.current_event:
        ev = game.current_event
        lines.append("")
        lines.append(f"🌍 **Событие:** {ev['title']}")
        flavor = ev.get("flavor", "")
        if flavor:
            lines.append(flavor)

        choice = game.event_choices.get(user_id)
        if choice:
            opt = next((o for o in ev.get("options", []) if o["key"] == choice), None)
            if opt:
                lines.append(f"Твой выбор: ✅ {opt['label']}")
        else:
            lines.append("Твой выбор: — (не выбран)")

    if not orders:
        lines.append("— (пусто)")
    else:
        for k, v in orders.items():
            lines.append(f"• `{k}` = {v}")

    lines.append("")
    lines.append(f"💰 Казна: {country.treasury} у.е.")
    lines.append(f"🧾 Стоимость пакета: {report['total_cost']} у.е.")
    lines.append(f"🏦 Останется после списания: {report['treasury_after']} у.е.")

    # --- дипломатия ---
    def _names(ids):
        return ", ".join(game.countries[i].name for i in ids if i in game.countries) or "—"

    lines.append("")
    lines.append(f"🤝 Договоры: {len(country.trade_deals)} ( +{len(country.trade_deals)*50}/раунд )")
    lines.append(f"   {_names(country.trade_deals)}")

    lines.append(f"🚫 Санкции ПРОТИВ: {len(country.sanctions_to)}")
    lines.append(f"   {_names(country.sanctions_to)}")

    lines.append(f"🚫 Санкции ОТ: {len(country.sanctions_from)} ( -{len(country.sanctions_from)*50}/раунд )")
    lines.append(f"   {_names(country.sanctions_from)}")

    if report["breakdown"]:
        lines.append("")
        lines.append("🔎 Расшифровка стоимости:")
        lines.extend(report["breakdown"])

    if report["errors"]:
        lines.append("")
        lines.append("❌ Ошибки (исправь перед подтверждением):")
        for e in report["errors"]:
            lines.append(f"• {e}")

    if report["warnings"]:
        lines.append("")
        lines.append("⚠️ Предупреждения:")
        for w in report["warnings"]:
            lines.append(f"• {w}")



    # Удары
    strikes = getattr(country, "planned_strikes", [])
    if strikes:
        lines.append("☢️ Запланированные удары:")
        for i, (tid, ccode) in enumerate(strikes, start=1):
            tname = game.countries.get(tid).name if tid in game.countries else f"id:{tid}"
            lines.append(f"• {i}) {tname} / {ccode}")
        lines.append("")

    lines.append("")
    lines.append("Нажимай кнопки ниже, чтобы менять пакет.")

    return "\n".join(lines)

# ---------------- HANDLERS: ИГРА -----------------


async def start_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # Если игра уже есть в этом чате — выходим
    if chat_id in GAMES:
        await update.effective_chat.send_message("Игра уже создана в этом чате.")
        return

    user = update.effective_user

    # Создаём игру и сохраняем владельца
    game = WorldState(chat_id=chat_id)
    game.owner_id = user.id
    game.owner_name = user.full_name  # если добавил это поле в WorldState
    USER_ACTIVE_GAME[user.id] = update.effective_chat.id
    GAMES[chat_id] = game

    with SessionLocal() as db:
        with db.begin():
            archived = db.execute(
                sql_text("""
                    UPDATE game_sessions
                    SET status = 'archived',
                        archived_at = now()
                    WHERE chat_id = :chat_id
                    AND status IN ('lobby','active')
                    RETURNING id
                """),
                {"chat_id": chat_id},
            ).mappings().all()

            new_game = db.execute(
                sql_text("""
                    INSERT INTO game_sessions
                        (chat_id, status, owner_tg_user_id, round_num, current_phase, phase_seq, phase_started_at, afk_timeout_seconds, expires_at)
                    VALUES
                        (:chat_id, 'active', :owner, 0, 'lobby', 0, now(), 300, now() + interval '30 days')
                    RETURNING id
                """),
                {"chat_id": chat_id, "owner": user.id},
            ).scalar_one()

            game_id = str(new_game)

            audit_log(
                db,
                game_id=new_game,
                chat_id=chat_id,
                actor_tg_user_id=user.id,
                action_type="game.created",
                phase_seq=0,
                round_num=0,
                payload={"owner_tg_user_id": user.id},
            )
            
            emit_event(
                db,
                event_type="game.created",
                aggregate_type="game_session",
                aggregate_id=new_game,
                payload={
                    "chat_id": chat_id,
                    "owner_tg_user_id": user.id,
                    "status": "active",
                    "phase": "lobby",
                    "phase_seq": 0,
                },
                idempotency_key=f"game.created:{game_id}"
            )

    # Упоминание создателя тегом через HTML-ссылку
    owner_link = f'<a href="tg://user?id={user.id}">{user.full_name}</a>'

    msg = (
        f"Игра создана. Ведущий: {owner_link}.\n"
        "Игроки могут присоединяться командой /joingame."
    )

    await update.effective_chat.send_message(
        msg,
        parse_mode=ParseMode.HTML
    )
    # после успешного создания игры — показать постоянное меню
    await menu_cmd(update, context)



@require_game
async def join_game(update: Update, context: ContextTypes.DEFAULT_TYPE, game: WorldState):
    user = update.effective_user

    if user.id in game.countries:
        await update.effective_chat.send_message("Ты уже участвуешь как страна.")
        return

    # 1) ЖЁСТКОЕ ПРАВИЛО: нельзя вступить без выбора страны
    chosen_key = game.player_country_key.get(user.id)
    if not chosen_key:
        await update.effective_chat.send_message(
            "❌ Сначала выбери страну: открой /menu → 🌍 Выбрать страну.\n"
            "После выбора введи /joingame."
        )
        return

    # 2) Защита от рассинхрона: если кто-то уже занял твою страну
    for uid, k in game.player_country_key.items():
        if uid != user.id and k == chosen_key:
            await update.effective_chat.send_message(
                "❌ Эта страна уже занята другим игроком. Выбери другую: /menu → 🌍 Выбрать страну."
            )
            return

    preset = COUNTRY_PRESETS[chosen_key]

    # 3) Создаём страну уже с правильным названием и городами
    country = Country(
        country_id=user.id,
        name=preset["name"],
        president_id=user.id,
        treasury=0,
        country_life=60,
        cities={
            "A": City(name=preset["cities"]["A"], life=50),
            "B": City(name=preset["cities"]["B"], life=50),
            "C": City(name=preset["cities"]["C"], life=50),
            "CAP": City(name=preset["cities"]["CAP"], life=80),
        },
        username=user.username,
    )

    game.countries[user.id] = country
    USER_ACTIVE_GAME[user.id] = update.effective_chat.id

    # 4) Финально закрепляем занятость страны
    game.taken_countries.add(chosen_key)

    with SessionLocal() as db:
        with db.begin():
            # 1) текущая игра должна уже существовать (startgame)
            gs = get_active_game_by_chat(db, game.chat_id)
            if not gs:
                await update.effective_chat.send_message("Нет активной игры. Сначала создай /startgame.")
                return

            game_id = gs["id"]

            # 2) upsert country в справочник countries (code = chosen_key)
            country_id = db.execute(
                sql_text("""
                    INSERT INTO countries (code, name, is_active)
                    VALUES (:code, :name, true)
                    ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name
                    RETURNING id
                """),
                {"code": chosen_key, "name": preset["name"]},
            ).scalar_one()

            # 3) вставить игрока
            player_id = db.execute(
                sql_text("""
                    INSERT INTO game_players (game_id, tg_user_id, country_id, is_active, is_afk)
                    VALUES (:game_id, :tg_user_id, :country_id, true, false)
                    ON CONFLICT (game_id, tg_user_id) DO NOTHING
                    RETURNING id
                """),
                {"game_id": game_id, "tg_user_id": user.id, "country_id": country_id},
            ).scalar_one_or_none()

            # 4) событие (если реально вставили игрока)
            if player_id is not None:
                audit_log(
                    db,
                    game_id=game_id,
                    chat_id=game.chat_id,
                    actor_tg_user_id=user.id,
                    action_type="player.joined",
                    phase_seq=gs["phase_seq"],
                    round_num=gs.get("round_num"),
                    payload={
                        "player_id": str(player_id),
                        "country_code": chosen_key,
                        "country_name": preset["name"],
                    },
                )
    
    cities_str = ", ".join([city_label(country, c) for c in ("A", "B", "C", "CAP")])
    await update.effective_chat.send_message(
        f"✅ {user.full_name} вступил в игру как **{country.name}**.\n"
        f"🏙 Города: {cities_str}",
        parse_mode=ParseMode.MARKDOWN,
    )




@require_game
async def choose_country(update: Update, context: ContextTypes.DEFAULT_TYPE, game: WorldState):
    user = update.effective_user

    # список свободных ключей
    taken = getattr(game, "taken_countries", set())
    available = [(k, v["name"]) for k, v in COUNTRY_PRESETS.items() if k not in taken]

    if not available:
        await update.effective_chat.send_message("Свободных стран больше нет.")
        return

    # inline кнопки
    keyboard = []
    for key, name in available:
        keyboard.append([InlineKeyboardButton(name, callback_data=f"pickcountry:{key}")])

    await update.effective_chat.send_message(
        "🌍 Выбери страну (свободные):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Доступные команды:\n\n"
        "/start – проверить, что бот жив.\n"
        "/help – показать список команд.\n\n"
        "/startgame – создать новую игру в этом чате (ведущий).\n"
        "/joingame – присоединиться к игре как страна.\n"
        "/gameinfo – показать, кто создал игру, текущий раунд и фазу.\n"
        "/endgame – завершить текущую игру и очистить состояние.\n\n"
        "/begin_round – начать новый раунд (доходы, экономика).\n"
        "/next_phase – перейти к следующей фазе раунда.\n"
        "/status – показать твой статус: казна, города, жетоны.\n"
        "/orders - вам придет сообщение в личку со списоком заказов для вашего госудасртва.\n\n"
        "/votum @тег игрока – объявить вотум недоверия:\n"
        "/votum_result – подсчитать результат голосования по вотуму. (обязательное условие)\n"
    )
    await update.effective_chat.send_message(text)

RULES_TEXT = (
    "1) Цель игры\n"
    "— Игра NeMonopolia - около ролевой проект, где каждый игрок принимает на себя обязанности прездиента одного из государств\n"
    "— Задача игры - Добиться мирового порядка любым доступным способом (Путем переговоров, дипломати или войны). Для достижения этих целей предусмотренны рычаги давления и возможности развития своей страны.\n"
    "— Игроки развивают страну и получают очки (в т.ч. через S/P жетоны, которые влияют на твое отношение к миру. S - жетоны отвечают за политику мира, P - жетоны отвечают за политику миллитаризма).\n"
    "— Вы можете вести переговоры с каждой страной индивидуально, а также делать публичные заявления.\n\n"

    "2) Раунды и фазы\n"
    "— Игра идёт по раундам. В каждом раунде ведущий двигает фазы кнопкой/командой.\n"
    "— Типовой цикл: Доходы → Событие → Мировая арена → Переговоры → Указы → Резолв.\n\n"

    "3) Доходы (начало раунда)\n"
    "— Доход городов: сумма % уровней жизни всех городов (пример: 50% = 50 у.е.).\n"
    "— Доход страны: Уровень жизни страны * 110 у.е.\n"
    "— Доход от экологии: Уровень мировой экологии * 200 у.е.\n"
    "— Торговля/санкции: +50 у.е. за каждого партнёра по твоему договору, -50 у.е. за санкцию от каждого, кто ввел санкции против тебя (за каждый раунд).\n"
    "— Разрушенный город должен давать 0 дохода.\n\n"

    "4) Указы (делаются в кабинете через /orders в фазу 📦 Указы)\n"
    "— Игрок открывает свой кабинет в личных сообщениях с ботом, выбирает действия кнопками и ЖМЁТ ✅ Подтвердить.\n"
    "— Без подтверждения пакет НЕ применяется.\n"
    "— В конце раунда вы увидите итоги каждой страны, основываясь на указах каждого\n\n"

    "5) Базовые действия (если доступны в твоей версии)\n"
    "— Улучшить город (1 раз на город/раунд): повышает уровень жизни.\n"
    "— Поставить щит (1 раз на город/раунд): защищает от 1 удара и исчезает при срабатывании.\n"
    "— Улучшить экологию: повышает экологию мира.\n"
    "— Ядерная промышленность: нужна для производства боеголовок.\n"
    "— Боеголовки: до 3 за раунд (если включено правило).\n\n"

    "6) Ядерные удары (если включены)\n"
    "— До 3 ударов за раунд.\n"
    "— На удар тратится 1 боеголовка.\n"
    "— Если на цели щит: щит снимается, урон не проходит.\n"
    "— Если щита нет: город становится разрушенным, экология мира -5.\n"
    "— Восстановление разрушенного города: стоимость = Цена всех вложений в город + 150 у.е..\n\n"

    "7) Вотум недоверия\n"
    "— Объявляется на Мировой арене.\n"
    "— Голосование кнопками. При выполнении порога применяются санкции/штрафы.\n\n"

    "8) Главное правило\n"
    "— Всё, что не подтверждено ✅, считается НЕ сделанным."
)

async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает правила: в группе — отправляет в личку, в личке — печатает сразу."""
    user = update.effective_user
    chat = update.effective_chat
    text = "📘 Правила игры\n\n" + RULES_TEXT

    if chat.type == "private":
        await chat.send_message(text)
        return

    # группа/супергруппа → шлём в личку
    try:
        await context.bot.send_message(user.id, text)
        await chat.send_message("📘 Правила отправлены тебе в личку.")
    except Exception:
        await chat.send_message(
            "Не могу написать тебе в личку. Открой чат с ботом, нажми Start и повтори /rules."
        )


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать постоянное меню команд под строкой ввода (reply keyboard)."""
    keyboard = [
        [
            KeyboardButton("🎮 Старт игры"),
            KeyboardButton("➕ Вступить"),
        ],
        [
            KeyboardButton("🌍 Выбрать страну"),
            KeyboardButton("📘 Правила"),
        ],
        [
            KeyboardButton("📊 Статус"),
            KeyboardButton("ℹ Инфо об игре"),
        ],
        [
            KeyboardButton("▶ Новый раунд"),
            KeyboardButton("⏭ Следующая фаза"),
        ],
        [
            KeyboardButton("📜 Команды"),
            KeyboardButton("⛔ Завершить игру"),
        ],
    ]

    reply_markup = ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
    )

    await update.effective_chat.send_message(
        "Меню команд: кнопки доступны под строкой ввода.",
        reply_markup=reply_markup,
    )

async def ready_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1) разрешаем только в группе (где игра)
    chat = update.effective_chat
    user = update.effective_user

    if not chat or chat.type == "private":
        await update.effective_chat.send_message("Команда /ready работает только в игровом групповом чате.")
        return

    chat_id = chat.id
    tg_user_id = user.id

    db = SessionLocal()
    try:
        # 2) найти активную игру и текущую фазу
        gs = get_active_game_by_chat(db, chat_id)
        if not gs:
            await update.effective_chat.send_message("Активная игра не найдена. Сначала создай игру /startgame.")
            return

        game_id = gs["id"]
        phase_seq = gs["phase_seq"]

        # 3) найти игрока в этой игре
        row = db.execute(
            sql_text("""
                SELECT id AS player_id, is_afk
                FROM game_players
                WHERE game_id = :game_id AND tg_user_id = :tg_user_id AND is_active = TRUE
                LIMIT 1
            """),
            {"game_id": game_id, "tg_user_id": tg_user_id},
        ).mappings().first()

        if not row:
            await update.effective_chat.send_message("Ты не игрок этой партии. Сначала вступи в игру (/joingame).")
            return

        if row["is_afk"]:
            await update.effective_chat.send_message("Ты помечен AFK. Сними AFK (или подожди авто-снятие), потом /ready.")
            return

        player_id = row["player_id"]

        # 4) вставить ready (ON CONFLICT — чтобы повторный /ready не ломал)
        with db.begin():
            ins = db.execute(
                sql_text("""
                    INSERT INTO game_phase_ready (game_id, player_id, phase_seq)
                    VALUES (:game_id, :player_id, :phase_seq)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                """),
                {"game_id": game_id, "player_id": player_id, "phase_seq": phase_seq},
            ).mappings().first()

            # Если вставка была (не повторный /ready)
            if ins:
                ready_id = ins["id"]

                audit_log(
                    db,
                    game_id=game_id,
                    chat_id=chat_id,
                    actor_tg_user_id=tg_user_id,
                    action_type="player.ready_set",
                    phase_seq=phase_seq,
                    round_num=gs.get("round_num"),
                    payload={"player_id": str(player_id), "ready_id": str(ready_id)},
                )

                emit_event(
                    db,
                    event_type="player.ready_set",
                    aggregate_type="game_session",
                    aggregate_id=game_id,
                    payload={
                        "chat_id": chat_id,
                        "player_id": str(player_id),
                        "tg_user_id": tg_user_id,
                        "phase_seq": phase_seq,
                    },
                    idempotency_key=f"player.ready_set:{game_id}:{player_id}:{phase_seq}",
                )

        # 5) посчитать ready и total (исключаем AFK)
        rm = db.execute(
            sql_text("""
                SELECT ready_count, ready_total
                FROM v_current_game_by_chat
                WHERE chat_id = :chat_id
            """),
            {"chat_id": chat_id},
        ).mappings().first()


        ready_cnt = rm["ready_count"] if rm else 0
        total_cnt = rm["ready_total"] if rm else 0

        await update.effective_chat.send_message(f"✅ Ready принят. ({ready_cnt}/{total_cnt})")

        # 6) Если хочешь авто-переход — раскомментируй:
        # if total_cnt > 0 and ready_cnt >= total_cnt:
        #     await update.effective_chat.send_message("Все готовы. Пытаюсь перейти к следующей фазе...")
        #     await next_phase(update, context)  # если next_phase у тебя уже двигает фазу

    except Exception as e:
        db.rollback()
        # покажем реальную ошибку, иначе ты никогда не найдёшь причину
        await update.effective_chat.send_message(f"❌ Ошибка /ready: {type(e).__name__}: {e}")
        raise
    finally:
        db.close()

async def safe_edit(query, text, reply_markup=None, parse_mode=None):
    """
    Безопасно редактирует сообщение по callback query.
    Игнорирует 'Message is not modified'.
    """
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise

async def game_announce(context: ContextTypes.DEFAULT_TYPE, game: WorldState, text: str):
    """Пишет в игровой чат (публично). Ошибки не валят бот."""
    try:
        await context.bot.send_message(chat_id=game.chat_id, text=text)
    except Exception:
        logging.exception("game_announce failed")

async def reply_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Роутер для текстовых кнопок reply-меню."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    if text == "🎮 Старт игры":
        await start_game(update, context)

    elif text == "➕ Вступить":
        await join_game(update, context)

    elif text == "📊 Статус":
        await status_cmd(update, context)

    elif text == "ℹ Инфо об игре":
        await gameinfo_cmd(update, context)

    elif text == "▶ Новый раунд":
        await begin_round(update, context)

    elif text == "⏭ Следующая фаза":
        await next_phase(update, context)

    elif text == "📜 Команды":
        await help_cmd(update, context)

    elif text == "⛔ Завершить игру":
        await endgame_cmd(update, context)
        
    elif text == "🌍 Выбрать страну":
        await choose_country(update, context)
    
    elif text == "📘 Правила":
        await rules_cmd(update, context)



async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на inline-кнопки меню."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    data = data.strip()

    # Для удобства
    chat = update.effective_chat

    if data == "menu:startgame":
        # Старт игры может быть только когда её ещё нет – логика уже есть в start_game
        await start_game(update, context)
        return

    # Всё ниже – команды, которые требуют существующей игры.
    game = get_game(chat.id)
    if not game and data not in ("menu:help",):
        await chat.send_message("Игра ещё не создана. Используйте /startgame или кнопку 'Старт игры'.")
        return

    if data == "menu:joingame":
        await join_game(update, context)          # @require_game сам подставит game
    elif data == "menu:status":
        await status_cmd(update, context, game) if False else await status_cmd(update, context)
    elif data == "menu:gameinfo":
        await gameinfo_cmd(update, context, game) if False else await gameinfo_cmd(update, context)
    elif data == "menu:begin_round":
        await begin_round(update, context, game) if False else await begin_round(update, context)
    elif data == "menu:next_phase":
        await next_phase(update, context, game) if False else await next_phase(update, context)
    elif data == "menu:help":
        await help_cmd(update, context)
    elif data == "menu:endgame":
        await endgame_cmd(update, context, game) if False else await endgame_cmd(update, context)

async def orders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /orders:
    - в группе: отправляет игроку меню в личку и привязывает его к этой игре
    - в личке: открывает меню по последней привязке USER_ACTIVE_GAME
    """
    user = update.effective_user
    chat = update.effective_chat

    # Если вызвали в группе/супергруппе — привязываем к текущей игре
    if chat.type in ("group", "supergroup"):
        game_chat_id = chat.id
        if game_chat_id not in GAMES:
            await chat.send_message("Игра ещё не создана. Ведущий должен сделать /startgame.")
            return

        USER_ACTIVE_GAME[user.id] = game_chat_id

        # Пытаемся отправить в личку
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text="📦 Кабинет указов открыт. Здесь ты собираешь указы кнопками.\n"
                     "Нажимай кнопки — я буду обновлять твой пакет приказов.\n\n"
                     "Если кнопки не появляются — нажми /start в личке с ботом.",
                reply_markup=_orders_main_keyboard()
            )
            await chat.send_message(f"{user.full_name}, отправил меню указов тебе в личку ✅")
        except Forbidden:
            await chat.send_message(
                f"{user.full_name}, я не могу написать тебе в личку.\n"
                "Открой бота и нажми /start, затем снова введи /orders."
            )
        return

    # Если вызвали в личке
    game_chat_id = USER_ACTIVE_GAME.get(user.id)
    if not game_chat_id or game_chat_id not in GAMES:
        await chat.send_message(
            "Ты не привязан ни к одной активной игре.\n"
            "Зайди в игровой групповой чат и введи /orders там — я привяжу тебя и открою кабинет."
        )
        return

    await chat.send_message(
        "📦 Кабинет указов открыт.",
        reply_markup=_orders_main_keyboard()
    )

@require_game
async def endgame_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, game: WorldState):
    user = update.effective_user

    # Защита: завершать игру может только тот, кто её создал
    if game.owner_id is not None and user.id != game.owner_id:
        await update.effective_chat.send_message(
            "Завершить игру может только ведущий, который её создал."
        )
        return

    chat_id = update.effective_chat.id

    game.phase = Phase.FINISHED

    chat_id = update.effective_chat.id

    with SessionLocal() as db:
        with db.begin():
            
            gs = lock_game_row(db, game.chat_id)
            if not gs:
                return
            game_id = gs["id"]
            
            gs = get_active_game_by_chat(db, chat_id)
            if not gs:
                await update.effective_chat.send_message("Активная игра не найдена. Сначала создай игру /startgame.")
                return

            game_id = gs["id"]
            phase_seq = gs["phase_seq"]

            # фиксируем завершение игры в БД
            db.execute(
                sql_text("""
                    UPDATE game_sessions
                    SET status = 'finished',
                        current_phase = 'finished',
                        phase_started_at = now()
                    WHERE id = :id
                """),
                {"id": game_id},
            )

            audit_log(
                db,
                game_id=game_id,
                chat_id=chat_id,
                actor_tg_user_id=update.effective_user.id,
                action_type="game.finished",
                phase_seq=gs.get("phase_seq"),
                round_num=gs.get("round_num"),
                payload={},
            )

            # outbox
            emit_event(
                db,
                event_type="game.finished",
                aggregate_type="game_session",
                aggregate_id=game_id,
                payload={"chat_id": chat_id},
                idempotency_key=f"game.finished:{game_id}",
            )

    if chat_id in GAMES:
        del GAMES[chat_id]


    await update.effective_chat.send_message(
        "Игра завершена. Состояние очищено. Можно запустить новую игру командой /startgame. \n "
        "Создатель игры: Александр Т.\n"
        "Special Thanks: Марта <3 \n\n"
    )



@require_game
async def pickcountry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, game: WorldState):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    key = query.data.split(":", 1)[1]

    if key not in COUNTRY_PRESETS:
        await query.answer("Неизвестная страна.", show_alert=True)
        return

    # 1) Если занято другим — отказ
    if key in game.taken_countries and game.player_country_key.get(user.id) != key:
        await query.answer("Эта страна уже занята.", show_alert=True)
        return

    # 2) Если у пользователя был другой выбор — освобождаем
    old_key = game.player_country_key.get(user.id)
    if old_key and old_key != key:
        game.taken_countries.discard(old_key)

    # 3) Назначаем выбор
    game.player_country_key[user.id] = key
    game.taken_countries.add(key)

    preset = COUNTRY_PRESETS[key]

    # 4) Если игрок уже вступил — обновляем его страну на лету
    if user.id in game.countries:
        country = game.countries[user.id]
        country.name = preset["name"]
        for code in ("A", "B", "C", "CAP"):
            if code in country.cities:
                country.cities[code].name = preset["cities"][code]

        await query.edit_message_text(
            f"✅ Вы выбрали страну: {country.name}\n"
            f"🏙 Города: {city_label(country,'A')}, {city_label(country,'B')}, "
            f"{city_label(country,'C')}, {city_label(country,'CAP')}"
        )
        return

    # 5) Если НЕ вступил — просто подтверждаем выбор (без обращения к game.countries)
    await query.edit_message_text(
        f"✅ Вы выбрали страну: {preset['name']}\n"
        f"🏙 Города: {preset['cities']['A']} (A), {preset['cities']['B']} (B), "
        f"{preset['cities']['C']} (C), {preset['cities']['CAP']} (CAP)\n\n"
        f"Теперь введи /joingame или нажми кнопку Вступить."
    )


@require_game
async def begin_round(update: Update, context: ContextTypes.DEFAULT_TYPE, game: WorldState):
    if game.phase not in [Phase.LOBBY, Phase.RESOLVE]:
        await update.effective_chat.send_message(
            f"Новый раунд можно запустить только из фаз {Phase.LOBBY} или {Phase.RESOLVE}."
        )
        return

    if game.phase == Phase.RESOLVE and not getattr(game, "round_resolved", False):
        await update.effective_chat.send_message("Сначала заверши раунд командой /resolve_round.")
        return

    game.round_num += 1
    game.round_resolved = False
    game.phase = Phase.INCOME

    with SessionLocal() as db:
        with db.begin():
            # 1) ЛОЧИМ текущую игру (FOR UPDATE)
            gs = lock_game_row(db, game.chat_id)
            if not gs:
                await update.effective_chat.send_message("Нет активной игры. Сначала /startgame.")
                return

            game_id = gs["id"]

            # 2) UPDATE ТОЛЬКО ПО id
            row = db.execute(
                sql_text("""
                    UPDATE game_sessions
                    SET current_phase = :phase,
                        phase_seq = phase_seq + 1,
                        phase_started_at = now(),
                        round_num = :round_num
                    WHERE id = :id
                    RETURNING id, phase_seq, round_num
                """),
                {"phase": game.phase.value, "round_num": game.round_num, "id": game_id},
            ).mappings().first()

            audit_log(
                db,
                game_id=row["id"],
                chat_id=game.chat_id,
                actor_tg_user_id=update.effective_user.id,
                action_type="round.started",
                phase_seq=row["phase_seq"],
                round_num=row["round_num"],
                payload={"new_phase": game.phase.value},
            )

            if not row:
                await update.effective_chat.send_message("Не удалось обновить сессию игры (DB).")
                return

            phase_seq = row["phase_seq"]

            insert_snapshot(
                db,
                game_id=row["id"],
                chat_id=game.chat_id,
                phase_seq=row["phase_seq"],
                round_num=row["round_num"],
                snapshot={
                    "status": "active",
                    "current_phase": game.phase.value,
                    "phase_seq": row["phase_seq"],
                    "round_num": row["round_num"],
                    "source": "begin_round",
                },
            )

            # 3) round.started
            emit_event(
                db,
                event_type="round.started",
                aggregate_type="game_session",
                aggregate_id=row["id"],
                payload={
                    "chat_id": game.chat_id,
                    "round_num": row["round_num"],
                    "phase_seq": phase_seq,
                },
                idempotency_key=f"round.started:{row['id']}:{row['round_num']}",
            )

            # 4) phase.changed
            emit_event(
                db,
                event_type="phase.changed",
                aggregate_type="game_session",
                aggregate_id=row["id"],
                payload={
                    "chat_id": game.chat_id,
                    "new_phase": game.phase.value,
                    "phase_seq": phase_seq,
                    "round_num": row["round_num"],
                },
                idempotency_key=f"phase.changed:{row['id']}:{phase_seq}",
            )

    ecology_income = compute_ecology_income(game)
    messages = [f"Раунд {game.round_num}. Начисление доходов. Экология: {game.ecology}%"]

    for country in game.countries.values():
        cities_income = country.income_cities()
        country_income = country.income_country()
        trade_income = compute_trade_income(game, country)
        total_income = cities_income + country_income + ecology_income + trade_income
        country.treasury += total_income

        messages.append(
            f"🇺🇳 {country.name}: города={cities_income}, страна={country_income}, "
            f"экология={ecology_income}, торговля={trade_income} → всего {total_income}. "
            f"Казна: {country.treasury}"
        )

    # --- выбрать событие на раунд ---
    if EVENTS_POOL:
        game.current_event = random.choice(EVENTS_POOL)
        game.event_choices.clear()
        await game_announce(
            context, game,
            f"🌍 Событие раунда: {game.current_event['title']}\n{game.current_event.get('flavor','')}"
        )
    else:
        game.current_event = None
        game.event_choices.clear()

    await update.effective_chat.send_message("\n".join(messages))
    await update.effective_chat.send_message(
        "Фаза доходов завершена. Ведущий может перейти к событию /next_phase."
    )
    if game.round_num == 1:
        await update.effective_chat.send_message("📘 Правила игры (кратко):\n\n" + RULES_TEXT)




async def handle_event_phase(update: Update, context: ContextTypes.DEFAULT_TYPE, game: WorldState):
    if not EVENTS:
        await update.effective_chat.send_message(
            "Фаза события. События не настроены (events.json не найден или пуст)."
        )
        return

    event = random.choice(EVENTS)
    game.current_event = event
    title = event.get("title") or event.get("name") or "Без названия"
    desc = event.get("flavor") or event.get("description") or ""

    text = (
        f"📢 Событие раунда: {title}\n\n"
        f"{desc}\n\n"
        "Выберите необходимое действие во время раунда 📦 Указы → 🌍 Событие"
    )
    await update.effective_chat.send_message(text)


@require_game
async def next_phase(update: Update, context: ContextTypes.DEFAULT_TYPE, game: WorldState):
    order = [
        Phase.INCOME,
        Phase.EVENT,
        Phase.WORLD_ARENA,
        Phase.NEGOTIATIONS,
        Phase.ORDERS,
        Phase.RESOLVE,
    ]

    if game.phase == Phase.LOBBY:
        await update.effective_chat.send_message(
            "Используй /begin_round для запуска первого раунда."
        )
        return
    if game.phase == Phase.FINISHED:
        await update.effective_chat.send_message("Игра уже завершена.")
        return

    try:
        idx = order.index(game.phase)
        next_p = order[idx + 1]
    except (ValueError, IndexError):
        next_p = Phase.RESOLVE

    game.phase = next_p
    
    with SessionLocal() as db:
        with db.begin():

            gs = lock_game_row(db, game.chat_id)
            if not gs:
                return
            game_id = gs["id"]

            row = db.execute(
                sql_text("""
                    UPDATE game_sessions
                    SET current_phase = :phase,
                        phase_seq = phase_seq + 1,
                        phase_started_at = now()
                    WHERE chat_id = :chat_id
                    AND status IN ('lobby','active')
                    RETURNING id, phase_seq, round_num
                """),
                {"chat_id": game.chat_id, "phase": game.phase.value},
            ).mappings().first()

            insert_snapshot(
                db,
                game_id=row["id"],
                chat_id=game.chat_id,
                phase_seq=row["phase_seq"],
                round_num=row["round_num"],
                snapshot={
                    "status": gs["status"] if gs else None,
                    "current_phase": next_p.value,
                    "phase_seq": row["phase_seq"],
                    "round_num": row["round_num"],
                    "source": "next_phase",
                },
            )

            audit_log(
                db,
                game_id=row["id"],
                chat_id=game.chat_id,
                actor_tg_user_id=update.effective_user.id,
                action_type="phase.changed",
                phase_seq=row["phase_seq"],
                round_num=row["round_num"],
                payload={
                    "new_phase": next_p.value,
                    # опционально:
                    # "prev_phase": prev_phase_code,
                },
            )

            if row:
                emit_event(
                    db,
                    event_type="phase.changed",
                    aggregate_type="game_session",
                    aggregate_id=row["id"],
                    payload={
                        "chat_id": game.chat_id,
                        "new_phase": game.phase.value,
                        "phase_seq": row["phase_seq"],
                        "round_num": row["round_num"],
                    },
                )

    if game.phase == Phase.EVENT:
        await handle_event_phase(update, context, game)
    else:
        await send_phase_intro(update.effective_chat, game)

    if game.phase == Phase.RESOLVE:
        await resolve_round(update, context)

@require_game
async def gameinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, game: WorldState):
    chat = update.effective_chat
    chat_id = chat.id

    with SessionLocal() as db:
        gs = get_active_game_by_chat(db, chat_id)
        if not gs:
            await chat.send_message("Активная игра не найдена. /startgame")
            return

        owner_id = gs.get("owner_tg_user_id")
        owner_link = f'<a href="tg://user?id={owner_id}">Ведущий</a>' if owner_id else "не указан"

        text = (
            f"Создатель игры: {owner_link}\n"
            f"Текущий раунд: {gs.get('round_num')}\n"
            f"Текущая фаза: {gs.get('current_phase')} (seq={gs.get('phase_seq')})\n"
            f"Игроки: {gs.get('players_active')}/{gs.get('players_total')}\n"
            f"Ready: {gs.get('ready_count')}/{gs.get('ready_total')}"
        )

    await chat.send_message(text, parse_mode=ParseMode.HTML)

@require_game
async def resolve_round(update: Update, context: ContextTypes.DEFAULT_TYPE, game: WorldState):
    messages = ["Применение указов и пересчёт экономики."]

    for country in game.countries.values():
        
        if not getattr(country, "orders_confirmed", False):
            messages.append(f"⏳ {country.name}: пакет не подтверждён — пропуск")
            continue

        changes, errors = apply_orders_for_country(game, country)

        if errors:
            short = errors[0]
            messages.append(f"❌ {country.name}: указы не применены — {short}")
            continue

        if changes:
            messages.append(f"✅ {country.name}:\n" + "\n".join(changes))
        else:
            messages.append(f"ℹ️ {country.name}: нет указов")

    await update.effective_chat.send_message("\n\n".join(messages))
    await update.effective_chat.send_message(
        "Раунд завершён. Ведущий может запустить /begin_round или нажать кнопку Новый раунд для следующего раунда "
        "или завершить игру."
    )

    chat_id = update.effective_chat.id

    with SessionLocal() as db:
        with db.begin():
            gs = get_active_game_by_chat(db, chat_id)
            if not gs:
                await update.effective_chat.send_message("Активная игра не найдена. Сначала создай игру /startgame.")
                return

            game_id = gs["id"]
            phase_seq = gs["phase_seq"]

            emit_event(
                db,
                event_type="round.resolved",
                aggregate_type="game_session",
                aggregate_id=game_id,
                payload={
                    "chat_id": chat_id,
                    "round_num": round_num,
                },
                idempotency_key=f"round.resolved:{game_id}:{round_num}",
            )

            insert_snapshot(
                db,
                game_id=game_id,
                chat_id=chat_id,
                phase_seq=gs["phase_seq"] if gs else 0,
                round_num=round_num,
                snapshot={
                    "status": gs["status"] if gs else None,
                    "current_phase": gs["current_phase"] if gs else None,
                    "phase_seq": gs["phase_seq"] if gs else None,
                    "round_num": round_num,
                    "source": "resolve_round",
                },
            )

            audit_log(
                db,
                game_id=game_id,
                chat_id=chat_id,
                actor_tg_user_id=update.effective_user.id,
                action_type="round.resolved",
                phase_seq=gs.get("phase_seq"),
                round_num=round_num,
                payload={},
            )

    game.round_resolved = True


@require_game
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, game: WorldState):
    user = update.effective_user
    country = game.countries.get(user.id)
    lines: List[str] = []
    chat_id = update.effective_chat.id
    with SessionLocal() as db:
        gs = get_active_game_by_chat(db, chat_id)
    if gs:
        lines.append(f"Раунд: {gs.get('round_num')}, фаза: {gs.get('current_phase')}")
    else:
        lines.append(f"Раунд: {game.round_num}, фаза: {game.phase.value}")
    lines.append(f"Экология мира: {game.ecology}%")
    if country:
        lines.append(f"Ты — {country.name}")
        lines.append(f"Казна: {country.treasury}")
        lines.append(
            "Города: " +
            ", ".join([f"{c.name}: {c.life}%" for c in country.cities.values()])
        )
        lines.append(f"S-жетоны: {country.s_tokens}, P-жетоны: {country.p_tokens}")
        lines.append(
            f"Ядерпром: {'есть' if country.has_nuclear_industry else 'нет'}, "
            f"боеголовок: {country.nukes}"
        )
    else:
        lines.append("Ты пока не участвуешь как страна. Используй /joingame.")

    await update.effective_chat.send_message("\n".join(lines))


# ---------------- ОРДЕРА В ЛИЧКЕ -----------------

async def orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = (query.data or "")

    # Должно работать только в личке
    if update.effective_chat.type != "private":
        await query.answer("Открой /orders в личке с ботом.", show_alert=True)
        return

    # Находим, к какой игре привязан пользователь
    game_chat_id = USER_ACTIVE_GAME.get(user.id)
    if not game_chat_id:
        await query.answer("Ты не привязан к игре. Введи /orders в игровом чате.", show_alert=True)
        return

    game = GAMES.get(game_chat_id)
    if not game:
        await query.answer("Игра уже завершена/не найдена. Введи /orders в игровом чате снова.", show_alert=True)
        return

    # Проверяем что он страна
    if user.id not in game.countries:
        await query.answer("Ты не участвуешь в игре как страна.", show_alert=True)
        return

    country = game.countries[user.id]

    # режим меню
    mode = context.user_data.get("orders_mode", "main")

    # -------- навигация --------
    if data == "ord:city_menu":
        context.user_data["orders_mode"] = "city"
        country.orders_confirmed = False
        user_id = query.from_user.id
        await safe_edit(
            query,
            render_orders_ui(country, game, update.effective_user.id),
            _orders_city_keyboard("ord:city_set", country),
            parse_mode=ParseMode.MARKDOWN,
        )
        await query.answer()
        return

    if data == "ord:shield_menu":
        context.user_data["orders_mode"] = "shield"
        country.orders_confirmed = False
        await safe_edit(
            query,
            render_orders_ui(country, game, update.effective_user.id),
            _orders_city_keyboard("ord:shield_set", country),
            parse_mode=ParseMode.MARKDOWN,
        )
        await query.answer()
        return

    if data in ("ord:back", "ord:refresh"):
        context.user_data["orders_mode"] = "main"
        await safe_edit(
            query,
            render_orders_ui(country, game, update.effective_user.id),
            _orders_main_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
        await query.answer()
        return
    
    if data == "ord:sanctions_menu":
        context.user_data["orders_mode"] = "sanctions"
        await safe_edit(
            query,
            render_orders_ui(country, game, query.from_user.id),
            reply_markup=_orders_sanctions_keyboard(game, user.id, country),
            parse_mode=ParseMode.MARKDOWN,
        )
        await query.answer()
        return

    # -------- переключатели --------
    elif data == "ord:eco_toggle":
        key = ORDER_KEYS["eco"]
        country.orders_confirmed = False

        enabled = toggle_order_flag(country, key)
        await query.answer("🌿 Экология: включено" if enabled else "🌿 Экология: выключено")

        user_id = query.from_user.id
        await safe_edit(
            query,
            render_orders_ui(country, game, update.effective_user.id),
            _orders_main_keyboard(),
            ParseMode.MARKDOWN,
        )
        return

    elif data == "ord:nuc_ind_toggle":
        key = ORDER_KEYS["nuc_ind"]

        country.orders_confirmed = False

        if int(country.orders.get(key, 0) or 0) == 1:
            country.orders.pop(key, None)
            await query.answer("☢ Ядерная промышленность: выключено")
        else:
            country.orders[key] = 1
            await query.answer("☢ Ядерная промышленность: включено")

        user_id = query.from_user.id
        await safe_edit(
            query,
            render_orders_ui(country, game, update.effective_user.id),
            _orders_main_keyboard(),
            ParseMode.MARKDOWN,
        )
        return

    elif data == "ord:event_menu":
        await safe_edit(
            query,
            render_orders_ui(country, game, query.from_user.id),
            reply_markup=_orders_event_keyboard(game),
            parse_mode=ParseMode.MARKDOWN,
        )
        await query.answer()
        return

    elif data.startswith("ord:event_pick:"):
        if not game.current_event:
            await query.answer("События в этом раунде нет.", show_alert=True)
            return

        opt_key = data.split(":")[-1]
        valid = {o["key"] for o in game.current_event.get("options", [])}
        if opt_key not in valid:
            await query.answer("Некорректный выбор.", show_alert=True)
            return

        game.event_choices[query.from_user.id] = opt_key
        country.orders_confirmed = False
        await query.answer("Выбор события сохранён ✅")

        await safe_edit(
            query,
            render_orders_ui(country, game, query.from_user.id),
            reply_markup=_orders_main_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    elif data.startswith("ord:sanction_toggle:"):
        target_id = int(data.split(":")[-1])
        if target_id not in game.countries:
            await query.answer("Цель не найдена.", show_alert=True)
            return

        target = game.countries[target_id]
        actor_id = query.from_user.id 
        deal_was_broken = False

        # если санкции уже активны — снимаем
        if target_id in country.sanctions_to:
            country.sanctions_to.discard(target_id)
            target.sanctions_from.discard(actor_id)

            await query.answer("🚫 Санкции сняты ✅")
            await game_announce(context, game, f"✅ Санкции сняты: {country.name} снял санкции с {target.name}.")
        else:
            # если был договор — разрываем у обоих
            if target_id in country.trade_deals:
                country.trade_deals.discard(target_id)
                target.trade_deals.discard(actor_id)
                deal_was_broken = True

            # включаем санкции
            country.sanctions_to.add(target_id)
            target.sanctions_from.add(actor_id)

            await query.answer("🚫 Санкции наложены")
            if deal_was_broken:
                await game_announce(
                    context, game,
                    f"🚫 Санкции: {country.name} ввёл санкции против {target.name}. 🤝 Договор разорван. (−50 у.е./раунд для {target.name})"
                )
            else:
                await game_announce(
                    context, game,
                    f"🚫 Санкции: {country.name} ввёл санкции против {target.name}. (−50 у.е./раунд для {target.name})"
                )

        # обновляем экран санкций
        await safe_edit(
            query,
            render_orders_ui(country, game, update.effective_user.id),
            reply_markup=_orders_sanctions_keyboard(game, actor_id, country),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    
        
        


    elif data == "ord:nuke_plus":
        key = ORDER_KEYS["nukes"]
        cur = int(country.orders.get(key, 0) or 0)
        country.orders[key] = min(3, cur + 1)
        country.orders_confirmed = False

    elif data == "ord:nuke_minus":
        key = ORDER_KEYS["nukes"]
        cur = int(country.orders.get(key, 0) or 0)
        country.orders[key] = max(0, cur - 1)
        country.orders_confirmed = False

    # -------- выбор города --------
    elif data.startswith("ord:city_set:"):
        code = data.split(":")[-1]
        key = ORDER_KEYS["city"](code)
        country.orders[key] = 1
        country.orders_confirmed = False
        context.user_data["orders_mode"] = "main"
        await query.answer("Город выбран.")

    elif data.startswith("ord:shield_set:"):
        code = data.split(":")[-1]
        key = ORDER_KEYS["shield"](code)
        country.orders[key] = 1
        country.orders_confirmed = False
        context.user_data["orders_mode"] = "main"
        await query.answer("Щит: город выбран.")

    # -------- очистить / подтвердить --------
    elif data == "ord:clear":
        country.orders.clear()
        context.user_data["orders_mode"] = "main"
        country.planned_strikes.clear()
        country.orders_confirmed = False
        await safe_edit(
            query,
            render_orders_ui(country, game, update.effective_user.id),
            _orders_main_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
        await query.answer("Очищено.")
        return

    elif data == "ord:confirm":
        report = calc_orders_cost_and_validate(country, game)
        if not report["ok"]:
            await query.answer("Нельзя подтвердить: есть ошибки в пакете.", show_alert=True)
            return

        if country.orders_confirmed:
            await query.answer("Уже подтверждено ✅. Вернись в основной канал для продолжения игры")
            return

        country.orders_confirmed = True
        await query.answer("Указы подтверждены ✅. Вернись в основной канал для продолжения игры")
        
        try:
            confirmed = sum(
                1 for c in game.countries.values()
                if getattr(c, "orders_confirmed", False)
            )
            total = len(game.countries)

            await context.bot.send_message(
                chat_id=game_chat_id,
                text=f"✅ {country.name} подтвердил(а) указы. ({confirmed}/{total})"
            )
        except Exception:
            import logging
            logging.exception("Не смог отправить уведомление о подтверждении указов в общий чат.")

        await safe_edit(
            query,
            render_orders_ui(country, game, update.effective_user.id),
            _orders_main_keyboard(),
            ParseMode.MARKDOWN,
        )
        return

    elif data == "ord:trade_menu":
        context.user_data["orders_mode"] = "trade"
        await safe_edit(
            query,
            render_orders_ui(country, game, user.id),
            reply_markup=_orders_trade_keyboard(game, user.id, country),
            parse_mode=ParseMode.MARKDOWN,
        )
        await query.answer()
        return

    elif data.startswith("ord:trade_request:"):
        target_id = int(data.split(":")[-1])
        if target_id not in game.countries:
            await query.answer("Цель не найдена.", show_alert=True)
            return

        target = game.countries[target_id]

        # запрет, если есть санкции в любую сторону
        if (target_id in country.sanctions_to) or (user.id in target.sanctions_to):
            await query.answer("Нельзя: между вами санкции.", show_alert=True)
            return

        if target_id in country.trade_deals:
            await query.answer("Договор уже активен.", show_alert=True)
            return

        # сохраняем pending (на стороне game)
        game.pending_trade[target_id] = user.id

        # отправляем цели в личку запрос с кнопками
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=f"🤝 Торговое предложение от **{country.name}**.\n"
                    f"Если примешь — вы оба будете получать **+50 у.е./раунд**.\n"
                    f"Принять?",
                reply_markup=_trade_request_keyboard(user.id),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            await query.answer("Не смог отправить запрос цели (возможно, цель не открывала бота).", show_alert=True)
            return

        await query.answer("Запрос отправлен, ожидайте ответа ✅")
        await safe_edit(
            query,
            render_orders_ui(country, game, update.effective_user.id),
            reply_markup=_orders_trade_keyboard(game, user.id, country),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    elif data.startswith("ord:trade_accept:"):
        requester_id = int(data.split(":")[-1])

        # проверка pending
        if game.pending_trade.get(user.id) != requester_id:
            await query.answer("Запрос уже не актуален.", show_alert=True)
            return

        if requester_id not in game.countries:
            await query.answer("Инициатор не найден.", show_alert=True)
            return

        requester = game.countries[requester_id]

        # запрет, если санкции
        if (requester_id in country.sanctions_to) or (user.id in requester.sanctions_to):
            game.pending_trade.pop(user.id, None)
            await query.answer("Нельзя: между вами санкции.", show_alert=True)
            return

        # активируем договор у обоих
        country.trade_deals.add(requester_id)
        requester.trade_deals.add(user.id)

        game.pending_trade.pop(user.id, None)

        await query.answer("Договор активирован ✅")

        # уведомим инициатора
        try:
            await context.bot.send_message(
                chat_id=requester_id,
                text=f"🤝 **{country.name}** принял(а) торговое соглашение. Теперь у вас обоих +50 у.е./раунд.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

        # обновим экран получателя (если он сейчас в trade-меню)
        await safe_edit(
            query,
            render_orders_ui(country, game, update.effective_user.id),
            reply_markup=_orders_trade_keyboard(game, user.id, country),
            parse_mode=ParseMode.MARKDOWN,
        )
        return


    elif data.startswith("ord:trade_decline:"):
        requester_id = int(data.split(":")[-1])
        if game.pending_trade.get(user.id) == requester_id:
            game.pending_trade.pop(user.id, None)
        await query.answer("Отклонено ❌")
        return

    elif data == "ord:strike_menu":
        context.user_data["orders_mode"] = "strike_target"
        country.orders_confirmed = False

        await safe_edit(
            query,
            render_orders_ui(country, game, update.effective_user.id),
            _orders_targets_keyboard(game, user.id),
            ParseMode.MARKDOWN,
        )
        await query.answer()
        return
    
    elif data.startswith("ord:strike_tgt:"):
        target_id = int(data.split(":")[-1])
        context.user_data["strike_target_id"] = target_id
        context.user_data["orders_mode"] = "strike_city"
        country.orders_confirmed = False

        await safe_edit(
            query,
            "Выбери город-цель для удара:",
            _orders_city_keyboard("ord:strike_city", country),
            ParseMode.MARKDOWN,
        )
        await query.answer()
        return

    elif data.startswith("ord:trade_cancel:"):
        target_id = int(data.split(":")[-1])
        if target_id not in game.countries:
            await query.answer("Цель не найдена.", show_alert=True)
            return

        target = game.countries[target_id]
        if target_id in country.trade_deals:
            country.trade_deals.discard(target_id)
            target.trade_deals.discard(user.id)
            await query.answer("Договор разорван.")
        else:
            await query.answer("Договора нет.", show_alert=True)

        await safe_edit(
            query,
            render_orders_ui(country, game, update.effective_user.id),
            reply_markup=_orders_trade_keyboard(game, user.id, country),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    elif data.startswith("ord:strike_city:"):
        code = data.split(":")[-1]
        target_id = context.user_data.get("strike_target_id")

        if not target_id:
            await query.answer("Сначала выбери страну-цель.", show_alert=True)
            return

        if len(country.planned_strikes) >= 3:
            await query.answer("Лимит: 3 удара за раунд.", show_alert=True)
            return

        country.planned_strikes.append((int(target_id), code))
        country.orders_confirmed = False
        context.user_data["orders_mode"] = "main"
        context.user_data.pop("strike_target_id", None)

        await safe_edit(
            query,
            render_orders_ui(country, game, update.effective_user.id),
            _orders_main_keyboard(),
            ParseMode.MARKDOWN,
        )
        await query.answer(f"Удар добавлен ({len(country.planned_strikes)}/3)")
        return
    
    elif data == "ord:strike_pop":
        if country.planned_strikes:
            country.planned_strikes.pop()
            country.orders_confirmed = False
            await query.answer("Последний удар удалён.")
        else:
            await query.answer("Ударов нет.")

    elif data == "ord:recover_menu":
        context.user_data["orders_mode"] = "recover"
        country.orders_confirmed = False

        await safe_edit(
            query,
            "Выбери разрушенный город для восстановления:",
            _orders_recover_keyboard(country),
            ParseMode.MARKDOWN,
        )
        await query.answer()
        return
    
    elif data.startswith("ord:recover_set:"):
        code = data.split(":")[-1]
        k = f"recover_city_{code}"
        country.orders[k] = 1
        country.orders_confirmed = False
        context.user_data["orders_mode"] = "main"

        await safe_edit(
            query,
            render_orders_ui(country, game, update.effective_user.id),
            _orders_main_keyboard(),
            ParseMode.MARKDOWN,
        )
        await query.answer(f"Восстановление {code} добавлено.")
        return

    # -------- обновление экрана после действий --------
    mode = context.user_data.get("orders_mode", "main")
    user_id = query.from_user.id  # в callback это правильнее, чем update.effective_user.id

    if mode == "city":
        await safe_edit(
            query,
            render_orders_ui(country, game, update.effective_user.id),
            reply_markup=_orders_city_keyboard("ord:city_set", country),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif mode == "shield":
        await safe_edit(
            query,
            render_orders_ui(country, game, update.effective_user.id),
            reply_markup=_orders_city_keyboard("ord:shield_set", country),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await safe_edit(
            query,
            render_orders_ui(country, game, update.effective_user.id),
            reply_markup=_orders_main_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )

    await query.answer("Ок.")

# ---------------- ВОТУМ НЕДОВЕРИЯ -----------------


@require_game
async def votum_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, game: WorldState):
    chat = update.effective_chat
    message = update.message
    initiator = update.effective_user

    # 1. Проверки фазы и активного вотума
    if game.phase != Phase.WORLD_ARENA:
        await chat.send_message(
            "Вотум недоверия можно объявлять только на Мировой арене."
        )
        return

    if game.current_votum and game.current_votum.active:
        await chat.send_message(
            "Сейчас уже идёт голосование по вотуму. Дождитесь его завершения."
        )
        return

    target_user_id: Optional[int] = None
    target_display_name: Optional[str] = None
    reason = ""

    # 2. ПРИОРИТЕТ: вотум по РЕПЛАЮ
    # /votum <причина> в ответ на сообщение нужного игрока
    if message and message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        target_user_id = u.id
        target_display_name = u.full_name
        reason = " ".join(context.args) if context.args else ""

    # 3. /votum @username [причина] — по упоминанию
    if target_user_id is None and message and message.entities:
        text = message.text or ""
        for ent in message.entities:
            if ent.type in (MessageEntityType.TEXT_MENTION, MessageEntityType.MENTION):
                if ent.type == MessageEntityType.TEXT_MENTION and ent.user:
                    # когда тыкаешь по человеку, телега сразу даёт user.id
                    u = ent.user
                    target_user_id = u.id
                    target_display_name = u.full_name
                else:
                    # обычное @username
                    mention = text[ent.offset: ent.offset + ent.length]
                    username = mention.lstrip("@").lower()
                    for c in game.countries.values():
                        if c.username and c.username.lower() == username:
                            target_user_id = c.country_id
                            target_display_name = c.name
                            break
                break

        # грубо: всё после первого аргумента считаем причиной
        if context.args:
            reason = " ".join(context.args[1:]) if target_user_id else " ".join(context.args)

    # 4. Fallback: старый формат /votum <user_id> [причина]
    if target_user_id is None and context.args:
        try:
            candidate_id = int(context.args[0])
        except ValueError:
            candidate_id = None

        if candidate_id:
            target_user_id = candidate_id
            if candidate_id in game.countries:
                target_display_name = game.countries[candidate_id].name

        reason = " ".join(context.args[1:]) if len(context.args) > 1 else reason

    # 5. Если так и не определили цель
    if target_user_id is None:
        await chat.send_message(
            "Как объявить вотум недоверия:\n"
            "• ответь на сообщение игрока: /votum <причина>\n"
            "• или укажи его через @упоминание: /votum @username <причина>\n"
            "• при желании можно по id: /votum <user_id> <причина>"
        )
        return

    # 6. Проверяем, что цель участвует в игре как страна
    if target_user_id not in game.countries:
        await chat.send_message("Этот игрок не участвует в игре как страна.")
        return

    if target_user_id == initiator.id:
        await chat.send_message("Нельзя объявить вотум недоверия самому себе.")
        return

    if not reason:
        reason = "причина не указана"

    target_country = game.countries[target_user_id]
    target_display_name = target_display_name or target_country.name

    # 7. Создаём объект вотума
    game.current_votum = VotumVote(
        target_country_id=target_user_id,
        initiated_by=initiator.id,
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("За", callback_data="votum:yes"),
            InlineKeyboardButton("Против", callback_data="votum:no"),
        ]
    ])

    await chat.send_message(
        f"🧨 {initiator.full_name} объявляет вотум недоверия стране {target_country.name}.\n"
        f"Причина: {reason}\n\n"
        "Голосуйте:",
        reply_markup=keyboard,
    )


@require_game
async def votum_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, game: WorldState):
    query = update.callback_query

    data = query.data  # "votum:yes" или "votum:no"
    parts = data.split(":")
    if len(parts) != 2 or parts[0] != "votum":
        await query.answer()
        return

    vote_yes = parts[1] == "yes"
    user = query.from_user

    # Если вотум уже не активен — НЕ редактируем сообщение (иначе убьём клавиатуру)
    if not game.current_votum or not game.current_votum.active:
        await query.answer("Голосование уже завершено.", show_alert=True)
        return

    # Голосуют только страны
    if user.id not in game.countries:
        await query.answer("Голосовать могут только представители стран.", show_alert=True)
        return

    # Записываем/перезаписываем голос
    game.current_votum.votes[user.id] = vote_yes

    yes_count = sum(1 for v in game.current_votum.votes.values() if v)
    no_count = sum(1 for v in game.current_votum.votes.values() if not v)
    total_voters = len(game.countries)
    voted = len(game.current_votum.votes)
    left = total_voters - voted

    # ВАЖНО: при редактировании текста всегда возвращаем reply_markup,
    # иначе кнопки исчезнут у всех!
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("За", callback_data="votum:yes"),
            InlineKeyboardButton("Против", callback_data="votum:no"),
        ]
    ])

    text = (
        "Голосование по вотуму идёт...\n"
        f"За: {yes_count} | Против: {no_count} | Всего стран: {total_voters}\n"
        f"Проголосовали: {voted}, осталось: {left}\n"
        "Голос можно менять повторным нажатием."
    )

    await safe_edit(query, text, reply_markup=keyboard)
    await query.answer("Голос учтён.")


@require_game
async def votum_result_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, game: WorldState):
    votum = game.current_votum
    if not votum or not votum.active:
        await update.effective_chat.send_message("Сейчас нет активного голосования по вотуму.")
        return

    total_countries = len(game.countries)
    if total_countries == 0:
        await update.effective_chat.send_message("Нет стран для голосования.")
        return

    yes_count = sum(1 for v in votum.votes.values() if v)
    percent_yes = yes_count * 100 / total_countries

    target_country = game.countries[votum.target_country_id]

    if percent_yes >= 75:
        votum.active = False
        target_country.p_tokens -= 1  # штраф цели
        await update.effective_chat.send_message(
            f"Вотум ПРОЙДЕН: {yes_count}/{total_countries} ({percent_yes:.1f}%).\n"
            f"Страна {target_country.name} получает политический штраф."
        )
    else:
        votum.active = False
        initiator = game.countries.get(votum.initiated_by)
        if initiator:
            initiator.p_tokens -= 1  # инициатор опозорился
        await update.effective_chat.send_message(
            f"Вотум НЕ ПРОЙДЕН: {yes_count}/{total_countries} ({percent_yes:.1f}%)."
        )


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я жив. Используй /startgame в этом чате, чтобы создать игру. Версия 1.1"
    )


# ---------------- main -----------------

def main():
    print(">>> Вошёл в main()")
    load_events()  # если есть events.json, подгрузится

    request = HTTPXRequest(
        connect_timeout=20,
        read_timeout=20,
        write_timeout=20,
        pool_timeout=20,
    )

    app = ApplicationBuilder().token(TOKEN).build()

    # Хендлеры команд
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("startgame", start_game))
    app.add_handler(CommandHandler("joingame", join_game))
    app.add_handler(CommandHandler("begin_round", begin_round))
    app.add_handler(CommandHandler("next_phase", next_phase))
    app.add_handler(CommandHandler("ready", ready_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("endgame", endgame_cmd))
    app.add_handler(CommandHandler("gameinfo", gameinfo_cmd)) 
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd)) 
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_menu_router))
    app.add_handler(CommandHandler("orders", orders_cmd))
    app.add_handler(CallbackQueryHandler(orders_callback, pattern="^ord:"))
    app.add_handler(CallbackQueryHandler(pickcountry_callback, pattern="^pickcountry:"))
    app.add_handler(CommandHandler("rules", rules_cmd))
    # Вотум
    app.add_handler(CommandHandler("votum", votum_cmd))
    app.add_handler(CommandHandler("votum_result", votum_result_cmd))
    app.add_handler(CallbackQueryHandler(votum_callback, pattern="^votum:"))
    
    app.add_error_handler(error_handler)
    

    print(">>> Бот запущен, слушаю Telegram...1.2")
    app.run_polling()


if __name__ == "__main__":
    main()

