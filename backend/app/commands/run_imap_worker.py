import asyncio

from app.config import get_settings
from app.database import create_database
from app.services.imap_bounces import run_imap_worker


def main() -> None:
    create_database()
    asyncio.run(run_imap_worker(get_settings()))


if __name__ == "__main__":
    main()
