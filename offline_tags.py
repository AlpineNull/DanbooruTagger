"""Read-only, optional SQLite snapshot of Danbooru tag metadata.

The downloaded database is from hlibr/danbooru-tag-metadata-snapshot,
not from a live Danbooru API call. Neither the application nor this module
modifies it. Missing entries in a dated snapshot are NOT proof a tag is invalid.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SNAPSHOT_DATE = datetime.fromisoformat('2026-04-08T09:15:30+00:00').timestamp()
SOURCE_URL = 'https://huggingface.co/datasets/hlibr/danbooru-tag-metadata-snapshot'


class OfflineTags:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.error = None
        self.ready = False
        if self.path.is_file():
            try:
                with closing(self._connect()) as db:
                    required = {
                        'tags': {'name', 'is_deprecated', 'category', 'post_count'},
                        'tag_aliases': {'antecedent_name', 'consequent_name', 'status'},
                        'tag_implications': {'antecedent_name', 'consequent_name', 'status'},
                    }
                    for table, columns in required.items():
                        found = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
                        if not columns <= found:
                            raise ValueError(f'Niepoprawny schemat tabeli {table}')
                    db.execute('SELECT name FROM tags LIMIT 1').fetchone()
                self.ready = True
            except (sqlite3.Error, OSError, ValueError) as exc:
                self.error = str(exc)

    def _connect(self):
        return sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)

    @staticmethod
    def _unique(names):
        return [v for v in dict.fromkeys(names) if isinstance(v, str) and v]

    def tags(self, names):
        names = self._unique(names)
        if not self.ready or not names:
            return {}
        result = {}
        try:
            with closing(self._connect()) as db:
                for i in range(0, len(names), 300):
                    chunk = names[i:i+300]
                    marks = ','.join('?' for _ in chunk)
                    for name, category, count, deprecated in db.execute(
                        f'SELECT name, category, post_count, is_deprecated FROM tags WHERE name IN ({marks})', chunk
                    ):
                        result[name] = {'exists': True, 'category': category, 'post_count': count,
                                        'deprecated': bool(deprecated)}
        except sqlite3.Error as exc:
            self.error = str(exc)
            self.ready = False
            return {}
        return result

    def aliases(self, names):
        names = self._unique(names)
        if not self.ready or not names:
            return {}
        result = {}
        try:
            with closing(self._connect()) as db:
                for i in range(0, len(names), 300):
                    chunk = names[i:i+300]
                    marks = ','.join('?' for _ in chunk)
                    rows = db.execute(
                        f"SELECT antecedent_name, consequent_name FROM tag_aliases WHERE status='active' AND antecedent_name IN ({marks})", chunk
                    )
                    for name, canonical in rows:
                        result[name] = {'canonical': canonical}
        except sqlite3.Error as exc:
            self.error = str(exc)
            self.ready = False
            return {}
        return result

    def implications(self, names):
        names = self._unique(names)
        if not self.ready or not names:
            return {}
        result = {}
        try:
            with closing(self._connect()) as db:
                for i in range(0, len(names), 300):
                    chunk = names[i:i+300]
                    marks = ','.join('?' for _ in chunk)
                    rows = db.execute(
                        f"SELECT antecedent_name, consequent_name FROM tag_implications WHERE status='active' AND antecedent_name IN ({marks})", chunk
                    )
                    for name, parent in rows:
                        result.setdefault(name, {'parents': []})['parents'].append(parent)
        except sqlite3.Error as exc:
            self.error = str(exc)
            self.ready = False
            return {}
        for v in result.values():
            v['parents'] = sorted(set(v['parents']))
        return result

    def character_variants(self, character_name: str, limit: int = 8):
        """Advisory costume/variant suggestions from the local snapshot only.

        Constrain suggestions to the same _(copyright) suffix when present.
        This deliberately avoids expensive broad fuzzy matching and online calls.
        """
        if not self.ready or not character_name:
            return []
        name = str(character_name).strip()
        marker = name.rfind('_(')
        suffix = name[marker:] if marker > 0 and name.endswith(')') else ''
        base = name[:marker] if suffix else name
        if not base:
            return []
        prefixes = [base]
        if 6 <= len(base.replace('_', '')) <= 18 and len(base) >= 5:
            prefixes.append(base[:-1])
        found = {}
        try:
            with closing(self._connect()) as db:
                for rank, prefix in enumerate(dict.fromkeys(prefixes)):
                    escaped = (prefix.replace('\\', '\\\\').replace('%', '\\%')
                               .replace('_', '\\_'))
                    rows = db.execute(
                        "SELECT name, post_count FROM tags "
                        "WHERE category = 4 AND is_deprecated = 0 AND post_count > 0 "
                        "AND name LIKE ? ESCAPE '\\' ORDER BY post_count DESC LIMIT 250",
                        (escaped + '%',),
                    )
                    for candidate, count in rows:
                        if candidate == name:
                            continue
                        if suffix and not candidate.endswith(suffix):
                            continue
                        found[candidate] = (min(rank, found.get(candidate, (rank, 0))[0]), int(count or 0))
        except sqlite3.Error as exc:
            self.error = str(exc)
            return []
        return sorted(found, key=lambda x: (found[x][0], -found[x][1], x))[:limit]
