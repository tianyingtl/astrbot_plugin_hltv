"""直播比赛订阅的持久化与状态流转。"""

import json
import math
import re
import time
from pathlib import Path
from typing import Any


def default_subscription_path() -> Path:
    return Path.home() / ".astrbot_plugin_hltv" / "live_subscriptions.json"


def default_spoiler_delay_path() -> Path:
    return Path.home() / ".astrbot_plugin_hltv" / "spoiler_delays.json"


def normalize_event_name(value: object) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]", "", str(value or "").casefold())


class SpoilerDelayStore:
    """按赛事保存额外防剧透分钟数，设置对所有用户共享。"""

    def __init__(self, path: Path | None = None):
        self.path = path or default_spoiler_delay_path()
        self._items = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        result = {}
        for key, item in data.items():
            try:
                extra = item.get("extra_minutes", 0) if isinstance(item, dict) else item
                value = float(extra)
                normalized = normalize_event_name(key)
                if not normalized or not math.isfinite(value):
                    continue
                result[normalized] = {
                    "name": str(item.get("name") or key) if isinstance(item, dict) else str(key),
                    "extra_minutes": max(0.0, value),
                }
            except (TypeError, ValueError):
                continue
        return result

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self._items, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def get_extra_minutes(self, event: object) -> float:
        key = normalize_event_name(event)
        item = self._items.get(key) or {}
        return float(item.get("extra_minutes", 0))

    def set_extra_minutes(self, event: object, minutes: float) -> float:
        key = normalize_event_name(event)
        if not key:
            raise ValueError("赛事名称不能为空")
        value = float(minutes)
        if not math.isfinite(value):
            raise ValueError("延迟分钟数必须是有限数字")
        value = max(0.0, value)
        self._items[key] = {"name": str(event).strip() or key, "extra_minutes": value}
        self._save()
        return value

    def adjust_extra_minutes(self, event: object, delta: float) -> float:
        value = float(delta)
        if not math.isfinite(value):
            raise ValueError("延迟分钟数必须是有限数字")
        current = self.get_extra_minutes(event)
        return self.set_extra_minutes(event, current + value)


def subscription_key(item: dict) -> tuple[str, str, str]:
    return (
        str(item.get("match_id") or ""),
        str(item.get("umo") or ""),
        str(item.get("user_id") or ""),
    )


class LiveSubscriptionStore:
    def __init__(self, path: Path | None = None):
        self.path = path or default_subscription_path()
        self._items = self._load()

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(self._items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(self.path)

    def all(self) -> list[dict]:
        return [dict(item) for item in self._items]

    def contains(self, item: dict) -> bool:
        key = subscription_key(item)
        return any(subscription_key(old) == key for old in self._items)

    def add(
        self,
        match: dict,
        snapshot: dict | None,
        *,
        umo: str,
        user_id: str,
        user_name: str,
        pending_start: bool = False,
    ) -> bool:
        snapshot = snapshot or {}
        item = {
            "match_id": str(match.get("id") or ""),
            "url": str(match.get("url") or ""),
            "team1": str(match.get("team1") or "?"),
            "team2": str(match.get("team2") or "?"),
            "event": str(match.get("event") or ""),
            "umo": str(umo),
            "user_id": str(user_id),
            "user_name": str(user_name),
            "last_map_index": int(snapshot.get("active_map_index") or 0),
            "last_map_name": str(snapshot.get("current_map_name") or ""),
            "sent_map_ratings": sorted(
                {
                    int(item.get("index") or 0)
                    for item in (snapshot.get("map_ratings") or [])
                    if str(item.get("index") or "").isdigit()
                    and int(item.get("index") or 0) > 0
                }
            ),
            "created_at": int(time.time()),
        }
        if pending_start:
            item["pending_start"] = True
            try:
                item["start_unix"] = int(float(match.get("unix") or 0))
            except (TypeError, ValueError):
                item["start_unix"] = 0
        key = subscription_key(item)
        if not all(key) or any(subscription_key(old) == key for old in self._items):
            return False
        self._items.append(item)
        self._save()
        return True

    def update(self, item: dict) -> None:
        key = subscription_key(item)
        for index, old in enumerate(self._items):
            if subscription_key(old) == key:
                self._items[index] = dict(item)
                self._save()
                return

    def remove(self, item: dict) -> None:
        key = subscription_key(item)
        kept = [old for old in self._items if subscription_key(old) != key]
        if len(kept) != len(self._items):
            self._items = kept
            self._save()

    def remove_user(self, umo: str, user_id: str) -> int:
        before = len(self._items)
        self._items = [
            item
            for item in self._items
            if not (
                str(item.get("umo") or "") == str(umo)
                and str(item.get("user_id") or "") == str(user_id)
            )
        ]
        removed = before - len(self._items)
        if removed:
            self._save()
        return removed


def _first_map_started(snapshot: dict) -> bool:
    if int(snapshot.get("active_map_index") or 0) <= 0:
        return False
    if not str(snapshot.get("current_map_name") or "").strip():
        return False
    if snapshot.get("round_live") is not None:
        return snapshot.get("round_live") is True
    score = str(snapshot.get("current_score") or "").strip()
    try:
        left, right = (int(value) for value in score.split(":"))
    except (TypeError, ValueError):
        return False
    return left + right > 0


def _is_bo1(snapshot: dict) -> bool:
    best_of = str(snapshot.get("best_of") or "").upper().replace(" ", "")
    return best_of in {"BO1", "BESTOF1"}


def advance_subscription(
    subscription: dict,
    snapshot: dict,
    *,
    now: int | None = None,
    rating_wait_seconds: int = 180,
    rating_delay_seconds: float = 0,
) -> tuple[dict, list[dict], bool]:
    """用一次比赛快照推进订阅，返回（新状态、事件、是否完成）。"""
    updated = dict(subscription)
    is_bo1 = _is_bo1(snapshot)
    current = int(time.time()) if now is None else int(now)
    delay = max(0.0, float(rating_delay_seconds))
    sent_map_ratings = {
        int(index)
        for index in (updated.get("sent_map_ratings") or [])
        if str(index).isdigit() and int(index) > 0
    }
    pending_ratings = {
        str(index): float(timestamp)
        for index, timestamp in (updated.get("rating_pending_at") or {}).items()
        if str(index).isdigit() and float(timestamp or 0) > 0
    }
    if (
        str(snapshot.get("status")) == "finished"
        and snapshot.get("ratings")
        and delay > 0
        and not updated.get("match_rating_pending_at")
    ):
        updated["match_rating_pending_at"] = current
    events = []
    for map_rating in snapshot.get("map_ratings") or []:
        index = int(map_rating.get("index") or 0)
        if index <= 0 or index in sent_map_ratings or not map_rating.get("ratings"):
            continue
        first_seen = pending_ratings.get(str(index))
        if first_seen is None and delay > 0:
            pending_ratings[str(index)] = float(current)
            continue
        if delay > 0 and current - (first_seen or current) < delay:
            continue
        events.append(
            {"kind": "map_finished", "snapshot": snapshot, "map": map_rating}
        )
        sent_map_ratings.add(index)
        pending_ratings.pop(str(index), None)
        if is_bo1:
            updated["bo1_rating_sent"] = True
    updated["sent_map_ratings"] = sorted(sent_map_ratings)
    if pending_ratings:
        updated["rating_pending_at"] = pending_ratings
    else:
        updated.pop("rating_pending_at", None)

    if str(snapshot.get("status")) == "finished":
        if is_bo1 and updated.get("bo1_rating_sent"):
            updated.pop("finished_seen_at", None)
            return updated, events, True
        if is_bo1 and snapshot.get("map_ratings") and not updated.get("bo1_rating_sent"):
            return updated, events, False
        completed_map_indexes = {
            int(item.get("index") or item.get("ordinal") or position)
            for position, item in enumerate(snapshot.get("maps") or [], start=1)
            if isinstance(item, dict)
            and item.get("finished")
            and str(item.get("index") or item.get("ordinal") or position).isdigit()
        }
        missing_map_indexes = completed_map_indexes - sent_map_ratings
        available_map_indexes = {
            int(item.get("index") or 0)
            for item in snapshot.get("map_ratings") or []
            if str(item.get("index") or "").isdigit() and item.get("ratings")
        }
        if missing_map_indexes & available_map_indexes:
            return updated, events, False
        waiting_for_map_rating = bool(missing_map_indexes) and not is_bo1
        if waiting_for_map_rating or not snapshot.get("ratings"):
            first_seen = int(updated.get("finished_seen_at") or 0)
            if not first_seen:
                updated["finished_seen_at"] = current
                return updated, events, False
            if current - first_seen < max(0, int(rating_wait_seconds)):
                return updated, events, False
        match_first_seen = float(updated.get("match_rating_pending_at") or 0)
        if snapshot.get("ratings"):
            if not match_first_seen and delay > 0:
                updated["match_rating_pending_at"] = current
                return updated, events, False
            if delay > 0 and current - match_first_seen < delay:
                return updated, events, False
        updated.pop("finished_seen_at", None)
        updated.pop("match_rating_pending_at", None)
        events.append({"kind": "match_finished", "snapshot": snapshot})
        return updated, events, True

    updated.pop("finished_seen_at", None)
    previous_index = int(updated.get("last_map_index") or 0)
    current_index = int(snapshot.get("active_map_index") or 0)
    previous_name = str(updated.get("last_map_name") or "")
    current_name = str(snapshot.get("current_map_name") or "")
    if updated.get("pending_start"):
        if not _first_map_started(snapshot):
            if current_index and current_name:
                updated["awaiting_map_start"] = True
            return updated, events, False
        events.append({"kind": "map_started", "snapshot": snapshot})
        updated.pop("pending_start", None)
        updated.pop("awaiting_map_start", None)
        updated.pop("start_unix", None)
        updated["created_at"] = int(time.time()) if now is None else int(now)
        updated["last_map_index"] = current_index
        updated["last_map_name"] = current_name
        return updated, events, False
    map_changed = (previous_index and current_index > previous_index) or (
        previous_name
        and current_name
        and previous_name.casefold() != current_name.casefold()
    )
    if map_changed and snapshot.get("round_live") is False:
        updated["awaiting_map_start"] = True
        return updated, events, False
    if map_changed:
        events.append({"kind": "map_started", "snapshot": snapshot})
        updated.pop("awaiting_map_start", None)
    updated["last_map_index"] = max(previous_index, current_index)
    if current_name:
        updated["last_map_name"] = current_name
    elif snapshot.get("maps") and snapshot["maps"][-1].get("finished"):
        updated["awaiting_map_start"] = True
    return updated, events, False
