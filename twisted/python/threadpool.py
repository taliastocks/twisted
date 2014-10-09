# -*- test-case-name: twisted.test.test_threadpool -*-
# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
twisted.python.threadpool: a pool of threads to which we dispatch tasks.

In most cases you can just use C{reactor.callInThread} and friends
instead of creating a thread pool directly.
"""

from __future__ import division, absolute_import

try:
    from Queue import Queue
except ImportError:
    from queue import Queue

import threading

from twisted.threads import ThreadWorker, Team
from twisted.python import log, context
from twisted.python.failure import Failure


WorkerStop = object()


class ThreadPool:
    """
    This class (hopefully) generalizes the functionality of a pool of threads
    to which work can be dispatched.

    L{callInThread} and L{stop} should only be called from a single thread,
    unless you make a subclass where L{stop} and L{_startSomeWorkers} are
    synchronized.

    TO DO:

    U{https://twistedmatrix.com/pipermail/twisted-python/2014-September/028797.html}

        - re-implement in terms of twisted.threads

    Core Interface:

        - callInThread - implement with Team.do; remember to capture and pass
          context.

        - callInThreadWithCallback - implement with callInThread.

        - adjustPoolsize - implement with Team.grow / Team.shrink

        - start - don't call Team.grow until this point.  (Note: in
          reactor-integrated scenario, the coordinator doesn't actually consume
          resources, since it just wraps the reactor)

        - stop - implement with Team.quit

    Base Compatibility Stuff:

        - min - implement with a call to Team.grow in start

        - max - implement with a createWorker function that returns

        - joined - implement by adding a
          callback-to-be-called-in-coordinator-when-everybody-is-finished?
          ThreadWorker.quit already has a call to join() in it.

        - started - implement in start

        - name - just set it, I guess

        - dumpStats - implement with C{Statistics}

    U{https://twistedmatrix.com/pipermail/twisted-python/2014-September/028798.html}

        - len()-able C{waiters} - implement with C{Statistics.idleWorkerCount}

        - len()-able C{working} - implement with C{Statistics.busyWorkerCount}

        - C{q} attribute with a C{qsize} method - implement with
          C{backloggedWorkCount}

    U{https://twistedmatrix.com/pipermail/twisted-python/2014-September/028814.html}

        - overridable C{threadFactory} hook, as an attribute - just pass this
          along to Team.__init__

    @ivar started: Whether or not the thread pool is currently running.
    @type started: L{bool}

    @ivar threads: List of workers currently running in this thread pool.
    @type threads: L{list}
    """
    min = 5
    max = 20
    joined = False
    started = False
    workers = 0
    name = None

    threadFactory = threading.Thread
    currentThread = staticmethod(threading.currentThread)

    def __init__(self, minthreads=5, maxthreads=20, name=None):
        """
        Create a new threadpool.

        @param minthreads: minimum number of threads in the pool
        @param maxthreads: maximum number of threads in the pool
        """
        assert minthreads >= 0, 'minimum is negative'
        assert minthreads <= maxthreads, 'minimum is greater than maximum'
        class NotAQueue(object):
            def qsize(q):
                return self._team.statistics().backloggedWorkCount
        self.q = NotAQueue()
        self.min = minthreads
        self.max = maxthreads
        self.name = name
        self.waiters = []
        self.threads = []
        self.working = []

        def workerCreator(name):
            def makeAThread(target):
                thread = self.threadFactory(name=name,
                                            target=target)
                self.threads.append(thread)
                return thread
            return ThreadWorker(makeAThread, Queue)

        def limitedWorkerCreator():
            # Called only from the workforce's coordinator.
            stats = self._team.statistics()
            if stats.busyWorkerCount + stats.idleWorkerCount >= self.max:
                return None
            return workerCreator(self._generateName())

        self._team = Team(createCoordinator=lambda:
                          ThreadWorker(
                              lambda target:
                              self.threadFactory(name="coordinator",
                                                 target=target),
                              Queue),
                          createWorker=limitedWorkerCreator,
                          logException=log.err)


    @property
    def workers(self):
        """
        For compatibility purposes, return a total number of workers.
        """
        stats = self._team.statistics()
        return stats.idleWorkerCount + stats.busyWorkerCount


    @property
    def working(self):
        """
        For compatibility purposes, return the number of busy workers.
        """
        return [None] * self._team.statistics().busyWorkerCount


    def start(self):
        """
        Start the threadpool.
        """
        self.joined = False
        self.started = True
        # Start some threads.
        self.adjustPoolsize()


    def startAWorker(self):
        self.workers += 1
        newThread = self.threadFactory(target=self._worker,
                                       name=self._generateName())
        self.threads.append(newThread)
        newThread.start()


    def _generateName(self):
        """
        Generate a name for a new pool thread.
        """
        return "PoolThread-%s-%s" % (self.name or id(self), self.workers)


    def stopAWorker(self):
        self.q.put(WorkerStop)
        self.workers -= 1


    def __setstate__(self, state):
        self.__dict__ = state
        ThreadPool.__init__(self, self.min, self.max)


    def __getstate__(self):
        state = {}
        state['min'] = self.min
        state['max'] = self.max
        return state


    def _startSomeWorkers(self):
        neededSize = self.q.qsize() + len(self.working)
        # Create enough, but not too many
        while self.workers < min(self.max, neededSize):
            self.startAWorker()


    def callInThread(self, func, *args, **kw):
        """
        Call a callable object in a separate thread.

        @param func: callable object to be called in separate thread

        @param *args: positional arguments to be passed to C{func}

        @param **kw: keyword args to be passed to C{func}
        """
        self.callInThreadWithCallback(None, func, *args, **kw)


    def callInThreadWithCallback(self, onResult, func, *args, **kw):
        """
        Call a callable object in a separate thread and call C{onResult}
        with the return value, or a L{twisted.python.failure.Failure}
        if the callable raises an exception.

        The callable is allowed to block, but the C{onResult} function
        must not block and should perform as little work as possible.

        A typical action for C{onResult} for a threadpool used with a
        Twisted reactor would be to schedule a
        L{twisted.internet.defer.Deferred} to fire in the main
        reactor thread using C{.callFromThread}.  Note that C{onResult}
        is called inside the separate thread, not inside the reactor thread.

        @param onResult: a callable with the signature C{(success, result)}.
            If the callable returns normally, C{onResult} is called with
            C{(True, result)} where C{result} is the return value of the
            callable. If the callable throws an exception, C{onResult} is
            called with C{(False, failure)}.

            Optionally, C{onResult} may be C{None}, in which case it is not
            called at all.

        @param func: callable object to be called in separate thread

        @param *args: positional arguments to be passed to C{func}

        @param **kwargs: keyword arguments to be passed to C{func}
        """
        if self.joined:
            return
        ctx = context.theContextTracker.currentContext().contexts[-1]
        @self._team.do
        def inThread():
            try:
                result = context.call(ctx, func, *args, **kw)
            except:
                ok, result = False, Failure()
            else:
                ok = True
            if onResult is not None:
                onResult(ok, result)


    def stop(self):
        """
        Shutdown the threads in the threadpool.
        """
        self.joined = True
        self.started = False
        self._team.quit()


    def adjustPoolsize(self, minthreads=None, maxthreads=None):
        if minthreads is None:
            minthreads = self.min
        if maxthreads is None:
            maxthreads = self.max

        assert minthreads >= 0, 'minimum is negative'
        assert minthreads <= maxthreads, 'minimum is greater than maximum'

        self.min = minthreads
        self.max = maxthreads
        if not self.started:
            return

        # Kill of some threads if we have too many.
        if self.workers > self.max:
            self._team.shrink(self.workers - self.max)
        # Start some threads if we have too few.
        if self.workers < self.min:
            self._team.grow(self.min - self.workers)
        # Start some threads if there is a need.
        self._startSomeWorkers()


    def dumpStats(self):
        log.msg('waiters: %s' % self.waiters)
        log.msg('workers: %s' % self.working)
        log.msg('total: %s'   % self.threads)

