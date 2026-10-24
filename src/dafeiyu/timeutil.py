"""统一时区口径：项目内所有“现在/今天”都按上海时间计算。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8))


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def today_shanghai() -> str:
    return now_shanghai().date().isoformat()
