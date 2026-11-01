"""Recoverable local multi-file publication, without claiming multi-file atomicity.

The caller holds its domain lock (lake writer/account runner). A second OS lock
serializes publications and readers of one namespace. Immutable old/new bytes
and a write-ahead journal live beneath root/.publication/<namespace>/<txid>/,
outside callers' TemporaryDirectory. PREPARED/PUBLISHING always recover to the
old version; only a verified COMMITTED journal selects the new version. A
ROLLBACK_REQUIRED journal blocks reads/writes until verified recovery succeeds.
No prices, candles, or account fields are synthesized by this protocol.
"""
from contextlib import contextmanager
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

PROTOCOL_VERSION = 1
TERMINAL = frozenset(('COMMITTED', 'ROLLED_BACK'))
REPLACE_ATTEMPTS = 6
TRANSIENT_WINDOWS_REPLACE_ERRORS = frozenset((5, 32, 33))


class PublicationError(OSError):
    """A publication or its recovery failed; the durable journal is evidence."""


def _sha(path):
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _sync_directory(path):
    # Windows does not expose directory fsync through Python. Every file is
    # flushed before replace; POSIX additionally persists directory entries.
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _replace(source, target):
    """Bound sharing-interference retries; the journal remains the safety model.

    WinError 5 can also mean persistent access denial, so it is only classified
    as *potentially* transient. Six failures still raise and invoke the same
    verified rollback/recovery protocol. Plain PermissionError/EACCES without a
    Windows code is never retried. No unbounded or generic exception retries.
    """
    source=Path(source).absolute();target=Path(target).absolute()
    base=next((p for p in source.parents if p.name in ('lake','paper') and p.parent.name=='.publication'),None)
    failures=[]
    for attempt in range(1,REPLACE_ATTEMPTS+1):
        try:
            return os.replace(source,target)
        except OSError as exc:
            classified=getattr(exc,'winerror',None) in TRANSIENT_WINDOWS_REPLACE_ERRORS
            retry=classified and attempt<REPLACE_ATTEMPTS
            event=dict(exception=type(exc).__name__,errno=getattr(exc,'errno',None),
                       winerror=getattr(exc,'winerror',None),attempt=attempt,will_retry=retry,
                       source=str(source.relative_to(base)) if base is not None else source.name,
                       destination=str(target.relative_to(base.parent.parent)) if base is not None else target.name)
            failures.append(event)
            # Store only nonsecret failure metadata, not exception payloads.
            # This append does not itself use replace and cannot recurse.
            if classified and base is not None:
                try:
                    with (base/'replacement_events.jsonl').open('a',encoding='utf-8') as stream:
                        stream.write(json.dumps(event,sort_keys=True)+'\n')
                        stream.flush();os.fsync(stream.fileno())
                except OSError as logging_error:
                    exc.publication_retry_log_error=type(logging_error).__name__
                    # Do not hide a transient failure behind a successful retry
                    # if its durable evidence itself could not be recorded.
                    retry=False;event['will_retry']=False
            exc.publication_replacement_attempts=attempt
            exc.publication_replacement_failures=failures
            if not retry:
                raise
            time.sleep(min(.01*2**(attempt-1),.1))


def _json(path, value):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    _write(tmp, json.dumps(value, indent=2, sort_keys=True, allow_nan=False).encode('utf-8'))
    _replace(tmp, path)
    _sync_directory(path.parent)


def _base(root, namespace):
    if namespace not in ('lake', 'paper'):
        raise ValueError('Unknown publication namespace')
    return Path(root)/'.publication'/namespace


def _target(root, relative):
    root = Path(root).resolve()
    target = (root/relative).resolve()
    if target == root or root not in target.parents or '.publication' in Path(relative).parts:
        raise ValueError('Publication target outside domain or inside journal')
    return target


def _failure(exc, phase, relative=''):
    # Exception text may contain provider payloads; record only safe metadata.
    result=dict(phase=phase, path=relative, exception=type(exc).__name__,
                errno=getattr(exc, 'errno', None), winerror=getattr(exc, 'winerror', None))
    if hasattr(exc,'publication_replacement_attempts'):
        result['replacement_attempts']=exc.publication_replacement_attempts
        result['replacement_failures']=exc.publication_replacement_failures
    if hasattr(exc,'publication_retry_log_error'):
        result['retry_log_error']=exc.publication_retry_log_error
    return result


@contextmanager
def publication_lock(root, namespace):
    base = _base(root, namespace)
    base.mkdir(parents=True, exist_ok=True)
    with (base/'lock').open('a+b') as stream:
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PublicationError('Publication busy; retry after its owner releases the lock') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _load(root, namespace):
    path = _base(root, namespace)/'journal.json'
    if not path.exists():
        return None
    try:
        journal = json.loads(path.read_text(encoding='utf-8'))
        if journal['protocol'] != PROTOCOL_VERSION or not isinstance(journal['entries'], list):
            raise ValueError('Invalid publication journal schema')
        if not isinstance(journal['transaction'], str) or not re.fullmatch('[0-9a-f]{32}', journal['transaction']):
            raise ValueError('Invalid transaction identity')
        if journal['phase'] not in TERMINAL | {'PREPARED', 'PUBLISHING', 'ROLLBACK_REQUIRED'}:
            raise ValueError('Invalid publication phase')
        for entry in journal['entries']:
            _target(root, entry['path'])
        return journal
    except Exception as exc:
        raise PublicationError('Unreadable publication journal; refuse mixed or unknown state') from exc


def assert_publication_ready(root, namespace='lake'):
    """Read-only guard. Never repairs/migrates stored data as a side effect."""
    journal = _load(root, namespace)
    if journal and journal['phase'] not in TERMINAL:
        raise PublicationError('Unresolved publication ' + journal['transaction'] +
                               '; recovery under the domain writer lock is required')
    return journal['transaction'] if journal else None


def _record(root, namespace, journal):
    base = _base(root, namespace)
    # A terminal pointer is the commit/recovery decision, written last. While
    # appending its durable evidence readers still see the nonterminal barrier.
    if journal['phase'] in TERMINAL:
        _json(base/journal['transaction']/'journal.json', journal)
        _json(base/'journal.json', journal)
    else:
        _json(base/'journal.json', journal)
        _json(base/journal['transaction']/'journal.json', journal)


def _install(source, target):
    # Copy instead of consume immutable backups, so a denied replace or process
    # death cannot destroy the sole recovery copy.
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = source.parent/(source.name + '.install-' + uuid.uuid4().hex)
    shutil.copyfile(source, tmp)
    with tmp.open('r+b') as stream:
        stream.flush()
        os.fsync(stream.fileno())
    _replace(tmp, target)
    _sync_directory(target.parent)


def _rollback(root, namespace, journal):
    base = _base(root, namespace)/journal['transaction']
    errors = []
    for entry in reversed(journal['entries']):
        target = _target(root, entry['path'])
        try:
            if _sha(target) != entry['old_sha256']:
                if entry['old_sha256'] is None:
                    target.unlink(missing_ok=True)
                    _sync_directory(target.parent)
                else:
                    old = base/'backup'/entry['slot']
                    if _sha(old) != entry['old_sha256']:
                        raise PublicationError('Recovery backup hash mismatch')
                    _install(old, target)
            if _sha(target) != entry['old_sha256']:
                raise PublicationError('Restored target hash mismatch')
        except Exception as exc:
            errors.append(_failure(exc, 'rollback', entry['path']))
    journal.setdefault('recovery_attempts', []).append(dict(errors=errors))
    journal['phase'] = 'ROLLBACK_REQUIRED' if errors else 'ROLLED_BACK'
    try:
        _record(root, namespace, journal)
    except Exception as exc:
        # The previous nonterminal authoritative journal remains a read barrier.
        errors.append(_failure(exc, 'record_rollback'))
    if errors:
        raise PublicationError('Recovery incomplete; secondary failures: ' + json.dumps(errors, sort_keys=True))
    return journal


def recover_publication(root, namespace='lake', *, _locked=False):
    """Call only while holding the lake writer/account domain lock."""
    if not _locked:
        with publication_lock(root, namespace):
            return recover_publication(root, namespace, _locked=True)
    journal = _load(root, namespace)
    if not journal:
        return journal
    if journal['phase'] in TERMINAL:
        # Paper readers require the exact joint version. Lake readers already
        # check manifest/file hashes through their quality/eligibility pipeline;
        # hashing every large partition for every metadata read is unnecessary.
        if namespace == 'paper':
            field = 'new_sha256' if journal['phase'] == 'COMMITTED' else 'old_sha256'
            if any(_sha(_target(root, entry['path'])) != entry[field] for entry in journal['entries']):
                raise PublicationError('Paper state/ledger differs from the verified joint publication; refuse overwrite')
        return journal
    return _rollback(root, namespace, journal)


def publish_files(root, namespace, files, *, final_writer=None, _locked=False):
    """Publish exact bytes/Paths. final_writer optionally writes the last target.

    Any ordinary exception triggers best-effort rollback of *every* target and
    hash verification. Process death/BaseException leaves a recoverable journal.
    A returned success means every new target hash and COMMITTED are durable.
    """
    if not _locked:
        with publication_lock(root, namespace):
            return publish_files(root, namespace, files, final_writer=final_writer, _locked=True)
    recover_publication(root, namespace, _locked=True)
    transaction = uuid.uuid4().hex
    base = _base(root, namespace)/transaction
    entries = []
    # Preparation never mutates public targets. A failed pre-journal preparation
    # leaves only unreferenced durable bytes, never a mixed readable generation.
    for relative, content in files.items():
        target = _target(root, relative)
        slot = str(len(entries))
        old = _sha(target)
        if old is not None:
            _write(base/'backup'/slot, target.read_bytes())
            if _sha(base/'backup'/slot) != old:
                raise PublicationError('Target changed during publication preparation')
        payload = content.read_bytes() if isinstance(content, Path) else content
        _write(base/'new'/slot, payload)
        entries.append(dict(path=relative, slot=slot, old_sha256=old, new_sha256=_sha(base/'new'/slot)))
    journal = dict(protocol=PROTOCOL_VERSION, transaction=transaction, phase='PREPARED', entries=entries,
                   recovery_attempts=[])
    _record(root, namespace, journal)
    try:
        journal['phase'] = 'PUBLISHING'
        _record(root, namespace, journal)
        for index, entry in enumerate(entries):
            target = _target(root, entry['path'])
            if final_writer is not None and index == len(entries)-1:
                final_writer(target)
            else:
                _install(base/'new'/entry['slot'], target)
            if _sha(target) != entry['new_sha256']:
                raise PublicationError('Published target hash mismatch')
        journal['phase'] = 'COMMITTED'
        _record(root, namespace, journal)
    except Exception as exc:
        # The authoritative COMMITTED pointer is the point of no return. A
        # later directory-flush acknowledgement can fail after readers already
        # selected the verified new generation; rolling it back would expose a
        # mixed generation under a terminal marker. Report uncertainty instead.
        visible = _load(root, namespace)
        if visible and visible['transaction'] == transaction and visible['phase'] == 'COMMITTED':
            if all(_sha(_target(root, entry['path'])) == entry['new_sha256'] for entry in entries):
                raise PublicationError('Joint version committed and hashes verified; durability acknowledgement failed; no rollback performed') from exc
            raise PublicationError('Committed publication changed unexpectedly; preserve evidence and stop') from exc
        journal['primary_failure'] = _failure(exc, 'publish')
        journal['phase'] = 'ROLLBACK_REQUIRED'
        try:
            _record(root, namespace, journal)
        except Exception as secondary:
            journal['journal_failure'] = _failure(secondary, 'record_failure')
        try:
            _rollback(root, namespace, journal)
        except Exception as secondary:
            raise PublicationError('Publication failed (' + type(exc).__name__ +
                                   '); ' + str(secondary)) from exc
        raise PublicationError('Publication failed (' + type(exc).__name__ +
                               '); all original hashes restored and verified') from exc
    return journal


def lake_consistent_read(method):
    """Detect publication overlapping a query, including a completed new version."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        before = assert_publication_ready(self.root)
        if method.__name__ != 'refresh' and getattr(self, '_publication_generation', before) != before:
            raise PublicationError('Lake manifest is stale after publication; refresh before querying')
        result = method(self, *args, **kwargs)
        after = assert_publication_ready(self.root)
        if before != after:
            raise PublicationError('Lake changed during read; discard this query and retry')
        if method.__name__ == 'refresh':
            self._publication_generation = after
        return result
    return wrapped
