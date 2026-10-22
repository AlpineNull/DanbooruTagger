import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://danbooru.donmai.us"
CACHE_VERSION = 5
CACHE_TTL_SECONDS = 24 * 60 * 60
TAG_POSITIVE_TTL_SECONDS = 90 * 24 * 60 * 60
TAG_NEGATIVE_TTL_SECONDS = 30 * 24 * 60 * 60
USER_AGENT = "DanbooruTagger/0.5.1 (local personal tagging helper)"


class DanbooruClient:
    """Read-only Danbooru API client with a local JSON cache.

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

    def set_auth(self, login: str, api_key: str):
        self.login = (login or "").strip()
        self.api_key = api_key or ""

    @property
    def is_authenticated(self):
        return bool(self.login and self.api_key)

    def reset_run_state(self):
        self.network_failed = False
        self.used_stale_cache = False
        self.last_error = None
        self.cache_hits = 0
        self.stale_cache_hits = 0
        self.api_requests = 0

    def run_stats(self):
        return {
            "cache_hits": int(self.cache_hits),
            "stale_cache_hits": int(self.stale_cache_hits),
            "api_requests": int(self.api_requests),
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

    def _auth_header(self):
        if not self.is_authenticated:
            return None
        token = base64.b64encode(f"{self.login}:{self.api_key}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"

    def _get_json(self, path, params, *, ignore_failed_state=False, mark_failed_state=True):
        if self.network_failed and not ignore_failed_state:
            raise OSError("Danbooru API unavailable in this run")
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
        self.api_requests += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            if mark_failed_state:
                self.network_failed = True
            self.last_error = str(exc)
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
        result, missing = self._get_cached_or_missing("tags", names)
        now = time.time()

        if missing and not self.network_failed:
            try:
                for chunk in self._chunks(missing):
                    rows = self._get_json(
                        "/tags.json",
                        {
                            "limit": 1000,
                            "only": "name,is_deprecated,category,post_count",
                            "search[name_normalize]": ",".join(chunk),
                        },
                    )
                    found = {row.get("name"): row for row in rows if row.get("name")}
                    for name in chunk:
                        row = found.get(name)
                        entry = {
                            "exists": row is not None,
                            "deprecated": bool(row.get("is_deprecated")) if row else False,
                            "category": row.get("category") if row else None,
                            "post_count": row.get("post_count") if row else None,
                            "checked_at": now,
                        }
                        self.cache["tags"][name] = entry
                        result[name] = entry
            except Exception:
                pass
        return result

    def get_aliases(self, names):
        names = list(dict.fromkeys(names))
        result, missing = self._get_cached_or_missing("aliases", names)
        now = time.time()

        if missing and not self.network_failed:
            try:
                for chunk in self._chunks(missing):
                    rows = self._get_json(
                        "/tag_aliases.json",
                        {
                            "limit": 1000,
                            "only": "antecedent_name,consequent_name,status",
                            "search[status]": "active",
                            "search[antecedent_name_comma]": ",".join(chunk),
                        },
                    )
                    found = {
                        row.get("antecedent_name"): row.get("consequent_name")
                        for row in rows
                        if row.get("status") == "active"
                        and row.get("antecedent_name")
                        and row.get("consequent_name")
                    }
                    for name in chunk:
                        entry = {"canonical": found.get(name), "checked_at": now}
                        self.cache["aliases"][name] = entry
                        result[name] = entry
            except Exception:
                pass
        return result

    def get_implications(self, names):
        names = list(dict.fromkeys(names))
        result, missing = self._get_cached_or_missing("implications", names)
        now = time.time()

        if missing and not self.network_failed:
            try:
                for chunk in self._chunks(missing):
                    rows = self._get_json(
                        "/tag_implications.json",
                        {
                            "limit": 1000,
                            "only": "antecedent_name,consequent_name,status",
                            "search[status]": "active",
                            "search[antecedent_name_comma]": ",".join(chunk),
                        },
                    )
                    found = {name: [] for name in chunk}
                    for row in rows:
                        if row.get("status") != "active":
                            continue
                        ant = row.get("antecedent_name")
                        con = row.get("consequent_name")
                        if ant in found and con:
                            found[ant].append(con)
                    for name in chunk:
                        entry = {
                            "parents": sorted(set(found.get(name, []))),
                            "checked_at": now,
                        }
                        self.cache["implications"][name] = entry
                        result[name] = entry
            except Exception:
                pass
        return result

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

            if self.network_failed:
                continue

            try:
                payload = self._get_json(
                    "/related_tag.json",
                    {
                        "search[query]": name,
                        "type": "frequent",
                    },
                    mark_failed_state=False,
                )
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

            if self.network_failed:
                continue

            try:
                payload = self._get_json(
                    "/related_tag.json",
                    {
                        "search[query]": name,
                        "type": "frequent",
                    },
                    mark_failed_state=False,
                )
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
            if exc.code in (401, 403):
                return {"status": "invalid", "error": f"HTTP {exc.code}"}
            return {"status": "error", "error": f"HTTP {exc.code}: {exc.reason}"}
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
            rows = self._get_json("/artists.json", params, ignore_failed_state=True)
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
                canonical = (aliases.get(tag) or {}).get("canonical") or tag
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
            if meta.get("exists") and meta.get("deprecated"):
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
                parents = list((rows.get(tag) or {}).get("parents", []))
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
        }
        report.update(self.run_stats())
        return cleaned, report
