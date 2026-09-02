from datetime import date, datetime, timedelta

from app.config import Settings
from app.main import DASHBOARD_HISTORY_DAYS, dashboard


def test_dashboard_returns_three_month_complete_week_history(db):
    settings = Settings(_env_file=None)
    result = dashboard(db, settings)
    history = result["daily_discoveries"]
    today = datetime.now(settings.timezone).date()

    current_week_start = today - timedelta(days=today.weekday())

    assert len(history) == DASHBOARD_HISTORY_DAYS == 98
    assert date.fromisoformat(history[0]["date"]) == current_week_start - timedelta(weeks=13)
    assert date.fromisoformat(history[-1]["date"]) == current_week_start + timedelta(days=6)
    assert date.fromisoformat(history[0]["date"]).weekday() == 0
    assert date.fromisoformat(history[-1]["date"]).weekday() == 6
    assert all(item["count"] == 0 for item in history)
