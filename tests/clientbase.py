# -*- coding: utf-8 -*-
from chorde import CacheMissError
from chorde.clients.base import SyncAdapter, ReadWriteSyncAdapter, NamespaceWrapper

import time

class CacheClientTestMixIn:
    capacity_means_entries = True
    is_lru = True
    contains_touches = True
    
    def setUpClient(self):
        raise NotImplementedError

    def _setUpClient(self):
        return self.setUpClient()
    
    def setUp(self):
        try:
            self.client = self._setUpClient()
        except NotImplementedError as e:
            self.skipTest("NotImplementedError (%s)" % (e,))

    def tearDown(self):
        # In case it's persistent
        self.client.clear()

    def testPut(self):
        client = self.client
        client.put(1, 2, 10)
        self.assertEqual(client.get(1), 2)

    def testAdd(self):
        client = self.client
        client.put(1, 3, 10)
        self.assertFalse(client.add(1, 4, 10))
        self.assertEqual(client.get(1), 3)
        self.assertTrue(client.add(2, 4, 10))
        self.assertEqual(client.get(2), 4)

    def testGet(self):
        client = self.client
        client.put(4, 10, 10)
        self.assertRaises(CacheMissError, client.get, 3)
        self.assertEqual(client.get(3, None), None)
        self.assertEqual(client.get(3, 1), 1)

    def testContains(self):
        client = self.client
        self.assertFalse(client.contains(4))
        client.put(4, 10, 10)
        self.assertTrue(client.contains(4))

    def testGetTtl(self):
        client = self.client
        client.put(4, 10, 10)
        time.sleep(0.1)
        v,ttl = client.getTtl(4)
        self.assertEqual(v, 10)
        self.assertTrue(ttl < 10)

    def testClear(self):
        client = self.client
        client.put(4, 11, 10)
        client.put(5, 12, 10)
        self.assertEqual(client.get(4), 11)
        self.assertEqual(client.get(5), 12)

        client.clear()
        self.assertRaises(CacheMissError, client.get, 4)
        self.assertRaises(CacheMissError, client.get, 5)

    def testDelete(self):
        client = self.client
        client.put(4, 11, 10)
        client.put(5, 12, 10)
        self.assertEqual(client.get(4), 11)
        self.assertEqual(client.get(5), 12)

        client.delete(4)
        self.assertRaises(CacheMissError, client.get, 4)
        self.assertEqual(client.get(5), 12)

    def testExpire(self):
        client = self.client
        client.put(4, 15, 20)
        client.expire(4)
        try:
            v,ttl = client.getTtl(4)
            self.assertTrue(ttl <= 0)
        except CacheMissError:
            pass

    def testLimit(self):
        if not self.capacity_means_entries:
            self.skipTest("Client capacity units other than entries")

        client = self.client
        cap = client.capacity

        self.assertEqual(client.usage, 0)
        
        for i in xrange(cap):
            client.put(i,i,86400)
            self.assertEqual(client.usage, i+1)

        for i in xrange(cap):
            i += cap
            client.put(i,i,86400)
            self.assertEqual(client.usage, cap)

    def testLRU(self):
        if not self.is_lru:
            self.skipTest("Client's not LRU")
        elif not self.capacity_means_entries:
            self.skipTest("LRU-testing non-entry-limited clients requires specialization")

        client = self.client
        cap = client.capacity

        for i in xrange(cap):
            client.put(i,i,86400)
            self.assertEqual(client.usage, i+1)

        self.assertTrue(client.contains(0))
        client.put(cap,cap,86400)

        if self.contains_touches:
            self.assertFalse(client.contains(1))
            self.assertTrue(client.contains(0))
        else:
            self.assertFalse(client.contains(0))
            self.assertTrue(client.contains(1))


class SyncWrapperTestMixIn(CacheClientTestMixIn):
    def _setUpClient(self):
        return SyncAdapter(self.setUpClient())

class RWSyncWrapperTestMixIn(CacheClientTestMixIn):
    def _setUpClient(self):
        return ReadWriteSyncAdapter(self.setUpClient())

class NamespaceWrapperTestMixIn(CacheClientTestMixIn):
    def _setUpClient(self):
        self.rclient = self.setUpClient()
        self.bclient = NamespaceWrapper("nsb", self.rclient)
        return NamespaceWrapper("nsa", self.rclient)

    def testNamespaceSeparation(self):
        self.client.put(1, 2, 10)
        self.bclient.put(2, 3, 10)
        self.rclient.put(3, 4, 10)
        self.client.put(4, 5, 10)
        self.bclient.put(4, 6, 10)
        self.rclient.put(4, 7, 10)
        
        self.assertEqual(self.client.get(1), 2)
        self.assertRaises(CacheMissError, self.bclient.get, 1)
        self.assertRaises(CacheMissError, self.rclient.get, 1)
        
        self.assertRaises(CacheMissError, self.client.get, 2)
        self.assertEqual(self.bclient.get(2), 3)
        self.assertRaises(CacheMissError, self.rclient.get, 2)
        
        self.assertRaises(CacheMissError, self.client.get, 3)
        self.assertRaises(CacheMissError, self.bclient.get, 3)
        self.assertEqual(self.rclient.get(3), 4)
        
        self.assertEqual(self.client.get(4), 5)
        self.assertEqual(self.bclient.get(4), 6)
        self.assertEqual(self.rclient.get(4), 7)

        self.client.clear()
        self.assertRaises(CacheMissError, self.client.get, 4)
        self.assertEqual(self.bclient.get(4), 6)
        self.assertEqual(self.rclient.get(4), 7)

        self.bclient.clear()
        self.assertRaises(CacheMissError, self.client.get, 4)
        self.assertRaises(CacheMissError, self.bclient.get, 4)
        self.assertEqual(self.rclient.get(4), 7)