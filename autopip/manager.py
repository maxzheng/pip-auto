from configparser import RawConfigParser
from collections import defaultdict
import os
from pathlib import Path, PurePath
import re
import shutil
from subprocess import check_call as run
import sys
import urllib.request
import urllib.error


class MissingCommandError(RuntimeError):
    """ Indicates a required CLI command is missing """


class AppsManager:
    """ Manages apps """

    def __init__(self):
        #: An instance of :cls:`AppsPath`
        self.paths = AppsPath()

        #: Path to install all our apps
        self.install_root = self.paths.install_root

        #: Path to create symlinks for bin scripts
        self.symlink_root = self.paths.symlink_root

        # PyPI url
        self._index_url = None

        # PyPI auth. Tuple of user and password.
        self._index_auth = None

    def install(self, apps):
        """
        Install the given apps

        :param list[str] apps: List of apps to install
        """
        self._ensure_paths()
        self._set_index()

        for app in apps:
            self._install_pkg(app)

    def _ensure_paths(self):
        """ Ensure install and symlink paths are created """
        self.install_root.mkdir(parents=True, exist_ok=True)
        self.symlink_root.mkdir(parents=True, exist_ok=True)

    def _install_pkg(self, name):
        """ Install the given app """
        version = self._pkg_version(name)

        app = App(name, self.paths)
        app.install(version)

    def _pkg_version(self, name):
        """ Get app version from PyPI """
        pkg_index_url = self._index_url + name + '/'

        try:
            if self._index_auth:
                password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
                password_mgr.add_password(None, self._index_url, self._index_auth[0], self._index_auth[1])
                handler = urllib.request.HTTPBasicAuthHandler(password_mgr)
                opener = urllib.request.build_opener(handler)

            else:
                opener = urllib.request.build_opener()

            with opener.open(str(pkg_index_url), timeout=10) as fp:
                version_links = fp.read().decode('utf-8')

        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NameError(f'App does not exist on {self._index_url}')

        version_re = re.compile(name + '-(\d+\.\d+\.\d+(?:\.\w+\d+)?)\.')
        version = None

        for line in version_links.split('\n'):
            match = version_re.search(line)
            if match:
                version = match.group(1)

        if not version:
            raise ValueError(f'No app version found in {pkg_index_url}')

        return version

    def _set_index(self):
        """ Set PyPI url and auth """
        if not self._index_url:
            try:
                pip_conf = Path('~/.pip/pip.conf').expanduser()
                parser = RawConfigParser()
                parser.read(pip_conf)
                self._index_url = parser.get('global', 'index-url')

                auth_re = re.compile('//([^:]+)(?::([^@]+))?@')
                match = auth_re.search(self._index_url)
                if match:
                    self._index_auth = match.groups()
                    self._index_url = auth_re.sub('//', self._index_url)

                if not self._index_url.endswith('/'):
                    self._index_url += '/'

            except Exception:
                self._index_url = 'https://pypi.org/simple/'

    def list(self, scripts=False):
        """
        List installed apps

        :param bool scripts: Show scripts
        """
        if not self.install_root.exists():
            print('No apps are installed yet')
            return

        app_info = []
        info_lens = defaultdict(int)

        for app_path in sorted(self.install_root.iterdir()):
            if app_path == self.symlink_root:
                continue

            app = App(app_path.name, self.paths)
            app_path = str(app.current_path.resolve())
            app_info.append((app.name, app.current_version, app_path))

            if scripts:
                for hide_path, script in enumerate(app.scripts()):
                    script_symlink = self.symlink_root / script
                    if script_symlink.exists() and str(script_symlink.resolve()).startswith(app_path):
                        script_path = str(script_symlink)
                        if hide_path:
                            script_path = script_path.replace(str(script_symlink.parent) + '/',
                                                              ' ' * len(str(script_symlink.parent)) + ' ')
                        app_info.append(('', '', script_path))

        # Figure out max length of each column
        for info in app_info:
            for i, value in enumerate(info):
                info_lens[i] = len(value) if len(value) > info_lens[i] else info_lens[i]

        # Print table
        table_style = '  '.join('{{:{}}}'.format(l) for l in info_lens.values())
        for info in app_info:
            print(table_style.format(*info))

    def uninstall(self, apps):
        """ Uninstall apps """
        for name in apps:
            if name == 'bin':  # Don't try to remove bin (contains symlinks to scripts) from the app dir
                continue

            app = App(name, self.paths)
            app.uninstall()


class App:
    """ Represents an app that may or may not be installed on disk """

    #: Prefixes of scripts to skip when creating symlinks
    SKIP_SCRIPT_PREFIXES = {'activate', 'pip', 'easy_install', 'python', 'wheel'}

    def __init__(self, name, paths):
        """
        :param str name: Name of the app
        :param AppsPath paths: Path paths
        """
        self.name = name
        self.paths = paths

        self.path = self.paths.install_root / name
        self._current_symlink = self.path / 'current'

    @property
    def is_installed(self):
        """ Is the app installed? """
        return self._current_symlink.exists()

    @property
    def current_path(self):
        """ Path to currently installed app """
        return self._current_symlink if self.is_installed else None

    @property
    def current_version(self):
        """ Currently installed version """
        if self.current_path:
            return self.current_path.resolve().name

    def install(self, version):
        """
        Install the version of the app if it is not already installed

        :param str version: Version of the app to install
        """
        version_path = self.path / version
        prev_version_path = self.current_path and self.current_path.resolve()
        important_paths = [version_path, prev_version_path, self._current_symlink]

        if not shutil.which('curl'):
            raise MissingCommandError('curl is not available and is required to install pip. '
                                      'Please install and then re-run')

        if version_path.exists():
            print(f'{self.name} is already installed')

        else:
            try:
                print(f'Installing {self.name} to {version_path}')
                if 'VIRTUAL_ENV' in os.environ:
                    venv_dir = os.environ.pop('VIRTUAL_ENV')
                    os.environ['PATH'] = os.pathsep.join([p for p in os.environ['PATH'].split(os.pathsep)
                                                          if os.path.exists(p) and not p.startswith(venv_dir)])
                run(f"""
                    python3 -m venv {version_path} --without-pip
                    source {version_path / 'bin/activate'}
                    curl -s https://bootstrap.pypa.io/get-pip.py | python > /dev/null
                    pip install -q {self.name}=={version}
                    """, executable='/bin/bash', shell=True)

            except:  # noqa
                shutil.rmtree(version_path, ignore_errors=True)
                raise

        # Update current symlink
        if not self.current_path or self.current_path.resolve() != version_path:
            atomic_symlink = self.path / f'atomic_symlink_for_{self.name}'
            atomic_symlink.symlink_to(version_path)
            atomic_symlink.replace(self._current_symlink)

            # Remove older versions
            for path in [p for p in self.path.iterdir() if p not in important_paths]:
                shutil.rmtree(path, ignore_errors=True)

        # Install script symlinks
        current_bin_path = self.current_path / 'bin'
        prev_bin_path = prev_version_path / 'bin' if prev_version_path else None
        current_scripts = self.scripts(current_bin_path)

        if not current_scripts:
            print('! Odd, there are no scripts included in the app, so there is no point installing it.')
            print('  autopip is for installing apps with scripts. To install libraries, please use pip.')
            print('  If you are the app owner, be make sure to setup entry_points in setup.py.')
            print('  See http://setuptools.readthedocs.io/en/latest/setuptools.html#automatic-script-creation')
            self.uninstall()
            sys.exit(1)

        prev_scripts = self.scripts(prev_bin_path) if prev_bin_path else set()
        old_scripts = prev_scripts - current_scripts

        printed_updating = False

        for script in sorted(current_scripts):
            script_symlink = self.paths.symlink_root / script
            script_path = current_bin_path / script

            if script_symlink.resolve() == script_path.resolve():
                continue

            if not printed_updating:
                print('Updating symlinks in {}'.format(self.paths.symlink_root))
                printed_updating = True

            if script_symlink.exists():
                if self.paths.covers(script_symlink):
                    atomic_symlink = self.paths.symlink_root / f'atomic_symlink_for_{self.name}'
                    atomic_symlink.symlink_to(script_path)
                    atomic_symlink.replace(script_symlink)
                    print('* {} (updated)'.format(script_symlink.name))

                else:
                    print('! {} (can not change / not managed by autopip)'.format(script_symlink.name))

            else:
                script_symlink.symlink_to(script_path)
                print('+ ' + str(script_symlink.name))

        for script in sorted(old_scripts):
            script_symlink = self.symlink_root / script
            if script_symlink.exists():
                script_symlink.unlink()
                print('- '.format(script_symlink.name))

    def scripts(self, path=None):
        """ Get scripts for the given path. Defaults to current path for app. """
        if not path:
            if not self.current_path:
                return []

            path = self.current_path / 'bin'

        scripts = set()
        for script in path.iterdir():
            if any(p for p in self.SKIP_SCRIPT_PREFIXES if script.name.startswith(p)):
                continue
            scripts.add(script.name)

        return scripts

    def uninstall(self):
        """ Uninstall app """
        print('Uninstalling', self.name)

        for script in self.scripts():
            script_symlink = self.symlink_root / script
            if script_symlink.exists() and str(script_symlink.resolve()).startswith(str(self.path)):
                script_symlink.unlink()

        shutil.rmtree(self.path)


class AppsPath:
    """
    Checks user access and determine if we are installing to system vs user path.

    System paths are /opt and /usr/local/bin and user paths are in ~
    """
    #: Directory name to store apps in
    SYSTEM_INSTALL_ROOT = Path('/opt/apps')
    SYSTEM_SYMLINK_ROOT = Path('/usr/local/bin')

    USER_INSTALL_ROOT = Path('~/.apps').expanduser()
    USER_SYMLINK_ROOT = USER_INSTALL_ROOT / 'bin'

    def __init__(self):
        #: A list of reasons why we don't have system access
        self.system_access_denied_reasons = self._check_system_access()

        #: Indicates if we have access to system resources
        self.system_access = not self.system_access_denied_reasons

        #: Indicates if we should act on user resources as we do not have access to system resources.
        self.user_access = not self.system_access

    def _check_system_access(self):
        """ Check to see if we have access to system resources and return the reasons """
        reasons = []

        if not os.access(self.SYSTEM_INSTALL_ROOT.parent, os.W_OK):
            reasons.append(f'No permission to write to {self.SYSTEM_INSTALL_ROOT.parent}')

        if not os.access(self.SYSTEM_SYMLINK_ROOT, os.W_OK):
            reasons.append(f'No permission to write to {self.SYSTEM_SYMLINK_ROOT}')

        return reasons

    @property
    def install_root(self):
        return self.SYSTEM_INSTALL_ROOT if self.system_access else self.USER_INSTALL_ROOT

    @property
    def symlink_root(self):
        return self.SYSTEM_SYMLINK_ROOT if self.system_access else self.USER_SYMLINK_ROOT

    def covers(self, path):
        """ True if the given path belongs to autopip """
        path = path.resolve() if isinstance(path, PurePath) else path
        return str(path).startswith(str(self.install_root))
