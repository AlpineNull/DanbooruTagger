import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from offline_tags import OfflineTags, SNAPSHOT_DATE

API_BASE = "https://danbooru.donmai.us"
CACHE_VERSION = 5
CACHE_TTL_SECONDS = 24 * 60 * 60
TAG_POSITIVE_TTL_SECONDS = 90 * 24 * 60 * 60
TAG_NEGATIVE_TTL_SECONDS = 30 * 24 * 60 * 60
USER_AGENT = "DanbooruTagger/0.7.2 (local personal tagging helper)"


class DanbooruClient:
    """Danbooru API client: read-only SQLite + backwards-compatible JSON cache.

    Tag cleanup sends tag names only. Artist-ban checking sends only the Source
    URL (or artist name) entered by the user. The analyzed image is never sent.
    """

    def __init__(self, cache_path: Path, timeout: float = 5.0, login: str = "", api_key: str = ""):
        self.cache_path = Path(cache_path)
        self.timeout = timeout
        self.login = login or ""
        self.api_key = api_key or ""
        self.network_failed = False
        self.used_stale_cache = False
        self.last_error = None
        self.cache_hits = 0
        self.stale_cache_hits = 0
        self.api_requests = 0
        self.cache = self._load_cache()
        self.offline = OfflineTags(self.cache_path.parent / "danbooru_tags.sqlite")
        # LOCAL by default: ordinary analysis never contacts Danbooru.
        # HYBRID explicitly allows missing/expired metadata to use the official API.
        self.network_mode = 'local'
        self._explicit_refresh = False
        self._related_payload_cache = {}
        self.offline_hits = 0
        self.conflicts = set()
        self.cloudflare_challenge = False
        self._retry_after = 0.0
        self._last_request_at = 0.0
        self._rate_lock = threading.Lock()

    def set_auth(self, login: str, api_key: str):
        self.login = (login or "").strip()
        self.api_key = api_key or ""

    @property
    def is_authenticated(self):
        return bool(self.login and self.api_key)

    def reset_run_state(self):
        self.network_failed = time.time() < self._retry_after
        self.cloudflare_challenge = self.network_failed
        self.used_stale_cache = False
        self.last_error = "Cloudflare Challenge (cooldown)" if self.network_failed else None
        self.cache_hits = 0
        self.stale_cache_hits = 0
        self.offline_hits = 0
        self.conflicts = set()
        self.api_requests = 0
        self._related_payload_cache = {}

    def run_stats(self):
        return {
            "cache_hits": int(self.cache_hits),
            "stale_cache_hits": int(self.stale_cache_hits),
            "api_requests": int(self.api_requests),
            "network_mode": self.network_mode,
            "offline_hits": int(self.offline_hits),
            "offline_ready": bool(self.offline.ready),
            "cloudflare_challenge": bool(self.cloudflare_challenge),
            "source_conflicts": sorted(self.conflicts),
        }

    def _empty_cache(self):
        return {
            "version": CACHE_VERSION,
            "tags": {},
            "aliases": {},
            "implications": {},
            "character_related": {},
            "character_cooccurrence": {},
            "character_copyrights": {},
        }

    def _load_cache(self):
        empty = self._empty_cache()
        if not self.cache_path.exists():
            return empty
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            version = data.get("version")
            if version not in (2, 3, 4, CACHE_VERSION):
                return empty
            for key in ("tags", "aliases", "implications", "character_related", "character_cooccurrence", "character_copyrights"):
                if not isinstance(data.get(key), dict):
                    data[key] = {}
            data["version"] = CACHE_VERSION
            return data
        except Exception:
            return empty

    def save(self):
        try:
            self.cache_path.write_text(
                json.dumps(self.cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    @staticmethod
    def _chunks(values, size=25):
        values = list(dict.fromkeys(values))
        for i in range(0, len(values), size):
            yield values[i : i + size]

    @staticmethod
    def _fresh(entry, section=None):
        try:
            age = time.time() - float(entry.get("checked_at", 0))
            if section == "tags":
                ttl = TAG_POSITIVE_TTL_SECONDS if entry.get("exists") else TAG_NEGATIVE_TTL_SECONDS
                return age < ttl
            return age < CACHE_TTL_SECONDS
        except Exception:
            return False

    @staticmethod
    def _material_values(section, record):
        if record is None:
            return None
        if section == 'tags':
            return (record.get('exists'), record.get('deprecated') if record.get('exists') else None,
                    record.get('category') if record.get('exists') else None)
        if section == 'aliases':
            return record.get('canonical')
        if section == 'implications':
            return tuple(sorted(record.get('parents') or []))
        return None

    def _combine(self, section, names, cached, snapshot):
        """A newer direct API record wins; unresolved disagreements never auto-correct.

        The snapshot date is its data collection time, not download time.
        A missing tag from the old snapshot is never treated as proof of absence.
        """
        merged = {}
        for name in names:
            old = cached.get(name)
            new = snapshot.get(name)
            if new is None:
                if old is not None:
                    merged[name] = dict(old)
                    merged[name]['_source'] = 'json'
                continue
            self.offline_hits += 1
            if old is None:
                merged[name] = dict(new, _source='sqlite')
                continue
            old_date = float(old.get('checked_at') or 0)
            differs = self._material_values(section, old) != self._material_values(section, new)
            if old_date > SNAPSHOT_DATE:
                merged[name] = dict(old, _source='json')
            elif differs:
                merged[name] = dict(new, _source='sqlite', _conflict=True)
                self.conflicts.add(f'{section}: {name}')
            else:
                merged[name] = dict(new, _source='sqlite')
        return merged

    def _offline_records(self, section, names):
        names = list(dict.fromkeys(names))
        if not self.offline.ready:
            return {}
        if section == 'tags':
            return self.offline.tags(names)
        # No alias/implication in the snapshot counts as a known negative ONLY
        # for canonical tags that are actually present in that snapshot.
        known = set(self.offline.tags(names))
        found = self.offline.aliases(names) if section == 'aliases' else self.offline.implications(names)
        for tag in known - set(found):
            found[tag] = {'canonical': None} if section == 'aliases' else {'parents': []}
        return found

    def _resolve_offline(self, section, names, cached):
        return self._combine(section, names, cached, self._offline_records(section, names))

    def _online_allowed(self):
        return self.network_mode == 'hybrid' or self._explicit_refresh

    def _should_fetch(self, section, names, resolved):
        # No implicit tag/alias/implication traffic in default local mode.
        if not self._online_allowed():
            return []
        if self._explicit_refresh:
            return list(dict.fromkeys(names))
        # Hybrid mode checks only genuinely unknown or expired JSON-only entries.
        return [name for name in names
                if name not in resolved or
                (resolved[name].get('_source') == 'json'
                 and not self._fresh(resolved[name], section))]

    def refresh_metadata(self, tag_names):
        """Explicit, user-initiated official API refresh; no background polling.

        Updates only the existing local JSON cache, never the SQLite snapshot.
        After completion the GUI can re-analyze to apply new metadata.
        """
        names = list(dict.fromkeys(str(n).strip() for n in tag_names if str(n).strip()))
        if not names:
            return {'status': 'empty', 'api_requests': 0}
        self.reset_run_state()
        if self.network_failed:
            return {'status': 'blocked', 'api_requests': 0, 'error': self.last_error}
        self._explicit_refresh = True
        try:
            for getter in (self.get_tag_info, self.get_aliases, self.get_implications):
                if self.network_failed:
                    break
                getter(names)
            self.save()
        finally:
            self._explicit_refresh = False
        return {
            'status': 'blocked' if self.cloudflare_challenge else
                      'error' if self.network_failed else 'ok',
            'api_requests': self.api_requests,
            'error': self.last_error,
        }

    def _related_frequent(self, name):
        """Share /related_tag.json response between co-occurrence and copyright checks."""
        if name in self._related_payload_cache:
            return self._related_payload_cache[name]
        result = self._get_json('/related_tag.json', {
            'search[query]': name, 'type': 'frequent',
        })
        self._related_payload_cache[name] = result
        return result

    def _note_http_failure(self, exc):
        self.last_error = str(exc)
        if isinstance(exc, urllib.error.HTTPError):
            if exc.code == 403 and exc.headers.get('Cf-Mitigated', '').lower() == 'challenge':
                self.cloudflare_challenge = True
                self.last_error = 'HTTP 403: Cloudflare Challenge (to nie oznacza błędnego klucza API)'
                self._retry_after = time.time() + 1800
            elif exc.code == 429:
                self._retry_after = time.time() + 300
        self.network_failed = True

    def _auth_header(self):
        if not self.is_authenticated:
            return None
        token = base64.b64encode(f"{self.login}:{self.api_key}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"

    def _get_json(self, path, params, *, ignore_failed_state=False, mark_failed_state=True):
        if (self.network_failed or time.time() < self._retry_after):
            raise OSError("Danbooru API unavailable (or Cloudflare cooldown)")
        query = urllib.parse.urlencode(params)
        url = f"{API_BASE}{path}?{query}" if query else f"{API_BASE}{path}"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        auth = self._auth_header()
        if auth:
            headers["Authorization"] = auth
        req = urllib.request.Request(url, headers=headers)
        with self._rate_lock:
            remaining = 1.0 - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
            self._last_request_at = time.monotonic()
        self.api_requests += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            self._note_http_failure(exc)
            raise

    def _get_cached_or_missing(self, section, names):
        result = {}
        missing = []
        bucket = self.cache[section]
        for name in names:
            entry = bucket.get(name)
            if entry is not None and self._fresh(entry, section):
                result[name] = entry
                self.cache_hits += 1
            else:
                if entry is not None:
                    result[name] = entry
                    self.used_stale_cache = True
                    self.stale_cache_hits += 1
                missing.append(name)
        return result, missing

    def get_tag_info(self, names):
        names = list(dict.fromkeys(names))
        cached, _missing = self._get_cached_or_missing('tags', names)
        merged = self._resolve_offline('tags', names, cached)
        missing = self._should_fetch('tags', names, merged)
        if missing and not self.network_failed and time.time() >= self._retry_after:
            try:
                for chunk in self._chunks(missing):
                    rows = self._get_json('/tags.json', {
                        'limit': 1000, 'only': 'name,is_deprecated,category,post_count',
                        'search[name_normalize]': ','.join(chunk),
                    })
                    found = {r.get('name'): r for r in rows if r.get('name')}
                    now = time.time()
                    for name in chunk:
                        row = found.get(name)
                        entry = {'exists': row is not None, 'deprecated': bool(row.get('is_deprecated')) if row else False,
                                 'category': row.get('category') if row else None,
                                 'post_count': row.get('post_count') if row else None, 'checked_at': now}
                        self.cache['tags'][name] = entry
                        merged[name] = dict(entry, _source='api')
            except Exception:
                pass
        return merged

    def get_aliases(self, names):
        names = list(dict.fromkeys(names))
        cached, _missing = self._get_cached_or_missing('aliases', names)
        merged = self._resolve_offline('aliases', names, cached)
        missing = self._should_fetch('aliases', names, merged)
        if missing and not self.network_failed and time.time() >= self._retry_after:
            try:
                for chunk in self._chunks(missing):
                    rows = self._get_json('/tag_aliases.json', {
                        'limit': 1000, 'only': 'antecedent_name,consequent_name,status',
                        'search[status]': 'active', 'search[antecedent_name_comma]': ','.join(chunk),
                    })
                    found = {r['antecedent_name']: r['consequent_name'] for r in rows
                             if r.get('status') == 'active' and r.get('antecedent_name') and r.get('consequent_name')}
                    now = time.time()
                    for name in chunk:
                        entry = {'canonical': found.get(name), 'checked_at': now}
                        self.cache['aliases'][name] = entry
                        merged[name] = dict(entry, _source='api')
            except Exception:
                pass
        return merged

    def get_implications(self, names):
        names = list(dict.fromkeys(names))
        cached, _missing = self._get_cached_or_missing('implications', names)
        merged = self._resolve_offline('implications', names, cached)
        missing = self._should_fetch('implications', names, merged)
        if missing and not self.network_failed and time.time() >= self._retry_after:
            try:
                for chunk in self._chunks(missing):
                    rows = self._get_json('/tag_implications.json', {
                        'limit': 1000, 'only': 'antecedent_name,consequent_name,status',
                        'search[status]': 'active', 'search[antecedent_name_comma]': ','.join(chunk),
                    })
                    found = {name: [] for name in chunk}
                    for r in rows:
                        if r.get('status') == 'active' and r.get('antecedent_name') in found and r.get('consequent_name'):
                            found[r['antecedent_name']].append(r['consequent_name'])
                    now = time.time()
                    for name in chunk:
                        entry = {'parents': sorted(set(found[name])), 'checked_at': now}
                        self.cache['implications'][name] = entry
                        merged[name] = dict(entry, _source='api')
            except Exception:
                pass
        return merged

    @staticmethod
    def _split_character_tag(tag):
        """Return (base, suffix) for common `name_(copyright)` character tags."""
        tag = str(tag or "").strip()
        marker = tag.rfind("_(")
        if marker > 0 and tag.endswith(")"):
            return tag[:marker], tag[marker:]
        return tag, ""

    @staticmethod
    def _character_search_patterns(tag):
        base, suffix = DanbooruClient._split_character_tag(tag)
        patterns = []
        if base:
            patterns.append(f"{base}*{suffix}" if suffix else f"{base}*")
        # Conservative fuzzy fallback for renamed/reworked counterparts such as
        # typhon_(arknights) -> typhoeus_(arknights). Suggestions are advisory only.
        compact = base.replace("_", "")
        if 6 <= len(compact) <= 18 and len(base) >= 5:
            prefix = base[:-1]
            fuzzy = f"{prefix}*{suffix}" if suffix else f"{prefix}*"
            if fuzzy not in patterns:
                patterns.append(fuzzy)
        return patterns

    def get_character_related(self, character_names, limit_per_character=8):
        """Return advisory current Danbooru character tags related to detected tags.

        Sources:
        1. active reverse implications (specific child -> detected parent),
        2. conservative name-prefix search in the same tag namespace.

        Nothing here is an automatic replacement. The caller should present these
        as human-review suggestions only.
        """
        names = list(dict.fromkeys(str(x).strip() for x in character_names if str(x).strip()))
        result = {}
        now = time.time()
        bucket = self.cache["character_related"]

        for name in names:
            cached = bucket.get(name)
            if cached is not None and self._fresh(cached):
                self.cache_hits += 1
                result[name] = list(cached.get("suggestions", []))[:limit_per_character]
                continue

            if cached is not None:
                self.used_stale_cache = True
                self.stale_cache_hits += 1
                result[name] = list(cached.get("suggestions", []))[:limit_per_character]

            if not self._online_allowed():
                # Snapshot-based prefix search plus existing JSON suggestions.
                # Never store an empty network result as a fresh cache entry.
                local = self.offline.character_variants(name, limit=limit_per_character)
                if local:
                    result[name] = list(dict.fromkeys(local + result.get(name, [])))[:limit_per_character]
                continue
            if self.network_failed:
                continue

            candidates = {}
            direct_children = set()

            # First use a current tag-name search. This is the most robust path and
            # catches common costume/variant naming plus conservative near-prefixes.
            prefix_query_succeeded = False
            for pattern in self._character_search_patterns(name):
                try:
                    rows = self._get_json(
                        "/tags.json",
                        {
                            "limit": 100,
                            "only": "name,is_deprecated,category,post_count",
                            "search[name_matches]": pattern,
                            "search[category]": 4,
                            "search[hide_empty]": "yes",
                        },
                        mark_failed_state=False,
                    )
                    prefix_query_succeeded = True
                except Exception:
                    break
                for row in rows or []:
                    candidate = row.get("name")
                    if not candidate or candidate == name:
                        continue
                    if row.get("is_deprecated") or row.get("category") != 4:
                        continue
                    current = candidates.setdefault(candidate, {"post_count": 0, "direct": False})
                    current["post_count"] = max(int(row.get("post_count") or 0), current["post_count"])

            # If the basic tag search worked, optionally enrich it with active
            # reverse implications (specific child -> detected parent). This query
            # is advisory; a failure must not poison cleanup for the whole run.
            if prefix_query_succeeded:
                try:
                    rows = self._get_json(
                        "/tag_implications.json",
                        {
                            "limit": 1000,
                            "only": "antecedent_name,consequent_name,status",
                            "search[status]": "active",
                            "search[consequent_name_matches]": name,
                        },
                        mark_failed_state=False,
                    )
                    for row in rows or []:
                        if row.get("status") != "active":
                            continue
                        if row.get("consequent_name") != name:
                            continue
                        child = row.get("antecedent_name")
                        if child and child != name:
                            direct_children.add(child)
                            candidates.setdefault(child, {"post_count": 0, "direct": True})
                except Exception:
                    pass

            if not prefix_query_succeeded:
                # Do not turn a temporary network/API failure into a fresh empty
                # cache entry. Keep stale suggestions if we had them.
                result.setdefault(name, [])
                continue

            # Validate reverse-implication candidates as current character tags.
            if direct_children:
                info = self.get_tag_info(direct_children)
                for candidate in list(direct_children):
                    meta = info.get(candidate) or {}
                    if not meta.get("exists") or meta.get("deprecated") or meta.get("category") != 4:
                        candidates.pop(candidate, None)
                        continue
                    current = candidates.setdefault(candidate, {"post_count": 0, "direct": True})
                    current["direct"] = True
                    current["post_count"] = max(int(meta.get("post_count") or 0), current["post_count"])

            ordered = sorted(
                candidates,
                key=lambda tag: (
                    0 if candidates[tag].get("direct") else 1,
                    -int(candidates[tag].get("post_count") or 0),
                    tag,
                ),
            )[:limit_per_character]
            bucket[name] = {"suggestions": ordered, "checked_at": now}
            result[name] = ordered

        self.save()
        return result

    def get_character_cooccurrence(self, character_names, limit_per_character=10):
        """Return advisory character tags that frequently co-occur with a character.

        This uses Danbooru's read-only related_tag endpoint and then filters the
        response locally to character tags in the same common namespace suffix,
        e.g. abe_(nikke) -> cinderella_(nikke), red_shoes_(nikke), etc.

        The result is advisory only. It is intended for incomplete group tagging,
        not for automatic insertion into HIGH/MEDIUM/LOW.
        """
        names = list(dict.fromkeys(str(x).strip() for x in character_names if str(x).strip()))
        result = {}
        now = time.time()
        bucket = self.cache["character_cooccurrence"]

        for name in names:
            cached = bucket.get(name)
            if cached is not None and self._fresh(cached, "character_cooccurrence"):
                self.cache_hits += 1
                result[name] = list(cached.get("suggestions", []))[:limit_per_character]
                continue

            if cached is not None:
                self.used_stale_cache = True
                self.stale_cache_hits += 1
                result[name] = list(cached.get("suggestions", []))[:limit_per_character]

            if self.network_failed or not self._online_allowed():
                continue

            try:
                payload = self._related_frequent(name)
            except Exception:
                result.setdefault(name, [])
                continue

            _base, source_suffix = self._split_character_tag(name)
            suggestions = []
            for row in (payload or {}).get("tags", []):
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                candidate, category = row[0], row[1]
                if category != 4 or not candidate or candidate == name:
                    continue
                if source_suffix:
                    _candidate_base, candidate_suffix = self._split_character_tag(candidate)
                    if candidate_suffix != source_suffix:
                        continue
                if candidate not in suggestions:
                    suggestions.append(candidate)
                if len(suggestions) >= limit_per_character:
                    break

            bucket[name] = {"suggestions": suggestions, "checked_at": now}
            result[name] = suggestions

        self.save()
        return result

    def get_character_copyrights(self, character_names, limit_per_character=5):
        """Return copyright tags strongly associated with each character.

        Uses Danbooru's related_tag endpoint and keeps category=3 entries only.
        This is used for conservative crossover warnings. A warning is emitted
        only when Danbooru itself provides copyright relationship data.
        """
        names = list(dict.fromkeys(str(x).strip() for x in character_names if str(x).strip()))
        result = {}
        now = time.time()
        bucket = self.cache["character_copyrights"]

        for name in names:
            cached = bucket.get(name)
            if cached is not None and self._fresh(cached, "character_copyrights"):
                self.cache_hits += 1
                result[name] = list(cached.get("copyrights", []))[:limit_per_character]
                continue

            if cached is not None:
                self.used_stale_cache = True
                self.stale_cache_hits += 1
                result[name] = list(cached.get("copyrights", []))[:limit_per_character]

            if self.network_failed or not self._online_allowed():
                continue

            try:
                payload = self._related_frequent(name)
            except Exception:
                result.setdefault(name, [])
                continue

            copyrights = []
            for row in (payload or {}).get("tags", []):
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                candidate, category = row[0], row[1]
                if category != 3 or not candidate:
                    continue
                if candidate not in copyrights:
                    copyrights.append(candidate)
                if len(copyrights) >= limit_per_character:
                    break

            bucket[name] = {"copyrights": copyrights, "checked_at": now}
            result[name] = copyrights

        self.save()
        return result

    def test_auth(self):
        """Verify Danbooru login + API key using the authenticated profile endpoint."""
        if not self.is_authenticated:
            return {"status": "invalid", "error": "missing credentials"}

        url = f"{API_BASE}/profile.json"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Authorization": self._auth_header(),
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self._note_http_failure(exc)
            if exc.code == 401:
                return {"status": "invalid", "error": "HTTP 401"}
            return {"status": "error", "error": self.last_error}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"status": "error", "error": str(exc)}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

        if not isinstance(data, dict):
            return {"status": "error", "error": "unexpected response"}
        name = str(data.get("name") or "").strip()
        if name and name.casefold() != self.login.casefold():
            return {
                "status": "error",
                "error": f"API zwróciło profil {name}, oczekiwano {self.login}",
            }
        return {
            "status": "ok",
            "name": name or self.login,
            "level": data.get("level"),
            "level_string": data.get("level_string"),
        }

    def check_artist_source(self, source: str):
        """Check whether a source resolves to a Danbooru-banned artist."""
        source = (source or "").strip()
        if not source:
            return {"status": "skipped", "artists": [], "banned": []}
        if not self.is_authenticated:
            return {"status": "auth_missing", "artists": [], "banned": []}

        params = {
            "limit": 20,
            "only": "id,name,is_banned,is_deleted",
        }
        if source.lower().startswith(("http://", "https://")):
            params["search[url_matches]"] = source
        else:
            params["search[any_name_matches]"] = source

        try:
            rows = self._get_json("/artists.json", params)
        except Exception:
            return {
                "status": "error",
                "artists": [],
                "banned": [],
                "error": self.last_error,
            }

        if isinstance(rows, dict):
            rows = [rows]
        rows = [row for row in (rows or []) if isinstance(row, dict)]
        banned = [row for row in rows if bool(row.get("is_banned"))]
        return {
            "status": "blocked" if banned else ("ok" if rows else "not_found"),
            "artists": rows,
            "banned": banned,
        }

    def _canonicalize(self, score_map):
        alias_replacements = 0
        current = dict(score_map)
        for _ in range(5):
            aliases = self.get_aliases(current.keys())
            changed = False
            next_map = {}
            for tag, score in current.items():
                record = aliases.get(tag) or {}
                canonical = tag if record.get('_conflict') else record.get('canonical') or tag
                if canonical != tag:
                    alias_replacements += 1
                    changed = True
                if canonical not in next_map or score > next_map[canonical]:
                    next_map[canonical] = score
            current = next_map
            if not changed:
                break
        return current, alias_replacements

    def _drop_deprecated(self, score_map):
        info = self.get_tag_info(score_map.keys())
        kept = {}
        removed = []
        unknown = []
        for tag, score in score_map.items():
            meta = info.get(tag)
            if meta is None:
                kept[tag] = score
                unknown.append(tag)
                continue
            if meta.get("exists") and meta.get("deprecated") and not meta.get('_conflict'):
                removed.append(tag)
                continue
            if not meta.get("exists"):
                unknown.append(tag)
            kept[tag] = score
        return kept, removed, unknown

    def _find_implied_parents_present(self, present_tags):
        present = set(present_tags)
        graph = {}
        frontier = set(present)
        visited = set()
        for _ in range(6):
            batch = sorted(frontier - visited)
            if not batch:
                break
            visited.update(batch)
            rows = self.get_implications(batch)
            new_frontier = set()
            for tag in batch:
                row = rows.get(tag) or {}
                parents = [] if row.get('_conflict') else list(row.get('parents', []))
                graph[tag] = parents
                for parent in parents:
                    if parent not in visited:
                        new_frontier.add(parent)
            frontier = new_frontier

        redundant = set()
        for child in present:
            stack = list(graph.get(child, []))
            seen = set()
            while stack:
                parent = stack.pop()
                if parent in seen:
                    continue
                seen.add(parent)
                if parent in present and parent != child:
                    redundant.add(parent)
                stack.extend(graph.get(parent, []))
        return redundant

    def clean_items(self, items, hide_implied_parents=True, reset_state=True):
        """Canonicalize aliases, remove deprecated tags and optional parents."""
        if reset_state:
            self.reset_run_state()

        score_map = {}
        for tag, score in items:
            if tag not in score_map or score > score_map[tag]:
                score_map[tag] = score

        score_map, alias_count = self._canonicalize(score_map)
        score_map, deprecated_removed, unknown = self._drop_deprecated(score_map)

        implied_removed = []
        if hide_implied_parents and score_map:
            redundant = self._find_implied_parents_present(score_map.keys())
            for tag in sorted(redundant):
                if tag in score_map:
                    implied_removed.append(tag)
                    score_map.pop(tag, None)

        self.save()
        cleaned = sorted(score_map.items(), key=lambda item: (-item[1], item[0]))
        report = {
            "alias_replacements": alias_count,
            "deprecated_removed": deprecated_removed,
            "implied_removed": implied_removed,
            "unknown": unknown,
            "network_failed": self.network_failed,
            "used_stale_cache": self.used_stale_cache,
            "last_error": self.last_error,
            "source_conflicts": sorted(self.conflicts),
            "offline_ready": self.offline.ready,
            "cloudflare_challenge": self.cloudflare_challenge,
        }
        report.update(self.run_stats())
        return cleaned, report
