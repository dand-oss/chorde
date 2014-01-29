# -*- coding: utf-8 -*-
import collections
import functools
import itertools
import logging
import multiprocessing
import os
import operator
import sys
import thread
import threading
import time
import weakref

import worker

class TimeoutError(Exception):
    pass

class WorkerThread(threading.Thread):
    def __init__(self,target,*args,**kwargs):
        threading.Thread.__init__(self)
        self.target = target
        self.args = args
        self.kwargs = kwargs
        self.__terminate = False

    def run(self):
        global _callCleanupHooks
        while not self.__terminate:
            try:
                try:
                    self.target(*self.args,**self.kwargs)
                finally:
                    worker._callCleanupHooks()
            except Exception:
                logging.error("Exception ocurred in worker thread:", exc_info = True)

    def start(self):
        global _nothreads
        threading.Thread.start(self)

    def terminate(self, wait = True):
        self.__terminate = True
        if wait:
            self.join()

class ExceptionWrapper(object):
    __slots__ = ('exc',)

    def __init__(self, exc):
        self.exc = exc

    def reraise(self):
        exc = self.exc
        del self.exc
        raise exc[0], exc[1], exc[2]

class WaitIter:
    def __init__(self, event, queues, timeout = 5):
        self.event = event
        self.queues = queues
        self.timeout = timeout
        self.terminate = False
    def __iter__(self):
        return self
    def next(self):
        if self.terminate:
            threading.current_thread().terminate(False)
        self.event.wait(self.timeout)
        raise StopIteration

class ThreadPool:
    Thread = WorkerThread
    
    """
    Re-implementation of multiprocessing.pool.ThreadPool optimized for threads
    and asynchronous result-less tasks.
    Implements quasi-lockless double-buffering so queue insertions are very fast, and
    multiple queues for managing fairness.

    This implementation is forcibly a daemon thread pool, which when destroyed
    will cancel all pending tasks, and no task returns any result, and has been 
    optimized for that usage pattern.
    """
    def __init__(self, workers = None):
        if workers is None:
            workers = multiprocessing.cpu_count()
        
        self.workers = workers
        self.__workers = None
        self.__pid = os.getpid()
        self.__spawnlock = threading.Lock()
        self.__swap_lock = threading.Lock()
        self.__not_empty = threading.Event()
        self.__empty = threading.Event()
        self.__empty.set()

        self.queues = collections.defaultdict(list)
        self.queue_weights = {}
        self.__workset = set()
        self.__busyqueues = set()
        self.__exhausted_iter = WaitIter(self.__not_empty, self.queues)
        self.__dequeue = self.__exhausted = self.__exhausted_iter.next

    def queuelen(self, queue = None):
        return len(self.queues.get(queue,()))

    def queueprio(self, queue = None):
        return self.queue_weights.get(queue,1)

    def set_queueprio(self, prio, queue = None):
        self.queue_weights[queue] = prio

    def __swap_queues(self):
        qpop = self.queues.pop
        qprio = self.queue_weights.get
        wqueues = []
        wprios = []
        qnames = []
        for qname in self.queues.keys():
            q = qpop(qname)
            qnames.append(qname)
            wqueues.append(q)
            wprios.append(qprio(qname,1))

        if wqueues:
            self.__busyqueues.clear()
            self.__busyqueues.update(qnames)
            
            # Flatten with weights
            # Do it repeatedly to catch stragglers (those that straggle past the flattening step)
            queues = [ functools.partial(itertools.repeat, iter(q).next, qprio) 
                       for q,qprio in itertools.izip(wqueues, wprios) ]
            iqueue = []
            iappend = iqueue.append
            islice = itertools.islice
            cycle = itertools.cycle
            retry = True

            while retry:
                # Wait for stragglers
                time.sleep(0.0001)
                
                wqueues = queues
                ioffs = 0
                ilen = len(iqueue)
                while wqueues:
                    try:
                        for ioffs,q in islice(cycle(enumerate(wqueues)), ioffs, None):
                            for q in q():
                                iappend(q())
                    except StopIteration:
                        del wqueues[ioffs]
                retry = len(iqueue) != ilen
            self.__dequeue = iter(iqueue).next
        elif self.__dequeue is not self.__exhausted:
            self.__not_empty.clear()
            self.__dequeue = self.__exhausted

            # Try again
            # This is a transition from working to empty, which means
            # until now, pushing threads didn't set the weakeup call event.
            # So, before actually sleeping, try again
            self.__swap_queues()
        else:
            # Still empty, give up
            self.__not_empty.clear()
            self.__dequeue = self.__exhausted

    def _dequeue(self):
        tid = thread.get_ident()
        workset = self.__workset
        while True:
            if self.__dequeue is self.__exhausted and not self.queues and not self.__not_empty.is_set():
                # Sounds like there's nothing to do
                # Yeah, gonna wait
                workset.discard(tid)
                if not workset and not self.queues:
                    self.__empty.set()
            else:
                workset.add(tid)
            try:
                return self.__dequeue()
            except StopIteration:
                # Exhausted whole workqueue?
                with self.__swap_lock:
                    try:
                        if self.__dequeue is self.__exhausted:
                            # Pointless to wait, just swap again
                            raise StopIteration
                        else:
                            # Try it
                            workset.add(tid)
                            return self.__dequeue()
                    except StopIteration:
                        # Yep, exhausted queue, build up new workqueue
                        self.__swap_queues()

    def _enqueue(self, queue, task):
        self.queues[queue].append(task)
        if self.__dequeue is self.__exhausted:
            # Wake up waiting threads
            # Note that it's not necessary if dequeue is set to a dequeuing
            # iterator, since that means threads are busy working already
            self.__not_empty.set()
            self.__empty.clear()
        self.assert_started()

    @staticmethod
    def worker(self):
        self = self()

        task = self._dequeue()
        if task is not None:
            task()

    def is_started(self):
        if self.__workers is None or self.__pid != os.getpid():
            return False
        try:
            return all(itertools.imap(operator.methodcaller('is_alive'), self.__workers))
        except:
            return False

    def stop(self):
        if self.__workers:
            with self.__spawnlock:
                for w in self.__workers or ():
                    try:
                        if w.is_alive():
                            w.terminate(False)
                    except:
                        pass
                self.__workers = None

    def close(self):
        # Signal idle threads to commit suicide
        self.__exhausted_iter.terminate = True

    def terminate(self):
        self.stop()

    def start(self):
        if not self.is_started():
            self.populate_workers()

    def assert_started(self):
        if not self.is_started():
            self.populate_workers()

    def join(self, timeout = None):
        if timeout is not None:
            now = time.time()
            timeout += now
        while timeout is None or now < timeout:
            if self.__empty.wait(timeout - now if timeout is not None else None):
                # The event is not 100% certain, we can still get awakened when there's work to do
                # We have to check under __swap_lock to be sure
                with self.__swap_lock:
                    if self.__dequeue is self.__exhausted and not self.queues and not self.__workset:
                        return True
                    else:
                        # False alarm, clear it so we don't spin
                        self.__empty.clear()
            else:
                # Timeout
                return False
            if timeout is not None:
                now = time.time()

    def populate_workers(self):
        with self.__spawnlock:
            if not self.is_started():
                self.__workers = [ self.Thread(functools.partial(self.worker, weakref.ref(self)))
                                   for i in xrange(self.workers) ]
                for w in self.__workers:
                    w.daemon = True
                    w.start()
                
                self.__pid = os.getpid()
            # Else, just keep number of workers in sync
            elif len(self.__workers) < self.workers:
                nworkers = [ self.Thread(functools.partial(self.worker, weakref.ref(self)))
                                   for i in xrange(self.workers - len(self.__workers)) ]
                for w in nworkers:
                    w.daemon = True
                    w.start()
                self.__workers.extend(nworkers)
            elif len(self.__workers) > self.workers:
                nworkers = self.__workers[self.workers:]
                del self.__workers[self.workers:]
                for w in nworkers:
                    w.terminate(False)

    def apply_async(self, task, args = (), kwargs = {}, queue = None):
        if args or kwargs:
            task = functools.partial(task, *args, **kwargs)
        self._enqueue(queue, task)

    def apply(self, task, args = (), kwargs = {}, queue = None, timeout = None):
        rv = []
        ev = threading.Event()
        def stask():
            try:
                rv.append(task(*args, **kwargs))
            except:
                rv.append(ExceptionWrapper(sys.exc_info()))
            ev.set()
        self._enqueue(queue, stask)
        if ev.wait(timeout):
            rv = rv[0]
            if isinstance(rv, ExceptionWrapper):
                rv.reraise()
            else:
                return rv
        else:
            raise TimeoutError
        