from dataclasses import dataclass
from sqlalchemy.orm import Session

from repositories.game_repo import (
    get_active_game_by_chat,
    get_player_in_game,
    lock_game_row,
    set_phase,
    count_active_players,
)
from repositories.ready_repo import mark_ready, count_ready


PHASES = ["EVENT", "WORLD_ARENA", "NEGOTIATIONS", "ORDERS", "RESOLVE"]  # пример
def phase_name(seq: int) -> str:
    return PHASES[seq % len(PHASES)]


def next_phase_name(phase_seq: int) -> str:
    # простая цикличность; можешь потом сделать по правилам
    return PHASES[(phase_seq + 1) % len(PHASES)]


@dataclass
class ReadyResult:
    ok: bool
    message: str


def handle_ready(session: Session, chat_id: int, tg_user_id: int) -> ReadyResult:
    game = get_active_game_by_chat(session, chat_id)
    if not game:
        return ReadyResult(False, "Нет активной игры в этом чате.")

    player = get_player_in_game(session, game["id"], tg_user_id)
    if not player:
        return ReadyResult(False, "Ты не в игре. Сначала вступи и выбери страну.")
    if not player["is_active"] or player["is_afk"]:
        return ReadyResult(False, "Ты неактивен/AFK и не можешь подтвердить готовность.")

    # ставим ready на текущую фазу (phase_seq из game_sessions)
    mark_ready(session, game["id"], player["id"], game["phase_seq"])

    active_cnt = count_active_players(session, game["id"])
    ready_cnt = count_ready(session, game["id"], game["phase_seq"])

    return ReadyResult(True, f"Готовность принята. Ready: {ready_cnt}/{active_cnt}.")


@dataclass
class NextPhaseResult:
    ok: bool
    message: str
    new_phase: str | None = None


def try_next_phase(session: Session, chat_id: int) -> NextPhaseResult:
    game = get_active_game_by_chat(session, chat_id)
    if not game:
        return NextPhaseResult(False, "Нет активной игры.")

    # ТРАНЗАКЦИЯ: ты снаружи должен сделать session.begin()
    locked = lock_game_row(session, game["id"])
    if not locked:
        return NextPhaseResult(False, "Игра не найдена (race condition).")

    phase_seq = locked["phase_seq"]

    active_cnt = count_active_players(session, game["id"])
    ready_cnt = count_ready(session, game["id"], phase_seq)

    if active_cnt == 0:
        return NextPhaseResult(False, "Нет активных игроков.")
    if ready_cnt < active_cnt:
        return NextPhaseResult(False, f"Пока не все готовы: {ready_cnt}/{active_cnt}.")

    new_seq = phase_seq + 1
    new_phase = next_phase_name(phase_seq)

    set_phase(session, game["id"], new_seq, new_phase)
    # триггер сам очистит game_phase_ready

    return NextPhaseResult(True, f"Фаза изменена: {new_phase}.", new_phase=new_phase)