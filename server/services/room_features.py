"""Room feature tags and the filters that search on them.

Rooms are tagged with short strings from FEATURES. Tags come from three places:
- 25Live room details: feature/category names are matched by LIVE25_NAME_RULES
  (mostly equipment, plus indoor/outdoor)
- Room.features in our database (curated by CampusReserve admins)
- data/room_features.json (see below)
Rules like "food allowed" usually aren't in 25Live, so admins tag those.

Until the database exists (or for bulk-seeding it), tags can also come from
data/room_features.json, keyed by 25Live space_id:

    {
      "1810": ["food_allowed", "projector", "wheelchair_accessible"],
      "2962": ["outdoor", "food_allowed", "crafts_allowed"]
    }

A room's tags are the union of its DB tags and its file tags. A room with no
tag for something counts as "no": a room isn't food_allowed unless tagged so.
The exception is indoor/outdoor: rooms are indoor unless tagged "outdoor".
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

FEATURES: dict[str, str] = {
    # Setting
    "outdoor": "Outdoor space (rooms without this tag are indoor)",
    # What's allowed
    "food_allowed": "Food and drinks are allowed",
    "crafts_allowed": "Crafts allowed (paint, glue, glitter, etc.)",
    "amplified_sound_allowed": "Amplified music/speakers allowed",
    # Accessibility
    "wheelchair_accessible": "Wheelchair accessible",
    # Equipment
    "projector": "Projector or display screen",
    "whiteboard": "Whiteboard or chalkboard",
    "sound_system": "Built-in speakers / microphone",
    "computers": "Computers available",
    "piano": "Piano",
    "stage": "Stage or performance area",
    "mirrors": "Wall mirrors (dance / rehearsal)",
    # Layout and amenities
    "movable_furniture": "Tables and chairs can be rearranged",
    "sink": "Sink in or next to the room",
    "kitchen": "Kitchen access",
}

FEATURES_FILE = Path(__file__).resolve().parent.parent / "data" / "room_features.json"

_file_tags: dict[str, set[str]] | None = None


def _load_file_tags() -> dict[str, set[str]]:
    """Read data/room_features.json once. Missing file means no file tags."""
    global _file_tags
    if _file_tags is None:
        _file_tags = {}
        if FEATURES_FILE.exists():
            try:
                raw = json.loads(FEATURES_FILE.read_text())
                for room_id, tags in raw.items():
                    _file_tags[str(room_id)] = {normalize(t) for t in tags}
                    unknown = _file_tags[str(room_id)] - FEATURES.keys()
                    if unknown:
                        logger.warning("room_features.json: room %s has unknown tags %s",
                                       room_id, sorted(unknown))
            except (OSError, ValueError, AttributeError) as e:
                logger.error("Couldn't read %s: %s", FEATURES_FILE, e)
    return _file_tags


def normalize(tag: str) -> str:
    """'Food Allowed' -> 'food_allowed'."""
    return tag.strip().lower().replace(" ", "_").replace("-", "_")


# Tags derived from 25Live's feature and category names: if a name contains
# any of these phrases (case-insensitive), the room gets the tag. Adjust once
# you can see SBU's real names in data/live25_lookups.json.
LIVE25_NAME_RULES: dict[str, list[str]] = {
    "outdoor": ["outdoor", "outside", "lawn", "quad", "field", "plaza", "courtyard"],
    "projector": ["projector", "projection", "display", "screen", "monitor", "tv "],
    "whiteboard": ["whiteboard", "white board", "chalkboard", "chalk board", "dry erase"],
    "sound_system": ["sound system", "speaker", "microphone", "audio", "pa system"],
    "computers": ["computer", "pc ", "workstation", "laptop"],  # "pc " = whole word
    "piano": ["piano"],
    "stage": ["stage", "auditorium", "theater", "theatre"],
    "mirrors": ["mirror"],
    "movable_furniture": ["movable", "moveable", "flexible seating", "rearrange"],
    "sink": ["sink"],
    "kitchen": ["kitchen", "catering"],
    "wheelchair_accessible": ["accessible", "ada ", "wheelchair"],
    "food_allowed": ["food allowed", "food permitted", "food & drink", "food and drink"],
}


def tags_from_live25(detail: dict | None) -> set[str]:
    """Tags implied by a room's 25Live feature and category names."""
    if not detail:
        return set()
    names = [(x.get("name") or "").lower()
             for x in (detail.get("features") or []) + (detail.get("categories") or [])]
    names = [n for n in names if n]
    return {tag for tag, phrases in LIVE25_NAME_RULES.items()
            if any(_phrase_re(p).search(n) for p in phrases for n in names)}


def _phrase_re(phrase: str) -> re.Pattern:
    """Match at the start of a word ("computer" matches "Computers"). A phrase
    ending in a space must be a whole word ("ada " doesn't match "adapter")."""
    end = r"\b" if phrase.endswith(" ") else ""
    return re.compile(r"\b" + re.escape(phrase.strip()) + end)


def tags_for(room: dict, detail: dict | None = None) -> set[str]:
    """All feature tags for a merged room: DB tags, file tags, and tags from
    25Live's feature/category names (when `detail` is given)."""
    tags = {normalize(t) for t in room.get("features") or []}
    return tags | _load_file_tags().get(room["id"], set()) | tags_from_live25(detail)


@dataclass
class RoomFilters:
    """Feature filters. For each yes/no filter: True = room must have it,
    False = room must not have it, None = don't care."""

    setting: str | None = None               # "indoor" or "outdoor"
    yes_no: dict[str, bool] = field(default_factory=dict)   # tag -> required value
    required_tags: set[str] = field(default_factory=set)    # extra tags, all required
    room_type: str | None = None
    min_capacity: int | None = None
    max_capacity: int | None = None

    def matches(self, room: dict, tags: set[str]) -> bool:
        if self.setting == "outdoor" and "outdoor" not in tags:
            return False
        if self.setting == "indoor" and "outdoor" in tags:
            return False
        for tag, wanted in self.yes_no.items():
            if (tag in tags) != wanted:
                return False
        if not self.required_tags <= tags:
            return False
        if self.room_type and self.room_type.lower() not in (room.get("room_type") or "").lower():
            return False
        cap = room.get("capacity")
        if self.min_capacity is not None and (cap is None or cap < self.min_capacity):
            return False
        if self.max_capacity is not None and (cap is None or cap > self.max_capacity):
            return False
        return True
