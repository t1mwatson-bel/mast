import re
from datetime import datetime

from config import MOSCOW_TZ, SUIT_ALIASES, SUIT_CYCLE, CYCLE_BASE_GAME

# =====================================================================
# МАСТИ
# =====================================================================

def normalize_suit(suit):
    if not suit:
        return ""
    return SUIT_ALIASES.get(suit, suit)

# =====================================================================
# ПАРСИНГ КАРТ
# =====================================================================

CARD_RE = re.compile(r"(10|[2-9JQKA])([♠♣♦♥]️?)")

def parse_cards(text):
    """'(9♥️A♠️5♦️)' → [('9','♥️'), ('A','♠️'), ('5','♦️')]"""
    if not text:
        return []
    return [
        (m.group(1).upper(), normalize_suit(m.group(2)))
        for m in CARD_RE.finditer(text)
    ]

# =====================================================================
# ПАРСИНГ ИГРЫ
# =====================================================================

GAME_ID_RE = re.compile(r"#N(\d+)")
BLOCK_RE = re.compile(r"\(([^)]+)\)")

def parse_game_message(text):
    if not text:
        return None

    m = GAME_ID_RE.search(text)
    if not m:
        return None
    game_id = int(m.group(1))

    blocks = BLOCK_RE.findall(text)
    if len(blocks) < 2:
        return None

    return {
        "game_id": game_id,
        "player_cards": parse_cards(blocks[0]),   # левая скобка = игрок
        "dealer_cards": parse_cards(blocks[1]),   # правая = дилер
        "raw": text,
        "received_at": datetime.now(MOSCOW_TZ).isoformat(),
    }

# =====================================================================
# ЦИКЛ МАСТЕЙ
# =====================================================================

def get_suit_for_game(game_id):
    """Возвращает масть для нечётного game_id по циклу.
    Чётные возвращают None."""
    if game_id % 2 == 0:
        return None

    steps = (game_id - CYCLE_BASE_GAME) // 2
    suit = SUIT_CYCLE[steps % len(SUIT_CYCLE)]
    return suit

# =====================================================================
# ПОИСК МАСТИ У ИГРОКА
# =====================================================================

def find_suit_in_player(game, suit):
    target = normalize_suit(suit)
    for rank, s in game.get("player_cards") or []:
        if normalize_suit(s) == target:
            return f"{rank}{s}"
    return None

# =====================================================================
# ПРОВЕРКА ДОГОНОВ (д0, д1, д2)
# =====================================================================

def check_dogon(game, target_game, target_suit, max_dogon=2):
    """
    Проверяет, попала ли масть в догон.
    Возвращает dict с результатом или None, если игра вне диапазона.
    """
    diff = game["game_id"] - target_game
    if diff < 0 or diff > max_dogon:
        return None

    card = find_suit_in_player(game, target_suit)
    if card:
        return {
            "hit": True,
            "dogon": diff,
            "card": card,
            "game_id": game["game_id"],
            "target_game": target_game,
            "target_suit": target_suit,
        }
    return None

# =====================================================================
# ФОРМИРОВАНИЕ СООБЩЕНИЙ
# =====================================================================

def format_prediction(target_game, target_suit):
    return f"🎯 Прогноз на игру #N{target_game}: масть {target_suit}"

def format_hit(result):
    return (
        f"✅ Прогноз #N{result['target_game']} ({result['target_suit']}) сработал!\n"
        f"Масть зашла на д{result['dogon']} (игра #N{result['game_id']})\n"
        f"Карта игрока: {result['card']}"
    )

def format_miss(target_game, target_suit):
    return (
        f"❌ Прогноз #N{target_game} ({target_suit}) не сработал.\n"
        f"Масть не зашла за 3 игры (д0, д1, д2)."
    )

# =====================================================================
# ХЕЛПЕРЫ
# =====================================================================

def log_game(game):
    p = game.get("player_cards", [])
    d = game.get("dealer_cards", [])
    ps = " ".join(f"{r}{s}" for r, s in p)
    ds = " ".join(f"{r}{s}" for r, s in d)
    print(f"🎲 #N{game['game_id']}  игрок: ({ps})  дилер: ({ds})", flush=True)
