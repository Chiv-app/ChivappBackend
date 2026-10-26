"""Helpers for musician repertoire items (title + optional YouTube URL)."""

from __future__ import annotations


def _normalize_youtube_url(raw_url) -> str | None:
    if not isinstance(raw_url, str):
        return None
    cleaned = raw_url.strip()
    if not cleaned or len(cleaned) > 500:
        return None
    lower = cleaned.lower()
    if lower.startswith(("javascript:", "data:", "vbscript:", "file:", "blob:", "/uploads/private/")):
        return None
    if lower.startswith(("https://", "http://", "/uploads/")):
        return cleaned
    if not lower.startswith("/") and "." in lower.split("/")[0] and ":" not in lower.split("/")[0]:
        return f"https://{cleaned}"
    return None


def normalize_repertoire_items(raw) -> list[dict]:
    """Normalize repertoire payloads to [{title, youtube_url}]."""
    if not raw:
        return []

    items: list[dict] = []
    for entry in raw:
        if isinstance(entry, str):
            title = entry.strip()
            if not title:
                continue
            items.append({"title": title, "youtube_url": None})
            continue

        if isinstance(entry, dict):
            title = str(entry.get("title") or "").strip()
            if not title:
                continue
            youtube = _normalize_youtube_url(entry.get("youtube_url"))
            items.append({"title": title, "youtube_url": youtube})
    return items


def repertoire_from_profile(profile) -> list[dict]:
    """Return repertoire items, falling back to legacy songs titles."""
    raw = getattr(profile, "repertoire", None) or []
    items = normalize_repertoire_items(raw)
    if items:
        return items
    return normalize_repertoire_items(getattr(profile, "songs", None) or [])


def prepare_musician_updates(updates: dict) -> dict:
    """Keep repertoire and songs in sync when either field is updated."""
    prepared = dict(updates)

    if "repertoire" in prepared and prepared["repertoire"] is not None:
        items = normalize_repertoire_items(prepared["repertoire"])
        prepared["repertoire"] = items
        prepared["songs"] = [item["title"] for item in items]
    elif "songs" in prepared and prepared["songs"] is not None:
        items = normalize_repertoire_items(prepared["songs"])
        prepared["repertoire"] = items
        prepared["songs"] = [item["title"] for item in items]

    if "showreel_video_url" in prepared:
        showreel = prepared["showreel_video_url"]
        if isinstance(showreel, str):
            showreel = showreel.strip() or None
            prepared["showreel_video_url"] = showreel
        if showreel and "videos" in prepared and prepared["videos"] is not None:
            videos = prepared["videos"] or []
            if showreel not in videos:
                prepared["showreel_video_url"] = None

    return prepared
