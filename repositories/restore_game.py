print("SCRIPT LOADED")
import argparse
import json
from db import SessionLocal
from repositories.snapshot_repo import get_latest_snapshot_by_chat

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-id", type=int, required=True)
    args = ap.parse_args()

    with SessionLocal() as db:
        row = get_latest_snapshot_by_chat(db, args.chat_id)

    if not row:
        print("NO SNAPSHOT for chat_id =", args.chat_id)
        return

    snap = row["snapshot"]
    # snapshot может прийти как dict или как str — зависит от драйвера/настроек
    if isinstance(snap, str):
        try:
            snap = json.loads(snap)
        except Exception:
            pass

    print("LATEST SNAPSHOT")
    print(" game_id   =", row["game_id"])
    print(" chat_id   =", row["chat_id"])
    print(" phase_seq =", row["phase_seq"])
    print(" round_num =", row["round_num"])
    print(" created_at=", row["created_at"])
    print(" snapshot  =", json.dumps(snap, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()