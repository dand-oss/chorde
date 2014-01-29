# -*- coding: utf-8 -*-
import collections
import thread
from threading import Event, Thread
import multiprocessing.pool 
import time
import unittest

from chorde.threadpool import ThreadPool

class ThreadpoolTest(unittest.TestCase):
    def setUp(self):
        self.pool = ThreadPool()

    def tearDown(self):
        self.join_close(self.pool, 60)

    @staticmethod
    def join_close(pool, timeout):
        pool.close()
        pool.join(timeout)
    
    @staticmethod
    def join_continue(pool, timeout):
        pool.join(timeout)
    
    def testAsyncLatency(self):
        for i in xrange(100):
            t0 = time.time()
            ev = Event()
            self.pool.apply_async(ev.set)
            ev.wait()
            t1 = time.time()
            self.assertLess(t1-t0, 0.05)

    def testSyncLatency(self):
        for i in xrange(100):
            t0 = time.time()
            t1 = self.pool.apply(time.time)
            self.assertLess(t1-t0, 0.05)

    def testExceptions(self):
        def raiseme():
            raise RuntimeError
        self.assertRaises(RuntimeError, self.pool.apply, raiseme)

    def testConcurrency(self):
        N = 10000
        M = 50
        counts = collections.defaultdict(int)
        def accounting(i):
            counts[thread.get_ident()] += 1
        def killit(i):
            for j in xrange(N):
                self.pool.apply_async(accounting, (i,))
        threads = [ Thread(target=killit, args=(i,)) for i in xrange(M) ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.join_continue(self.pool, 60)
        total_counts = self.pool.apply(sum, (counts.itervalues(),))
        self.assertEqual(total_counts, N*M)

class ThreadpoolMultiprocessingCompatiblitityTest(ThreadpoolTest):
    def setUp(self):
        self.pool = multiprocessing.pool.ThreadPool()

    @staticmethod
    def join_close(pool, timeout):
        pool.close()
        pool.join()

    @staticmethod
    def join_continue(pool, timeout):
        pool.close()
        pool.join()
        pool.__init__() # hackish, but works

class MultiQueueTest(unittest.TestCase):
    def setUp(self):
        self.pool = ThreadPool()

    def tearDown(self):
        self.pool.terminate()
    
    def testFairness(self):
        N = 10000
        M = 50
        counts = collections.defaultdict(int)
        def accounting(i):
            counts[thread.get_ident()] += 1
        def killit(i):
            for j in xrange(N):
                self.pool.apply_async(accounting, (i,))
        threads = [ Thread(target=killit, args=(i,)) for i in xrange(M) ]
        for t in threads:
            t.start()
        time.sleep(0.1) # let it fill up
        t0 = time.time()
        self.pool.apply(sum, (counts.itervalues(),), queue = "Johnny")
        t1 = time.time()
        self.assertLess(t1-t0, 0.01)

    def testWeighting(self):
        terminate = []
        counts = collections.defaultdict(int)
        def accounting(i):
            counts[i] += 1
        def killit(q):
            while not terminate:
                self.pool.apply_async(accounting, (q,))
        threads = [ 
            Thread(target=killit, args=("mean",)),
            Thread(target=killit, args=("simple",)),
        ]
        self.pool.set_queueprio("mean",3)
        self.pool.set_queueprio("simple",1)
        for t in threads:
            t.start()
        time.sleep(1) # let it fill up
        terminate.append(None)
        self.assertLess(counts["simple"]*2, counts["mean"])
