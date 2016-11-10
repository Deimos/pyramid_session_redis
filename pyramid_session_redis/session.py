# -*- coding: utf-8 -*-

import binascii
import os
import hashlib

from pyramid.compat import text_
from pyramid.decorator import reify
from pyramid.interfaces import ISession
from zope.interface import implementer

from .compat import cPickle
from .exceptions import InvalidSession
from .util import (
    persist,
    refresh,
    to_unicode,
    )


def hashed_value(serialized):
    """
    quick hash of serialized data
    only used for comparison
    """
    return hashlib.md5(serialized).hexdigest()


class _SessionState(object):
    # markers for update
    please_persist = None
    please_refresh = None

    # these markers are consulted in cleanup routines
    dont_persist = None
    dont_refresh = None

    def __init__(
        self,
        session_id,
        managed_dict,
        created,
        timeout,
        new,
        persisted_hash
    ):
        self.session_id = session_id
        self.managed_dict = managed_dict
        self.created = created
        self.timeout = timeout
        self.new = new
        self.persisted_hash = persisted_hash

    def should_persist(self, session):
        """
        this is a backup routine
        compares the persisted hash with a hash of the current value
        returns `False` or `serialized_session`
        """
        if self.dont_persist:
            return False
        if self.please_persist:
            return True
        if not session._detect_changes:
            return False
        serialized_session = session.to_redis()
        serialized_hash = hashed_value(serialized_session)
        if serialized_hash == self.persisted_hash:
            return False
        return serialized_session


@implementer(ISession)
class RedisSession(object):
    """
    Implements the Pyramid ISession and IDict interfaces and is returned by
    the ``RedisSessionFactory``.

    Methods that modify the ``dict`` (get, set, update, etc.) are decorated
    with ``@persist`` to update the persisted copy in Redis and reset the
    timeout.

    Methods that are read-only (items, keys, values, etc.) are decorated
    with ``@refresh`` to reset the session's expire time in Redis.

    Session methods make use of the dict methods that already communicate with
    Redis, so they are not decorated.

    Parameters:

    ``redis``
    A Redis connection object.

    ``session_id``
    A unique string associated with the session. Used as a prefix for keys
    and hashes associated with the session.

    ``new``
    Boolean. Whether this session is new (whether it was created in this
    request).

    ``new_session``
    A function that takes no arguments. It should insert a new session into
    Redis under a new session_id, and return that session_id.

    ``serialize``
    A function to serialize pickleable Python objects. Default:
    ``cPickle.dumps``.

    ``deserialize``
    The dual of ``serialize``, to convert serialized strings back to Python
    objects. Default: ``cPickle.loads``.

    ``assume_redis_lru``
    If ``True``, assumes redis is configured as a LRU and does not update the
    expiry data. Default: ``None``

    ``detect_changes``
    If ``True``, supports change detection Default: ``True``
    
    ``deserialized_fails_new``
    If ``True`` will handle deserializtion errors by creating a new session.
    """

    def __init__(
        self,
        redis,
        session_id,
        new,
        new_session,
        serialize=cPickle.dumps,
        deserialize=cPickle.loads,
        assume_redis_lru=None,
        detect_changes=True,
        deserialized_fails_new=None,
    ):

        self.redis = redis
        self.serialize = serialize
        self.deserialize = deserialize
        self._new_session = new_session
        self._assume_redis_lru = assume_redis_lru
        self._detect_changes = detect_changes
        self._deserialized_fails_new = deserialized_fails_new
        self._session_state = self._make_session_state(
            session_id=session_id,
            new=new,
            )

    @reify
    def _session_state(self):
        return self._make_session_state(
            session_id=self._new_session(),
            new=True,
            )

    def _make_session_state(self, session_id, new):
        (persisted,
         persisted_hash
         ) = self.from_redis(session_id=session_id,
                             persisted_hash=(True if self._detect_changes
                                             else False),
                             )
        # self.from_redis needs to take a session_id here, because otherwise it
        # would look up self.session_id, which is not ready yet as
        # session_state has not been created yet.
        return _SessionState(
            session_id=session_id,
            managed_dict=persisted['managed_dict'],
            created=persisted['created'],
            timeout=persisted['timeout'],
            new=new,
            persisted_hash=persisted_hash,
            )

    @property
    def session_id(self):
        return self._session_state.session_id

    @property
    def managed_dict(self):
        return self._session_state.managed_dict

    @property
    def created(self):
        return self._session_state.created

    @property
    def timeout(self):
        return self._session_state.timeout

    @property
    def new(self):
        return self._session_state.new

    def to_redis(self):
        """Serialize a dict of the data that needs to be persisted for this
        session, for storage in Redis.

        Primarily used by the ``@persist`` decorator to save the current
        session state to Redis.
        """
        return self.serialize({
            'managed_dict': self.managed_dict,
            'created': self.created,
            'timeout': self.timeout,
            })

    def from_redis(self, session_id=None, persisted_hash=None):
        """
        Get and deserialize the persisted data for this session from Redis.
        If ``persisted_hash`` is ``None`` (default), returns a single
        variable `deserialized`.
        If set to ``True`` or ``False``, returns a tuple.
        """
        persisted = self.redis.get(session_id or self.session_id)
        if persisted is None:
            raise InvalidSession("`session_id` (%s) not in Redis" % session_id)
        try:
            deserialized = self.deserialize(persisted)
        except Exception, e:
            if self._deserialized_fails_new:
                raise InvalidSession("`session_id` (%s) did not deserialize correctly" % session_id)
            raise e
        if persisted_hash is True:
            return (deserialized, hashed_value(persisted))
        elif persisted_hash is False:
            return (deserialized, None)
        return deserialized

    def invalidate(self):
        """Invalidate the session."""
        self.redis.delete(self.session_id)
        del self._session_state
        # Delete the self._session_state attribute so that direct access to or
        # indirect access via other methods and properties to .session_id,
        # .managed_dict, .created, .timeout and .new (i.e. anything stored in
        # self._session_state) after this will trigger the creation of a new
        # session with a new session_id.

    def do_persist(self, serialized_session=None):
        """actually and immediately persist to Redis backend"""
        # Redis is `key, value, timeout`
        # StrictRedis is `key, timeout, value`
        # this package uses StrictRedis
        if serialized_session is None:
            serialized_session = self.to_redis()
        self.redis.setex(self.session_id, self.timeout, serialized_session, )
        self._session_state.please_persist = False

    def do_refresh(self):
        """actually and immediately refresh the TTL to Redis backend"""
        self.redis.expire(self.session_id, self.timeout)
        self._session_state.please_refresh = False

    # dict modifying methods decorated with @persist
    @persist
    def __delitem__(self, key):
        del self.managed_dict[key]

    @persist
    def __setitem__(self, key, value):
        self.managed_dict[key] = value

    @persist
    def setdefault(self, key, default=None):
        return self.managed_dict.setdefault(key, default)

    @persist
    def clear(self):
        return self.managed_dict.clear()

    @persist
    def pop(self, key, default=None):
        return self.managed_dict.pop(key, default)

    @persist
    def update(self, other):
        return self.managed_dict.update(other)

    @persist
    def popitem(self):
        return self.managed_dict.popitem()

    # dict read-only methods decorated with @refresh
    @refresh
    def __getitem__(self, key):
        return self.managed_dict[key]

    @refresh
    def __contains__(self, key):
        return key in self.managed_dict

    @refresh
    def keys(self):
        return self.managed_dict.keys()

    @refresh
    def items(self):
        return self.managed_dict.items()

    @refresh
    def get(self, key, default=None):
        return self.managed_dict.get(key, default)

    @refresh
    def __iter__(self):
        return self.managed_dict.__iter__()

    @refresh
    def has_key(self, key):
        return key in self.managed_dict

    @refresh
    def values(self):
        return self.managed_dict.values()

    @refresh
    def itervalues(self):
        try:
            values = self.managed_dict.itervalues()
        except AttributeError:  # pragma: no cover
            values = self.managed_dict.values()
        return values

    @refresh
    def iteritems(self):
        try:
            items = self.managed_dict.iteritems()
        except AttributeError:  # pragma: no cover
            items = self.managed_dict.items()
        return items

    @refresh
    def iterkeys(self):
        try:
            keys = self.managed_dict.iterkeys()
        except AttributeError:  # pragma: no cover
            keys = self.managed_dict.keys()
        return keys

    @persist
    def changed(self):
        """ Persist all the data that needs to be persisted for this session
        immediately with ``@persist``.
        """
        pass

    # session methods persist or refresh using above dict methods
    def new_csrf_token(self):
        token = text_(binascii.hexlify(os.urandom(20)))
        self['_csrft_'] = token
        return token

    def get_csrf_token(self):
        token = self.get('_csrft_', None)
        if token is None:
            token = self.new_csrf_token()
        else:
            token = to_unicode(token)
        return token

    def flash(self, msg, queue='', allow_duplicate=True):
        storage = self.setdefault('_f_' + queue, [])
        if allow_duplicate or (msg not in storage):
            storage.append(msg)
            self.changed()  # notify redis of change to ``storage`` mutable

    def peek_flash(self, queue=''):
        storage = self.get('_f_' + queue, [])
        return storage

    def pop_flash(self, queue=''):
        storage = self.pop('_f_' + queue, [])
        return storage

    # RedisSession extra methods
    @persist
    def adjust_timeout_for_session(self, timeout_seconds):
        """
        Permanently adjusts the timeout for this session to ``timeout_seconds``
        for as long as this session is active. Useful in situations where you
        want to change the expire time for a session dynamically.
        """
        self._session_state.timeout = timeout_seconds

    @property
    def _invalidated(self):
        """
        Boolean property indicating whether the session is in the state where
        it has been invalidated but a new session has not been created in its
        place.
        """
        return '_session_state' not in self.__dict__
