"""A tracker preset file: a Settings > Watcher convenience, never read for behaviour.

If ``<STATE_DIR>/tracker-preset.json`` (``/config/tracker-preset.json``) exists, the Watcher tab offers
to fill the tracker fields from it. The file is read only when that tab is drawn or the button is
pressed - never at start-up and never by a cycle - and applying it writes exactly the settings keys the
tab's own Save writes, through the very same checks. Nothing in this module touches the network.

The file::

    {"label": "Name shown on the tab", "settings": {"TRACKER_URL": "https://...", ...},
     "categories": {"13": "Audiobooks", "14": "Ebooks"}}

* ``label`` is the preset's own name; the app never names a tracker itself.
* ``settings`` maps ``TRACKER_*`` names to text. An empty value means "leave this field alone".
* Only the fields in ``FILLABLE`` can be filled. The ones that are yours - the session cookie's value,
  VIP / Premium member, the proxy, the home address and the allowed address - are refused with a plain
  message: a file that is shared between people must not carry them.
* ``categories`` is optional: the tracker's own search-category value -> a readable name. When it is there the
  Search category field on the tab is a dropdown (the saved value is the bare key). It is only ever read to draw
  that field; applying a preset does not touch it. Absent or empty = a plain text field.
* The whole file is checked before anything is written. One bad value means no value is saved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import CONFIG, SETTING_CHOICES, WATCHER_TEXT_CHECKS

PRESET_FILE = "tracker-preset.json"
MAX_BYTES = 32 * 1024
MAX_LABEL = 60
MAX_CATEGORIES = 100
MAX_CATEGORY_NAME = 60

#: the file's name for a field -> the settings key the Watcher tab saves it under
FILLABLE = {
    "TRACKER_URL": "tracker_url",
    "TRACKER_COOKIE_NAME": "tracker_cookie_name",
    "TRACKER_SEARCH_PATH": "tracker_search_path",
    "TRACKER_SEARCH_BODY": "tracker_search_body",
    "TRACKER_SEARCH_METHOD": "tracker_search_method",
    "TRACKER_SEARCH_ENCODING": "tracker_search_encoding",
    "TRACKER_SEARCH_CATEGORY": "tracker_search_category",
    "TRACKER_DOWNLOAD_PATH": "tracker_download_path",
    "TRACKER_DOWNLOAD_PARAM": "tracker_download_param",
    "TRACKER_FREELEECH_PARAM": "tracker_freeleech_param",
    "TRACKER_USER_AGENT": "tracker_user_agent",
}
#: every other TRACKER_* name the app reads: personal to one person or one container, so never from a file
PERSONAL = ("TRACKER_SESSION", "TRACKER_VIP", "TRACKER_PROXY", "TRACKER_HOME_IP", "TRACKER_EXPECTED_EGRESS_IP")

#: the field's name on the Watcher tab
LABELS = {
    "tracker_url": "Tracker URL",
    "tracker_cookie_name": "Session cookie name",
    "tracker_search_path": "Search path",
    "tracker_search_body": "Search request body",
    "tracker_search_method": "Search method",
    "tracker_search_encoding": "Search encoding",
    "tracker_search_category": "Search category",
    "tracker_download_path": "Download path",
    "tracker_download_param": "Download parameter",
    "tracker_freeleech_param": "Freeleech parameter",
    "tracker_user_agent": "User-Agent",
}
#: what an untouched field holds: filling it is not "overwriting" anything
BUILT_IN = {"tracker_search_method": "POST", "tracker_search_encoding": "json", "tracker_download_param": "tid"}


@dataclass
class Preset:
    label: str
    #: settings key -> the value to save (already checked; empty values are left out)
    values: dict = field(default_factory=dict)
    #: the tracker's own category value -> readable name (empty = no dropdown)
    categories: dict = field(default_factory=dict)


def path_of(directory=None) -> Path:
    return Path(directory if directory is not None else CONFIG.state_dir) / PRESET_FILE


def _check_value(key: str, raw: str) -> str:
    """The value as the Watcher tab's Save would store it, or ValueError with a plain message."""
    if key in SETTING_CHOICES:
        match = next((c for c in SETTING_CHOICES[key] if c.lower() == raw.strip().lower()), None)
        if match is None:
            raise ValueError(f"{LABELS[key]}: use one of {', '.join(SETTING_CHOICES[key])}")
        return match.upper() if key == "tracker_search_method" else match.lower()
    return WATCHER_TEXT_CHECKS[key][1](raw)


def _check_categories(raw) -> dict:
    """The optional ``categories`` object as {value: name}, or ValueError with a plain message."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("categories must be an object of the tracker's value -> a readable name")
    if len(raw) > MAX_CATEGORIES:
        raise ValueError(f"categories has more than {MAX_CATEGORIES} entries")
    out: dict = {}
    for key, name in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("every category needs a name written as text")
        try:
            value = WATCHER_TEXT_CHECKS["tracker_search_category"][1](key)
        except ValueError:
            raise ValueError("a category value must be one line of plain text") from None
        name = " ".join(name.split())
        if not value or len(name) > MAX_CATEGORY_NAME or any(ord(ch) < 32 for ch in name):
            raise ValueError(f"each category needs a value and a one-line name of at most {MAX_CATEGORY_NAME} characters")
        out[value] = name
    return out


def parse(text: str) -> tuple[Preset | None, str]:
    """(preset, "") for a good file's text, (None, why) for anything else. Never raises."""
    try:
        data = json.loads(text)
    except ValueError:
        return None, "it is not valid JSON"
    if not isinstance(data, dict):
        return None, "it must be a JSON object with a label and settings"
    label = data.get("label")
    if not isinstance(label, str) or not label.strip():
        return None, "it needs a \"label\": the name to show for this preset"
    label = " ".join(label.split())
    if len(label) > MAX_LABEL or any(ord(ch) < 32 for ch in label):
        return None, f"the label must be one line of at most {MAX_LABEL} characters"
    settings = data.get("settings")
    if not isinstance(settings, dict) or not settings:
        return None, "it needs \"settings\": an object of TRACKER_* fields to fill"
    values: dict = {}
    problems: list[str] = []
    for name, raw in settings.items():
        if name in PERSONAL:
            problems.append(f"{name} is yours to set, so a preset cannot carry it")
        elif name not in FILLABLE:
            problems.append(f"{str(name)[:40]} is not a field a preset can fill")
        elif not isinstance(raw, str):
            problems.append(f"{name} must be text")
        elif raw.strip():
            key = FILLABLE[name]
            try:
                values[key] = _check_value(key, raw)
            except ValueError as exc:
                problems.append(str(exc))
    try:
        categories = _check_categories(data.get("categories"))
    except ValueError as exc:
        problems.append(str(exc))
        categories = {}
    if problems:
        return None, "; ".join(problems[:5]) + (f"; and {len(problems) - 5} more" if len(problems) > 5 else "")
    if not values:
        return None, "every value in it is empty, so there is nothing to fill"
    return Preset(label, values, categories), ""


def load(directory=None) -> tuple[Preset | None, str]:
    """(None, "") when there is no preset file; (preset, "") for a good one; (None, plain message) for a
    file that cannot be used. Never raises, never writes."""
    path = path_of(directory)
    try:
        if not path.exists():
            return None, ""
        if not path.is_file():
            return None, "The tracker preset file cannot be used: it is not a file."
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError:
        return None, "The tracker preset file cannot be used: it could not be read."
    if len(raw) > MAX_BYTES:
        return None, f"The tracker preset file cannot be used: it is bigger than {MAX_BYTES // 1024} KB."
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "The tracker preset file cannot be used: it is not UTF-8 text."
    preset, why = parse(text)
    return preset, ("" if preset else f"The tracker preset file cannot be used: {why}.")


def plan(preset: Preset, current) -> dict:
    """What applying would do, given ``current(key)`` -> the field's value now:
    ``{"fill": [key], "replace": [key], "same": [key]}``. ``fill`` = the field is empty (or built-in),
    ``replace`` = it holds something else that the preset would overwrite, ``same`` = already equal."""
    out: dict = {"fill": [], "replace": [], "same": []}
    for key, value in preset.values.items():
        now = str(current(key) or "").strip()
        if now == value or (key in SETTING_CHOICES and now.lower() == value.lower()):
            out["same"].append(key)
        elif not now or now == BUILT_IN.get(key):
            out["fill"].append(key)
        else:
            out["replace"].append(key)
    return out


def label_of(key: str) -> str:
    return LABELS[key]
