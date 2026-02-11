import json
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from ..auth import require_bearer
from ..db import get_db

router = APIRouter(prefix="/admin", tags=["admin"])


class ChatReq(BaseModel):
    chat_id: int = Field(...)


class PhaseAdvanceReq(BaseModel):
    chat_id: int = Field(...)
    next_phase: str | None = Field(default=None)
    bump_seq: bool = Field(default=True)


class SnapshotReq(BaseModel):
    chat_id: int = Field(...)


def _lock_current_game(db: Session, chat_id: int):
    return db.execute(
        sql_text("""
            SELECT id, chat_id, status, current_phase, phase_seq, round_num
            FROM game_sessions
            WHERE chat_id = :chat_id
              AND status IN ('lobby','active')
            ORDER BY created_at DESC
            LIMIT 1
            FOR UPDATE
        """),
        {"chat_id": chat_id},
    ).mappings().first()


def _audit(db: Session, *, game_id, chat_id: int, action_type: str, phase_seq: int, round_num: int | None, payload_json: str):
    # ВАЖНО: audit НЕ имеет idempotency_key
    db.execute(
        sql_text("""
            INSERT INTO game_audit_log (
                game_id, chat_id, actor_tg_user_id, action_type, phase_seq, round_num, payload
            )
            VALUES (
                :game_id, :chat_id, NULL, :action_type, :phase_seq, :round_num, CAST(:payload AS jsonb)
            )
        """),
        {
            "game_id": game_id,
            "chat_id": chat_id,
            "action_type": action_type,
            "phase_seq": phase_seq,
            "round_num": round_num,
            "payload": payload_json,
        },
    )


def _outbox(db: Session, *, event_type: str, game_id, payload_json: str, idem: str):
    db.execute(
        sql_text("""
            INSERT INTO outbox_events (event_type, aggregate_type, aggregate_id, payload, idempotency_key)
            VALUES (:event_type, 'game_session', :game_id, CAST(:payload AS jsonb), :idem)
            ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
        """),
        {
            "event_type": event_type,
            "game_id": game_id,
            "payload": payload_json,
            "idem": idem,
        },
    )


@router.post("/archive")
def admin_archive(
    req: ChatReq,
    _: bool = Depends(require_bearer),
    db: Session = Depends(get_db),
):
    with db.begin():
        gs = _lock_current_game(db, req.chat_id)
        if not gs:
            raise HTTPException(status_code=404, detail="No active/lobby game for this chat_id")

        game_id = gs["id"]

        db.execute(
            sql_text("""
                UPDATE game_sessions
                SET status='archived', archived_at=now()
                WHERE id=:game_id
            """),
            {"game_id": game_id},
        )

        _audit(
            db,
            game_id=game_id,
            chat_id=req.chat_id,
            action_type="admin.archive",
            phase_seq=gs["phase_seq"],
            round_num=gs["round_num"],
            payload_json="{}",
        )

        _outbox(
            db,
            event_type="game.archived",
            game_id=game_id,
            payload_json=json.dumps({"chat_id": req.chat_id}),
            idem=f"admin.archive:{game_id}",
        )

    return {"ok": True, "chat_id": req.chat_id, "game_id": str(game_id)}


@router.post("/phase-advance")
def admin_phase_advance(
    req: PhaseAdvanceReq,
    _: bool = Depends(require_bearer),
    db: Session = Depends(get_db),
):
    with db.begin():
        gs = _lock_current_game(db, req.chat_id)
        if not gs:
            raise HTTPException(status_code=404, detail="No active/lobby game for this chat_id")

        game_id = gs["id"]
        old_seq = gs["phase_seq"]
        old_phase = gs["current_phase"]

        new_seq = old_seq + 1 if req.bump_seq else old_seq
        new_phase = req.next_phase if req.next_phase is not None else old_phase

        db.execute(
            sql_text("""
                UPDATE game_sessions
                SET current_phase = :new_phase,
                    phase_seq = :new_seq,
                    phase_started_at = now()
                WHERE id = :game_id
            """),
            {"new_phase": new_phase, "new_seq": new_seq, "game_id": game_id},
        )

        payload = {
            "chat_id": req.chat_id,
            "old_phase": old_phase,
            "new_phase": new_phase,
            "old_seq": old_seq,
            "new_seq": new_seq,
        }

        _audit(
            db,
            game_id=game_id,
            chat_id=req.chat_id,
            action_type="admin.phase_advance",
            phase_seq=new_seq,
            round_num=gs["round_num"],
            payload_json=json.dumps(payload),
        )

        _outbox(
            db,
            event_type="phase.changed",
            game_id=game_id,
            payload_json=json.dumps(payload),
            idem=f"admin.phase.changed:{game_id}:{new_seq}",
        )

    return {"ok": True, "chat_id": req.chat_id, "game_id": str(game_id), "phase_seq": new_seq, "phase": new_phase}


def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, UUID):
        return str(o)
    return str(o)


@router.post("/snapshot")
def admin_snapshot(
    req: SnapshotReq,
    _: bool = Depends(require_bearer),
    db: Session = Depends(get_db),
):
    with db.begin():
        gs = _lock_current_game(db, req.chat_id)
        if not gs:
            raise HTTPException(status_code=404, detail="No active/lobby game for this chat_id")

        game_id = gs["id"]

        # Берём read model как “оперативный снимок” (минимум)
        rm = db.execute(
            sql_text("""
                SELECT *
                FROM v_current_game_by_chat
                WHERE chat_id=:chat_id
                LIMIT 1
            """),
            {"chat_id": req.chat_id},
        ).mappings().first()

        snapshot_json = "{}" if not rm else json.dumps(dict(rm), default=_json_default)

        db.execute(
            sql_text("""
                INSERT INTO game_state_snapshots (game_id, chat_id, phase_seq, round_num, snapshot)
                VALUES (:game_id, :chat_id, :phase_seq, :round_num, CAST(:snapshot AS jsonb))
            """),
            {
                "game_id": game_id,
                "chat_id": req.chat_id,
                "phase_seq": gs["phase_seq"],
                "round_num": gs["round_num"],
                "snapshot": snapshot_json,
            },
        )

        _audit(
            db,
            game_id=game_id,
            chat_id=req.chat_id,
            action_type="admin.snapshot",
            phase_seq=gs["phase_seq"],
            round_num=gs["round_num"],
            payload_json="{}",
        )

        _outbox(
            db,
            event_type="snapshot.created",
            game_id=game_id,
            payload_json=json.dumps({"chat_id": req.chat_id, "phase_seq": gs["phase_seq"], "round_num": gs["round_num"]}),
            idem=f"admin.snapshot:{game_id}:{gs['phase_seq']}:{gs['round_num']}",
        )

    return {"ok": True, "chat_id": req.chat_id, "game_id": str(game_id)}