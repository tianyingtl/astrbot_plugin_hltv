"""直播比赛订阅的持久化与状态流转。"""

import json
import time
from pathlib import Path
from typing import Any


def default_subscription_path() -> Path:
    return Path.home() / ".astrbot_plugin_hltv" / "live_subscriptions.json"


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
        snapshot: dict,
        *,
        umo: str,
        user_id: str,
        user_name: str,
    ) -> bool:
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
            "created_at": int(time.time()),
        }
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


def advance_subscription(
    subscription: dict,
    snapshot: dict,
    *,
    now: int | None = None,
    rating_wait_seconds: int = 180,
) -> tuple[dict, list[dict], bool]:
    """用一次比赛快照推进订阅，返回（新状态、事件、是否完成）。"""
    updated = dict(subscription)
    if str(snapshot.get("status")) == "finished":
        if snapshot.get("ratings"):
            return updated, [{"kind": "match_finished", "snapshot": snapshot}], True
        current = int(time.time()) if now is None else int(now)
        first_seen = int(updated.get("finished_seen_at") or 0)
        if not first_seen:
            updated["finished_seen_at"] = current
            return updated, [], False
        if current - first_seen < max(0, int(rating_wait_seconds)):
            return updated, [], False
        return updated, [{"kind": "match_finished", "snapshot": snapshot}], True

    updated.pop("finished_seen_at", None)
    previous_index = int(updated.get("last_map_index") or 0)
    current_index = int(snapshot.get("active_map_index") or 0)
    current_name = str(snapshot.get("current_map_name") or "")
    events = []
    if previous_index and current_index > previous_index:
        events.append({"kind": "map_started", "snapshot": snapshot})
    updated["last_map_index"] = max(previous_index, current_index)
    if current_name:
        updated["last_map_name"] = current_name
    return updated, events, False
