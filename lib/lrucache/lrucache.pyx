"""Efficient (O(log n) amortized update) Least-Recently-Used cache"""

cdef extern from "Python.h":
    int PySequence_SetItem(object o, Py_ssize_t i, object v) except -1
    object PySequence_GetItem(object o, Py_ssize_t i)
    void PyList_SET_ITEM(object o, Py_ssize_t i, void* v)
    void* PyList_GET_ITEM(object o, Py_ssize_t i)
    Py_ssize_t PyList_GET_SIZE(object o)

cdef extern from *:
    # Note: the C name below is extracted from the generated code. As such,
    #       it can change from time to time. It's a hack. Beware.
    struct _borrowed_node "__pyx_obj_8lrucache__node":
        unsigned int prio
        unsigned int index

class CacheMissError(KeyError):
    """Error raised when a cache miss occurs"""
    pass

cdef class _node:
    cdef unsigned int prio
    cdef unsigned int index
    cdef object key
    cdef object value

    def __init__(_node self not None, unsigned int prio, unsigned int index, object key, object value):
        self.prio = prio
        self.index = index
        self.key = key
        self.value = value

    def __richcmp__(_node self not None, _node other, int op):
        if op == 0:
            return self.prio < other.prio
        elif op == 1:
            return self.prio <= other.prio
        elif op == 2:
            return self.prio == other.prio
        elif op == 3:
            return self.prio != other.prio
        elif op == 4:
            return self.prio > other.prio
        elif op == 5:
            return self.prio >= other.prio
        else:
            return False


cdef class LRUCache:
    """Least-Recently-Used (LRU) cache.
    
    Instances of this class behave like a Python mapping type, except that
    its size is guaranteed to never surpass the assigned limit and when
    spillover occurs, the least recently used items get removed first.

    If an eviction callback is provided, it must take two arguments, key and
    value, and it will only be invoked for automatic (overflow related) evictions,
    not manual ones invoked with del, pop or clear.
    """

    cdef unsigned int next_prio
    cdef readonly unsigned int size
    cdef readonly unsigned int touch_on_read
    cdef list pqueue
    cdef dict emap
    cdef object eviction_callback
    
    def __init__(LRUCache self, unsigned int size, unsigned int touch_on_read = 1, object eviction_callback = None):
        self.size = size
        self.touch_on_read = touch_on_read
        self.pqueue = []
        self.emap = {}
        self.next_prio = 0
        self.eviction_callback = eviction_callback

    def __len__(LRUCache self not None):
        return len(self.pqueue)
    
    def __contains__(LRUCache self not None, object key):
        return key in self.emap

    cdef void c_rehash(LRUCache self):        
        cdef _borrowed_node *node
        cdef unsigned int i, sz
        cdef unsigned int bprio

        bprio = self.pqueue[0].prio
        sz = <unsigned int>PyList_GET_SIZE(self.pqueue)
        for i from 0 <= i < sz:
            node = <_borrowed_node*>PyList_GET_ITEM(self.pqueue, i)
            node.prio = node.prio - bprio
        self.next_prio = self.next_prio - bprio

    cdef void c_decrease(LRUCache self, _node node):
        cdef unsigned int ix, l, r, sw, sz
        cdef _borrowed_node *ln, *rn, *swn, *bnode

        node.prio = self.next_prio
        self.next_prio = self.next_prio + 1

        if self.next_prio == 0:
            self.c_rehash()

        # From here on, we work with borrowed references exclusively
        # This is possible since we're only shuffling items, in 
        # reference-neutral way, atomically, within a GIL-protected opcode
        bnode = <_borrowed_node*><void*>node
        sz = <unsigned int>PyList_GET_SIZE(self.pqueue)
        while 1:
            ix = bnode.index
            l  = 2 * ix + 1
            r  = 2 * ix + 2

            if r < sz:
                ln = <_borrowed_node*>PyList_GET_ITEM(self.pqueue,l)
                rn = <_borrowed_node*>PyList_GET_ITEM(self.pqueue,r)
            elif l < sz:
                ln = <_borrowed_node*>PyList_GET_ITEM(self.pqueue,l)

            if r < sz and rn.prio < ln.prio:
                sw = r
                swn= rn
            elif l < sz:
                sw = l
                swn= ln
            else:
                break

            # This is reference-neutral, so we can use the SET_ITEM macro
            PyList_SET_ITEM(self.pqueue, sw, <void*>bnode)
            PyList_SET_ITEM(self.pqueue, ix, <void*>swn)
            bnode.index = sw
            swn.index = ix

    def iterkeys(LRUCache self not None):
        return self.emap.iterkeys()

    def itervalues(LRUCache self not None):
        return LRUCacheValuesIterator(self)

    def iteritems(LRUCache self not None):
        return LRUCacheItemsIterator(self)

    def keys(LRUCache self not None):
        return self.emap.keys()

    def values(LRUCache self not None):
        return list(self.itervalues())

    def items(LRUCache self not None):
        return list(self.iteritems())

    def __iter__(LRUCache self not None):
        return self.iterkeys()

    cdef int c__setitem__(LRUCache self, object key, object val) except -1:
        cdef _node node
        cdef object oldkey, oldval

        if key in self.emap:
            node = self.emap[key]
            # delay collection of old value, to avoid firing python code and thus releasing the GIL
            oldval = node.value 
            node.value = val
            self.c_decrease(node)
        elif len(self.pqueue) >= self.size:
            node = PySequence_GetItem(self.pqueue, 0)
            oldkey = node.key   # delay collection of old key/value, to avoid
            oldval = node.value # firing python code and thus releasing the GIL
            del self.emap[node.key]
            node.key = key
            node.value = val
            self.emap[key] = node
            self.c_decrease(node)

            # Notify eviction
            if self.eviction_callback is not None:
                self.eviction_callback(oldkey, oldval)
        else:
            node = _node(self.next_prio, len(self.pqueue), key, val)
            self.emap[key] = node
            self.pqueue.append(node)
            self.next_prio = self.next_prio + 1
            if self.next_prio == 0:
                self.c_rehash()
        
        return 0

    def __setitem__(LRUCache self not None, object key, object val):
        self.c__setitem__(key, val)

    cdef object c__getitem__(LRUCache self, key):
        cdef _node node

        if key not in self.emap:
            raise CacheMissError(key)
        else:
            node = self.emap[key]
            if self.touch_on_read:
                self.c_decrease(node)
            return node.value
    
    def __getitem__(LRUCache self not None, key):
        return self.c__getitem__(key)

    cdef int c__delitem__(LRUCache self, key) except -1:
        cdef _node node, node2

        if key not in self.emap:
            raise CacheMissError(key)
        else:
            node = self.emap[key]
            self.c_decrease(node)

            node2 = self.pqueue[-1]
            if node2 is not node:
                self.pqueue[node.index] = node2
                node2.index = node.index

            del self.emap[key]
            del self.pqueue[-1]
            
            return 0

    def __delitem__(LRUCache self not None, key):
        self.c__delitem__(key)

    def get(LRUCache self not None, object key, object deflt = None):
        cdef _node node

        if key not in self.emap:
            return deflt
        else:
            node = self.emap[key]
            self.c_decrease(node)
            return node.value
    
    def pop(LRUCache self not None, object key, object deflt = CacheMissError):
        cdef object rv

        if key not in self.emap:
            if deflt is CacheMissError:
                raise CacheMissError(key)
            else:
                rv = deflt
        else:
            rv = self.c__getitem__(key)
            self.c__delitem__(key)
        
        return rv

    def setdefault(LRUCache self not None, object key, object deflt = None):
        cdef _node node

        if key not in self.emap:
            self.c__setitem__(key, deflt)
            return deflt
        else:
            node = self.emap[key]
            self.c_decrease(node)
            return node.value

    def update(LRUCache self not None, object iterOrDict):
        if isinstance(iterOrDict, dict) or isinstance(iterOrDict, LRUCache):
            for k,v in iterOrDict.iteritems():
                self[k] = v
        else:
            for k,v in iterOrDict:
                self[k] = v

    def clear(LRUCache self not None):
        # Hold onto old lists to prevent decref from freeing them before we're done
        cdef object pqueue, emap
        cdef object opqueue, oemap
        opqueue = self.pqueue
        oemap = self.emap
        pqueue = []
        emap = {}
        self.pqueue = pqueue
        self.emap = emap
        self.next_prio = 0
    
    def defrag(LRUCache self not None):
        # Hold onto old lists to prevent decref from freeing them before we're done
        cdef object pqueue, emap
        cdef object opqueue, oemap
        opqueue = self.pqueue
        oemap = self.emap
        pqueue = list(self.pqueue)
        emap = self.emap.copy()
        self.pqueue = pqueue
        self.emap = emap

    def __repr__(LRUCache self not None):
        return "<LRUCache (%d elements, %d max)>" % (len(self), self.size)


cdef class LRUCacheItemsIterator:
    cdef LRUCache cache
    cdef unsigned int pos

    def __init__(LRUCacheItemsIterator self not None, LRUCache cache not None):
        self.cache = cache
        self.pos = 0
    def __next__(LRUCacheItemsIterator self not None):
        cdef _node node
        if self.pos >= len(self.cache.pqueue):
            raise StopIteration
        else:
            node = self.cache.pqueue[self.pos]
            self.pos = self.pos + 1
            return ( node.key, node.value )
    def __iter__(LRUCacheItemsIterator self not None):
        return self
    
cdef class LRUCacheValuesIterator:
    cdef LRUCache cache
    cdef unsigned int pos

    def __init__(LRUCacheValuesIterator self not None, LRUCache cache not None):
        self.cache = cache
        self.pos = 0
    def __next__(LRUCacheValuesIterator self not None):
        cdef _node node
        if self.pos >= len(self.cache.pqueue):
            raise StopIteration
        else:
            node = self.cache.pqueue[self.pos]
            self.pos = self.pos + 1
            return node.value
    def __iter__(LRUCacheValuesIterator self not None):
        return self
    
