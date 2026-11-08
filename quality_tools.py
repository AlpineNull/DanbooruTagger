"""Danbooru Tagger: helpers for a conservative quality/diagnostics build.

Does not load, replace, or reconfigure the tagging model.
Requires explicit integration with the existing GUI and inference path.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif', '.tif', '.tiff'}
_WATCH_TAGS = ('standing', 'full_body', 'lying', 'upper_body', 'dragon_tail',
               'one_eye_closed', 'teeth', 'tank', 'hand_up', 'sketch')
_TAG_COUNT = re.compile(r'^(\d+)\+?(girl|girls|boy|boys|other|others)$')
_MULTIPLE = {'multiple_girls': 'girl', 'multiple_boys': 'boy', 'multiple_others': 'other'}


def normalize_tag(tag: object) -> str:
    return str(tag).strip().lower().replace(' ', '_')


def validate_image_path(path: str | os.PathLike, *, inspect_content: bool = False) -> tuple[bool, str]:
    """Cheap validation for UI; optional Pillow validation before starting worker.

    No model is initialized and no GPU work is performed.
    """
    if not path or not str(path).strip():
        return False, 'Nie wybrano obrazu.'
    candidate = Path(path).expanduser()
    if candidate.suffix.lower() not in _IMAGE_EXTS:
        return False, 'Nieobsługiwane rozszerzenie obrazu.'
    if not candidate.is_file():
        return False, 'Plik obrazu nie istnieje.'
    if not inspect_content:
        return True, ''
    try:
        from PIL import Image
        with Image.open(candidate) as img:
            img.verify()
    except (ImportError, OSError, ValueError) as exc:
        return False, f'Nie można odczytać obrazu: {exc}'
    return True, ''


class AnalyzeButtonController:
    """Keep Tkinter analyze button disabled if path is invalid or analysis busy.

    Pass StringVar to auto-refresh after file selection; use set_busy() when
    worker starts/finishes. All controller methods must run on Tk's main thread.
    """
    def __init__(self, button, image_path_var, *, is_busy: Callable[[], bool] | None = None):
        self.button = button
        self.image_path_var = image_path_var
        self._busy = False
        self._external_busy = is_busy
        self._trace_id = image_path_var.trace_add('write', self._on_path_changed)
        self.refresh()

    def _on_path_changed(self, *_args):
        self.refresh()

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self.refresh()

    def refresh(self) -> bool:
        valid, _reason = validate_image_path(self.image_path_var.get())
        busy = self._busy or (bool(self._external_busy()) if self._external_busy else False)
        self.button.configure(state='normal' if valid and not busy else 'disabled')
        return valid and not busy

    def detach(self) -> None:
        self.image_path_var.trace_remove('write', self._trace_id)


class BlacklistStore:
    """Editable, persistent, exact-match tag blacklist. JSON is saved atomically."""
    def __init__(self, filename: str | os.PathLike, *, initial_tags: Iterable[str] = ()): 
        self.filename = Path(filename)
        self.tags = set(map(normalize_tag, initial_tags)) - {''}
        if self.filename.is_file():
            self.load()

    def load(self) -> set[str]:
        data = json.loads(self.filename.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('tags'), list):
            raise ValueError('Błędny format blacklisty: oczekiwano {"tags": [...]}.')
        self.tags = {normalize_tag(t) for t in data['tags'] if isinstance(t, str) and t.strip()}
        return set(self.tags)

    def replace_from_text(self, text: str) -> None:
        """One tag per line; do not split on commas or underscores."""
        proposed = set()
        for line in text.splitlines():
            tag = normalize_tag(line)
            if not tag or tag.startswith('#'):
                continue
            if any(ch.isspace() for ch in tag) or ',' in tag:
                raise ValueError(f'Nieprawidłowy tag blacklisty: {line!r}')
            proposed.add(tag)
        self.tags = proposed
        self.save()

    def save(self) -> None:
        self.filename.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({'version': 1, 'tags': sorted(self.tags)},
                             ensure_ascii=False, indent=2) + '\n'
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                             dir=self.filename.parent, prefix='.blacklist_',
                                             suffix='.tmp', delete=False) as stream:
                temp_path = Path(stream.name)
                stream.write(payload)
            os.replace(temp_path, self.filename)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def allowed(self, tag: str) -> bool:
        return normalize_tag(tag) not in self.tags

    def filter_tags(self, tags: Iterable[str]) -> list[str]:
        return [t for t in tags if self.allowed(t)]


def show_blacklist_editor(parent, store: BlacklistStore):
    """Tkinter dialog; changes are committed only via Save."""
    import tkinter as tk
    from tkinter import messagebox
    window = tk.Toplevel(parent)
    window.title('Blacklist tagów')
    window.geometry('540x450')
    window.transient(parent)
    window.grab_set()
    tk.Label(window, text='Jeden tag w wierszu. Lista zastępuje dotychczasową blacklistę.').pack(
        anchor='w', padx=12, pady=(10, 4))
    editor = tk.Text(window, font=('Consolas', 11), undo=True, wrap='none')
    editor.pack(fill='both', expand=True, padx=12, pady=6)
    editor.insert('1.0', '\n'.join(sorted(store.tags)))
    footer = tk.Frame(window)
    footer.pack(fill='x', padx=12, pady=(4, 12))

    def save():
        try:
            store.replace_from_text(editor.get('1.0', 'end-1c'))
        except (ValueError, OSError) as exc:
            messagebox.showerror('Blacklist', str(exc), parent=window)
            return
        window.destroy()

    tk.Button(footer, text='Anuluj', command=window.destroy).pack(side='right', padx=4)
    tk.Button(footer, text='Zapisz', command=save).pack(side='right', padx=4)
    editor.focus_set()
    return window


def normalize_scores(raw: Mapping[str, float] | Iterable[tuple[str, float]] | Iterable[Mapping]) -> dict[str, float]:
    """Accept raw model probabilities as dict, pairs, or {'tag','score'} records.

    *Never* invent scores for tags absent from the model's returned output.
    The caller must supply model output BEFORE threshold/filter/implications.
    """
    if isinstance(raw, Mapping):
        rows = raw.items()
    else:
        rows = []
        for row in raw:
            if isinstance(row, Mapping):
                tag = row.get('tag', row.get('name'))
                score = row.get('score', row.get('confidence'))
                rows.append((tag, score))
            else:
                rows.append(row)
    normalized: dict[str, float] = {}
    for tag, score in rows:
        if not isinstance(tag, str) or isinstance(score, bool):
            raise ValueError('Oczekiwano par: (nazwa tagu, prawdopodobieństwo).')
        try:
            score = float(score)
        except (ValueError, TypeError) as exc:
            raise ValueError(f'Nieprawidłowy score tagu {tag!r}.') from exc
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ValueError(f'Prawdopodobieństwo poza zakresem 0..1: {tag!r}')
        key = normalize_tag(tag)
        if key:
            normalized[key] = max(score, normalized.get(key, 0.0))
    if not normalized:
        raise ValueError('Brak surowych wyników modelu; eksport pominięty.')
    return normalized


def export_confidences(raw, image_path: str | os.PathLike, output_dir: str | os.PathLike,
                       *, watch_tags: Iterable[str] = _WATCH_TAGS,
                       include_timestamp: bool = True) -> tuple[Path, Path]:
    """Write CSV and TXT; notes missing watched tags without pretending score=0."""
    scores = normalize_scores(raw)
    dest = Path(output_dir)
    dest.mkdir(parents=True, exist_ok=True)
    name = Path(image_path).stem
    if include_timestamp:
        name += '_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    csv_path = dest / (name + '_confidence.csv')
    txt_path = dest / (name + '_watch.txt')
    ordered = sorted(scores.items(), key=lambda t: (-t[1], t[0]))
    with csv_path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(('tag', 'raw_confidence'))
        for tag, confidence in ordered:
            writer.writerow((tag, f'{confidence:.10f}'))
    with txt_path.open('w', encoding='utf-8') as stream:
        stream.write(f'Obraz: {Path(image_path).name}\n')
        stream.write(f'Liczba dostępnych wyników: {len(scores)}\n')
        stream.write('N/D oznacza brak wyniku, a nie prawdopodobieństwo 0.\n\n')
        for tag in watch_tags:
            key = normalize_tag(tag)
            stream.write(f'{key:28s} {scores[key]:.10f}\n' if key in scores
                         else f'{key:28s} N/D\n')
    return csv_path, txt_path


@dataclass(frozen=True)
class CharacterCount:
    minimum: int | None
    ambiguous: bool
    reason: str
    warning: str | None


def estimate_character_count(count_tags: Iterable[str], *, distinct_character_tags: Iterable[str] = (),
                             multiple_views: bool = False) -> CharacterCount:
    """Conservatively interpret gender count tags without summing alternatives.

    Caller supplies trustworthy count tags only (e.g. HIGH/MEDIUM) and canonical
    distinct character IDs; low-confidence and contradictory counts lead to NO
    numerical 'missing characters' warning rather than an invented count.
    """
    counts: dict[str, set[int]] = {'girl': set(), 'boy': set(), 'other': set()}
    uncertain: set[str] = set()
    tagset = {normalize_tag(t) for t in count_tags}
    for tag in tagset:
        match = _TAG_COUNT.fullmatch(tag)
        if match:
            number = int(match.group(1))
            category = match.group(2).rstrip('s')
            if number > 0:
                counts[category].add(number)
        elif tag in _MULTIPLE:
            uncertain.add(_MULTIPLE[tag])
    conflicts = [category for category, ns in counts.items() if len(ns) > 1]
    if conflicts:
        return CharacterCount(None, True,
                              'Sprzeczne liczniki: ' + ', '.join(conflicts), None)
    if 'solo' in tagset and any(max(ns, default=0) > 1 for ns in counts.values()) and not multiple_views:
        return CharacterCount(None, True, 'Sprzeczność solo z licznikiem postaci.', None)
    if multiple_views:
        return CharacterCount(None, True, 'multiple_views: liczba przedstawień nie oznacza liczby unikalnych osób.', None)
    total = 0
    for category, ns in counts.items():
        current = next(iter(ns), 0)
        if category in uncertain:
            if current == 1:
                return CharacterCount(None, True,
                                      f'Sprzeczność multiple_{category}s i 1{category}.', None)
            current = max(current, 2)
        total += current
    if total == 0:
        return CharacterCount(None, True, 'Brak wiarygodnych liczników postaci.', None)
    unique = {normalize_tag(t) for t in distinct_character_tags if normalize_tag(t)}
    warning = (f'Możliwe brakujące character tags: {len(unique)} rozpoznanych przy co najmniej {total} postaciach.'
               if len(unique) < total else None)
    return CharacterCount(total, False, 'Dolne ograniczenie na podstawie niesprzecznych tagów.', warning)
