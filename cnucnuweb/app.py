#-*- coding: utf-8 -*-

import codecs
import functools
import os
from collections import OrderedDict
from datetime import datetime

import docutils
import docutils.examples
import flask
import jinja2
import markupsafe
import sqlalchemy
from bunch import Bunch
from flask.ext.openid import OpenID

from cnucnu.package_list import Package

import cnucnuweb.model
import cnucnuweb.forms


__version__ = '0.1.0'

# Create the application.
APP = flask.Flask(__name__)

APP.config.from_object('cnucnuweb.default_config')
if 'CNUCNU_WEB_CONFIG' in os.environ:  # pragma: no cover
    app.config.from_envvar('CNUCNU_WEB_CONFIG')

# Set up OpenID
OID = OpenID(APP)

SESSION = cnucnuweb.model.init(
    APP.config['DB_URL'], debug=False, create=False)


URL_ALIASES = OrderedDict({
    '': 'Specific version page',
    'SF-DEFAULT': 'SourceForge project',
    'FM-DEFAULT': 'FreshMeat project',
    'GNU-DEFAULT': 'GNU project',
    'CPAN-DEFAULT': 'CPAN project',
    'HACKAGE-DEFAULT': 'Hackage project',
    'DEBIAN-DEFAULT': 'Debian project',
    'GOOGLE-DEFAULT': 'Google code project',
    'PYPI-DEFAULT': 'PYPI project',
    'PEAR-DEFAULT': 'PHP pear project',
    'PECL-DEFAULT': 'PHP pecl project',
    'LP-DEFAULT': 'LaunchPad project',
    'GNOME-DEFAULT': 'GNOME project',
    'RUBYGEMS-DEFAULT': 'Rubygems project',
})


REGEX_ALIASES = OrderedDict({
    '': 'Specific regex',
    'DEFAULT': 'Default regex',
    'CPAN-DEFAULT': 'Default CPAN regex',
    'PEAR-DEFAULT': 'Default PEAR regex',
    'PECL-DEFAULT': 'Default PECL regex',
    'FM-DEFAULT': 'Default FreshMeat regex',
    'HACKAGE-DEFAULT': 'Default Hackage regex',
    'RUBYGEMS-DEFAULT': 'Default Rubygems regex',
})


@APP.before_request
def check_auth():
    flask.g.auth = Bunch(
        logged_in=False,
        method=None,
        id=None,
    )
    if 'openid' in flask.session:
        flask.g.auth.logged_in = True
        flask.g.auth.method = u'openid'
        flask.g.auth.openid = flask.session.get('openid')
        flask.g.auth.fullname = flask.session.get('fullname', None)
        flask.g.auth.nickname = flask.session.get('nickname', None)
        flask.g.auth.email = flask.session.get('email', None)


@OID.after_login
def after_openid_login(resp):
    default = flask.url_for('index')
    if resp.identity_url:
        openid_url = resp.identity_url
        flask.session['openid'] = openid_url
        flask.session['fullname'] = resp.fullname
        flask.session['nickname'] = resp.nickname or resp.fullname
        flask.session['email'] = resp.email
        next_url = flask.request.args.get('next', default)
        return flask.redirect(next_url)
    else:
        return flask.redirect(default)


@APP.teardown_request
def shutdown_session(exception=None):
    """ Remove the DB session at the end of each request. """
    SESSION.remove()


def is_admin(user=None):
    if not user and flask.g.auth.logged_in:
        user = flask.g.auth.openid
    return user in APP.config.get('CNUCNU_WEB_ADMINS', [])


def login_required(function):
    """ Flask decorator to retrict access to logged-in users. """
    @functools.wraps(function)
    def decorated_function(*args, **kwargs):
        """ Decorated function, actually does the work. """
        if not flask.g.auth.logged_in:
            flask.flash('Login required', 'errors')
            return flask.redirect(
                flask.url_for('login', next=flask.request.url))

        return function(*args, **kwargs)
    return decorated_function


def api_method(function):
    """ A decorator to handle common API output stuff. """

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            result = function(*args, **kwargs)
        except APIError as e:
            app.logger.exception(e)
            response = flask.jsonify(e.errors)
            response.status_code = e.status_code
        else:
            # Redirect browsers to the object.
            # otherwise, return json response to api clients.
            if 'url' in result and request_wants_html():
                response = flask.redirect(result['url'])
            else:
                response = flask.jsonify(result)
                response.status_code = 200
        return response

    return wrapper


@APP.context_processor
def inject_variable():
    """ Inject into all templates variables that we would like to have all
    the time.
    """
    return dict(version=__version__,
                is_admin=is_admin())


@APP.route('/login/', methods=('GET', 'POST'))
@APP.route('/login', methods=('GET', 'POST'))
@OID.loginhandler
def login():
    default = flask.url_for('index')
    next_url = flask.request.args.get('next', default)
    if flask.g.auth.logged_in:
        return flask.redirect(next_url)

    openid_server = flask.request.form.get('openid', None)
    if openid_server:
        return OID.try_login(
            openid_server, ask_for=['email', 'fullname', 'nickname'])

    return flask.render_template(
        'login.html', next=OID.get_next_url(), error=OID.fetch_error())


@APP.route('/login/fedora/')
@APP.route('/login/fedora')
@OID.loginhandler
def fedora_login():
    default = flask.url_for('index')
    next_url = flask.request.args.get('next', default)
    return OID.try_login(
        APP.config['CNUCNU_WEB_FEDORA_OPENID'],
        ask_for=['email', 'fullname', 'nickname'])


@APP.route('/login/google/')
@APP.route('/login/google')
@OID.loginhandler
def google_login():
    default = flask.url_for('index')
    next_url = flask.request.args.get('next', default)
    return OID.try_login(
        "https://www.google.com/accounts/o8/id",
        ask_for=['email', 'fullname'])


@APP.route('/login/yahoo/')
@APP.route('/login/yahoo')
@OID.loginhandler
def yahoo_login():
    default = flask.url_for('index')
    next_url = flask.request.args.get('next', default)
    return OID.try_login(
        "https://me.yahoo.com/",
        ask_for=['email', 'fullname'])


@APP.route('/logout/')
@APP.route('/logout')
def logout():
    flask.session.pop('openid')
    return flask.redirect(flask.url_for('index'))


def modify_rst(rst):
    """ Downgrade some of our rst directives if docutils is too old. """

    try:
        # The rst features we need were introduced in this version
        minimum = [0, 9]
        version = map(int, docutils.__version__.split('.'))

        # If we're at or later than that version, no need to downgrade
        if version >= minimum:
            return rst
    except Exception:
        # If there was some error parsing or comparing versions, run the
        # substitutions just to be safe.
        pass

    # Otherwise, make code-blocks into just literal blocks.
    substitutions = {
        '.. code-block:: javascript': '::',
    }
    for old, new in substitutions.items():
        rst = rst.replace(old, new)

    return rst


def modify_html(html):
    """ Perform style substitutions where docutils doesn't do what we want.
    """

    substitutions = {
        '<tt class="docutils literal">': '<code>',
        '</tt>': '</code>',
    }
    for old, new in substitutions.items():
        html = html.replace(old, new)

    return html


def preload_docs(endpoint):
    """ Utility to load an RST file and turn it into fancy HTML. """

    here = os.path.dirname(os.path.abspath(__file__))
    fname = os.path.join(here, 'docs', endpoint + '.rst')
    with codecs.open(fname, 'r', 'utf-8') as f:
        rst = f.read()

    rst = modify_rst(rst)
    api_docs = docutils.examples.html_body(rst)
    api_docs = modify_html(api_docs)
    api_docs = markupsafe.Markup(api_docs)
    return api_docs

htmldocs = dict.fromkeys(['about'])
for key in htmldocs:
    htmldocs[key] = preload_docs(key)


def load_docs(request):
    URL = request.url_root
    docs = htmldocs[request.endpoint]
    docs = jinja2.Template(docs).render(URL=URL)
    return markupsafe.Markup(docs)


# Finalize the import of other controllers
import api
import ui
import admin
