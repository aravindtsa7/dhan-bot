"""Synthetic-only crash/denial regressions for multi-file publications."""
import json
import os
from pathlib import Path
from datetime import date
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from quant_data_sync import _promote
from paper_engine import PaperAccount
from quant_publication import (PublicationError, publish_files, recover_publication,
                               assert_publication_ready)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def paper(self):
        account = PaperAccount.__new__(PaperAccount)
        account.root = self.root
        account.state = dict(trades=[], marker='old')
        account.save()
        return account

    def test_paper_denied_ledger_does_not_publish_state_only(self):
        account = self.paper()
        before = {p: (self.root/p).read_bytes() for p in ('state.json', 'ledger.csv')}
        account.state['marker'] = 'new'
        real = os.replace
        def denied(src, dst):
            if Path(dst).name == 'ledger.csv':
                raise PermissionError('synthetic ledger denial')
            return real(src, dst)
        with patch('paper_engine.os.replace', side_effect=denied):
            with self.assertRaises(OSError):
                account.save()
        self.assertEqual((self.root/'state.json').read_bytes(), before['state.json'])

    def test_lake_secondary_rollback_failure_does_not_skip_other_restores(self):
        stage = self.root/'stage'; stage.mkdir()
        for name in ('partition.bin', 'quarantine.bin'):
            (self.root/name).write_bytes(b'old')
            (stage/name).write_bytes(b'new')
        (self.root/'ingestion_manifest.json').write_text('{"old":true}')
        real = os.replace
        def denied(src, dst):
            # Both old and new protocols name immutable old copies backup/.
            if Path(dst).name == 'quarantine.bin' and 'backup' in Path(src).parts:
                raise PermissionError('synthetic rollback denial')
            return real(src, dst)
        with patch('step2_build_data_lake.atomic_json', side_effect=OSError('synthetic disk full')):
            with patch('quant_data_sync.os.replace', side_effect=denied):
                with self.assertRaises(OSError):
                    _promote(self.root, stage, [dict(path='partition.bin', quarantine_path='quarantine.bin')], {'new': True})
        self.assertEqual((self.root/'partition.bin').read_bytes(), b'old')
        journal = json.loads((self.root/'.publication/lake/journal.json').read_text())
        self.assertEqual(journal['phase'], 'ROLLBACK_REQUIRED')
        self.assertEqual(journal['primary_failure']['exception'], 'OSError')
        self.assertEqual(journal['recovery_attempts'][-1]['errors'][0]['path'], 'quarantine.bin')
        self.assertTrue((self.root/'.publication/lake'/journal['transaction']/'backup/0').exists())
        with self.assertRaises(PublicationError):
            assert_publication_ready(self.root)
        # Caller-owned temporary staging can disappear: originals remain durable.
        import shutil
        shutil.rmtree(stage)
        with patch('quant_publication.os.replace', side_effect=denied):
            with self.assertRaises(PublicationError):
                publish_files(self.root, 'lake', {'partition.bin': b'overwrite forbidden'})
        self.assertEqual((self.root/'partition.bin').read_bytes(), b'old')
        recover_publication(self.root)
        self.assertEqual((self.root/'quarantine.bin').read_bytes(), b'old')
        self.assertEqual(json.loads((self.root/'.publication/lake/journal.json').read_text())['phase'], 'ROLLED_BACK')

    def test_interrupted_publication_recovers_all_old_hashes_on_restart(self):
        for namespace in ('lake', 'paper'):
            with self.subTest(namespace=namespace):
                root = self.root/namespace;root.mkdir()
                (root/'a').write_bytes(b'old a')
                (root/'b').write_bytes(b'old b')
                real = os.replace
                def interrupted(src, dst):
                    if Path(dst) == root/'b':
                        raise KeyboardInterrupt('synthetic process interruption')
                    return real(src, dst)
                with patch('quant_publication.os.replace', side_effect=interrupted):
                    with self.assertRaises(KeyboardInterrupt):
                        publish_files(root, namespace, {'a': b'new a', 'b': b'new b', 'new-only': b'new'})
                self.assertEqual((root/'a').read_bytes(), b'new a')
                with self.assertRaises(PublicationError):
                    assert_publication_ready(root, namespace)
                recover_publication(root, namespace)
                self.assertEqual((root/'a').read_bytes(), b'old a')
                self.assertEqual((root/'b').read_bytes(), b'old b')
                self.assertFalse((root/'new-only').exists())
                self.assertIsNotNone(assert_publication_ready(root, namespace))

    def test_restart_removes_new_file_published_before_interruption(self):
        real = os.replace
        def interrupted(src, dst):
            if Path(dst) == self.root/'last':
                raise KeyboardInterrupt()
            return real(src, dst)
        with patch('quant_publication.os.replace', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                publish_files(self.root, 'lake', {'created': b'new', 'last': b'new'})
        self.assertTrue((self.root/'created').exists())
        recover_publication(self.root)
        self.assertFalse((self.root/'created').exists())

    def test_committed_paper_pair_tampering_blocks_restart_and_overwrite(self):
        account = self.paper()
        (self.root/'ledger.csv').write_bytes(b'mismatched independent ledger')
        with self.assertRaisesRegex(PublicationError, 'joint publication'):
            recover_publication(self.root, 'paper')
        with self.assertRaises(PublicationError):
            account.save()
        self.assertEqual((self.root/'ledger.csv').read_bytes(), b'mismatched independent ledger')

    def test_lake_reader_rejects_incomplete_generation(self):
        from quant_lake import QuantDataLake
        (self.root/'ingestion_manifest.json').write_text('{"chunks":{}}')
        reader = QuantDataLake(self.root)
        self.addCleanup(reader.close)
        real = os.replace
        def interrupted(src, dst):
            if Path(dst) == self.root/'ingestion_manifest.json':
                raise KeyboardInterrupt()
            return real(src, dst)
        with patch('quant_publication.os.replace', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                publish_files(self.root, 'lake', {'partition': b'new', 'ingestion_manifest.json': b'{"chunks":{}}'})
        with self.assertRaises(PublicationError):
            reader.query('NIFTY')
        recover_publication(self.root)
        with self.assertRaisesRegex(PublicationError, 'stale'):
            reader.query('NIFTY')
        reader.refresh()
        self.assertTrue(reader.query('NIFTY').empty)

    def test_paper_constructor_recovers_interrupted_pair_under_account_lock(self):
        from paper_engine import AccountLock
        from test_paper_trader import config, ASSET
        account = PaperAccount(self.root, config(), ASSET, lambda _: None)
        before = {name: (self.root/name).read_bytes() for name in ('state.json', 'ledger.csv')}
        account.state['synthetic_marker'] = 'new'
        real = os.replace
        def interrupted(src, dst):
            if Path(dst) == self.root/'ledger.csv':
                raise KeyboardInterrupt()
            return real(src, dst)
        with patch('quant_publication.os.replace', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                account.save()
        with AccountLock(self.root):
            restarted = PaperAccount(self.root, config(), ASSET, lambda _: None)
        self.assertNotIn('synthetic_marker', restarted.state)
        self.assertEqual((self.root/'ledger.csv').read_bytes(), before['ledger.csv'])
        self.assertEqual((self.root/'state.json').read_bytes(), before['state.json'])

    def test_corrupt_backup_remains_blocked_without_overwrite(self):
        (self.root/'a').write_bytes(b'old')
        real = os.replace
        def interrupted(src, dst):
            if Path(dst) == self.root/'b':
                raise KeyboardInterrupt()
            return real(src, dst)
        with patch('quant_publication.os.replace', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                publish_files(self.root, 'lake', {'a': b'new', 'b': b'new'})
        journal = json.loads((self.root/'.publication/lake/journal.json').read_text())
        (self.root/'.publication/lake'/journal['transaction']/'backup/0').write_bytes(b'damaged')
        with self.assertRaises(PublicationError):
            recover_publication(self.root)
        with self.assertRaises(PublicationError):
            publish_files(self.root, 'lake', {'a': b'never'})
        self.assertEqual((self.root/'a').read_bytes(), b'new')

    def test_actual_process_death_and_fresh_process_recovery(self):
        for name in ('first', 'last'):
            (self.root/name).write_bytes(('old ' + name).encode())
        script = '''
import os, sys
from pathlib import Path
from quant_publication import publish_files
root=Path(sys.argv[1]); real=os.replace
def crash(src,dst):
    if Path(dst)==root/'last': os._exit(91)
    return real(src,dst)
os.replace=crash
publish_files(root,'lake',{'first':b'new first','last':b'new last'})
'''
        crashed = subprocess.run([sys.executable, '-B', '-c', script, str(self.root)], capture_output=True, text=True)
        self.assertEqual(crashed.returncode, 91, crashed.stderr)
        self.assertEqual((self.root/'first').read_bytes(), b'new first')
        recovered = subprocess.run([sys.executable, '-B', '-c',
            'import sys; from pathlib import Path; from step2_build_data_lake import writer_lock; '
            'from quant_publication import recover_publication; '
            '\nwith writer_lock(Path(sys.argv[1])): recover_publication(Path(sys.argv[1]))',
            str(self.root)], capture_output=True, text=True)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        for name in ('first', 'last'):
            self.assertEqual((self.root/name).read_bytes(), ('old ' + name).encode())

    def test_denied_commit_marker_rolls_back_and_keeps_primary_failure(self):
        (self.root/'a').write_bytes(b'old')
        import quant_publication as publication
        write_json = publication._json
        def deny_commit(path, value):
            if path == self.root/'.publication/lake/journal.json' and value['phase'] == 'COMMITTED':
                raise PermissionError('synthetic commit marker denied')
            return write_json(path, value)
        with patch('quant_publication._json', side_effect=deny_commit):
            with self.assertRaises(PublicationError):
                publish_files(self.root, 'lake', {'a': b'new'})
        self.assertEqual((self.root/'a').read_bytes(), b'old')
        journal = json.loads((self.root/'.publication/lake/journal.json').read_text())
        self.assertEqual(journal['phase'], 'ROLLED_BACK')
        self.assertEqual(journal['primary_failure']['exception'], 'PermissionError')

    def test_preparation_write_failure_preserves_targets(self):
        (self.root/'a').write_bytes(b'old')
        import quant_publication as publication
        write = publication._write
        def disk_full(path, payload):
            if 'new' in path.parts:
                raise OSError('synthetic preparation disk full')
            return write(path, payload)
        with patch('quant_publication._write', side_effect=disk_full):
            with self.assertRaises(OSError):
                publish_files(self.root, 'lake', {'a': b'new'})
        self.assertEqual((self.root/'a').read_bytes(), b'old')
        self.assertIsNone(assert_publication_ready(self.root))

    def test_post_commit_acknowledgement_failure_never_rolls_back_visible_version(self):
        (self.root/'a').write_bytes(b'old')
        import quant_publication as publication
        write_json=publication._json
        def lost_ack(path,value):
            result=write_json(path,value)
            if path == self.root/'.publication/lake/journal.json' and value['phase']=='COMMITTED':
                raise OSError('synthetic post-rename fsync failure')
            return result
        with patch('quant_publication._json',side_effect=lost_ack):
            with self.assertRaisesRegex(PublicationError,'committed and hashes verified'):
                publish_files(self.root,'lake',{'a':b'new','b':b'new b'})
        self.assertEqual((self.root/'a').read_bytes(),b'new')
        self.assertEqual((self.root/'b').read_bytes(),b'new b')
        self.assertEqual(json.loads((self.root/'.publication/lake/journal.json').read_text())['phase'],'COMMITTED')

    def test_direct_builder_manifest_failure_preserves_disk_and_memory(self):
        from step2_build_data_lake import Builder
        from test_quant_lake import sample, META
        with patch('step2_build_data_lake.API') as api:
            api.return_value.post.return_value=sample(['2025-01-01 09:15'])
            builder=Builder(self.root,date(2025,1,1),date(2025,1,3))
            args=('spot_1m',META,date(2025,1,1))
            record=builder.chunk(*args,date(2025,1,2),'/charts/intraday',{})
            before={name:(self.root/name).read_bytes() for name in (record['path'],'ingestion_manifest.json')}
            memory=json.loads(json.dumps(builder.manifest))
            api.return_value.post.return_value=sample(['2025-01-01 09:15','2025-01-02 09:15'])
            with patch('step2_build_data_lake.atomic_json',side_effect=OSError('synthetic manifest denial')):
                with self.assertRaises(OSError):
                    builder.chunk(*args,date(2025,1,3),'/charts/intraday',{})
            for name,payload in before.items():
                self.assertEqual((self.root/name).read_bytes(),payload)
            self.assertEqual(builder.manifest,memory)

    def test_direct_builder_interrupted_commit_recovers_on_writer_restart(self):
        from step2_build_data_lake import Builder, writer_lock
        from test_quant_lake import sample, META
        with patch('step2_build_data_lake.API') as api:
            api.return_value.post.return_value=sample(['2025-01-01 09:15'])
            builder=Builder(self.root,date(2025,1,1),date(2025,1,3))
            args=('spot_1m',META,date(2025,1,1))
            record=builder.chunk(*args,date(2025,1,2),'/charts/intraday',{})
            before={name:(self.root/name).read_bytes() for name in (record['path'],'ingestion_manifest.json')}
            memory=json.loads(json.dumps(builder.manifest))
            api.return_value.post.return_value=sample(['2025-01-01 09:15','2025-01-02 09:15'])
            with patch('step2_build_data_lake.atomic_json',side_effect=KeyboardInterrupt('synthetic stop')):
                with self.assertRaises(KeyboardInterrupt):
                    builder.chunk(*args,date(2025,1,3),'/charts/intraday',{})
            self.assertEqual(builder.manifest,memory)
            with self.assertRaises(PublicationError):
                Builder(self.root,date(2025,1,1),date(2025,1,3))
            with writer_lock(self.root):
                restarted=Builder(self.root,date(2025,1,1),date(2025,1,3))
            self.assertEqual(restarted.manifest,memory)
            for name,payload in before.items():
                self.assertEqual((self.root/name).read_bytes(),payload)

    def test_classified_transient_windows_replacement_recovers_with_evidence(self):
        for kind in ('journal','target'):
            with self.subTest(kind=kind):
                root=self.root/kind;root.mkdir();(root/'a').write_bytes(b'old')
                real=os.replace; failures=[]
                def sharing_denial(src,dst):
                    match=(Path(dst).name=='journal.json' and Path(dst).parent.name!='lake') if kind=='journal' else Path(dst)==root/'a'
                    if match and not failures:
                        exc=PermissionError(13,'synthetic sharing interference');exc.winerror=5
                        failures.append(True);raise exc
                    return real(src,dst)
                with patch('quant_publication.os.replace',side_effect=sharing_denial):
                    publish_files(root,'lake',{'a':b'new'})
                self.assertEqual((root/'a').read_bytes(),b'new')
                events=[json.loads(line) for line in (root/'.publication/lake/replacement_events.jsonl').read_text().splitlines()]
                self.assertEqual(len(events),1)
                self.assertEqual(events[0]['winerror'],5)
                self.assertTrue(events[0]['will_retry'])

    def test_persistent_windows_denial_exhausts_bound_then_verified_rollback(self):
        (self.root/'a').write_bytes(b'old')
        real=os.replace;calls=[]
        def persistent(src,dst):
            if Path(dst)==self.root/'a':
                calls.append(True)
                exc=PermissionError(13,'synthetic persistent denial');exc.winerror=5
                raise exc
            return real(src,dst)
        with patch('quant_publication.os.replace',side_effect=persistent),patch('quant_publication.time.sleep') as sleep:
            with self.assertRaises(PublicationError):
                publish_files(self.root,'lake',{'a':b'new'})
        self.assertEqual(len(calls),6)
        self.assertEqual(sleep.call_count,5)
        self.assertEqual((self.root/'a').read_bytes(),b'old')
        journal=json.loads((self.root/'.publication/lake/journal.json').read_text())
        self.assertEqual(journal['phase'],'ROLLED_BACK')
        self.assertEqual(journal['primary_failure']['winerror'],5)
        self.assertEqual(journal['primary_failure']['replacement_attempts'],6)
        events=[json.loads(line) for line in (self.root/'.publication/lake/replacement_events.jsonl').read_text().splitlines()]
        self.assertEqual(len(events),6)
        self.assertFalse(events[-1]['will_retry'])

    def test_unclassified_permission_denial_is_not_retried(self):
        (self.root/'a').write_bytes(b'old')
        real=os.replace;calls=[]
        def denied(src,dst):
            if Path(dst)==self.root/'a':
                calls.append(True);raise PermissionError('synthetic unclassified denial')
            return real(src,dst)
        with patch('quant_publication.os.replace',side_effect=denied):
            with self.assertRaises(PublicationError):
                publish_files(self.root,'lake',{'a':b'new'})
        self.assertEqual(len(calls),1)
        self.assertEqual((self.root/'a').read_bytes(),b'old')


if __name__ == '__main__':
    unittest.main()
