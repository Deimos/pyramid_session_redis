# -*- coding: utf-8 -*-

import hashlib

from pyramid.decorator import reify
from pyramid.exceptions import ConfigurationError
from pyramid.interfaces import ISession
from zope.interface import implementer

from .compat import cPickle, token_hex
from .exceptions import InvalidSession, TimedOutSession, LegacySession
from .util import (
    persist,
    refresh,
    to_unicode,
    warn_future,
    LAZYCREATE_SESSION,
    int_time,
    SESSION_API_VERSION,
    empty_session_payload,
)
from .util import encode_session_payload as encode_session_payload_func
from .util import decode_session_payload as decode_session_payload_func


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
        expires,
        version,
        new,
        persisted_hash,
    ):
        self.session_id = session_id
        self.managed_dict = managed_dict
        self.created = created
        self.timeout = timeout
        self.expires = expires
        self.version = version
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
        if self.expires and session._timeout_trigger:
            if session.timestamp >= (self.expires - session._timeout_trigger):
                self.please_persist = True
        if self.please_persist:
            return session.to_redis()
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

    ``new_session_payload``
    UNDER DEVELOPMENT
    A function that takes no arguments.  It is used to to generate a new session
    payload without creating the id (as new_session might)

    ``serialize``
    A function to serialize pickleable Python objects. Default:
    ``cPickle.dumps``.

    ``deserialize``
    The dual of ``serialize``, to convert serialized strings back to Python
    objects. Default: ``cPickle.loads``.

    ``set_redis_ttl``
     If ``True`` sets TTL data in Redis.  If ``False`` assumes redis is
     configured as a LRU and does not update the expiry data via SETEX.
     Default: ``True``

    ``detect_changes``
    If ``True``, supports change detection Default: ``True``

    ``deserialized_fails_new``
    If ``True`` will handle deserializtion errors by creating a new session.
    
    ``new_payload_func``
    Default ``None``.  Function used to create a new session.

    ``timeout_trigger``
    Default ``None``.  If an int, used to trigger timeouts.

    ``python_expires``
    Default ``None``.  If True, Python is used to manage timeout data.  setting
    ``timeout_trigger`` will enable this.

    """

    def __init__(
        self,
        redis,
        session_id,  # could be ``LAZYCREATE_SESSION``
        new,
        new_session,
        new_payload_func=None,
        serialize=cPickle.dumps,
        deserialize=cPickle.loads,
        set_redis_ttl=True,
        detect_changes=True,
        deserialized_fails_new=None,
        encode_session_payload_func=None,
        decode_session_payload_func=None,
        timeout=None,
        timeout_trigger=None,
        python_expires=None,
    ):
        if timeout_trigger and not python_expires:  # fix this
            python_expires = True
        self.redis = redis
        self.serialize = serialize
        self.deserialize = deserialize
        self.new_session = new_session
        if new_payload_func is not None:
            self.new_payload = new_payload_func
        if encode_session_payload_func is not None:
            self.encode_session_payload = encode_session_payload_func
        if decode_session_payload_func is not None:
            self.decode_session_payload = decode_session_payload_func
        self._set_redis_ttl = set_redis_ttl
        self._detect_changes = detect_changes
        self._deserialized_fails_new = deserialized_fails_new
        self._timeout = timeout
        self._timeout_trigger = timeout_trigger
        self._python_expires = python_expires
        self._session_state = self._make_session_state(
            session_id=session_id,
            new=new,
        )

    def new_session(self):
        # this should be set via __init__
        raise NotImplementedError()

    def new_payload(self):
        # this should be set via __init__
        return empty_session_payload()

    def encode_session_payload(self, *args, **kwargs):
        """
        used to recode for serialization
        this can be overridden via __init__
        """
        return encode_session_payload_func(*args, **kwargs)

    def decode_session_payload(self, payload):
        """
        used to recode for serialization
        this can be overridden via __init__
        """
        return decode_session_payload_func(payload)

    def serialize(self):
        # this should be set via __init__
        raise NotImplementedError()

    def deserialize(self):
        # this should be set via __init__
        raise NotImplementedError()

    @reify
    def _session_state(self):
        """this should only be executed after an `invalidate()`
        The `invalidate()` will "del self._session_state", which will remove the
        '_session_state' entry from the object dict (as created by __init__ or by
        this function which reify's the return value). Removing that function
        allows this method to execute, and `reify` puts the new result in the
        object's dict.
        """
        return self._make_session_state(
            session_id=LAZYCREATE_SESSION,
            new=True,
        )

    @reify
    def timestamp(self):
        return int_time()

    def _make_session_state(self, session_id, new):
        """this will try to load the session_id in redis via ``from_redis``.
        If this fails, it will raise ``InvalidSession``"""
        if session_id == LAZYCREATE_SESSION:
            persisted_hash = None
            persisted = self.new_payload()
        else:
            # self.from_redis needs to take a session_id here, because otherwise it
            # would look up self.session_id, which is not ready yet as
            # session_state has not been created yet.
            (persisted,
             persisted_hash
             ) = self.from_redis(session_id=session_id,
                                 persisted_hash=(True if self._detect_changes
                                                 else False),
                                 )
            expires = persisted.get('x')
            if expires:
                if self.timestamp > expires:
                    raise TimedOutSession("`session_id` (%s) timed out in python" % session_id)
            version = persisted.get('v')
            if not version or (version < SESSION_API_VERSION):
                raise LegacySession("`session_id` (%s) is a legacy format" % session_id)
        return _SessionState(
            session_id=session_id,
            managed_dict=persisted['m'],  # managed_dict
            created=persisted['c'],  # created
            timeout=persisted.get('t'),  # timeout
            expires=persisted.get('x'),  # expires
            version=persisted.get('v'),  # session api version
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
    def expires(self):
        return self._session_state.expires

    @property
    def version(self):
        return self._session_state.version

    @property
    def new(self):
        return self._session_state.new

    def to_redis(self):
        """Serialize a dict of the data that needs to be persisted for this
        session, for storage in Redis.

        Primarily used by the ``@persist`` decorator to save the current
        session state to Redis.
        """
        data = self.encode_session_payload(self.managed_dict,
                                           self.created,
                                           self.timeout,
                                           python_expires=self._python_expires,
                                           )
        return self.serialize(data)

    def from_redis(self, session_id=None, persisted_hash=None):
        """
        Get and deserialize the persisted data for this session from Redis.
        If ``persisted_hash`` is ``None`` (default), returns a single
        variable `deserialized`.
        If set to ``True`` or ``False``, returns a tuple.
        """
        _session_id = session_id or self.session_id
        if _session_id == LAZYCREATE_SESSION:
            raise InvalidSession("`session_id` is LAZYCREATE_SESSION")
        persisted = self.redis.get(_session_id)
        if persisted is None:
            raise InvalidSession("`session_id` (%s) not in Redis" % _session_id)
        try:
            deserialized = self.deserialize(persisted)
        except Exception:
            if self._deserialized_fails_new:
                raise InvalidSession("`session_id` (%s) did not deserialize correctly" % _session_id)
            raise
        if persisted_hash is True:
            return (deserialized, hashed_value(persisted))
        elif persisted_hash is False:
            return (deserialized, None)
        return deserialized

    def invalidate(self):
        """Invalidate the session."""
        if self.session_id != LAZYCREATE_SESSION:
            self.redis.delete(self.session_id)
        # Delete the self._session_state attribute so that direct access to or
        # indirect access via other methods and properties to .session_id,
        # .managed_dict, .created, .timeout and .new (i.e. anything stored in
        # self._session_state) after this will trigger the creation of a new
        # session with a new session_id via the `_session_state()` reified
        # property.
        del self._session_state

    def ensure_id(self):
        # this ensures we have a session_id
        if self._session_state.session_id == LAZYCREATE_SESSION:
            self._session_state.session_id = self.new_session()
        return self._session_state.session_id

    @property
    def session_id_safecheck(self):
        """if we don't have a managed_dict, return None"""
        if not self.managed_dict:
            return None
        return self.ensure_id()

    def do_persist(self, serialized_session=None):
        """
        Actually and immediately persist to Redis backend
        Only set a timeout in Redis timeout if we have timeouts AND are not in LRU mode
        Note: this package uses StrictRedis(`key, timeout, value`)
              not Redis(`key, value, timeout`)
        """
        self.ensure_id()
        if serialized_session is None:
            serialized_session = self.to_redis()
        serverside_timeout = True if self.timeout is not None and self._set_redis_ttl else False
        if serverside_timeout:
            self.redis.setex(self.session_id, self.timeout, serialized_session)
        else:
            self.redis.set(self.session_id, serialized_session)
        self._session_state.please_persist = False

    def do_refresh(self, force_redis_ttl=None):
        """
        Actually and immediately refresh the TTL to Redis backend.
        Does nothing if no timeout set (in LRU mode).
        Optional kwarg for developers ``force_redis_ttl`` (default None)
        can be provided to force a new Redis TTL.
        """
        if force_redis_ttl is not None:
            self.redis.expire(self.session_id, force_redis_ttl)
        else:
            if self.timeout is not None:
                if self._set_redis_ttl:
                    # set a TTL if unless we're in LRU mode
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
        token = token_hex(32)
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

    @persist
    def adjust_expires_for_session(self, expires_epoch):
        """
        Updates the epoch used for python timeouts.
        """
        self._session_state.expires = expires_epoch

    @property
    def _invalidated(self):
        """
        Boolean property indicating whether the session is in the state where
        it has been invalidated but a new session has not been created in its
        place.
        """
        return '_session_state' not in self.__dict__
