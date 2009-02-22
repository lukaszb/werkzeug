# -*- coding: utf-8 -*-
"""
    werkzeug.contrib.sessions
    ~~~~~~~~~~~~~~~~~~~~~~~~~

    This module contains some helper classes that helps one to add session
    support to a python WSGI application.

    Example::

        from werkzeug.contrib.sessions import SessionMiddleware, \
             FilesystemSessionStore

        app = SessionMiddleware(app, FilesystemSessionStore())

    The current session will then appear in the WSGI environment as
    `werkzeug.session`.  However it's recommended to not use the middleware
    but the stores directly in the application.  However for very simple
    scripts a middleware for sessions could be sufficient.

    This module does not implement methods or ways to check if a session is
    expired.  That should be done by a cronjob and storage specific.  For
    example to prune unused filesystem sessions one could check the modified
    time of the files.  It sessions are stored in the database the new()
    method should add an expiration timestamp for the session.

    For better flexibility it's recommended to not use the middleware but the
    store and session object directly in the application dispatching::

        session_store = FilesystemSessionStore()

        def application(environ, start_response):
            request = Request(environ)
            sid = request.cookie.get('cookie_name')
            if sid is None:
                request.session = session_store.new()
            else:
                request.session = session_store.get(sid)
            response = get_the_response_object(request)
            if request.session.should_save:
                session_store.save(request.session)
                response.set_cookie('cookie_name', request.session.sid)
            return response(environ, start_response)

    :copyright: (c) 2009 by the Werkzeug Team, see AUTHORS for more details.
    :license: BSD, see LICENSE for more details.
"""
import re
import os
from os import path
from time import time
from random import random
try:
    from hashlib import sha1
except ImportError:
    from sha import new as sha1
from cPickle import dump, load, HIGHEST_PROTOCOL
from werkzeug.utils import ClosingIterator, dump_cookie, parse_cookie


_sha1_re = re.compile(r'^[a-fA-F0-9]{40}$')


def _urandom():
    if hasattr(os, 'urandom'):
        return os.urandom(30)
    return random()


def generate_key(salt=None):
    return sha1('%s%s%s' % (salt, time(), _urandom())).hexdigest()


class ModificationTrackingDict(dict):
    __slots__ = ('modified',)

    def __init__(self, *args, **kwargs):
        dict.__init__(self, *args, **kwargs)
        self.modified = False

    def __repr__(self):
        return '<%s %s%s>' % (
            self.__class__.__name__,
            dict.__repr__(self)
        )

    def copy(self):
        """Create a flat copy of the dict."""
        missing = object()
        result = object.__new__(self.__class__)
        for name in self.__slots__:
            val = getattr(self, name, missing)
            if val is not missing:
                setattr(result, name, val)
        return result

    def __copy__(self):
        return self.copy()

    def call_with_modification(f):
        def oncall(self, *args, **kw):
            try:
                return f(self, *args, **kw)
            finally:
                self.modified = True
        try:
            oncall.__name__ = f.__name__
            oncall.__doc__ = f.__doc__
            oncall.__module__ = f.__module__
        except:
            pass
        return oncall

    __setitem__ = call_with_modification(dict.__setitem__)
    __delitem__ = call_with_modification(dict.__delitem__)
    clear = call_with_modification(dict.clear)
    pop = call_with_modification(dict.pop)
    popitem = call_with_modification(dict.popitem)
    setdefault = call_with_modification(dict.setdefault)
    update = call_with_modification(dict.update)
    del call_with_modification


class Session(ModificationTrackingDict):
    """
    Subclass of a dict that keeps track of direct object changes.  Changes
    in mutable structures are not tracked, for those you have to set
    `modified` to `True` by hand.
    """
    __slots__ = ModificationTrackingDict.__slots__ + ('sid', 'new')

    def __init__(self, data, sid, new=False):
        ModificationTrackingDict.__init__(self, data)
        self.sid = sid
        self.new = new

    def __repr__(self):
        return '<%s %s%s>' % (
            self.__class__.__name__,
            dict.__repr__(self),
            self.should_save and '*' or ''
        )

    @property
    def should_save(self):
        """True if the session should be saved."""
        return self.modified or self.new


class SessionStore(object):
    """Baseclass for all session stores.  The Werkzeug contrib module does not
    implement any useful stores beside the filesystem store, application
    developers are encouraged to create their own stores.
    """

    def __init__(self, session_class=None):
        if session_class is None:
            session_class = Session
        self.session_class = session_class

    def is_valid_key(self, key):
        """Check if a key has the correct format."""
        return _sha1_re.match(key) is not None

    def generate_key(self, salt=None):
        """Simple function that generates a new session key."""
        return generate_key(salt)

    def new(self):
        """Generate a new session."""
        return self.session_class({}, self.generate_key(), True)

    def save(self, session):
        """Save a session."""

    def save_if_modified(self, session):
        """Save if a session class wants an update."""
        if session.should_save:
            self.save(session)

    def delete(self, session):
        """Delete a session."""

    def get(self, sid):
        """Get a session for this sid or a new session object.  This method
        has to check if the session key is valid and create a new session if
        that wasn't the case.
        """
        return self.session_class({}, sid, True)


class FilesystemSessionStore(SessionStore):
    """Simple example session store that saves session on the filesystem like
    PHP does.
    """

    def __init__(self, path=None, filename_template='werkzeug_%s.sess',
                 session_class=Session):
        SessionStore.__init__(self, session_class)
        if path is None:
            from tempfile import gettempdir
            path = gettempdir()
        self.path = path
        self.filename_template = filename_template

    def get_session_filename(self, sid):
        return path.join(self.path, self.filename_template % sid)

    def save(self, session):
        f = file(self.get_session_filename(session.sid), 'wb')
        try:
            dump(dict(session), f, HIGHEST_PROTOCOL)
        finally:
            f.close()

    def delete(self, session):
        fn = self.get_session_filename(session.sid)
        try:
            # Late import because Google Appengine won't allow os.unlink
            from os import unlink
            unlink(fn)
        except OSError:
            pass

    def get(self, sid):
        fn = self.get_session_filename(sid)
        if not self.is_valid_key(sid) or not path.exists(fn):
            return self.new()
        else:
            f = file(fn, 'rb')
            try:
                data = load(f)
            finally:
                f.close()
        return self.session_class(data, sid, False)


class SessionMiddleware(object):
    """A simple middleware that puts the session object of a store provided
    into the WSGI environ.  It automatically sets cookies and restores
    sessions.

    However a middleware is not the preferred solution because it won't be as
    fast as sessions managed by the application itself and will put a key into
    the WSGI environment only relevant for the application which is against
    the concept of WSGI.
    """

    def __init__(self, app, store, cookie_name='session_id',
                 cookie_age=None, cookie_expires=None, cookie_path='/',
                 cookie_domain=None, cookie_secure=None,
                 cookie_httponly=False, environ_key='werkzeug.session'):
        """The cookie parameters are the same as for the `dump_cookie`
        function just prefixed with "cookie_".  Additionally "max_age" is
        called "cookie_age" and not "cookie_max_age" because of backwards
        compatibility.
        """
        self.app = app
        self.store = store
        self.cookie_name = cookie_name
        self.cookie_age = cookie_age
        self.cookie_expires = cookie_expires
        self.cookie_path = cookie_path
        self.cookie_domain = cookie_domain
        self.cookie_secure = cookie_secure
        self.cookie_httponly = cookie_httponly
        self.environ_key = environ_key

    def __call__(self, environ, start_response):
        cookie = parse_cookie(environ.get('HTTP_COOKIE', ''))
        sid = cookie.get(self.cookie_name, None)
        if sid is None:
            session = self.store.new()
        else:
            session = self.store.get(sid)
        environ[self.environ_key] = session

        def injecting_start_response(status, headers, exc_info=None):
            if session.should_save:
                self.store.save(session)
                headers.append(('Set-Cookie', dump_cookie(self.cookie_name,
                                session.sid, self.cookie_age,
                                self.cookie_expires, self.cookie_path,
                                self.cookie_domain, self.cookie_secure,
                                self.cookie_httponly)))
            return start_response(status, headers, exc_info)
        return ClosingIterator(self.app(environ, injecting_start_response),
                               lambda: self.store.save_if_modified(session))
