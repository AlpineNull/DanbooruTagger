import gc
import json
import os
import re
import threading
import time
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from credential_store import delete_secret, load_secret, save_secret, secret_exists
from danbooru_client import DanbooruClient

APP_DIR = Path(__file__).resolve().parent
MODEL_DIR = APP_DIR / "models" / "pixai-tagger-v1.0"
SETTINGS_PATH = APP_DIR / "settings.json"
DANBOORU_CACHE_PATH = APP_DIR / "danbooru_cache.json"
API_KEY_PATH = APP_DIR / "danbooru_api_key.bin"
MODEL_ID = "pixai-labs/pixai-tagger-v1.0"

# Windows named mutex used to keep the app single-instance.
# The name intentionally does not contain the app version, so an updated build
# cannot run next to an older build from the same user session by accident.
_SINGLE_INSTANCE_MUTEX_NAME = r"Local\DanbooruTagger_SingleInstance_5D1A3E8C"
_SINGLE_INSTANCE_MUTEX_HANDLE = None


def acquire_single_instance() -> bool:
    """Return False when another Danbooru Tagger process already owns the mutex."""
    global _SINGLE_INSTANCE_MUTEX_HANDLE
    if os.name != "nt":
        return True

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX_NAME)
    if not handle:
        # Fail open: a mutex problem should not make the application unusable.
        return True

    ERROR_ALREADY_EXISTS = 183
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False

    _SINGLE_INSTANCE_MUTEX_HANDLE = handle
    return True


def show_already_running_message():
    if os.name != "nt":
        return
    import ctypes

    MB_OK = 0x00000000
    MB_ICONINFORMATION = 0x00000040
    ctypes.windll.user32.MessageBoxW(
        None,
        "Danbooru Tagger jest już uruchomiony.",
        "Danbooru Tagger",
        MB_OK | MB_ICONINFORMATION,
    )


RESOLUTION_TAGS = {
    "lowres",
    "highres",
    "absurdres",
    "incredibly_absurdres",
}

DEFAULT_SETTINGS = {
    "high": 0.75,
    "medium": 0.45,
    "low": 0.17,
    "categories": {
        "general": True,
        "character": True,
        "copyright": True,
        "meta": True,
        "style": False,
    },
    "danbooru_cleanup": True,
    "hide_implied_parents": True,
    "blacklist": ["office_lady", "mature_female"],
    "danbooru_login": "",
    "check_blocked_artist": True,
    "gpu_idle_unload_minutes": 10,
    "character_variant_suggestions": True,
    "conflict_warnings": True,
    "suggest_translation_request": True,
    "unknown_tag_warnings": True,
    "character_context_checks": True,
    "greyscale_warnings": True,
    "near_threshold_suggestions": True,
}


def load_settings():
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    if SETTINGS_PATH.exists():
        try:
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            for key in ("high", "medium", "low"):
                if key in saved:
                    settings[key] = float(saved[key])
            if isinstance(saved.get("categories"), dict):
                settings["categories"].update(saved["categories"])
            for key in (
                "danbooru_cleanup",
                "hide_implied_parents",
                "check_blocked_artist",
                "character_variant_suggestions",
                "conflict_warnings",
                "suggest_translation_request",
                "unknown_tag_warnings",
                "character_context_checks",
                "greyscale_warnings",
                "near_threshold_suggestions",
            ):
                if key in saved:
                    settings[key] = bool(saved[key])
            if "gpu_idle_unload_minutes" in saved:
                try:
                    settings["gpu_idle_unload_minutes"] = max(0, int(saved["gpu_idle_unload_minutes"]))
                except (TypeError, ValueError):
                    pass
            if isinstance(saved.get("blacklist"), list):
                settings["blacklist"] = [str(x) for x in saved["blacklist"] if str(x).strip()]
            if isinstance(saved.get("danbooru_login"), str):
                settings["danbooru_login"] = saved["danbooru_login"].strip()
        except Exception:
            pass
    return settings


def save_settings(settings):
    SETTINGS_PATH.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def normalize_tag(tag: str) -> str:
    return "_".join(str(tag).strip().split())


def parse_blacklist(text_or_list) -> list[str]:
    if isinstance(text_or_list, list):
        raw = text_or_list
    else:
        raw = str(text_or_list or "").replace(",", " ").split()
    result = []
    seen = set()
    for value in raw:
        tag = normalize_tag(value)
        if tag and tag not in seen:
            result.append(tag)
            seen.add(tag)
    return result


def flatten_scores(results: dict, enabled_categories: dict) -> list[tuple[str, float]]:
    best = {}
    for category, enabled in enabled_categories.items():
        if not enabled:
            continue
        category_results = results.get(category, {}) or {}
        for raw_tag, raw_score in category_results.items():
            tag = normalize_tag(raw_tag)
            if not tag or tag.startswith("rating:"):
                continue
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                continue
            if tag not in best or score > best[tag]:
                best[tag] = score
    return sorted(best.items(), key=lambda item: (-item[1], item[0]))


def hide_resolution_autotags(items):
    return [(tag, score) for tag, score in items if tag not in RESOLUTION_TAGS]


def apply_user_blacklist(items, blacklist):
    blocked = set(parse_blacklist(blacklist))
    kept = []
    removed = []
    for tag, score in items:
        if tag in blocked:
            removed.append(tag)
        else:
            kept.append((tag, score))
    return kept, removed


def apply_deterministic_rules(items):
    score_map = dict(items)
    removed = []
    if "multiple_views" in score_map and "solo" in score_map:
        score_map.pop("solo", None)
        removed.append("solo")
    if "portrait" in score_map and "upper_body" in score_map:
        score_map.pop("upper_body", None)
        removed.append("upper_body")

    # Role tags are especially prone to semantic guessing from clothing alone.
    # Keep them only when the scene contains at least one contextual cue.
    role_context = {
        "teacher": {
            "classroom", "blackboard", "chalkboard", "school", "school_desk",
            "teaching", "student", "students",
        },
        "office_lady": {
            "office", "office_chair", "cubicle", "desk", "computer", "workplace",
        },
    }
    present = set(score_map)
    for role_tag, context_tags in role_context.items():
        if role_tag in score_map and not (present & context_tags):
            score_map.pop(role_tag, None)
            removed.append(role_tag)
    ordered = sorted(score_map.items(), key=lambda item: (-item[1], item[0]))
    return ordered, removed


def split_by_confidence(items, high, medium, low):
    buckets = {"high": [], "medium": [], "low": []}
    for tag, score in items:
        if score >= high:
            buckets["high"].append(tag)
        elif score >= medium:
            buckets["medium"].append(tag)
        elif score >= low:
            buckets["low"].append(tag)
    return buckets


def build_copy_all_text(high: str, medium: str, low: str) -> str:
    """Exact requested clipboard layout: blank / high / blank / medium / blank / low / blank."""
    return f"\n{high.strip()}\n\n{medium.strip()}\n\n{low.strip()}\n\n"


NON_ENGLISH_TEXT_TAGS = {
    "chinese_text",
    "cyrillic_text",
    "french_text",
    "german_text",
    "greek_text",
    "japanese_text",
    "korean_text",
    "non_english_text",
    "russian_text",
    "spanish_text",
}

EYE_COLOR_TAGS = {
    "aqua_eyes", "black_eyes", "blue_eyes", "brown_eyes", "green_eyes",
    "grey_eyes", "orange_eyes", "pink_eyes", "purple_eyes", "red_eyes",
    "white_eyes", "yellow_eyes",
}

MULTI_PERSON_TAGS = {
    "2girls", "3girls", "4girls", "5girls", "6+girls", "multiple_girls",
    "2boys", "3boys", "4boys", "5boys", "6+boys", "multiple_boys",
    "2others", "3others", "4others", "5others", "6+others", "multiple_others",
}

COLOR_PREFIXES = {
    "aqua", "black", "blue", "brown", "green", "grey", "gray", "orange",
    "pink", "purple", "red", "white", "yellow", "blonde",
}

GREYSCALE_COLOR_EXCLUSIONS = {
    "black_and_white", "black_background", "white_background", "grey_background",
    "gray_background", "monochrome", "greyscale",
}

BREAST_SIZE_TAGS = {
    "flat_chest", "small_breasts", "medium_breasts", "large_breasts", "huge_breasts",
}


def suggested_translation_request(tags):
    tags = set(tags)
    if "translation_request" in tags or "translated" in tags:
        return []
    detected = sorted(tags & NON_ENGLISH_TEXT_TAGS)
    return detected


def find_conflict_warnings(tags):
    tags = set(tags)
    warnings = []
    plural = sorted(tags & MULTI_PERSON_TAGS)
    is_multi_subject_context = (
        "multiple_views" in tags
        or "comic" in tags
        or any(tag.endswith("koma") for tag in tags)
        or bool(plural)
    )
    if not is_multi_subject_context and "heterochromia" not in tags and "multicolored_eyes" not in tags:
        eye_colors = sorted(tags & EYE_COLOR_TAGS)
        if len(eye_colors) > 1:
            warnings.append("kolory oczu: " + " / ".join(eye_colors))

    if not is_multi_subject_context:
        if "short_hair" in tags and ("long_hair" in tags or "very_long_hair" in tags):
            lengths = [x for x in ("short_hair", "long_hair", "very_long_hair") if x in tags]
            warnings.append("długość włosów: " + " / ".join(lengths))
        breast_sizes = [x for x in ("flat_chest", "small_breasts", "medium_breasts", "large_breasts", "huge_breasts") if x in tags]
        if len(breast_sizes) > 1:
            warnings.append("rozmiar piersi: " + " / ".join(breast_sizes))
    if "solo" in tags and plural:
        warnings.append("solo / " + " / ".join(plural))
    return warnings


def estimate_subject_minimum(tags):
    """Estimate the minimum visible subject count from Danbooru count tags."""
    tags = set(tags)
    per_kind = {"girls": 0, "boys": 0, "others": 0}
    for tag in tags:
        m = re.fullmatch(r"(\d+)(\+?)(girl|girls|boy|boys|other|others)", tag)
        if m:
            kind = {
                "girl": "girls", "girls": "girls",
                "boy": "boys", "boys": "boys",
                "other": "others", "others": "others",
            }[m.group(3)]
            per_kind[kind] = max(per_kind[kind], int(m.group(1)))
    if "multiple_girls" in tags:
        per_kind["girls"] = max(per_kind["girls"], 2)
    if "multiple_boys" in tags:
        per_kind["boys"] = max(per_kind["boys"], 2)
    if "multiple_others" in tags:
        per_kind["others"] = max(per_kind["others"], 2)
    total = sum(per_kind.values())
    if total == 0 and "solo" in tags:
        return 1
    return total


def find_character_coverage(tags, tag_info):
    minimum = estimate_subject_minimum(tags)
    character_tags = [
        tag for tag in tags
        if (tag_info.get(tag) or {}).get("exists")
        and (tag_info.get(tag) or {}).get("category") == 4
    ]
    if minimum >= 2 and len(character_tags) < minimum:
        return {
            "minimum_subjects": minimum,
            "recognized_characters": len(character_tags),
            "character_tags": character_tags,
        }
    return None


def find_copyright_sanity_warnings(tags, tag_info, character_copyrights=None):
    """Warn only when Danbooru relationship data confirms a mismatch.

    v0.5.0 guessed compatibility from character tag suffixes. That produced false
    positives for valid pairs such as green_heart_(neptunia) and
    choujigen_game_neptune. v0.5.1 is deliberately conservative: a crossover
    warning is emitted only when Danbooru related-tag data gives one or more
    copyright tags for the character and none of the copyrights present in the
    image match those confirmed copyrights.
    """
    copyrights = [
        tag for tag in tags
        if (tag_info.get(tag) or {}).get("exists")
        and (tag_info.get(tag) or {}).get("category") == 3
    ]
    if not copyrights:
        return []
    character_copyrights = character_copyrights or {}
    warnings = []
    for tag in tags:
        meta = tag_info.get(tag) or {}
        if not meta.get("exists") or meta.get("category") != 4:
            continue
        confirmed = set(character_copyrights.get(tag) or [])
        if not confirmed:
            continue
        if confirmed.intersection(copyrights):
            continue
        warnings.append(
            f"{tag} ↔ {' / '.join(copyrights)} (typowo: {' / '.join(sorted(confirmed))})"
        )
    return warnings


def select_near_threshold_candidates(items, low_threshold, margin=0.07, limit=8, blacklist=None):
    """Return highest-scoring known-shape candidates just below LOWEST threshold.

    These are advisory only and never enter HIGH/MEDIUM/LOW. The model is run
    once at a slightly lower threshold, so there is no second inference pass.
    """
    floor = max(0.05, float(low_threshold) - float(margin))
    blocked = set(parse_blacklist(blacklist or []))
    candidates = []
    for tag, score in items:
        if tag in blocked:
            continue
        if floor <= float(score) < float(low_threshold):
            candidates.append((tag, float(score)))
    candidates.sort(key=lambda item: (-item[1], item[0]))
    return candidates[: max(0, int(limit))]

def find_greyscale_color_warnings(tags, tag_info=None):
    tags = list(tags)
    if "greyscale" not in tags and "monochrome" not in tags:
        return []
    tag_info = tag_info or {}
    result = []
    for tag in tags:
        if tag in GREYSCALE_COLOR_EXCLUSIONS:
            continue
        prefix = tag.split("_", 1)[0]
        if prefix not in COLOR_PREFIXES:
            continue
        meta = tag_info.get(tag) or {}
        if meta and meta.get("category") not in (0, None):
            continue
        result.append(tag)
    return sorted(set(result))


def build_advice_text(
    tags,
    related_map=None,
    cooccurrence_map=None,
    unknown_tags=None,
    copyright_warnings=None,
    coverage=None,
    greyscale_tags=None,
    near_threshold_tags=None,
    translation_enabled=True,
    conflicts_enabled=True,
):
    lines = []
    related_map = related_map or {}
    for source_tag, suggestions in related_map.items():
        if suggestions:
            lines.append(f"[POSTACIE] Warianty/powiązane z {source_tag}: " + " ".join(suggestions))
    if coverage:
        lines.append(
            "[POSTACIE] Możliwe brakujące character tags: "
            f"rozpoznano {coverage['recognized_characters']} przy co najmniej {coverage['minimum_subjects']} postaciach."
        )
    cooccurrence_map = cooccurrence_map or {}
    for source_tag, suggestions in cooccurrence_map.items():
        if suggestions:
            lines.append(f"[POSTACIE] Często razem z {source_tag}: " + " ".join(suggestions))
    unknown_tags = sorted(set(unknown_tags or []))
    if unknown_tags:
        lines.append("[NIEZNANE] Nie istnieje na Danbooru: " + " ".join(unknown_tags))
    if copyright_warnings:
        lines.append("[COPYRIGHT] Sprawdź crossover: " + "; ".join(copyright_warnings))
    if greyscale_tags:
        lines.append("[GREYSCALE] Sprawdź tagi kolorów: " + " ".join(greyscale_tags))
    near_threshold_tags = list(dict.fromkeys(near_threshold_tags or []))
    if near_threshold_tags:
        lines.append("[NISKI PRÓG] Do sprawdzenia: " + " ".join(near_threshold_tags))
    if translation_enabled:
        language_tags = suggested_translation_request(tags)
        if language_tags:
            lines.append("[INNE] Do rozważenia: translation_request (" + ", ".join(language_tags) + ")")
    if conflicts_enabled:
        warnings = find_conflict_warnings(tags)
        if warnings:
            lines.append("[KONFLIKTY] Sprawdź: " + "; ".join(warnings))
    return "\n".join(lines) if lines else "Brak dodatkowych sugestii."


def parse_idle_minutes(value):
    text = str(value or "").strip()
    if not text or text.casefold() in {"nigdy", "never", "0"}:
        return 0
    return max(0, int(float(text.replace(",", "."))))


def format_cleanup_summary(report, blacklist_removed=None, rule_removed=None):
    parts = []
    if report is not None:
        alias_count = int(report.get("alias_replacements", 0))
        deprecated_count = len(report.get("deprecated_removed", []))
        implied_count = len(report.get("implied_removed", []))
        if alias_count:
            parts.append(f"aliasy→canonical: {alias_count}")
        if deprecated_count:
            parts.append(f"deprecated usunięte: {deprecated_count}")
        if implied_count:
            parts.append(f"implied parents ukryte: {implied_count}")
        if report.get("network_failed"):
            parts.append("API Danbooru niedostępne")
        elif report.get("used_stale_cache"):
            parts.append("użyto części cache")
    if blacklist_removed:
        parts.append(f"blacklista: {len(blacklist_removed)}")
    if rule_removed:
        parts.append(f"reguły: {len(rule_removed)}")
    return "; ".join(parts)


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, settings, on_save):
        super().__init__(parent)
        self.title("Ustawienia")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.settings = settings
        self.on_save = on_save

        frame = ttk.Frame(self, padding=16)
        frame.grid(sticky="nsew")

        ttk.Label(frame, text="Progi confidence", font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )
        self.high_var = tk.StringVar(value=str(settings["high"]))
        self.medium_var = tk.StringVar(value=str(settings["medium"]))
        self.low_var = tk.StringVar(value=str(settings["low"]))
        for row, (label, var) in enumerate(
            (("High", self.high_var), ("Medium", self.medium_var), ("Lowest", self.low_var)),
            start=1,
        ):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=3)
            ttk.Entry(frame, textvariable=var, width=12).grid(row=row, column=1, sticky="e", pady=3)

        ttk.Separator(frame).grid(row=4, column=0, columnspan=2, sticky="ew", pady=12)
        ttk.Label(frame, text="Kategorie", font=("Segoe UI", 10, "bold")).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        self.category_vars = {}
        labels = {
            "general": "General",
            "character": "Character",
            "copyright": "Copyright",
            "meta": "Meta",
            "style": "Style",
        }
        row = 6
        for key, label in labels.items():
            var = tk.BooleanVar(value=bool(settings["categories"].get(key, False)))
            self.category_vars[key] = var
            ttk.Checkbutton(frame, text=label, variable=var).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=2
            )
            row += 1

        ttk.Separator(frame).grid(row=row, column=0, columnspan=2, sticky="ew", pady=12)
        row += 1
        ttk.Label(frame, text="Blacklista", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        row += 1
        ttk.Label(
            frame,
            text="Dokładne nazwy tagów, rozdzielone spacją lub nową linią.",
            foreground="#666666",
        ).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        self.blacklist_text = tk.Text(frame, width=54, height=4, font=("Consolas", 9), wrap="word")
        self.blacklist_text.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.blacklist_text.insert("1.0", "\n".join(settings.get("blacklist", [])))
        row += 1

        ttk.Separator(frame).grid(row=row, column=0, columnspan=2, sticky="ew", pady=12)
        row += 1
        ttk.Label(frame, text="GPU", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        row += 1
        ttk.Label(frame, text="Zwolnij model z VRAM po bezczynności").grid(
            row=row, column=0, sticky="w", padx=(0, 16), pady=3
        )
        idle_value = int(settings.get("gpu_idle_unload_minutes", 10) or 0)
        self.gpu_idle_var = tk.StringVar(value="Nigdy" if idle_value <= 0 else str(idle_value))
        ttk.Combobox(
            frame, textvariable=self.gpu_idle_var, values=("5", "10", "15", "30", "Nigdy"),
            state="readonly", width=12
        ).grid(row=row, column=1, sticky="e", pady=3)
        row += 1

        ttk.Separator(frame).grid(row=row, column=0, columnspan=2, sticky="ew", pady=12)
        row += 1
        ttk.Label(frame, text="Pomoc po analizie", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        row += 1
        self.variant_var = tk.BooleanVar(value=bool(settings.get("character_variant_suggestions", True)))
        ttk.Checkbutton(
            frame, text="Pokazuj powiązane / bardziej szczegółowe character tags", variable=self.variant_var
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1
        self.conflict_var = tk.BooleanVar(value=bool(settings.get("conflict_warnings", True)))
        ttk.Checkbutton(
            frame, text="Ostrzegaj o podejrzanych konfliktach tagów", variable=self.conflict_var
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1
        self.translation_var = tk.BooleanVar(value=bool(settings.get("suggest_translation_request", True)))
        ttk.Checkbutton(
            frame, text="Sugeruj translation_request przy nieangielskim tekście", variable=self.translation_var
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1
        self.unknown_var = tk.BooleanVar(value=bool(settings.get("unknown_tag_warnings", True)))
        ttk.Checkbutton(
            frame, text="Ostrzegaj o tagach nieistniejących na Danbooru", variable=self.unknown_var
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1
        self.character_context_var = tk.BooleanVar(value=bool(settings.get("character_context_checks", True)))
        ttk.Checkbutton(
            frame, text="Sprawdzaj character/copyright i brakujące postacie", variable=self.character_context_var
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1
        self.greyscale_var = tk.BooleanVar(value=bool(settings.get("greyscale_warnings", True)))
        ttk.Checkbutton(
            frame, text="Ostrzegaj o tagach kolorów przy greyscale/monochrome", variable=self.greyscale_var
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1
        self.near_threshold_var = tk.BooleanVar(value=bool(settings.get("near_threshold_suggestions", True)))
        ttk.Checkbutton(
            frame, text="Pokazuj trafne tagi tuż poniżej progu LOWEST", variable=self.near_threshold_var
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1

        ttk.Separator(frame).grid(row=row, column=0, columnspan=2, sticky="ew", pady=12)
        row += 1
        ttk.Label(frame, text="Danbooru API", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        row += 1
        self.cleanup_var = tk.BooleanVar(value=bool(settings.get("danbooru_cleanup", True)))
        ttk.Checkbutton(
            frame,
            text="Waliduj tagi (aliasy + deprecated)",
            variable=self.cleanup_var,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1
        self.implied_var = tk.BooleanVar(value=bool(settings.get("hide_implied_parents", True)))
        ttk.Checkbutton(
            frame,
            text="Ukrywaj implied parent tags",
            variable=self.implied_var,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1
        self.artist_check_var = tk.BooleanVar(value=bool(settings.get("check_blocked_artist", True)))
        ttk.Checkbutton(
            frame,
            text="Sprawdzaj blocked artist dla podanego Artist URL / nazwy przed GPU",
            variable=self.artist_check_var,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1

        self.login_var = tk.StringVar(value=settings.get("danbooru_login", ""))
        ttk.Label(frame, text="Login Danbooru").grid(row=row, column=0, sticky="w", pady=(8, 3))
        ttk.Entry(frame, textvariable=self.login_var, width=34).grid(row=row, column=1, sticky="e", pady=(8, 3))
        row += 1
        self.api_key_var = tk.StringVar(value="")
        ttk.Label(frame, text="API key").grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(frame, textvariable=self.api_key_var, width=34, show="•").grid(row=row, column=1, sticky="e", pady=3)
        row += 1

        test_row = ttk.Frame(frame)
        test_row.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(5, 1))
        test_row.columnconfigure(1, weight=1)
        self.test_api_btn = ttk.Button(test_row, text="Test login + API key", command=self._test_api_credentials)
        self.test_api_btn.grid(row=0, column=0, sticky="w")
        self.api_test_status_var = tk.StringVar(value="Nie testowano.")
        ttk.Label(test_row, textvariable=self.api_test_status_var, foreground="#666666").grid(
            row=0, column=1, sticky="w", padx=(10, 0)
        )
        row += 1

        self.secret_status_var = tk.StringVar(
            value="Klucz zapisany przez Windows DPAPI. Zostaw puste, aby zachować."
            if secret_exists(API_KEY_PATH)
            else "Brak zapisanego klucza. Jest potrzebny do pewnego wykrywania blocked artist."
        )
        ttk.Label(frame, textvariable=self.secret_status_var, foreground="#666666", wraplength=430).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(2, 0)
        )
        row += 1
        ttk.Button(frame, text="Usuń zapisany API key", command=self._delete_key).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        row += 1

        ttk.Label(
            frame,
            text=(
                "Obraz pozostaje lokalny. Danbooru dostaje nazwy kandydatów tagów oraz, "
                "jeśli wpiszesz Artist URL / nazwę, tę wartość do sprawdzenia artist ban."
            ),
            foreground="#666666",
            wraplength=430,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
        row += 1

        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="Anuluj", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Zapisz", command=self._save).pack(side="right", padx=(0, 8))

        self.bind("<Escape>", lambda _e: self.destroy())
        self.after(50, lambda: self.focus_force())

    def _test_api_credentials(self):
        login = self.login_var.get().strip()
        entered_key = self.api_key_var.get().strip()
        api_key = entered_key or load_secret(API_KEY_PATH)

        if not login:
            self.api_test_status_var.set("Brak loginu.")
            return
        if not api_key:
            self.api_test_status_var.set("Brak API key.")
            return

        self.test_api_btn.configure(state="disabled")
        self.api_test_status_var.set("Testuję...")

        def worker():
            client = DanbooruClient(
                DANBOORU_CACHE_PATH,
                timeout=6.0,
                login=login,
                api_key=api_key,
            )
            result = client.test_auth()

            def finish():
                self.test_api_btn.configure(state="normal")
                status = result.get("status")
                if status == "ok":
                    name = result.get("name") or login
                    level = result.get("level_string")
                    suffix = f" ({level})" if level else ""
                    self.api_test_status_var.set(f"OK: {name}{suffix}")
                elif status == "invalid":
                    self.api_test_status_var.set("BŁĄD: login lub API key nieprawidłowy.")
                else:
                    detail = result.get("error") or "nieznany błąd"
                    self.api_test_status_var.set(f"BŁĄD połączenia: {detail}")

            self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def _delete_key(self):
        if messagebox.askyesno("API key", "Usunąć lokalnie zapisany API key?", parent=self):
            delete_secret(API_KEY_PATH)
            self.api_key_var.set("")
            self.secret_status_var.set("Brak zapisanego klucza.")

    def _save(self):
        try:
            high = float(self.high_var.get().replace(",", "."))
            medium = float(self.medium_var.get().replace(",", "."))
            low = float(self.low_var.get().replace(",", "."))
        except ValueError:
            messagebox.showerror("Błąd", "Progi muszą być liczbami, np. 0.75.", parent=self)
            return
        if not (0 <= low < medium < high <= 1):
            messagebox.showerror("Błąd", "Wymagane: 0 ≤ Lowest < Medium < High ≤ 1.", parent=self)
            return
        if not any(var.get() for var in self.category_vars.values()):
            messagebox.showerror("Błąd", "Włącz co najmniej jedną kategorię.", parent=self)
            return

        new_key = self.api_key_var.get().strip()
        if new_key:
            try:
                save_secret(API_KEY_PATH, new_key)
            except Exception as exc:
                messagebox.showerror("API key", f"Nie udało się zapisać klucza przez Windows DPAPI:\n{exc}", parent=self)
                return

        new_settings = {
            "high": high,
            "medium": medium,
            "low": low,
            "categories": {key: var.get() for key, var in self.category_vars.items()},
            "danbooru_cleanup": self.cleanup_var.get(),
            "hide_implied_parents": self.implied_var.get(),
            "blacklist": parse_blacklist(self.blacklist_text.get("1.0", "end-1c")),
            "danbooru_login": self.login_var.get().strip(),
            "check_blocked_artist": self.artist_check_var.get(),
            "gpu_idle_unload_minutes": parse_idle_minutes(self.gpu_idle_var.get()),
            "character_variant_suggestions": self.variant_var.get(),
            "conflict_warnings": self.conflict_var.get(),
            "suggest_translation_request": self.translation_var.get(),
            "unknown_tag_warnings": self.unknown_var.get(),
            "character_context_checks": self.character_context_var.get(),
            "greyscale_warnings": self.greyscale_var.get(),
            "near_threshold_suggestions": self.near_threshold_var.get(),
        }
        self.on_save(new_settings)
        self.destroy()


class DanbooruTaggerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Danbooru Tagger v0.5.1")
        self.geometry("1060x890")
        self.minsize(840, 700)
        self.settings = load_settings()
        self.image_path = None
        self.tagger = None
        self.model_on_cuda = False
        self.model_device_name = None
        self.busy = False
        self.last_activity = time.monotonic()
        self.api_key = load_secret(API_KEY_PATH)
        self.danbooru = DanbooruClient(
            DANBOORU_CACHE_PATH,
            login=self.settings.get("danbooru_login", ""),
            api_key=self.api_key,
        )

        try:
            ttk.Style(self).theme_use("vista")
        except tk.TclError:
            pass

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        root = ttk.Frame(self, padding=18)
        root.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(7, weight=1)
        root.rowconfigure(9, weight=1)
        root.rowconfigure(11, weight=1)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="Danbooru Tagger v0.5.1", font=("Segoe UI", 16, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        self.copy_all_btn = ttk.Button(header, text="Kopiuj ALL", command=self.copy_all, state="disabled")
        self.copy_all_btn.grid(row=0, column=2, sticky="e", padx=(0, 8))
        ttk.Button(header, text="⚙ Ustawienia", command=self.open_settings).grid(row=0, column=3, sticky="e")

        choose = ttk.Frame(root)
        choose.grid(row=1, column=0, sticky="ew", pady=(16, 6))
        choose.columnconfigure(1, weight=1)
        ttk.Button(choose, text="Wybierz obraz", command=self.choose_image).grid(row=0, column=0)
        self.path_var = tk.StringVar(value="Nie wybrano obrazu")
        ttk.Label(choose, textvariable=self.path_var).grid(row=0, column=1, sticky="w", padx=(12, 12))
        self.analyze_btn = ttk.Button(choose, text="ANALIZUJ", command=self.analyze, state="disabled")
        self.analyze_btn.grid(row=0, column=2)

        source = ttk.Frame(root)
        source.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        source.columnconfigure(1, weight=1)
        ttk.Label(source, text="Artist URL / nazwa").grid(row=0, column=0, sticky="w")
        self.source_var = tk.StringVar(value="")
        ttk.Entry(source, textvariable=self.source_var).grid(row=0, column=1, sticky="ew", padx=(12, 8))
        ttk.Button(source, text="Wklej", command=self.paste_source).grid(row=0, column=2)

        status_frame = ttk.Frame(root)
        status_frame.grid(row=3, column=0, sticky="ew", pady=(0, 3))
        status_frame.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value=self._initial_status())
        ttk.Label(status_frame, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(status_frame, mode="determinate", maximum=100, length=220)
        self.progress.grid(row=0, column=1, sticky="e")

        metrics_frame = ttk.Frame(root)
        metrics_frame.grid(row=4, column=0, sticky="ew", pady=(0, 7))
        metrics_frame.columnconfigure(0, weight=1)
        self.metrics_var = tk.StringVar(value="Model: — | GPU: — | Danbooru: — | Razem: —")
        ttk.Label(metrics_frame, textvariable=self.metrics_var, foreground="#555555").grid(
            row=0, column=0, sticky="w"
        )
        self.model_state_var = tk.StringVar(value="Model: niezaładowany")
        ttk.Label(metrics_frame, textvariable=self.model_state_var, foreground="#555555").grid(
            row=0, column=1, sticky="e", padx=(12, 0)
        )

        advice_frame = ttk.Frame(root)
        advice_frame.grid(row=5, column=0, sticky="ew", pady=(0, 7))
        advice_frame.columnconfigure(1, weight=1)
        ttk.Label(advice_frame, text="POMOC", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky="nw", padx=(0, 8), pady=(4, 0)
        )
        self.advice_text = tk.Text(
            advice_frame, height=6, wrap="word", font=("Segoe UI", 9), relief="solid",
            borderwidth=1, padx=6, pady=5
        )
        self.advice_text.grid(row=0, column=1, sticky="ew")
        advice_scroll = ttk.Scrollbar(advice_frame, orient="vertical", command=self.advice_text.yview)
        advice_scroll.grid(row=0, column=2, sticky="ns")
        self.advice_text.configure(yscrollcommand=advice_scroll.set)
        self.advice_text.configure(state="disabled")
        self.set_advice("—")

        self.text_widgets = {}
        self._make_output_section(root, 6, 7, "HIGH CONFIDENCE", "high")
        self._make_output_section(root, 8, 9, "MEDIUM", "medium")
        self._make_output_section(root, 10, 11, "LOWEST", "low")

        ttk.Label(
            root,
            text=(
                "Kopiowanie bucketu: tag1 tag2 tag3  |  ALL: pusta linia / HIGH / pusta / MEDIUM / pusta / LOW / pusta"
            ),
            foreground="#666666",
        ).grid(row=12, column=0, sticky="w", pady=(10, 0))

        self.bind_all("<KeyPress>", self._activity_event, add="+")
        self.bind_all("<Button>", self._activity_event, add="+")
        self.after(15000, self._idle_model_check)

    def set_advice(self, value):
        self.advice_text.configure(state="normal")
        self.advice_text.delete("1.0", "end")
        self.advice_text.insert("1.0", value)
        self.advice_text.configure(state="disabled")

    def _activity_event(self, _event=None):
        self.last_activity = time.monotonic()

    def _idle_model_check(self):
        try:
            timeout_minutes = int(self.settings.get("gpu_idle_unload_minutes", 10) or 0)
            if (
                timeout_minutes > 0
                and self.tagger is not None
                and self.model_on_cuda
                and not self.busy
                and time.monotonic() - self.last_activity >= timeout_minutes * 60
            ):
                self._unload_gpu_model(timeout_minutes)
        finally:
            self.after(15000, self._idle_model_check)

    def _unload_gpu_model(self, timeout_minutes):
        if self.tagger is None or self.busy or not self.model_on_cuda:
            return
        tagger = self.tagger
        self.tagger = None
        self.model_on_cuda = False
        self.model_device_name = None
        try:
            del tagger
            gc.collect()
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                try:
                    torch.cuda.ipc_collect()
                except Exception:
                    pass
        finally:
            self.model_state_var.set(f"Model: zwolniony po {timeout_minutes} min bezczynności")

    def _set_model_state(self, text):
        self.after(0, lambda: self.model_state_var.set(text))

    def _initial_status(self):
        if not (MODEL_DIR / "model.safetensors").exists():
            return "Brak modelu. Uruchom ponownie 1_SETUP.cmd."
        return "Gotowy. Model zostanie załadowany przy pierwszej analizie."

    def _make_output_section(self, parent, label_row, text_row, title, key):
        bar = ttk.Frame(parent)
        bar.grid(row=label_row, column=0, sticky="ew", pady=(4, 4))
        bar.columnconfigure(0, weight=1)
        ttk.Label(bar, text=title, font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Button(bar, text="Kopiuj", command=lambda k=key: self.copy_bucket(k)).grid(row=0, column=1, sticky="e")
        text = tk.Text(
            parent,
            height=5,
            wrap="word",
            font=("Consolas", 10),
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=8,
            undo=False,
        )
        text.grid(row=text_row, column=0, sticky="nsew")
        text.configure(state="disabled")
        self.text_widgets[key] = text

    def choose_image(self):
        path = filedialog.askopenfilename(
            title="Wybierz obraz",
            filetypes=[("Obrazy", "*.png *.jpg *.jpeg *.webp *.bmp"), ("Wszystkie pliki", "*.*")],
        )
        if not path:
            return
        self.image_path = Path(path)
        self.path_var.set(str(self.image_path))
        self.source_var.set("")
        self.analyze_btn.configure(state="normal" if not self.busy else "disabled")
        self.copy_all_btn.configure(state="disabled")
        self.progress["value"] = 0
        self.metrics_var.set("Model: — | GPU: — | Danbooru: — | Razem: —")
        self.set_advice("—")
        self.status_var.set("Obraz wybrany. Opcjonalnie podaj Artist URL / nazwę, potem kliknij ANALIZUJ.")
        self.clear_outputs()

    def paste_source(self):
        try:
            value = self.clipboard_get().strip()
        except tk.TclError:
            value = ""
        if value:
            self.source_var.set(value)

    def open_settings(self):
        SettingsDialog(self, self.settings, self.apply_settings)

    def apply_settings(self, settings):
        self.settings = settings
        save_settings(settings)
        self.api_key = load_secret(API_KEY_PATH)
        self.danbooru.set_auth(settings.get("danbooru_login", ""), self.api_key)
        self.status_var.set(
            f"Ustawienia zapisane. Blacklista: {len(settings.get('blacklist', []))} tagów."
        )

    def clear_outputs(self):
        for key in self.text_widgets:
            self.set_text(key, "")

    def set_text(self, key, value):
        widget = self.text_widgets[key]
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def _bucket_text(self, key):
        return self.text_widgets[key].get("1.0", "end-1c").strip()

    def copy_bucket(self, key):
        text = self._bucket_text(key)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        self.status_var.set(f"Skopiowano {key.upper()} do schowka.")

    def copy_all(self):
        payload = build_copy_all_text(
            self._bucket_text("high"),
            self._bucket_text("medium"),
            self._bucket_text("low"),
        )
        self.clipboard_clear()
        self.clipboard_append(payload)
        self.update_idletasks()
        self.status_var.set("Skopiowano ALL do schowka.")

    def set_stage(self, text, progress_value):
        def update():
            self.status_var.set(text)
            self.progress["value"] = progress_value
        self.after(0, update)

    def set_busy(self, busy):
        self.busy = busy
        self.analyze_btn.configure(state="disabled" if busy or self.image_path is None else "normal")
        if busy:
            self.copy_all_btn.configure(state="disabled")

    def analyze(self):
        if self.busy or self.image_path is None:
            return
        if not (MODEL_DIR / "model.safetensors").exists():
            messagebox.showerror("Brak modelu", "Nie znaleziono modelu PixAI Tagger v1.0. Uruchom 1_SETUP.cmd.")
            return
        self._activity_event()
        self.clear_outputs()
        self.set_advice("—")
        self.metrics_var.set("Model: — | GPU: — | Danbooru: — | Razem: —")
        self.set_busy(True)
        self.set_stage("Start analizy...", 3)
        source_text = self.source_var.get().strip()
        threading.Thread(target=self._analyze_worker, args=(source_text,), daemon=True).start()

    def _load_model(self):
        if self.tagger is not None:
            return self.tagger, 0.0
        # Cold start obejmuje nie tylko samo tworzenie pipeline, ale także koszt
        # pierwszego importu PyTorch/Transformers i inicjalizacji środowiska CUDA.
        # W v0.4.0 ten czas był błędnie raportowany jako "Wczytywanie obrazu".
        started = time.perf_counter()
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"

        self._set_model_state("Model: inicjalizacja...")
        self.set_stage("Inicjalizacja PyTorch / środowiska modelu...", 14)
        import torch

        self.set_stage("Inicjalizacja Transformers...", 18)
        from transformers import pipeline

        device = 0 if torch.cuda.is_available() else -1
        self._set_model_state("Model: ładowanie...")
        self.set_stage(
            "Ładowanie PixAI Tagger v1.0 na GPU..." if device == 0 else "Ładowanie PixAI Tagger v1.0 na CPU...",
            22,
        )
        self.tagger = pipeline(
            model=str(MODEL_DIR),
            image_processor=str(MODEL_DIR),
            trust_remote_code=True,
            device=device,
        )
        load_seconds = time.perf_counter() - started
        self.model_on_cuda = device == 0
        if self.model_on_cuda:
            try:
                self.model_device_name = torch.cuda.get_device_name(0)
            except Exception:
                self.model_device_name = "CUDA"
            self._set_model_state(f"Model: GPU ({self.model_device_name})")
        else:
            self.model_device_name = "CPU"
            self._set_model_state("Model: CPU")
        return self.tagger, load_seconds

    def _blocked_artist_precheck(self, source_text):
        if not self.settings.get("check_blocked_artist", True) or not source_text.strip():
            return {"status": "skipped", "artists": [], "banned": []}, 0.0
        self.set_stage("Danbooru: sprawdzam blocked artist dla Artist URL / nazwy...", 10)
        started = time.perf_counter()
        report = self.danbooru.check_artist_source(source_text)
        return report, time.perf_counter() - started

    def _analyze_worker(self, source_text):
        total_start = time.perf_counter()
        model_load_time = 0.0
        gpu_time = 0.0
        danbooru_time = 0.0
        self.danbooru.reset_run_state()
        try:
            from PIL import Image

            self.set_stage("Wczytywanie obrazu...", 6)
            with Image.open(self.image_path) as source_image:
                width, height = source_image.size
                image = source_image.convert("RGB")

            artist_report, artist_seconds = self._blocked_artist_precheck(source_text)
            danbooru_time += artist_seconds

            if artist_report.get("status") == "blocked":
                names = ", ".join(row.get("name", "?") for row in artist_report.get("banned", []))
                total_time = time.perf_counter() - total_start

                def blocked_finish():
                    self.set_busy(False)
                    self.progress["value"] = 100
                    stats = self.danbooru.run_stats()
                    self.metrics_var.set(
                        f"Model: 0.00 s | GPU: 0.00 s | Danbooru: {danbooru_time:.2f} s "
                        f"(cache {stats['cache_hits']} / API {stats['api_requests']}) | Razem: {total_time:.2f} s"
                    )
                    self.status_var.set(f"STOP: blocked artist na Danbooru: {names}")
                    messagebox.showwarning(
                        "Blocked artist",
                        f"Danbooru oznacza artystę jako blocked/banned:\n\n{names}\n\nAnaliza GPU została pominięta.",
                    )
                self.after(0, blocked_finish)
                return

            auth_note = ""
            if artist_report.get("status") == "auth_missing" and source_text:
                auth_note = "Blocked artist check pominięty: brak login/API key. "
            elif artist_report.get("status") == "error" and source_text:
                auth_note = "Blocked artist check nieudany. "

            tagger, model_load_time = self._load_model()
            self.set_stage("GPU: PixAI analizuje obraz...", 48)
            low_threshold = float(self.settings["low"])
            near_threshold_enabled = bool(self.settings.get("near_threshold_suggestions", True))
            model_threshold = max(0.05, low_threshold - 0.07) if near_threshold_enabled else low_threshold
            gpu_start = time.perf_counter()
            raw = tagger(image, threshold=model_threshold)
            gpu_time = time.perf_counter() - gpu_start
            results = raw.get("results", raw)

            all_items = flatten_scores(results, self.settings["categories"])
            all_items = hide_resolution_autotags(all_items)
            near_items = select_near_threshold_candidates(
                all_items, low_threshold, margin=0.07, limit=8,
                blacklist=self.settings.get("blacklist", []),
            ) if near_threshold_enabled else []
            items = [(tag, score) for tag, score in all_items if score >= low_threshold]

            cleanup_report = None
            if self.settings.get("danbooru_cleanup", True):
                self.set_stage("Danbooru: aliasy, deprecated i implications...", 70)
                api_start = time.perf_counter()
                items, cleanup_report = self.danbooru.clean_items(
                    items,
                    hide_implied_parents=bool(self.settings.get("hide_implied_parents", True)),
                    reset_state=False,
                )
                danbooru_time += time.perf_counter() - api_start

            self.set_stage("Postprocessing: blacklista i reguły...", 82)
            items, blacklist_removed = apply_user_blacklist(items, self.settings.get("blacklist", []))
            items, rule_removed = apply_deterministic_rules(items)

            final_tags = [tag for tag, _score in items]
            related_map = {}
            cooccurrence_map = {}
            unknown_tags = []
            copyright_warnings = []
            coverage = None
            greyscale_tags = []
            near_threshold_tags = []
            character_copyrights = {}
            tag_info = {}

            needs_tag_info = final_tags and any((
                self.settings.get("character_variant_suggestions", True),
                self.settings.get("unknown_tag_warnings", True),
                self.settings.get("character_context_checks", True),
                self.settings.get("greyscale_warnings", True),
                bool(near_items),
            ))
            if needs_tag_info:
                self.set_stage("Danbooru: sanity check tagów i postaci...", 88)
                sanity_start = time.perf_counter()
                tag_info = self.danbooru.get_tag_info(final_tags)
                danbooru_time += time.perf_counter() - sanity_start

            if near_items:
                self.set_stage("Danbooru: sprawdzam sugestie spod progu...", 89)
                near_start = time.perf_counter()
                near_names = [tag for tag, _score in near_items]
                near_info = self.danbooru.get_tag_info(near_names)
                final_set = set(final_tags)
                near_threshold_tags = [
                    tag for tag, _score in near_items
                    if tag not in final_set
                    and (near_info.get(tag) or {}).get("exists")
                    and not (near_info.get(tag) or {}).get("deprecated")
                ][:6]
                danbooru_time += time.perf_counter() - near_start

            character_tags = [
                tag for tag in final_tags
                if (tag_info.get(tag) or {}).get("exists")
                and (tag_info.get(tag) or {}).get("category") == 4
            ]

            if self.settings.get("unknown_tag_warnings", True):
                unknown_tags = [
                    tag for tag in final_tags
                    if tag in tag_info and not (tag_info.get(tag) or {}).get("exists")
                ]

            if self.settings.get("character_context_checks", True) and tag_info:
                coverage = find_character_coverage(final_tags, tag_info)
                if character_tags:
                    self.set_stage("Danbooru: sprawdzam character/copyright...", 90)
                    cp_start = time.perf_counter()
                    character_copyrights = self.danbooru.get_character_copyrights(
                        character_tags[:4], limit_per_character=5
                    )
                    danbooru_time += time.perf_counter() - cp_start
                copyright_warnings = find_copyright_sanity_warnings(
                    final_tags, tag_info, character_copyrights
                )

            if self.settings.get("greyscale_warnings", True):
                greyscale_tags = find_greyscale_color_warnings(final_tags, tag_info)

            if self.settings.get("character_variant_suggestions", True) and character_tags:
                self.set_stage("Danbooru: szukam wariantów character tags...", 91)
                related_start = time.perf_counter()
                related_map = self.danbooru.get_character_related(character_tags[:4], limit_per_character=5)
                danbooru_time += time.perf_counter() - related_start

            if (
                self.settings.get("character_context_checks", True)
                and coverage
                and character_tags
            ):
                self.set_stage("Danbooru: szukam postaci występujących razem...", 94)
                related_start = time.perf_counter()
                cooccurrence_map = self.danbooru.get_character_cooccurrence(
                    character_tags[:2], limit_per_character=8
                )
                final_set = set(final_tags)
                cooccurrence_map = {
                    source: [tag for tag in suggestions if tag not in final_set]
                    for source, suggestions in cooccurrence_map.items()
                    if suggestions
                }
                danbooru_time += time.perf_counter() - related_start

            advice_text = build_advice_text(
                final_tags,
                related_map=related_map,
                cooccurrence_map=cooccurrence_map,
                unknown_tags=unknown_tags,
                copyright_warnings=copyright_warnings,
                coverage=coverage,
                greyscale_tags=greyscale_tags,
                near_threshold_tags=near_threshold_tags,
                translation_enabled=bool(self.settings.get("suggest_translation_request", True)),
                conflicts_enabled=bool(self.settings.get("conflict_warnings", True)),
            )

            self.set_stage("Przygotowuję wyniki...", 96)
            buckets = split_by_confidence(
                items,
                float(self.settings["high"]),
                float(self.settings["medium"]),
                low_threshold,
            )
            rendered = {key: " ".join(tags) for key, tags in buckets.items()}
            total_time = time.perf_counter() - total_start

            try:
                import torch
                device_name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
            except Exception:
                device_name = "urządzenie"

            if cleanup_report is not None:
                cleanup_report["network_failed"] = self.danbooru.network_failed
                cleanup_report["used_stale_cache"] = self.danbooru.used_stale_cache
                cleanup_report.update(self.danbooru.run_stats())
            extra = format_cleanup_summary(cleanup_report, blacklist_removed, rule_removed)
            stats = self.danbooru.run_stats()
            artist_note = ""
            if artist_report.get("status") == "ok":
                names = ", ".join(row.get("name", "?") for row in artist_report.get("artists", []))
                artist_note = f" Artist OK: {names}." if names else " Artist check OK."
            elif artist_report.get("status") == "not_found" and source_text:
                artist_note = " Artist nieznany w bazie Danbooru."

            def finish():
                for key, text in rendered.items():
                    self.set_text(key, text)
                count = sum(len(v) for v in buckets.values())
                status = f"{auth_note}Gotowe: {count} tagów. {width}×{height}. Urządzenie: {device_name}."
                if extra:
                    status += f" {extra}."
                status += artist_note
                self.set_advice(advice_text)
                self.set_busy(False)
                self.progress["value"] = 100
                self.status_var.set(status)
                self.metrics_var.set(
                    f"Model: {model_load_time:.2f} s | GPU: {gpu_time:.2f} s | "
                    f"Danbooru: {danbooru_time:.2f} s (cache {stats['cache_hits']} / API {stats['api_requests']}) | "
                    f"Razem: {total_time:.2f} s"
                )
                self.copy_all_btn.configure(state="normal" if count else "disabled")
                self.last_activity = time.monotonic()

            self.after(0, finish)
        except Exception as exc:
            details = traceback.format_exc()
            (APP_DIR / "error.log").write_text(details, encoding="utf-8")
            total_time = time.perf_counter() - total_start

            def fail():
                self.set_busy(False)
                self.progress["value"] = 0
                stats = self.danbooru.run_stats()
                self.metrics_var.set(
                    f"Model: {model_load_time:.2f} s | GPU: {gpu_time:.2f} s | "
                    f"Danbooru: {danbooru_time:.2f} s (cache {stats['cache_hits']} / API {stats['api_requests']}) | "
                    f"Razem: {total_time:.2f} s"
                )
                self.status_var.set("Błąd analizy. Szczegóły zapisano w error.log.")
                messagebox.showerror(
                    "Błąd",
                    f"Nie udało się przeanalizować obrazu.\n\n{exc}\n\nSzczegóły: error.log",
                )
            self.after(0, fail)


if __name__ == "__main__":
    if not acquire_single_instance():
        show_already_running_message()
        raise SystemExit(0)
    app = DanbooruTaggerApp()
    app.mainloop()
