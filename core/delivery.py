"""Доставка: кому, что и когда отправлять.

Отдельный модуль, потому что «нашли подходящее» и «показали человеку» — разные
вопросы, и второй в v1 не был решён вовсе: всё улетало в чат мгновенно, 6.7
сообщения в день в среднем и 41 в пиковый день, а отклик на них был 3%.

Здесь у каждого профиля свой режим:

* **realtime** — сразу, но с ограничениями: тихие часы и дневной потолок.
  Для тех, кто хочет написать хозяину первым.
* **digest** — копится и уходит одним сообщением в выбранный час.

Обе доставки шлют только то, что появилось после настройки профиля. Найденное
до неё человек уже видел числом в конце визарда и листает в /feed; иначе первое
же утро уходит на разбор двухсот накопленных объявлений, а живой поток стоит в
очереди за ними.

Тихие часы не отменяют находку, а откладывают её: то, что пришло ночью, уйдёт
утром, а не пропадёт. Уйдёт при этом одним списком — иначе тихие часы просто
переносили бы флуд на восемь утра.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from bot import cards
from core import market as market_mod
from core.store import Store

log = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Jerusalem")
REALTIME_BATCH = 5          # сколько карточек за раз, чтобы не залить чат
BURST_TO_LIST = 6           # с этого числа шлём список, а не карточки
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
    """Отправить новые совпадения профилю. Возвращает, сколько ушло."""
    if profile.get("is_paused"):
        return 0
    if in_quiet_hours(profile):
        return 0

    cap = profile.get("max_per_day") or 20
    already = await store.sent_today(profile["id"])
    room = max(0, cap - already)
    since = profile.get("backlog_before")

    if room == 0:
        log.info("профиль %s: дневной лимит %s исчерпан", profile["id"], cap)
        # Молчание после исчерпанного лимита неотличимо от поломки: в первый же
        # день лимит выбрался за минуту, и дальше одиннадцать часов тишины
        # выглядели как сломанная доставка. Одна строка в сутки, и только если
        # что-то действительно осталось за бортом.
        waiting = len(await store.queue_for(profile["id"], limit=cap + 1, since=since))
        today = _now().date().isoformat()
        if waiting and profile.get("cap_notice_on") != today:
            await bot.send_message(
                profile["user_id"],
                f"Дневной лимит в {cap} сообщений исчерпан — "
                f"ещё {waiting} подходящих ждут в /feed. "
                f"Лимит меняется в /settings.")
            await store.update_profile(profile["id"], cap_notice_on=today)
        return 0

    queue = await store.queue_for(profile["id"], limit=min(room, BURST_LIMIT), since=since)
    if not queue:
        return 0

    # Пачка карточек — это пачка уведомлений. Так выглядит утро после тихих
    # часов: за ночь накопилось десять объявлений, и мгновенная доставка
    # вываливает их подряд, повторяя ровно ту ошибку, из-за которой в первое
    # утро дайджест ушёл двадцать четыре раза. Поэтому от шести штук —
    # компактный список, а карточка остаётся тем, чем задумана: одной находкой.
    if len(queue) >= BURST_TO_LIST:
        own = await market_mod.medians(store)
        # После тихих часов это накопленное за ночь, в середине дня — просто
        # пачка сразу. Заголовок называет вещи как есть: «пока тебя не
        # беспокоили» в три часа дня выглядело бы враньём.
        after_quiet = profile.get("quiet_to") is not None and _now().hour == profile["quiet_to"]
        total = len(await store.queue_for(profile["id"], limit=1000, since=since))
        await bot.send_message(
            profile["user_id"],
            cards.digest(queue, total=total, own_medians=own,
                         title="Пока тебя не беспокоили" if after_quiet else "Сразу несколько"),
            parse_mode="HTML", disable_web_page_preview=True)
        await store.mark_sent(profile["id"], [f["listing_id"] for f in queue])
        return len(queue)

    sent = []
    for facts in queue:
        history = await store.price_history(facts["listing_id"])
        prefix = ""
        if len(history) > 1 and history[0]["price"] != history[-1]["price"]:
            prefix = "🔁 <b>Снова в продаже</b>"
        try:
            await _send_card(bot, profile["user_id"], facts, rank=facts.get("rank"),
                             reasons=facts.get("reasons"), history=history, prefix=prefix,
                             own_medians=await market_mod.medians(store))
            sent.append(facts["listing_id"])
        except Exception as e:                       # noqa: BLE001
            log.warning("не отправилось %s: %s", facts["listing_id"], e)
    if sent:
        await store.mark_sent(profile["id"], sent)
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
    """Один проход доставки по всем активным профилям."""
    stats = {"realtime": 0, "digest": 0, "profiles": 0}
    for profile in await store.active_profiles():
        stats["profiles"] += 1
        if profile.get("delivery_mode") == "realtime":
            stats["realtime"] += await deliver_realtime(bot, store, profile)
        else:
            stats["digest"] += await deliver_digest(bot, store, profile)
    return stats
