from datetime import date, datetime, timedelta

from app.config import Settings
from app.main import DASHBOARD_HISTORY_DAYS, dashboard


def test_dashboard_returns_six_month_daily_history(db):
    settings = Settings(_env_file=None)
    result = dashboard(db, settings)
    history = result["daily_discoveries"]
    today = datetime.now(settings.timezone).date()

    assert len(history) == DASHBOARD_HISTORY_DAYS == 183
    assert date.fromisoformat(history[-1]["date"]) == today
    assert date.fromisoformat(history[0]["date"]) == today - timedelta(days=182)
    assert all(item["count"] == 0 for item in history)
