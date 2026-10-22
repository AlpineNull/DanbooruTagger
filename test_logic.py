import importlib.machinery
import importlib.util
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
loader = importlib.machinery.SourceFileLoader("app_module", str(HERE / "app.pyw"))
spec = importlib.util.spec_from_loader(loader.name, loader)
app_module = importlib.util.module_from_spec(spec)
loader.exec_module(app_module)

hide_resolution_autotags = app_module.hide_resolution_autotags
apply_user_blacklist = app_module.apply_user_blacklist
apply_deterministic_rules = app_module.apply_deterministic_rules
split_by_confidence = app_module.split_by_confidence
build_copy_all_text = app_module.build_copy_all_text
suggested_translation_request = app_module.suggested_translation_request
find_conflict_warnings = app_module.find_conflict_warnings
build_advice_text = app_module.build_advice_text
estimate_subject_minimum = app_module.estimate_subject_minimum
find_character_coverage = app_module.find_character_coverage
find_copyright_sanity_warnings = app_module.find_copyright_sanity_warnings
find_greyscale_color_warnings = app_module.find_greyscale_color_warnings
select_near_threshold_candidates = app_module.select_near_threshold_candidates
parse_idle_minutes = app_module.parse_idle_minutes
from danbooru_client import DanbooruClient


def test_resolution_hidden():
    raw = [("highres", 0.99), ("absurdres", 0.88), ("1girl", 0.95)]
    out = dict(hide_resolution_autotags(raw))
    assert "highres" not in out and "absurdres" not in out
    assert out["1girl"] == 0.95


def test_blacklist():
    items, removed = apply_user_blacklist(
        [("1girl", 0.9), ("office_lady", 0.8), ("mature_female", 0.7)],
        ["office_lady", "mature_female"],
    )
    assert [x[0] for x in items] == ["1girl"]
    assert removed == ["office_lady", "mature_female"]


def test_multiple_views_removes_solo():
    items, removed = apply_deterministic_rules(
        [("1girl", 0.9), ("solo", 0.8), ("multiple_views", 0.4)]
    )
    tags = [x[0] for x in items]
    assert "multiple_views" in tags
    assert "solo" not in tags
    assert removed == ["solo"]


def test_portrait_removes_upper_body():
    items, removed = apply_deterministic_rules(
        [("portrait", 0.9), ("upper_body", 0.8), ("1girl", 0.7)]
    )
    tags = [x[0] for x in items]
    assert "portrait" in tags
    assert "upper_body" not in tags
    assert "upper_body" in removed


def test_teacher_requires_context():
    items, removed = apply_deterministic_rules(
        [("teacher", 0.8), ("1girl", 0.9), ("school_uniform", 0.7)]
    )
    assert "teacher" not in [x[0] for x in items]
    assert "teacher" in removed

    items, removed = apply_deterministic_rules(
        [("teacher", 0.8), ("1girl", 0.9), ("classroom", 0.7)]
    )
    assert "teacher" in [x[0] for x in items]
    assert "teacher" not in removed


def test_copy_all_exact_format():
    text = build_copy_all_text("h1 h2", "m1", "l1 l2")
    assert text == "\nh1 h2\n\nm1\n\nl1 l2\n\n"


def test_buckets():
    buckets = split_by_confidence(
        [("a", 0.90), ("b", 0.60), ("c", 0.20), ("d", 0.10)], 0.75, 0.45, 0.17
    )
    assert buckets == {"high": ["a"], "medium": ["b"], "low": ["c"]}


def test_translation_request_suggestion():
    assert suggested_translation_request(["1girl", "japanese_text"]) == ["japanese_text"]
    assert suggested_translation_request(["japanese_text", "translation_request"]) == []
    assert suggested_translation_request(["english_text"]) == []


def test_conflict_warnings_single_subject():
    warnings = find_conflict_warnings([
        "1girl", "solo", "pink_eyes", "purple_eyes", "short_hair", "long_hair",
        "medium_breasts", "large_breasts",
    ])
    assert any("kolory oczu" in x for x in warnings)
    assert any("długość włosów" in x for x in warnings)
    assert any("rozmiar piersi" in x for x in warnings)


def test_conflict_warnings_suppressed_for_comic():
    warnings = find_conflict_warnings(["comic", "2girls", "pink_eyes", "purple_eyes", "short_hair", "long_hair"])
    assert warnings == []


def test_conflict_warnings_suppressed_for_multiple_people():
    warnings = find_conflict_warnings(
        ["6+girls", "pink_eyes", "purple_eyes", "short_hair", "very_long_hair"]
    )
    assert warnings == []


def test_solo_plural_conflict_still_reported():
    warnings = find_conflict_warnings(["solo", "2girls", "pink_eyes", "purple_eyes"])
    assert any("solo" in x and "2girls" in x for x in warnings)


def test_character_coverage_group():
    tags = ["6+girls", "abe_(nikke)", "goddess_of_victory:_nikke"]
    info = {
        "6+girls": {"exists": True, "category": 0},
        "abe_(nikke)": {"exists": True, "category": 4},
        "goddess_of_victory:_nikke": {"exists": True, "category": 3},
    }
    coverage = find_character_coverage(tags, info)
    assert estimate_subject_minimum(tags) == 6
    assert coverage["minimum_subjects"] == 6
    assert coverage["recognized_characters"] == 1


def test_copyright_sanity_warning():
    tags = ["arknights:_endfield", "nefer_(genshin_impact)"]
    info = {
        "arknights:_endfield": {"exists": True, "category": 3},
        "nefer_(genshin_impact)": {"exists": True, "category": 4},
    }
    relationships = {"nefer_(genshin_impact)": ["genshin_impact"]}
    warnings = find_copyright_sanity_warnings(tags, info, relationships)
    assert warnings and "nefer_(genshin_impact)" in warnings[0]


def test_copyright_sanity_valid_nonmatching_namespace_ok():
    tags = ["choujigen_game_neptune", "green_heart_(neptunia)"]
    info = {
        "choujigen_game_neptune": {"exists": True, "category": 3},
        "green_heart_(neptunia)": {"exists": True, "category": 4},
    }
    relationships = {"green_heart_(neptunia)": ["choujigen_game_neptune"]}
    assert find_copyright_sanity_warnings(tags, info, relationships) == []


def test_copyright_sanity_without_relationship_data_is_conservative():
    tags = ["arknights:_endfield", "nefer_(genshin_impact)"]
    info = {
        "arknights:_endfield": {"exists": True, "category": 3},
        "nefer_(genshin_impact)": {"exists": True, "category": 4},
    }
    assert find_copyright_sanity_warnings(tags, info, {}) == []


def test_greyscale_color_warning():
    tags = ["greyscale", "black_hair", "black_bra", "white_background", "1girl"]
    info = {
        "black_hair": {"exists": True, "category": 0},
        "black_bra": {"exists": True, "category": 0},
        "white_background": {"exists": True, "category": 0},
    }
    result = find_greyscale_color_warnings(tags, info)
    assert "black_hair" in result
    assert "black_bra" in result
    assert "white_background" not in result


def test_advice_text_related_and_translation():
    text = build_advice_text(
        ["female_rover_(wuthering_waves)", "chinese_text"],
        related_map={"female_rover_(wuthering_waves)": ["female_rover_(perpetual_spark)_(wuthering_waves)"]},
    )
    assert "female_rover_(perpetual_spark)_(wuthering_waves)" in text
    assert "translation_request" in text


def test_advice_text_grouped_checks():
    text = build_advice_text(
        ["6+girls", "abe_(nikke)", "greyscale", "black_hair"],
        cooccurrence_map={"abe_(nikke)": ["cinderella_(nikke)"]},
        unknown_tags=["duck_(arknights)"],
        copyright_warnings=["nefer_(genshin_impact) ↔ arknights:_endfield"],
        coverage={"minimum_subjects": 6, "recognized_characters": 1, "character_tags": ["abe_(nikke)"]},
        greyscale_tags=["black_hair"],
    )
    assert "[POSTACIE]" in text
    assert "cinderella_(nikke)" in text
    assert "[NIEZNANE]" in text and "duck_(arknights)" in text
    assert "[COPYRIGHT]" in text
    assert "[GREYSCALE]" in text


def test_near_threshold_candidates():
    items = [
        ("gradient_background", 0.16),
        ("sparkles", 0.12),
        ("already_low", 0.17),
        ("too_low", 0.05),
        ("office_lady", 0.15),
    ]
    result = select_near_threshold_candidates(
        items, 0.17, margin=0.07, limit=8, blacklist=["office_lady"]
    )
    assert [x[0] for x in result] == ["gradient_background", "sparkles"]


def test_idle_parse():
    assert parse_idle_minutes("10") == 10
    assert parse_idle_minutes("Nigdy") == 0
    assert parse_idle_minutes("0") == 0


def test_cleanup_seeded_cache():
    with tempfile.TemporaryDirectory() as td:
        client = DanbooruClient(Path(td) / "cache.json")
        now = time.time()
        client.cache["aliases"] = {
            "old_name": {"canonical": "new_name", "checked_at": now},
            "new_name": {"canonical": None, "checked_at": now},
            "child": {"canonical": None, "checked_at": now},
            "parent": {"canonical": None, "checked_at": now},
            "dead": {"canonical": None, "checked_at": now},
        }
        client.cache["tags"] = {
            "new_name": {"exists": True, "deprecated": False, "checked_at": now},
            "child": {"exists": True, "deprecated": False, "checked_at": now},
            "parent": {"exists": True, "deprecated": False, "checked_at": now},
            "dead": {"exists": True, "deprecated": True, "checked_at": now},
        }
        client.cache["implications"] = {
            "new_name": {"parents": [], "checked_at": now},
            "child": {"parents": ["parent"], "checked_at": now},
            "parent": {"parents": [], "checked_at": now},
        }
        cleaned, report = client.clean_items(
            [("old_name", 0.9), ("child", 0.8), ("parent", 0.7), ("dead", 0.6)], True
        )
        tags = [t for t, _ in cleaned]
        assert "new_name" in tags and "old_name" not in tags
        assert "child" in tags and "parent" not in tags
        assert "dead" not in tags
        assert report["alias_replacements"] == 1
        assert report["cache_hits"] > 0


def test_unknown_tag_is_cached_but_not_removed():
    with tempfile.TemporaryDirectory() as td:
        cache_path = Path(td) / "cache.json"
        client = DanbooruClient(cache_path)

        def fake_get_json(path, params, **kwargs):
            client.api_requests += 1
            if path == "/tag_aliases.json":
                return []
            if path == "/tags.json":
                return []
            if path == "/tag_implications.json":
                return []
            return []

        client._get_json = fake_get_json
        cleaned, report = client.clean_items([("duck_(arknights)", 0.8)], True)
        assert cleaned == [("duck_(arknights)", 0.8)]
        assert "duck_(arknights)" in report["unknown"]
        saved = client.cache["tags"]["duck_(arknights)"]
        assert saved["exists"] is False
        assert saved["checked_at"] > 0


def test_tag_cache_uses_longer_positive_and_negative_ttls():
    client = DanbooruClient(Path(tempfile.gettempdir()) / "unused-danbooru-cache.json")
    now = time.time()
    assert client._fresh({"exists": False, "checked_at": now - 20 * 86400}, "tags")
    assert not client._fresh({"exists": False, "checked_at": now - 31 * 86400}, "tags")
    assert client._fresh({"exists": True, "checked_at": now - 60 * 86400}, "tags")
    assert not client._fresh({"exists": True, "checked_at": now - 91 * 86400}, "tags")


def test_character_related_seeded_cache():
    with tempfile.TemporaryDirectory() as td:
        client = DanbooruClient(Path(td) / "cache.json")
        client.cache["character_related"]["female_rover_(wuthering_waves)"] = {
            "suggestions": ["female_rover_(perpetual_spark)_(wuthering_waves)"],
            "checked_at": time.time(),
        }
        result = client.get_character_related(["female_rover_(wuthering_waves)"])
        assert result["female_rover_(wuthering_waves)"] == [
            "female_rover_(perpetual_spark)_(wuthering_waves)"
        ]
        assert client.cache_hits == 1


def test_character_related_mock_prefix_and_implication():
    with tempfile.TemporaryDirectory() as td:
        client = DanbooruClient(Path(td) / "cache.json")

        def fake_get_json(path, params, **kwargs):
            client.api_requests += 1
            if path == "/tags.json" and "search[name_matches]" in params:
                pattern = params["search[name_matches]"]
                if pattern.startswith("female_rover"):
                    return [
                        {
                            "name": "female_rover_(perpetual_spark)_(wuthering_waves)",
                            "is_deprecated": False,
                            "category": 4,
                            "post_count": 120,
                        }
                    ]
                return []
            if path == "/tag_implications.json":
                return [
                    {
                        "antecedent_name": "female_rover_(perpetual_spark)_(wuthering_waves)",
                        "consequent_name": "female_rover_(wuthering_waves)",
                        "status": "active",
                    }
                ]
            if path == "/tags.json" and "search[name_normalize]" in params:
                return [
                    {
                        "name": "female_rover_(perpetual_spark)_(wuthering_waves)",
                        "is_deprecated": False,
                        "category": 4,
                        "post_count": 120,
                    }
                ]
            return []

        client._get_json = fake_get_json
        result = client.get_character_related(["female_rover_(wuthering_waves)"])
        assert "female_rover_(perpetual_spark)_(wuthering_waves)" in result["female_rover_(wuthering_waves)"]


def test_character_related_fuzzy_typhon():
    with tempfile.TemporaryDirectory() as td:
        client = DanbooruClient(Path(td) / "cache.json")

        def fake_get_json(path, params, **kwargs):
            client.api_requests += 1
            if path == "/tags.json" and "search[name_matches]" in params:
                pattern = params["search[name_matches]"]
                if pattern.startswith("typho*"):
                    return [
                        {
                            "name": "typhoeus_(arknights)",
                            "is_deprecated": False,
                            "category": 4,
                            "post_count": 42,
                        }
                    ]
                return []
            if path == "/tag_implications.json":
                return []
            return []

        client._get_json = fake_get_json
        result = client.get_character_related(["typhon_(arknights)"])
        assert "typhoeus_(arknights)" in result["typhon_(arknights)"]


def test_character_cooccurrence_abe():
    with tempfile.TemporaryDirectory() as td:
        client = DanbooruClient(Path(td) / "cache.json")

        def fake_get_json(path, params, **kwargs):
            client.api_requests += 1
            assert path == "/related_tag.json"
            return {
                "query": "abe_(nikke)",
                "tags": [
                    ["goddess_of_victory:_nikke", 3],
                    ["cinderella_(nikke)", 4],
                    ["red_shoes_(nikke)", 4],
                    ["nefer_(genshin_impact)", 4],
                    ["picnic", 0],
                ],
            }

        client._get_json = fake_get_json
        result = client.get_character_cooccurrence(["abe_(nikke)"])
        assert result["abe_(nikke)"] == ["cinderella_(nikke)", "red_shoes_(nikke)"]
        cached = client.cache["character_cooccurrence"]["abe_(nikke)"]
        assert cached["suggestions"] == ["cinderella_(nikke)", "red_shoes_(nikke)"]


def test_character_copyrights_green_heart():
    with tempfile.TemporaryDirectory() as td:
        client = DanbooruClient(Path(td) / "cache.json")

        def fake_get_json(path, params, **kwargs):
            client.api_requests += 1
            assert path == "/related_tag.json"
            return {
                "query": "green_heart_(neptunia)",
                "tags": [
                    ["choujigen_game_neptune", 3],
                    ["green_heart_(4go)_(neptunia)", 4],
                ],
            }

        client._get_json = fake_get_json
        result = client.get_character_copyrights(["green_heart_(neptunia)"])
        assert result["green_heart_(neptunia)"] == ["choujigen_game_neptune"]
        assert client.cache["character_copyrights"]["green_heart_(neptunia)"]["copyrights"] == [
            "choujigen_game_neptune"
        ]


def test_artist_check_requires_auth():
    with tempfile.TemporaryDirectory() as td:
        client = DanbooruClient(Path(td) / "cache.json")
        report = client.check_artist_source("https://example.com/artist/status/123")
        assert report["status"] == "auth_missing"


def test_artist_check_banned_mock():
    with tempfile.TemporaryDirectory() as td:
        client = DanbooruClient(Path(td) / "cache.json", login="builder", api_key="secret")
        client._get_json = lambda *args, **kwargs: [
            {"id": 123, "name": "blocked_artist", "is_banned": True, "is_deleted": False}
        ]
        report = client.check_artist_source("https://example.com/artist/status/123")
        assert report["status"] == "blocked"
        assert report["banned"][0]["name"] == "blocked_artist"


def test_auth_ok_mock():
    import urllib.request
    from unittest.mock import patch

    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def read(self):
            return b'{"name":"builder","level":32,"level_string":"Builder"}'

    with tempfile.TemporaryDirectory() as td:
        client = DanbooruClient(Path(td) / "cache.json", login="builder", api_key="secret")
        with patch.object(urllib.request, "urlopen", return_value=FakeResponse()):
            report = client.test_auth()
        assert report["status"] == "ok"
        assert report["name"] == "builder"
        assert report["level_string"] == "Builder"


def test_auth_invalid_mock():
    import urllib.error
    import urllib.request
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as td:
        client = DanbooruClient(Path(td) / "cache.json", login="builder", api_key="wrong")
        err = urllib.error.HTTPError("https://danbooru.donmai.us/profile.json", 401, "Unauthorized", {}, None)
        with patch.object(urllib.request, "urlopen", side_effect=err):
            report = client.test_auth()
        assert report["status"] == "invalid"


if __name__ == "__main__":
    test_resolution_hidden()
    test_blacklist()
    test_multiple_views_removes_solo()
    test_portrait_removes_upper_body()
    test_teacher_requires_context()
    test_copy_all_exact_format()
    test_buckets()
    test_translation_request_suggestion()
    test_conflict_warnings_single_subject()
    test_conflict_warnings_suppressed_for_comic()
    test_conflict_warnings_suppressed_for_multiple_people()
    test_solo_plural_conflict_still_reported()
    test_character_coverage_group()
    test_copyright_sanity_warning()
    test_copyright_sanity_valid_nonmatching_namespace_ok()
    test_copyright_sanity_without_relationship_data_is_conservative()
    test_greyscale_color_warning()
    test_advice_text_related_and_translation()
    test_advice_text_grouped_checks()
    test_near_threshold_candidates()
    test_idle_parse()
    test_cleanup_seeded_cache()
    test_unknown_tag_is_cached_but_not_removed()
    test_tag_cache_uses_longer_positive_and_negative_ttls()
    test_character_related_seeded_cache()
    test_character_related_mock_prefix_and_implication()
    test_character_related_fuzzy_typhon()
    test_character_cooccurrence_abe()
    test_character_copyrights_green_heart()
    test_artist_check_requires_auth()
    test_artist_check_banned_mock()
    test_auth_ok_mock()
    test_auth_invalid_mock()
    print("OK")
