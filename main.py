# -*- coding: utf-8 -*-

import os
import sys
import re
import json
import time
from datetime import datetime, timedelta, time as dtime

from config import (
    BOT_TOKEN, CHANNEL_PROGNOZ, CHANNEL_STATS,
    PREDICTIONS_FILE, POLL_INTERVAL, FINALIZE_WAIT_SECONDS,
    DOGON_GAMES, PREDICTION_TIMEOUT_MINUTES,
    CLEANUP_HOUR, CLEANUP_MINUTE,
    MAX_GAMES_CACHE, MAX_PREDICTIONS_STORED, MOSCOW_TZ,
    SUIT_CYCLE, CYCLE_BASE_GAME,
)

from parsers import (
    parse_game_message, log_game, add_game_offset,
    get_suit_for_game, find_suit_in_player, normalize_suit,
)

from telegram_api import (
    delete_webhook, telegram_send, telegram_edit,
    process_telegram_updates, load_offset,
)

from bank import (
    load_bank, get_current_bet, bet_for_dogon,
    apply_win, apply_lose, apply_return,
)


if not BOT_TOKEN or not CHANNEL_PROGNOZ or not CHANNEL_STATS:
    print("❌ Ошибка переменных окружения", flush=True)
    sys.exit(1)


games_cache = {}
pending_games = {}
predictions = []
last_cleanup_date = None


def should_cleanup_now(now=None):
    global last_cleanup_date
    if now is None:
        now = datetime.now(MOSCOW_TZ)
    if last_cleanup_date == now.date():
        return False
    return now.time() >= dtime(CLEANUP_HOUR, CLEANUP_MINUTE)


def cleanup_nightly():
    global games_cache, pending_games, last_cleanup_date
    print("\n🧹 НОЧНАЯ ОЧИСТКА", flush=True)
    games_cache.clear()
    pending_games.clear()
    last_cleanup_date = datetime.now(MOSCOW_TZ).date()
    print("✅ Готово\n", flush=True)


def load_predictions():
    global predictions
    try:
        if not os.path.exists(PREDICTIONS_FILE):
            predictions = []
            return
        with open(PREDICTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        predictions = data if isinstance(data, list) else []
    except Exception as e:
        print(f"⚠️ Ошибка чтения прогнозов: {e}", flush=True)
        predictions = []


def save_predictions():
    try:
        tmp = PREDICTIONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(predictions, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PREDICTIONS_FILE)
    except Exception as e:
        print(f"🔥 Ошибка сохранения прогнозов: {e}", flush=True)


def make_prediction_message(p):
    return f"🎯 Игра: <b>#N{p['target_number']}</b>: {normalize_suit(p['predicted_suit'])}"


def make_result_message(p, result):
    suit = normalize_suit(p["predicted_suit"])
    dogon = p.get("dogon", 0)
    if result == "win":
        mark = f" ✅ (Догон {dogon})"
    elif result == "lose":
        mark = " ❌"
    elif result == "return":
        mark = " ♻️"
    else:
        mark = ""
    return f"🎯 Игра: <b>#N{p['target_number']}</b>: {suit}{mark}"


def create_prediction(game):
    current_id = game["game_id"]

    if current_id % 2 == 0:
        return None

    target = current_id + 2
    suit = get_suit_for_game(target)
    if not suit:
        return None

    # Проверка: если прогноз на эту игру уже есть (чтобы не дублировать при перезапуске)
    for p in predictions:
        if p.get("target_number") == target and p.get("status") == "pending":
            return None

    base_bet = get_current_bet()

    prediction = {
        "algorithm": "game_id_cycle",
        "trigger_number": current_id,
        "trigger_game_id": game.get("game_id"),
        "target_number": target,
        "predicted_suit": suit,
        "dogon": 0,
        "base_bet": base_bet,
        "bet": base_bet,
        "status": "pending",
        "created_at": datetime.now(MOSCOW_TZ).isoformat(),
        "sent_at": None,
        "closed_at": None,
        "message_id": None,
        "result_game": None,
        "found_card": None,
        "close_reason": None,
    }

    predictions.append(prediction)
    save_predictions()

    print(f"\n🔮 ПРОГНОЗ: #N{target} {suit} (триггер #N{current_id})", flush=True)
    send_prediction(prediction)
    return prediction


def send_prediction(p):
    mid = telegram_send(make_prediction_message(p))
    if not mid:
        print(f"❌ Не отправлен #N{p['target_number']}", flush=True)
        return False
    p["message_id"] = mid
    p["sent_at"] = datetime.now(MOSCOW_TZ).isoformat()
    save_predictions()
    print(f"📤 ОТПРАВЛЕН #N{p['target_number']} {p['predicted_suit']}", flush=True)
    return True


def check_predictions():
    changed = False
    now = datetime.now(MOSCOW_TZ)

    for p in predictions:
        if p.get("status") != "pending":
            continue

        target = p.get("target_number")
        if not target:
            continue

        # Таймаут
        created_at_str = p.get("created_at")
        if created_at_str:
            try:
                created_at = datetime.fromisoformat(created_at_str)
                if now - created_at > timedelta(minutes=PREDICTION_TIMEOUT_MINUTES):
                    p["status"] = "return"
                    p["close_reason"] = "timeout"
                    p["closed_at"] = now.isoformat()
                    if p.get("message_id"):
                        telegram_edit(p.get("message_id"), make_result_message(p, "return"))
                    apply_return(p, p.get("bet", 0))
                    print(f"♻️ RETURN #N{target} (timeout)", flush=True)
                    changed = True
                    continue
            except Exception:
                pass

        waiting = False
        won = False
        checked = []
        base_bet = p.get("base_bet", p.get("bet", 0))

        # Проверка Д0, Д1, Д2
        for dogon in range(0, DOGON_GAMES):
            gnum = add_game_offset(target, dogon)
            
            # ЕСЛИ ИГРА ВСЕ ЕЩЕ ЖДЕТ ОБНОВЛЕНИЯ (PENDING) - ЖДЕМ
            if gnum in pending_games:
                waiting = True
                break

            g = games_cache.get(gnum)

            if not g:
                waiting = True
                break

            checked.append(gnum)

            card = find_suit_in_player(g, p["predicted_suit"])

            if card:
                bet = bet_for_dogon(base_bet, dogon)
                p["status"] = "win"
                p["result_game"] = gnum
                p["found_card"] = card
                p["dogon"] = dogon
                p["bet"] = bet
                p["closed_at"] = now.isoformat()

                if p.get("message_id"):
                    telegram_edit(p.get("message_id"), make_result_message(p, "win"))

                apply_win(p, dogon, bet)
                print(f"✅ WIN #N{target} на Д{dogon} ({card})", flush=True)
                changed = True
                won = True
                break

        if won or waiting:
            continue

        # Проигрыш (все догоны исчерпаны)
        total = sum(bet_for_dogon(base_bet, d) for d in range(0, DOGON_GAMES))
        last_bet = bet_for_dogon(base_bet, DOGON_GAMES - 1)

        p["status"] = "lose"
        p["result_game"] = checked[-1] if checked else add_game_offset(target, DOGON_GAMES - 1)
        p["dogon"] = DOGON_GAMES - 1
        p["bet"] = last_bet
        p["total_lost"] = total
        p["closed_at"] = now.isoformat()

        if p.get("message_id"):
            telegram_edit(p.get("message_id"), make_result_message(p, "lose"))

        apply_lose(p, base_bet)
        print(f"❌ LOSE #N{target} (потеря серии {total} ₽)", flush=True)
        changed = True

    if changed:
        save_predictions()


def on_game_message(game_number, text, is_edited):
    if game_number in pending_games:
        pending_games[game_number]["text"] = text
        return

    if game_number in games_cache:
        g = parse_game_message(text)
        if g:
            games_cache[game_number] = g
            print(f"🔄 Обновлена #N{game_number}", flush=True)
            # ПЕРЕПРОВЕРЯЕМ ПРОГНОЗЫ ПОСЛЕ ОБНОВЛЕНИЯ
            check_predictions()
        return

    if re.search(r"[✅🔰▶️◀️]", text):
        pending_games[game_number] = {
            "first_seen": time.time(),
            "text": text,
        }
        print(f"👀 #N{game_number} → pending", flush=True)


def finalize_pending_games():
    now = time.time()
    ready = [
        n for n, i in pending_games.items()
        if now - i.get("first_seen", now) >= FINALIZE_WAIT_SECONDS
    ]
    for n in ready:
        info = pending_games.pop(n, None)
        if not info:
            continue
        g = parse_game_message(info.get("text", ""))
        if not g:
            print(f"⚠️ #N{n} не распарсился", flush=True)
            continue
        games_cache[n] = g
        log_game(g)
        create_prediction(g)
        # ПЕРЕПРОВЕРЯЕМ ПРОГНОЗЫ ПОСЛЕ ФИНАЛИЗАЦИИ ИГРЫ
        check_predictions()


def cleanup_games_cache():
    if len(games_cache) <= MAX_GAMES_CACHE:
        return
    items = sorted(games_cache.items(), key=lambda kv: kv[0])
    for n, _ in items[:-MAX_GAMES_CACHE]:
        del games_cache[n]


def cleanup_predictions():
    global predictions
    if len(predictions) > MAX_PREDICTIONS_STORED:
        predictions = predictions[-MAX_PREDICTIONS_STORED:]
        save_predictions()


def main():
    print("\n==================================================", flush=True)
    print("🚀 CYBER 21 — CYCLE ALGORITHM (ALL ODD GAMES)", flush=True)
    print("==================================================", flush=True)
    delete_webhook()
    load_bank()
    load_predictions()
    offset = load_offset()

    print(f"📌 offset: {offset} | прогнозов: {len(predictions)}", flush=True)
    print("🟢 БОТ ГОТОВ\n", flush=True)

    while True:
        try:
            if should_cleanup_now():
                cleanup_nightly()

            offset = process_telegram_updates(offset, on_game_message)
            finalize_pending_games()
            check_predictions()
            cleanup_games_cache()
            cleanup_predictions()
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\n🛑 Остановлен", flush=True)
            break
        except Exception as e:
            print(f"❌ Ошибка: {e}", flush=True)
            time.sleep(3)


if __name__ == "__main__":
    import threading
    threading.Thread(target=main, daemon=True).start()
    from web_server import start_web_server
    start_web_server()