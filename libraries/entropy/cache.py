# -*- coding: utf-8 -*-
"""

    @author: Fabio Erculiani <lxnay@sabayon.org>
    @contact: lxnay@sabayon.org
    @copyright: Fabio Erculiani
    @license: GPL-2

    B{Entropy Framework cache module}.

    This module contains the Entropy, asynchronous caching logic.
    It is not meant to handle cache pollution management, because
    this is either handled implicitly when cached items are pulled
    in or by using entropy.dump or cache cleaners (see
    entropy.client.interfaces.cache mixin methods)

"""
import os
import sys
from entropy.const import etpConst, etpUi, const_debug_write, const_pid_exists
from entropy.core import Singleton
from entropy.misc import TimeScheduled, Lifo
import time

import entropy.dump
import entropy.tools

class EntropyCacher(Singleton):

    CACHE_IDS = {
            'db_match': 'match/db',
            'dep_tree': 'deptree/dep_tree_',
            'atom_match': 'atom_match/atom_match_',
            'world_update': 'world_update/world_cache_',
            'critical_update': 'critical_update/critical_cache_',
            'world_available': 'world_available/available_cache_',
            'check_package_update': 'check_update/package_update_',
            'depends_tree': 'depends/depends_tree_',
            'filter_satisfied_deps': 'depfilter/filter_satisfied_deps_',
            'library_breakage': 'libs_break/library_breakage_',
        }

    # Max amount of processes to spawn
    _PROC_LIMIT = 10
    # Max number of cache objects written at once
    _OBJS_WRITTEN_AT_ONCE = 50

    """
    Entropy asynchronous and synchronous cache writer
    and reader. This class is a Singleton and contains
    a thread doing the cache writes asynchronously, thus
    it must be stopped before your application is terminated
    calling the stop() method.

    Sample code:

        >>> # import module
        >>> from entropy.cache import EntropyCacher
        ...
        >>> # first EntropyCacher load, start it
        >>> cacher = EntropyCacher()
        >>> cacher.start()
        ...
        >>> # now store something into its cache
        >>> cacher.push('my_identifier1', [1, 2, 3])
        >>> # now store something synchronously
        >>> cacher.push('my_identifier2', [1, 2, 3], async = False)
        ...
        >>> # now flush all the caches to disk, and make sure all
        >>> # is written
        >>> cacher.sync()
        ...
        >>> # now fetch something from the cache
        >>> data = cacher.pop('my_identifier1')
        [1, 2, 3]
        ...
        >>> # now discard all the cached (async) writes
        >>> cacher.discard()
        ...
        >>> # and stop EntropyCacher
        >>> cacher.stop()

    """

    def init_singleton(self):
        """
        Singleton overloaded method. Equals to __init__.
        This is the place where all the properties initialization
        takes place.
        """
        import threading
        import copy
        self.__copy = copy
        self.__alive = False
        self.__cache_writer = None
        self.__cache_buffer = Lifo()
        self.__proc_pids = set()
        self.__proc_pids_lock = threading.Lock()

    def __copy_obj(self, obj):
        """
        Return a copy of an object done by the standard
        library "copy" module.

        @param obj: object to copy
        @type obj: any Python object
        @rtype: copied object
        @return: copied object
        """
        return self.__copy.deepcopy(obj)

    def __clean_pids(self):
        with self.__proc_pids_lock:
            dead_pids = set()
            for pid in self.__proc_pids:

                try:
                    dead = os.waitpid(pid, os.WNOHANG)[0]
                except OSError as err:
                    if err.errno != 10:
                        raise
                    dead = True
                if dead:
                    dead_pids.add(pid)
                elif not const_pid_exists(pid):
                    dead_pids.add(pid)

            if dead_pids:
                self.__proc_pids.difference_update(dead_pids)

    def __wait_cacher_semaphore(self):
        self.__clean_pids()
        while len(self.__proc_pids) > EntropyCacher._PROC_LIMIT:
            if etpUi['debug']:
                const_debug_write(__name__,
                    "EntropyCacher.__wait_cacher_semaphore: too many pids")
            time.sleep(0.1)
            self.__clean_pids()

    def __cacher(self, run_until_empty = False):
        """
        This is where the actual asynchronous copy takes
        place. __cacher runs on a different threads and
        all the operations done by this are atomic and
        thread-safe. It just loops over and over until
        __alive becomes False.
        """
        while self.__alive or run_until_empty:

            massive_data = []
            massive_data_count = EntropyCacher._OBJS_WRITTEN_AT_ONCE
            while massive_data_count > 0:
                massive_data_count -= 1
                try:
                    data = self.__cache_buffer.pop()
                except (ValueError, TypeError,):
                    # TypeError is when objects are being destroyed
                    break # stack empty
                massive_data.append(data)

            # this must stay before massive_data to make sure to clean
            # every defunct process
            self.__wait_cacher_semaphore()

            if not massive_data:
                break

            pid = os.fork()
            if pid == 0:
                for (key, cache_dir), data in massive_data:
                    d_o = entropy.dump.dumpobj
                    if d_o is not None:
                        d_o(key, data, dump_dir = cache_dir)
                os._exit(0)
            else:
                if etpUi['debug']:
                    const_debug_write(__name__,
                        "EntropyCacher.__cacher [%s], writing %s objs" % (
                            pid, len(massive_data),))
                with self.__proc_pids_lock:
                    self.__proc_pids.add(pid)
                del massive_data[:]
                del massive_data

    def __del__(self):
        self.stop()

    def start(self):
        """
        This is the method used to start the asynchronous cache
        writer but also the whole cacher. If this method is not
        called, the instance will always trash and cache write
        request.

        @return: None
        """
        self.__cache_buffer.clear()
        self.__cache_writer = TimeScheduled(1, self.__cacher)
        self.__cache_writer.set_delay_before(True)
        self.__cache_writer.start()
        while not self.__cache_writer.isAlive():
            continue
        self.__alive = True

    def is_started(self):
        """
        Return whether start is called or not. This equals to
        checking if the cacher is running, thus is writing cache
        to disk.

        @return: None
        """
        return self.__alive

    def stop(self):
        """
        This method stops the execution of the cacher, which won't
        accept cache writes anymore. The thread responsible of writing
        to disk is stopped here and the Cacher will be back to being
        inactive. A watchdog will avoid the thread to freeze the
        call if the write buffer is overloaded.

        @return: None
        """
        self.__alive = False
        if self.__cache_writer is not None:
            self.__cache_writer.kill()
            self.__cache_writer.join()
            self.__cache_writer = None
        self.sync()

    def sync(self):
        """
        This method can be called anytime and forces the instance
        to flush all the cache writes queued to disk. If wait == False
        a watchdog prevents this call to get stuck in case of write
        buffer overloads.
        """
        self.__cacher(run_until_empty = True)

    def discard(self):
        """
        This method makes buffered cache to be discarded synchronously.

        @return: None
        """
        self.__cache_buffer.clear()

    def push(self, key, data, async = True, cache_dir = None):
        """
        This is the place where data is either added
        to the write queue or written to disk (if async == False)
        only and only if start() method has been called.

        @param key: cache data identifier
        @type key: string
        @param data: picklable object
        @type data: any picklable object
        @keyword async: store cache asynchronously or not
        @type async: bool
        @keyword cache_dir: alternative cache directory
        @type cache_dir: string
        @rtype: None
        @return: None
        """
        if not self.__alive:
            return

        if cache_dir is None:
            cache_dir = entropy.dump.D_DIR

        if async:
            try:
                self.__cache_buffer.push(((key, cache_dir,),
                    self.__copy_obj(data),))
            except TypeError:
                # sometimes, very rarely, copy.deepcopy() is unable
                # to properly copy an object (blame Python bug)
                sys.stdout.write("!!! cannot cache object with key %s\n" % (
                    key,))
                sys.stdout.flush()
            if etpUi['debug']:
                const_debug_write(__name__,
                    "EntropyCacher.push, async push %s, into %s" % (
                        key, cache_dir,))
        else:
            if etpUi['debug']:
                const_debug_write(__name__,
                    "EntropyCacher.push, sync push %s, into %s" % (
                        key, cache_dir,))
            entropy.dump.dumpobj(key, data, dump_dir = cache_dir)

    def pop(self, key, cache_dir = None):
        """
        This is the place where data is retrieved from cache.
        You must know the cache identifier used when push()
        was called.

        @param key: cache data identifier
        @type key: string
        @keyword cache_dir: alternative cache directory
        @type cache_dir: string
        @rtype: Python object
        @return: object stored into the stack or None (if stack is empty)
        """
        if cache_dir is None:
            cache_dir = entropy.dump.D_DIR

        l_o = entropy.dump.loadobj
        if not l_o:
            return
        return l_o(key, dump_dir = cache_dir)

    @staticmethod
    def clear_cache_item(cache_item, cache_dir = None):
        """
        Clear Entropy Cache item from on-disk cache.

        @param cache_item: Entropy Cache item identifier
        @type cache_item: string
        @keyword cache_dir: alternative cache directory
        @type cache_dir: string
        """
        if cache_dir is None:
            cache_dir = entropy.dump.D_DIR
        dump_path = os.path.join(cache_dir, cache_item)

        dump_dir = os.path.dirname(dump_path)
        for currentdir, subdirs, files in os.walk(dump_dir):
            path = os.path.join(dump_dir, currentdir)
            for item in files:
                if item.endswith(entropy.dump.D_EXT):
                    item = os.path.join(path, item)
                    try:
                        os.remove(item)
                    except (OSError, IOError,):
                        pass
            try:
                if not os.listdir(path):
                    os.rmdir(path)
            except (OSError, IOError,):
                pass

    @staticmethod
    def clear_cache(excluded_items = None, cache_dir = None):
        """
        Clear all the on-disk cache items included in EntropyCacher.CACHE_IDS.

        @keyword excluded_items: list of items to exclude from cleaning
        @type excluded_items: list
        @keyword cache_dir: alternative cache directory
        @type cache_dir: string
        """
        if excluded_items is None:
            excluded_items = []
        for key, value in EntropyCacher.CACHE_IDS.items():
            if key in excluded_items:
                continue
            EntropyCacher.clear_cache_item(value, cache_dir = cache_dir)
