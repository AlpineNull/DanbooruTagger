"""No network/GPU: fake offline dataset verifies merging and safe cleanup."""
import json
from contextlib import closing
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from danbooru_client import DanbooruClient
from offline_tags import SNAPSHOT_DATE, OfflineTags


def make_db(path):
    db = sqlite3.connect(path)
    db.executescript('''
      CREATE TABLE tags (name TEXT PRIMARY KEY, category INTEGER, post_count INTEGER, is_deprecated INTEGER);
      CREATE TABLE tag_aliases (antecedent_name TEXT, consequent_name TEXT, status TEXT);
      CREATE TABLE tag_implications (antecedent_name TEXT, consequent_name TEXT, status TEXT);
      INSERT INTO tags VALUES ('obsolete',0,2,1);
      INSERT INTO tags VALUES ('canonical',0,200,0);
      INSERT INTO tags VALUES ('parent',0,500,0);
      INSERT INTO tags VALUES ('child',0,100,0);
      INSERT INTO tags VALUES ('alias_old',0,50,0);
      INSERT INTO tag_aliases VALUES ('alias_old','canonical','active');
      INSERT INTO tag_aliases VALUES ('old_retired','canonical','retired');
      INSERT INTO tag_implications VALUES ('child','parent','active');
      INSERT INTO tag_implications VALUES ('child','obsolete','deleted');
    ''')
    db.commit()
    db.close()


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.path = self.folder / 'danbooru_tags.sqlite'
        make_db(self.path)
        self.client = DanbooruClient(self.folder / 'danbooru_cache.json')
        self.assertTrue(self.client.offline.ready)

    def test_db_lookup_readonly_and_relations(self):
        db = OfflineTags(self.path)
        self.assertTrue(db.tags(['canonical'])['canonical']['exists'])
        self.assertTrue(db.tags(['obsolete'])['obsolete']['deprecated'])
        self.assertEqual(db.aliases(['alias_old','old_retired']), {'alias_old': {'canonical':'canonical'}})
        self.assertEqual(db.implications(['child']), {'child': {'parents':['parent']}})

    def test_offline_resolves_existing_without_network(self):
        with patch.object(self.client, '_get_json', side_effect=AssertionError('unwanted network')):
            self.assertTrue(self.client.get_tag_info(['canonical'])['canonical']['exists'])
            self.assertEqual(self.client.get_aliases(['alias_old'])['alias_old']['canonical'],'canonical')
            self.assertEqual(self.client.get_implications(['child'])['child']['parents'],['parent'])

    def test_old_json_conflict_no_deprecated_drop(self):
        self.client.cache['tags']['obsolete'] = {'exists': True, 'deprecated': False, 'category':0,
                                                'checked_at': SNAPSHOT_DATE - 86400}
        with patch.object(self.client, '_get_json', side_effect=AssertionError('unwanted network')):
            result, report = self.client.clean_items([('obsolete',.9)], hide_implied_parents=False)
        self.assertEqual(result,[('obsolete',.9)])
        self.assertIn('tags: obsolete', report['source_conflicts'])

    def test_recent_json_wins_and_deprecated_removes(self):
        self.client.cache['tags']['obsolete'] = {'exists': True, 'deprecated': False, 'category':0,
                                                'checked_at': SNAPSHOT_DATE + 86400}
        with patch.object(self.client, '_get_json', side_effect=AssertionError('unwanted network')):
            r = self.client.get_tag_info(['obsolete'])
        self.assertEqual(r['obsolete']['_source'], 'json')
        self.assertFalse(r['obsolete']['deprecated'])

    def test_older_alias_conflict_no_substitution(self):
        self.client.cache['aliases']['alias_old'] = {'canonical':'wrong_tag', 'checked_at': SNAPSHOT_DATE - 100}
        self.client.cache['tags']['alias_old'] = {'exists': True, 'deprecated': False, 'category':0, 'checked_at': SNAPSHOT_DATE+100}
        with patch.object(self.client,'_get_json',side_effect=AssertionError('network')):
            r, report = self.client.clean_items([('alias_old',.8)], hide_implied_parents=False)
        self.assertEqual(r,[('alias_old',.8)])
        self.assertIn('aliases: alias_old', report['source_conflicts'])

    def test_implication_conflict_does_not_drop_parent(self):
        self.client.cache['implications']['child'] = {'parents':[], 'checked_at': SNAPSHOT_DATE-1}
        with patch.object(self.client, '_get_json', side_effect=AssertionError('network')):
            r, report = self.client.clean_items([('child',.9),('parent',.8)])
        self.assertEqual(set(t for t,_ in r),{'child','parent'})
        self.assertIn('implications: child',report['source_conflicts'])

    def test_explicit_refresh_uses_live_api_and_updates_json(self):
        def fake_response(path, params, **kwargs):
            if path == '/tags.json':
                return [{'name': 'obsolete', 'category': 0, 'post_count': 99, 'is_deprecated': False}]
            return []
        with patch.object(self.client, '_get_json', side_effect=fake_response) as request:
            report = self.client.refresh_metadata(['obsolete'])
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['api_requests'], 0)  # mocked _get_json bypasses metrics
        self.assertEqual(request.call_count, 3)  # tags, aliases, implications
        self.assertFalse(self.client.cache['tags']['obsolete']['deprecated'])
        info = self.client.get_tag_info(['obsolete'])
        self.assertEqual(info['obsolete']['_source'], 'json')
        self.assertEqual(self.client.network_mode, 'local')

    def test_local_default_no_api_for_unknown_or_stale_entries(self):
        self.assertEqual(self.client.network_mode, 'local')
        self.client.cache['tags']['old_record'] = {
            'exists': True, 'deprecated': False, 'category': 0,
            'checked_at': SNAPSHOT_DATE - 86400 * 365,
        }
        with patch.object(self.client, '_get_json', side_effect=AssertionError('automatic API call')):
            self.client.get_tag_info(['never_seen', 'old_record'])
            self.client.get_aliases(['never_seen'])
            self.client.get_implications(['never_seen'])
            self.client.get_character_related(['maki_(blue_archive)'])
            self.client.get_character_cooccurrence(['maki_(blue_archive)'])
            self.client.get_character_copyrights(['maki_(blue_archive)'])
        self.assertEqual(self.client.api_requests, 0)

    def test_hybrid_fetches_unknown_entries(self):
        self.client.network_mode = 'hybrid'
        rows = [{'name': 'new_tag', 'category': 0, 'post_count': 15, 'is_deprecated': False}]
        with patch.object(self.client, '_get_json', return_value=rows) as request:
            info = self.client.get_tag_info(['new_tag'])
        self.assertEqual(info['new_tag']['_source'], 'api')
        request.assert_called_once()

    def test_local_character_variants_from_sqlite(self):
        # sqlite3.Connection's context manager commits but does NOT close.
        # Close explicitly: Windows otherwise refuses TemporaryDirectory cleanup.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO tags VALUES ('maki_(blue_archive)',4,100,0)")
            db.execute("INSERT INTO tags VALUES ('maki_(camp)_(blue_archive)',4,30,0)")
            db.execute("INSERT INTO tags VALUES ('makina_(blue_archive)',4,1000,0)")
        with patch.object(self.client, '_get_json', side_effect=AssertionError('network')):
            variants = self.client.get_character_related(['maki_(blue_archive)'])
        self.assertIn('maki_(camp)_(blue_archive)', variants['maki_(blue_archive)'])

    def test_shared_related_payload_only_one_call(self):
        self.client.network_mode = 'hybrid'
        fake = {'tags': [['other_(test)', 4], ['test_series', 3]]}
        with patch.object(self.client, '_get_json', return_value=fake) as request:
            self.client.get_character_cooccurrence(['person_(test)'])
            self.client.get_character_copyrights(['person_(test)'])
        request.assert_called_once()

    def test_missing_in_snapshot_not_marked_nonexistent(self):
        self.client.network_failed = True
        result = self.client.get_tag_info(['made_up_after_snapshot'])
        self.assertNotIn('made_up_after_snapshot', result)

    def test_cache_survives_and_keeps_version(self):
        self.client.cache['tags']['some_recent_tag'] = {'exists':True,'deprecated':False,
                                                       'category':4,'checked_at':SNAPSHOT_DATE+86400}
        self.client.save()
        reloaded = DanbooruClient(self.folder/'danbooru_cache.json')
        self.assertEqual(reloaded.cache['version'],5)
        self.assertTrue(reloaded.cache['tags']['some_recent_tag']['exists'])

    def test_cf_403_does_not_trigger_further_requests(self):
        import urllib.error
        from email.message import Message
        headers = Message();headers['Cf-Mitigated']='challenge'
        error=urllib.error.HTTPError('https://danbooru.donmai.us/tags.json',403,'Forbidden',headers,None)
        with closing(error), patch('urllib.request.urlopen',side_effect=error) as request:
            try:
                self.client._get_json('/tags.json',{'limit':1})
            except urllib.error.HTTPError:
                pass
            self.assertTrue(self.client.cloudflare_challenge)
            self.assertEqual(self.client.api_requests,1)
            self.client.reset_run_state()
            self.assertTrue(self.client.network_failed)
            with self.assertRaises(OSError):
                self.client._get_json('/tags.json',{'limit':1})
            request.assert_called_once()


if __name__ == '__main__':
    unittest.main()
