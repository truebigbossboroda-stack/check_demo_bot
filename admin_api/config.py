import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    database_url: str
    admin_bearer_token: str

def get_settings() -> Settings:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL is missing")

    token = os.getenv("ADMIN_API_TOKEN")
    if not token:
        raise RuntimeError("ADMIN_API_TOKEN is missing")

    return Settings(database_url=db_url, admin_bearer_token=token)