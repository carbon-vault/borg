from datetime import datetime, timedelta, timezone
from getpass import getuser
from itertools import groupby
import errno
import shutil
import tempfile
from attic.key import key_factory
import msgpack
import os
import socket
import stat
import sys
import time
from io import BytesIO
from attic import xattr
from attic.chunker import chunkify
from attic.hashindex import ChunkIndex
from attic.helpers import Error, uid2user, user2uid, gid2group, group2gid, \
    Manifest, Statistics, decode_dict, st_mtime_ns, make_path_safe, StableDict

ITEMS_BUFFER = 1024 * 1024
CHUNK_MIN = 1024
WINDOW_SIZE = 0xfff
CHUNK_MASK = 0xffff

utime_supports_fd = os.utime in getattr(os, 'supports_fd', {})
has_mtime_ns = sys.version >= '3.3'
has_lchmod = hasattr(os, 'lchmod')


class DownloadPipeline:

    def __init__(self, repository, key):
        self.repository = repository
        self.key = key

    def unpack_many(self, ids, filter=None, preload=False):
        unpacker = msgpack.Unpacker(use_list=False)
        for data in self.fetch_many(ids):
            unpacker.feed(data)
            items = [decode_dict(item, (b'path', b'source', b'user', b'group')) for item in unpacker]
            if filter:
                items = [item for item in items if filter(item)]
            if preload:
                for item in items:
                    if b'chunks' in item:
                        self.repository.preload([c[0] for c in item[b'chunks']])
            for item in items:
                yield item

    def fetch_many(self, ids, is_preloaded=False):
        for id_, data in zip(ids, self.repository.get_many(ids, is_preloaded=is_preloaded)):
            yield self.key.decrypt(id_, data)


class ChunkBuffer:
    BUFFER_SIZE = 1 * 1024 * 1024

    def __init__(self, key):
        self.buffer = BytesIO()
        self.packer = msgpack.Packer(unicode_errors='surrogateescape')
        self.chunks = []
        self.key = key

    def add(self, item):
        self.buffer.write(self.packer.pack(StableDict(item)))
        if self.is_full():
            self.flush()

    def write_chunk(self, chunk):
        raise NotImplementedError

    def flush(self, flush=False):
        if self.buffer.tell() == 0:
            return
        self.buffer.seek(0)
        chunks = list(bytes(s) for s in chunkify(self.buffer, WINDOW_SIZE, CHUNK_MASK, CHUNK_MIN, self.key.chunk_seed))
        self.buffer.seek(0)
        self.buffer.truncate(0)
        # Leave the last parital chunk in the buffer unless flush is True
        end = None if flush or len(chunks) == 1 else -1
        for chunk in chunks[:end]:
            self.chunks.append(self.write_chunk(chunk))
        if end == -1:
            self.buffer.write(chunks[-1])

    def is_full(self):
        return self.buffer.tell() > self.BUFFER_SIZE


class CacheChunkBuffer(ChunkBuffer):

    def __init__(self, cache, key, stats):
        super(CacheChunkBuffer, self).__init__(key)
        self.cache = cache
        self.stats = stats

    def write_chunk(self, chunk):
        id_, _, _ = self.cache.add_chunk(self.key.id_hash(chunk), chunk, self.stats)
        return id_


class Archive:

    class DoesNotExist(Error):
        """Archive {} does not exist"""

    class AlreadyExists(Error):
        """Archive {} already exists"""

    def __init__(self, repository, key, manifest, name, cache=None, create=False,
                 checkpoint_interval=300, numeric_owner=False):
        self.cwd = os.getcwd()
        self.key = key
        self.repository = repository
        self.cache = cache
        self.manifest = manifest
        self.hard_links = {}
        self.stats = Statistics()
        self.name = name
        self.checkpoint_interval = checkpoint_interval
        self.numeric_owner = numeric_owner
        self.items_buffer = CacheChunkBuffer(self.cache, self.key, self.stats)
        self.pipeline = DownloadPipeline(self.repository, self.key)
        if create:
            if name in manifest.archives:
                raise self.AlreadyExists(name)
            self.last_checkpoint = time.time()
            i = 0
            while True:
                self.checkpoint_name = '%s.checkpoint%s' % (name, i and ('.%d' % i) or '')
                if not self.checkpoint_name in manifest.archives:
                    break
                i += 1
        else:
            if name not in self.manifest.archives:
                raise self.DoesNotExist(name)
            info = self.manifest.archives[name]
            self.load(info[b'id'])

    def load(self, id):
        self.id = id
        data = self.key.decrypt(self.id, self.repository.get(self.id))
        self.metadata = msgpack.unpackb(data)
        if self.metadata[b'version'] != 1:
            raise Exception('Unknown archive metadata version')
        decode_dict(self.metadata, (b'name', b'hostname', b'username', b'time'))
        self.metadata[b'cmdline'] = [arg.decode('utf-8', 'surrogateescape') for arg in self.metadata[b'cmdline']]
        self.name = self.metadata[b'name']

    @property
    def ts(self):
        """Timestamp of archive creation in UTC"""
        t, f = self.metadata[b'time'].split('.', 1)
        return datetime.strptime(t, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc) + timedelta(seconds=float('.' + f))

    def __repr__(self):
        return 'Archive(%r)' % self.name

    def iter_items(self, filter=None, preload=False):
        for item in self.pipeline.unpack_many(self.metadata[b'items'], filter=filter, preload=preload):
            yield item

    def add_item(self, item):
        self.items_buffer.add(item)
        now = time.time()
        if now - self.last_checkpoint > self.checkpoint_interval:
            self.last_checkpoint = now
            self.write_checkpoint()

    def write_checkpoint(self):
        self.save(self.checkpoint_name)
        del self.manifest.archives[self.checkpoint_name]
        self.cache.chunk_decref(self.id)

    def save(self, name=None):
        name = name or self.name
        if name in self.manifest.archives:
            raise self.AlreadyExists(name)
        self.items_buffer.flush(flush=True)
        metadata = StableDict({
            'version': 1,
            'name': name,
            'items': self.items_buffer.chunks,
            'cmdline': sys.argv,
            'hostname': socket.gethostname(),
            'username': getuser(),
            'time': datetime.utcnow().isoformat(),
        })
        data = msgpack.packb(metadata, unicode_errors='surrogateescape')
        self.id = self.key.id_hash(data)
        self.cache.add_chunk(self.id, data, self.stats)
        self.manifest.archives[name] = {'id': self.id, 'time': metadata['time']}
        self.manifest.write()
        self.repository.commit()
        self.cache.commit()

    def calc_stats(self, cache):
        def add(id):
            count, size, csize = self.cache.chunks[id]
            stats.update(size, csize, count == 1)
            self.cache.chunks[id] = count - 1, size, csize
        def add_file_chunks(chunks):
            for id, _, _ in chunks:
                add(id)
        # This function is a bit evil since it abuses the cache to calculate
        # the stats. The cache transaction must be rolled back afterwards
        unpacker = msgpack.Unpacker(use_list=False)
        cache.begin_txn()
        stats = Statistics()
        add(self.id)
        for id, chunk in zip(self.metadata[b'items'], self.repository.get_many(self.metadata[b'items'])):
            add(id)
            unpacker.feed(self.key.decrypt(id, chunk))
            for item in unpacker:
                if b'chunks' in item:
                    stats.nfiles += 1
                    add_file_chunks(item[b'chunks'])
        cache.rollback()
        return stats

    def extract_item(self, item, restore_attrs=True, dry_run=False):
        if dry_run:
            if b'chunks' in item:
                for _ in self.pipeline.fetch_many([c[0] for c in item[b'chunks']], is_preloaded=True):
                    pass
            return

        dest = self.cwd
        if item[b'path'].startswith('/') or item[b'path'].startswith('..'):
            raise Exception('Path should be relative and local')
        path = os.path.join(dest, item[b'path'])
        # Attempt to remove existing files, ignore errors on failure
        try:
            st = os.lstat(path)
            if stat.S_ISDIR(st.st_mode):
                os.rmdir(path)
            else:
                os.unlink(path)
        except OSError:
            pass
        mode = item[b'mode']
        if stat.S_ISDIR(mode):
            if not os.path.exists(path):
                os.makedirs(path)
            if restore_attrs:
                self.restore_attrs(path, item)
        elif stat.S_ISREG(mode):
            if not os.path.exists(os.path.dirname(path)):
                os.makedirs(os.path.dirname(path))
            # Hard link?
            if b'source' in item:
                source = os.path.join(dest, item[b'source'])
                if os.path.exists(path):
                    os.unlink(path)
                os.link(source, path)
            else:
                with open(path, 'wb') as fd:
                    ids = [c[0] for c in item[b'chunks']]
                    for data in self.pipeline.fetch_many(ids, is_preloaded=True):
                        fd.write(data)
                    fd.flush()
                    self.restore_attrs(path, item, fd=fd.fileno())
        elif stat.S_ISFIFO(mode):
            if not os.path.exists(os.path.dirname(path)):
                os.makedirs(os.path.dirname(path))
            os.mkfifo(path)
            self.restore_attrs(path, item)
        elif stat.S_ISLNK(mode):
            if not os.path.exists(os.path.dirname(path)):
                os.makedirs(os.path.dirname(path))
            source = item[b'source']
            if os.path.exists(path):
                os.unlink(path)
            os.symlink(source, path)
            self.restore_attrs(path, item, symlink=True)
        elif stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
            os.mknod(path, item[b'mode'], item[b'rdev'])
            self.restore_attrs(path, item)
        else:
            raise Exception('Unknown archive item type %r' % item[b'mode'])

    def restore_attrs(self, path, item, symlink=False, fd=None):
        xattrs = item.get(b'xattrs')
        if xattrs:
                for k, v in xattrs.items():
                    try:
                        xattr.setxattr(fd or path, k, v)
                    except OSError as e:
                        if e.errno != errno.ENOTSUP:
                            raise
        uid = gid = None
        if not self.numeric_owner:
            uid = user2uid(item[b'user'])
            gid = group2gid(item[b'group'])
        uid = uid or item[b'uid']
        gid = gid or item[b'gid']
        # This code is a bit of a mess due to os specific differences
        try:
            if fd:
                os.fchown(fd, uid, gid)
            else:
                os.lchown(path, uid, gid)
        except OSError:
            pass
        if fd:
            os.fchmod(fd, item[b'mode'])
        elif not symlink:
            os.chmod(path, item[b'mode'])
        elif has_lchmod:  # Not available on Linux
            os.lchmod(path, item[b'mode'])
        if fd and utime_supports_fd:  # Python >= 3.3
            os.utime(fd, None, ns=(item[b'mtime'], item[b'mtime']))
        elif utime_supports_fd:  # Python >= 3.3
            os.utime(path, None, ns=(item[b'mtime'], item[b'mtime']), follow_symlinks=False)
        elif not symlink:
            os.utime(path, (item[b'mtime'] / 10**9, item[b'mtime'] / 10**9))

    def delete(self, cache):
        unpacker = msgpack.Unpacker(use_list=False)
        for id_, data in zip(self.metadata[b'items'], self.repository.get_many(self.metadata[b'items'])):
            unpacker.feed(self.key.decrypt(id_, data))
            self.cache.chunk_decref(id_)
            for item in unpacker:
                if b'chunks' in item:
                    for chunk_id, size, csize in item[b'chunks']:
                        self.cache.chunk_decref(chunk_id)

        self.cache.chunk_decref(self.id)
        del self.manifest.archives[self.name]
        self.manifest.write()
        self.repository.commit()
        cache.commit()

    def stat_attrs(self, st, path):
        item = {
            b'mode': st.st_mode,
            b'uid': st.st_uid, b'user': uid2user(st.st_uid),
            b'gid': st.st_gid, b'group': gid2group(st.st_gid),
            b'mtime': st_mtime_ns(st),
        }
        if self.numeric_owner:
            item[b'user'] = item[b'group'] = None
        xattrs = xattr.get_all(path, follow_symlinks=False)
        if xattrs:
            item[b'xattrs'] = StableDict(xattrs)
        return item

    def process_item(self, path, st):
        item = {b'path': make_path_safe(path)}
        item.update(self.stat_attrs(st, path))
        self.add_item(item)

    def process_dev(self, path, st):
        item = {b'path': make_path_safe(path), b'rdev': st.st_rdev}
        item.update(self.stat_attrs(st, path))
        self.add_item(item)

    def process_symlink(self, path, st):
        source = os.readlink(path)
        item = {b'path': make_path_safe(path), b'source': source}
        item.update(self.stat_attrs(st, path))
        self.add_item(item)

    def process_file(self, path, st, cache):
        safe_path = make_path_safe(path)
        # Is it a hard link?
        if st.st_nlink > 1:
            source = self.hard_links.get((st.st_ino, st.st_dev))
            if (st.st_ino, st.st_dev) in self.hard_links:
                item = self.stat_attrs(st, path)
                item.update({b'path': safe_path, b'source': source})
                self.add_item(item)
                return
            else:
                self.hard_links[st.st_ino, st.st_dev] = safe_path
        path_hash = self.key.id_hash(os.path.join(self.cwd, path).encode('utf-8', 'surrogateescape'))
        ids = cache.file_known_and_unchanged(path_hash, st)
        chunks = None
        if ids is not None:
            # Make sure all ids are available
            for id_ in ids:
                if not cache.seen_chunk(id_):
                    break
            else:
                chunks = [cache.chunk_incref(id_, self.stats) for id_ in ids]
        # Only chunkify the file if needed
        if chunks is None:
            with open(path, 'rb') as fd:
                chunks = []
                for chunk in chunkify(fd, WINDOW_SIZE, CHUNK_MASK, CHUNK_MIN, self.key.chunk_seed):
                    chunks.append(cache.add_chunk(self.key.id_hash(chunk), chunk, self.stats))
            cache.memorize_file(path_hash, st, [c[0] for c in chunks])
        item = {b'path': safe_path, b'chunks': chunks}
        item.update(self.stat_attrs(st, path))
        self.stats.nfiles += 1
        self.add_item(item)

    @staticmethod
    def list_archives(repository, key, manifest, cache=None):
        for name, info in manifest.archives.items():
            yield Archive(repository, key, manifest, name, cache=cache)


class ArchiveChecker:

    def __init__(self):
        self.error_found = False
        self.progress = True
        self.possibly_superseded = set()
        self.tmpdir = tempfile.mkdtemp()

    def __del__(self):
        shutil.rmtree(self.tmpdir)

    def init_chunks(self):
        # Explicity set the initial hash table capacity to avoid performance issues
        # due to hash table "resonance"
        capacity = int(len(self.repository) * 1.2)
        self.chunks = ChunkIndex.create(os.path.join(self.tmpdir, 'chunks').encode('utf-8'), capacity=capacity)
        marker = None
        while True:
            result = self.repository.list(limit=10000, marker=marker)
            if not result:
                break
            marker = result[-1]
            for id_ in result:
                self.chunks[id_] = (0, 0, 0)

    def report_progress(self, msg, error=False):
        if error:
            self.error_found = True
        if error or self.progress:
            print(msg, file=sys.stderr)
            sys.stderr.flush()

    def identify_key(self, repository):
        cdata = repository.get(next(self.chunks.iteritems())[0])
        return key_factory(repository, cdata)

    def rebuild_manifest(self):
        self.report_progress('Rebuilding missing manifest, this might take some time...', error=True)
        manifest = Manifest(self.key, self.repository)
        for chunk_id, _ in self.chunks.iteritems():
            cdata = self.repository.get(chunk_id)
            data = self.key.decrypt(chunk_id, cdata)
            try:
                archive = msgpack.unpackb(data)
            except:
                continue
            if isinstance(archive, dict) and b'items' in archive and b'cmdline' in archive:
                self.report_progress('Found archive ' + archive[b'name'].decode('utf-8'), error=True)
                manifest.archives[archive[b'name'].decode('utf-8')] = {b'id': chunk_id, b'time': archive[b'time']}
        self.report_progress('Manifest rebuild complete', error=True)
        return manifest

    def check(self, repository, progress=True, repair=False):
        self.report_progress('Starting archive consistency check...')
        self.repair = repair
        self.progress = progress
        self.repository = repository
        self.init_chunks()
        self.key = self.identify_key(repository)
        if not Manifest.MANIFEST_ID in self.chunks:
            self.manifest = self.rebuild_manifest()
        else:
            self.manifest, _ = Manifest.load(repository, key=self.key)
        self.rebuild_chunks()
        self.verify_chunks()
        if not self.error_found:
            self.report_progress('Archive consistency check complete, no problems found.')
        return self.repair or not self.error_found

    def verify_chunks(self):
        unused = set()
        for id_, (count, size, csize) in self.chunks.iteritems():
            if count == 0:
                unused.add(id_)
        orphaned = unused - self.possibly_superseded
        if orphaned:
            self.report_progress('{} orphaned objects found'.format(len(orphaned)), error=True)
        if self.repair:
            for id_ in unused:
                self.repository.delete(id_)
            self.manifest.write()
            self.repository.commit()

    def rebuild_chunks(self):
        # Exclude the manifest from chunks
        del self.chunks[Manifest.MANIFEST_ID]

        def record_unused(id_):
            if self.chunks.get(id_, (0,))[0] == 0:
                self.possibly_superseded.add(id_)

        def add_callback(chunk):
            id_ = self.key.id_hash(chunk)
            cdata = self.key.encrypt(chunk)
            add_reference(id_, len(chunk), len(cdata), cdata)
            return id_

        def add_reference(id_, size, csize, cdata=None):
            try:
                count, _, _ = self.chunks[id_]
                self.chunks[id_] = count + 1, size, csize
            except KeyError:
                assert cdata is not None
                self.chunks[id_] = 1, size, csize
                if self.repair:
                    self.repository.put(id_, cdata)

        def verify_file_chunks(item):
            offset = 0
            chunk_list = []
            for chunk_id, size, csize in item[b'chunks']:
                if not chunk_id in self.chunks:
                    # If a file chunk is missing, create an all empty replacement chunk
                    self.report_progress('{}: Missing file chunk detected (Byte {}-{})'.format(item[b'path'].decode('utf-8', 'surrogateescape'), offset, offset + size), error=True)
                    data = bytes(size)
                    chunk_id = self.key.id_hash(data)
                    cdata = self.key.encrypt(data)
                    csize = len(cdata)
                    add_reference(chunk_id, size, csize, cdata)
                else:
                    add_reference(chunk_id, size, csize)
                chunk_list.append((chunk_id, size, csize))
                offset += size
            item[b'chunks'] = chunk_list

        def msgpack_resync(data):
            data = memoryview(data)
            while data:
                unpacker = msgpack.Unpacker()
                unpacker.feed(data)
                item = next(unpacker)
                if isinstance(item, dict) and b'path' in item:
                    return data
                data = data[1:]

        def robust_iterator(archive):
            prev_state = None
            state = 0
            def missing_chunk_detector(chunk_id):
                nonlocal state
                if state % 2 != int(not chunk_id in self.chunks):
                    state += 1
                return state

            for state, items in groupby(archive[b'items'], missing_chunk_detector):
                if state != prev_state:
                    unpacker = msgpack.Unpacker(object_hook=StableDict)
                    prev_state = state
                if state % 2:
                    self.report_progress('Archive metadata damage detected', error=True)
                    return
                items = list(items)
                for i, (chunk_id, cdata) in enumerate(zip(items, self.repository.get_many(items))):
                    data = self.key.decrypt(chunk_id, cdata)
                    if state and i == 0:
                        data = msgpack_resync(data)
                    unpacker.feed(data)
                    for item in unpacker:
                        yield item

        num_archives = len(self.manifest.archives)
        for i, (name, info) in enumerate(list(self.manifest.archives.items()), 1):
            self.report_progress('Analyzing archive {} ({}/{})'.format(name, i, num_archives))
            archive_id = info[b'id']
            if not archive_id in self.chunks:
                self.report_progress('Archive metadata block is missing', error=True)
                del self.manifest.archives[name]
                continue
            items_buffer = ChunkBuffer(self.key)
            items_buffer.write_chunk = add_callback
            cdata = self.repository.get(archive_id)
            data = self.key.decrypt(archive_id, cdata)
            archive = StableDict(msgpack.unpackb(data))
            if archive[b'version'] != 1:
                raise Exception('Unknown archive metadata version')
            decode_dict(archive, (b'name', b'hostname', b'username', b'time'))  # fixme: argv
            for item in robust_iterator(archive):
                if b'chunks' in item:
                    verify_file_chunks(item)
                items_buffer.add(item)
            items_buffer.flush(flush=True)
            for previous_item_id in archive[b'items']:
                record_unused(previous_item_id)
            archive[b'items'] = items_buffer.chunks
            data = msgpack.packb(archive, unicode_errors='surrogateescape')
            new_archive_id = self.key.id_hash(data)
            cdata = self.key.encrypt(data)
            add_reference(new_archive_id, len(data), len(cdata), cdata)
            record_unused(archive_id)
            info[b'id'] = new_archive_id
