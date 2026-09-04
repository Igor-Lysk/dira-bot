"""Доставка: кому, что и когда отправлять.

Отдельный модуль, потому что «нашли подходящее» и «показали человеку» — разные
вопросы, и второй в v1 не был решён вовсе: всё улетало в чат мгновенно, 6.7
сообщения в день в среднем и 41 в пиковый день, а отклик на них был 3%.

Здесь у каждого профиля свой режим:

* **realtime** — карточка на каждое объявление, как оно появилось. Для тех,
  кто хочет написать хозяину первым. Ограничений два: тихие часы и дневной
  предел.
* **digest** — копится и уходит одним сообщением в выбранный час.

Обе доставки шлют только то, что появилось после настройки профиля. Найденное
до неё человек уже видел числом в конце визарда и листает в /feed; иначе первое
же утро уходит на разбор двухсот накопленных объявлений, а живой поток стоит в
очереди за ними.

Тихие часы не отменяют находку, а откладывают её: то, что пришло ночью, уйдёт
утром, а не пропадёт. И уйдёт одним списком — иначе тихие часы просто
переносили бы флуд на восемь утра. Это единственный случай, когда мгновенная
доставка меняет формат: днём всегда карточки.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from bot import cards
from core import market as market_mod
from core.store import Store

log = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Jerusalem")
REALTIME_BATCH = 5          # сколько карточек за раз, чтобы не залить чат
MORNING_TO_LIST = 3         # с этого числа накопленное за ночь уходит списком
FLOOD_TO_LIST = 10          # столько сразу среди дня — уже не поток, а сбой
BURST_LIMIT = 20            # строк в таком списке
DIGEST_LIMIT = 15           # строк в одном дайджесте; остальное — по /feed


def _now() -> datetime:
    return datetime.now(TZ)


def in_quiet_hours(profile: dict, now: datetime = None) -> bool:
    """Тихие часы. Интервал может пересекать полночь: с 23 до 8 — это ночь."""
    start, end = profile.get("quiet_from"), profile.get("quiet_to")
    if start is None or end is None:
        return False
    hour = (now or _now()).hour
    return start <= hour < end if start < end else (hour >= start or hour < end)


async def _send_card(bot, chat_id: int, facts: dict, rank=None, reasons=None,
                     history=None, prefix: str = "", own_medians=None):
    from bot.app import _card_kb
    text = cards.card(facts, rank=rank, reasons=reasons, price_history=history,
                      own_medians=own_medians)
    if prefix:
        text = prefix + "\n" + text
    await bot.send_message(chat_id, text, parse_mode="HTML",
                           disable_web_page_preview=True,
                           reply_markup=_card_kb(facts["listing_id"]))


async def deliver_realtime(bot, store: Store, profile: dict) -> int:
    """Отправить новые совпадения профилю. Возвращает, сколько ушло.

    Формат по умолчанию — карточка на объявление. Живой поток это позволяет:
    телеграм отдаёт объявление в момент публикации, доски опрашиваются раз в
    час и приносят единицы. Списком уходит только накопленное за тихие часы.
    """
    if profile.get("is_paused"):
        return 0

    if in_quiet_hours(profile):
        # Отметка, что доставку придержали. По ней утренняя отправка узнает,
        # что перед ней ночное накопленное, а не обычный поток. Считать по
        # часам было бы приблизительно: «первый час после тихих» и «первая
        # доставка после тихих» — разные вещи, доставка просыпается каждые
        # пять минут.
        if not profile.get("quiet_held_at"):
            await store.update_profile(profile["id"], quiet_held_at=_now().isoformat())
        return 0

    cap = profile.get("max_per_day") or 50
    already = await store.sent_today(profile["id"])
    room = max(0, cap - already)
    since = profile.get("backlog_before")

    if room == 0:
        log.info("профиль %s: дневной предел %s исчерпан", profile["id"], cap)
        # Предел — предохранитель, а не норма: при живом потоке в два десятка
        # объявлений в сутки до него не доходит. Значит, если он сработал, дело
        # почти наверняка в критериях или в догоняющем сборе, и человеку надо
        # сказать об этом прямо. Молчание неотличимо от поломки: в первый же
        # день предел выбрался за минуту, и одиннадцать часов тишины выглядели
        # как сломанная доставка.
        waiting = len(await store.queue_for(profile["id"], limit=cap + 1, since=since))
        today = _now().date().isoformat()
        if waiting and profile.get("cap_notice_on") != today:
            await bot.send_message(
                profile["user_id"],
                f"За сутки набралось больше {cap} подходящих объявлений — "
                f"это выше обычного потока, и похоже, что критерии слишком широки. "
                f"Ещё {waiting} ждут в /feed. Критерии и предел — в /settings.")
            await store.update_profile(profile["id"], cap_notice_on=today)
        return 0

    queue = await store.queue_for(profile["id"], limit=min(room, BURST_LIMIT), since=since)
    if not queue:
        return 0

    held = profile.get("quiet_held_at")
    morning = bool(held) and len(queue) >= MORNING_TO_LIST
    # Десяток разом среди дня живой поток не даёт. Столько сразу означает не
    # поток, а сбой — и десять уведомлений подряд его только усугубят.
    flood = len(queue) >= FLOOD_TO_LIST

    if morning or flood:
        own = await market_mod.medians(store)
        total = len(await store.queue_for(profile["id"], limit=1000, since=since))
        await bot.send_message(
            profile["user_id"],
            cards.digest(queue, total=total, own_medians=own,
                         title="Пока тебя не беспокоили" if morning else "Сразу несколько"),
            parse_mode="HTML", disable_web_page_preview=True)
        await store.mark_sent(profile["id"], [f["listing_id"] for f in queue])
        if held:
            await store.update_profile(profile["id"], quiet_held_at=None)
        return len(queue)

    sent = []
    for facts in queue[:REALTIME_BATCH]:
        history = await store.price_history(facts["listing_id"])
        prefix = ""
        if len(history) > 1 and history[0]["price"] != history[-1]["price"]:
            prefix = "🔁 <b>Снова в продаже</b>"
        try:
            await _send_card(bot, profile["user_id"], facts, rank=facts.get("rank"),
                             reasons=facts.get("reasons"), history=history, prefix=prefix,
                             own_medians=await market_mod.medians(store))
            sent.append(facts["listing_id"])
        except TelegramForbiddenError:
            # Человек заблокировал бота. Ловить это здесь нельзя: карточка
            # осталась бы неотправленной и вернулась на следующем круге, и так
            # вечно. Пусть решает deliver_all — он отключит профиль.
            raise
        except Exception as e:                       # noqa: BLE001
            log.warning("не отправилось %s: %s", facts["listing_id"], e)
    if sent:
        await store.mark_sent(profile["id"], sent)
    if held:
        await store.update_profile(profile["id"], quiet_held_at=None)
    return len(sent)


async def deliver_digest(bot, store: Store, profile: dict, force: bool = False) -> int:
    """Собрать накопленное в одно сообщение и отправить.

    Раз в сутки, а не «в течение часа»: проверка `час == digest_hour` истинна
    все шестьдесят минут, а задача доставки просыпается каждые пять. В первое же
    утро это дало двадцать четыре сообщения между 9:00 и 9:25 вместо одного —
    шесть срабатываний по дайджесту и три карточки в придачу к каждому.
    Поэтому дата последней отправки лежит в профиле, и повторно за день дайджест
    не уходит.
    """
    if profile.get("is_paused"):
        return 0
    now = _now()
    today = now.date().isoformat()
    if not force:
        if now.hour != (profile.get("digest_hour") or 9):
            return 0
        if profile.get("digest_sent_on") == today:
            return 0

    since = profile.get("backlog_before")
    queue = await store.queue_for(profile["id"], limit=DIGEST_LIMIT, since=since)
    total = len(await store.queue_for(profile["id"], limit=1000, since=since))
    if not queue:
        # отметку ставим всё равно: пустой день — тоже отработанный день,
        # иначе следующая пятиминутка попробует снова
        await store.update_profile(profile["id"], digest_sent_on=today)
        return 0

    own = await market_mod.medians(store)
    await bot.send_message(profile["user_id"],
                           cards.digest(queue, total=total, own_medians=own),
                           parse_mode="HTML", disable_web_page_preview=True)
    await store.mark_sent(profile["id"], [f["listing_id"] for f in queue])
    await store.update_profile(profile["id"], digest_sent_on=today)
    return len(queue)


async def deliver_all(bot, store: Store) -> dict:
    """Один проход доставки по всем активным профилям.

    Каждый профиль обрабатывается отдельно от остальных. Пока пользователь был
    один, это ничего не значило; со вторым — значит всё: заблокировавший бота
    человек ронял бы задачу доставки целиком, и остальные переставали получать
    что-либо. Причём молча, потому что падает фоновая задача, а не запрос.
    """
    stats = {"realtime": 0, "digest": 0, "profiles": 0, "failed": 0}
    for profile in await store.active_profiles():
        stats["profiles"] += 1
        try:
            if profile.get("delivery_mode") == "realtime":
                stats["realtime"] += await deliver_realtime(bot, store, profile)
            else:
                stats["digest"] += await deliver_digest(bot, store, profile)
        except TelegramForbiddenError:
            # Бот заблокирован или чат удалён — писать туда больше некуда.
            # Отмечаем пользователя неактивным, иначе каждые пять минут будет
            # одна и та же ошибка.
            log.warning("профиль %s: бот заблокирован, отключаю пользователя %s",
                        profile["id"], profile["user_id"])
            await store.set_user(profile["user_id"], is_active=0)
            stats["failed"] += 1
        except TelegramRetryAfter as e:
            log.warning("профиль %s: Telegram просит подождать %s с", profile["id"], e.retry_after)
            stats["failed"] += 1
        except Exception as e:                        # noqa: BLE001
            log.exception("профиль %s: доставка не удалась: %s", profile["id"], e)
            stats["failed"] += 1
    return stats
