'''
Module to store conda build settings.
'''
from __future__ import absolute_import, division, print_function

from collections import namedtuple
import copy
import logging
import math
import os
from os.path import abspath, expanduser, join
import sys
import time

from .conda_interface import root_dir, root_writable, subdir, cc_platform, cc_conda_build
from .conda_interface import string_types, binstar_upload

from .utils import get_build_folders, rm_rf, get_conda_operation_locks

on_win = (sys.platform == 'win32')
DEFAULT_PREFIX_LENGTH = 255

# Don't "save" an attribute of this module for later, like build_prefix =
# conda_build.config.config.build_prefix, as that won't reflect any mutated
# changes.

conda_build = "conda-build"


def _ensure_dir(path):
    # this can fail in parallel operation, depending on timing.  Just try to make the dir,
    #    but don't bail if fail.
    if not os.path.isdir(path):
        try:
            os.makedirs(path)
        except OSError:
            pass


class Config(object):
    __file__ = __path__ = __file__
    __package__ = __package__
    __doc__ = __doc__

    def __init__(self, **kwargs):
        super(Config, self).__init__()
        self.set_keys(**kwargs)

    def _set_attribute_from_kwargs(self, kwargs, attr, default):
        value = kwargs.get(attr, getattr(self, attr) if hasattr(self, attr) else default)
        setattr(self, attr, value)
        if attr in kwargs:
            del kwargs[attr]

    def set_keys(self, **kwargs):
        def env(lang, default):
            version = kwargs.get(lang)
            if not version:
                # Hooray for corner cases.
                if lang == 'python':
                    lang = 'py'
                var = 'CONDA_' + lang.upper()
                version = os.getenv(var) if os.getenv(var) else default
            elif isinstance(version, list) and len(version) == 1:
                version = version[0]
            return version

        self.CONDA_PERL = env('perl', '5.20.3')
        self.CONDA_LUA = env('lua', '5.2')
        self.CONDA_R = env('r', '3.3.2')
        self.CONDA_PY = int(env('python', "%s%s" % (sys.version_info.major, sys.version_info.minor))
                        .replace('.', ''))

        self.CONDA_NPY = kwargs.get('numpy')
        # if keyword argument is not present get numpy version from environment variable
        if not self.CONDA_NPY:
            self.CONDA_NPY = os.getenv("CONDA_NPY")
        if self.CONDA_NPY:
            if not isinstance(self.CONDA_NPY, string_types):
                self.CONDA_NPY = self.CONDA_NPY[0]
            self.CONDA_NPY = int(self.CONDA_NPY.replace('.', '')) or None

        self._build_id = kwargs.get('build_id', getattr(self, '_build_id', ""))
        croot = kwargs.get('croot')
        if croot:
            self._croot = os.path.abspath(os.path.normpath(croot))
        else:
            # set default value (not actually None)
            self._croot = getattr(self, '_croot', None)

        Setting = namedtuple("ConfigSetting", "name, default")
        values = [Setting('activate', True),
                  Setting('anaconda_upload', binstar_upload),
                  Setting('force_upload', cc_conda_build.get('force_upload', True)),
                  Setting('channel_urls', []),
                  Setting('dirty', False),
                  Setting('include_recipe', True),
                  Setting('no_download_source', False),
                  Setting('override_channels', False),
                  Setting('skip_existing', False),
                  Setting('token', None),
                  Setting('user', None),
                  Setting('verbose', False),
                  Setting('debug', False),
                  Setting('timeout', 90),
                  Setting('arch', subdir.split('-')[-1]),
                  Setting('platform', cc_platform),
                  Setting('set_build_id', True),
                  Setting('disable_pip', False),
                  Setting('output_folder', None),
                  Setting('prefix_length_fallback', True),
                  Setting('_prefix_length', DEFAULT_PREFIX_LENGTH),
                  Setting('long_test_prefix', False),
                  Setting('locking', True),
                  Setting('max_env_retry', 3),
                  Setting('remove_work_dir', True),

                  # pypi upload settings (twine)
                  Setting('password', None),
                  Setting('sign', False),
                  Setting('sign_with', 'gpg'),
                  Setting('identity', None),
                  Setting('config_file', None),
                  Setting('repository', 'pypitest'),

                  Setting('ignore_recipe_verify_scripts',
                          cc_conda_build.get('ignore_recipe_verify_scripts', [])),
                  Setting('ignore_package_verify_scripts',
                          cc_conda_build.get('ignore_package_verify_scripts', [])),
                  Setting('run_recipe_verify_scripts',
                          cc_conda_build.get('run_package_verify_scripts', [])),
                  Setting('run_package_verify_scripts',
                          cc_conda_build.get('run_package_verify_scripts', [])),
                  ]

        # handle known values better than unknown (allow defaults)
        for value in values:
            self._set_attribute_from_kwargs(kwargs, value.name, value.default)

        # dangle remaining keyword arguments as attributes on this class
        for name, value in kwargs.items():
            setattr(self, name, value)

    @property
    def noarch(self):
        return self.platform == 'noarch'

    def reset_platform(self):
        self.platform = cc_platform

    @property
    def subdir(self):
        if self.platform == 'noarch':
            return self.platform
        else:
            return "-".join([self.platform, str(self.arch)])

    @subdir.setter
    def subdir(self, value):
        values = value.split('-')
        self.platform = values[0]
        if len(values) > 1:
            self.arch = values[1]

    @property
    def croot(self):
        """This is where source caches and work folders live"""
        if not self._croot:
            _bld_root_env = os.getenv('CONDA_BLD_PATH')
            _bld_root_rc = cc_conda_build.get('root-dir')
            if _bld_root_env:
                self._croot = abspath(expanduser(_bld_root_env))
            elif _bld_root_rc:
                self._croot = abspath(expanduser(_bld_root_rc))
            elif root_writable:
                self._croot = join(root_dir, 'conda-bld')
            else:
                self._croot = abspath(expanduser('~/conda-bld'))
        return self._croot

    @croot.setter
    def croot(self, croot):
        """Set croot - if None is passed, then the default value will be used"""
        self._croot = croot

    @property
    def build_folder(self):
        """This is the core folder for a given build.
        It has the environments and work directories."""
        return os.path.join(self.croot, self.build_id)

    @property
    def PY3K(self):
        return int(bool(self.CONDA_PY >= 30))

    @property
    def use_MSVC2015(self):
        """Returns whether python version is above 3.4

        (3.5 is compiler switch to MSVC 2015)"""
        return bool(self.CONDA_PY >= 35)

    def get_conda_py(self):
        return self.CONDA_PY

    def _get_python(self, prefix):
        if sys.platform == 'win32':
            if os.path.isfile(os.path.join(prefix, 'python_d.exe')):
                res = join(prefix, 'python_d.exe')
            else:
                res = join(prefix, 'python.exe')
        else:
            res = join(prefix, 'bin/python')
        return res

    def _get_perl(self, prefix):
        if sys.platform == 'win32':
            res = join(prefix, 'Library', 'bin', 'perl.exe')
        else:
            res = join(prefix, 'bin/perl')
        return res

    # TODO: This is probably broken on Windows, but no one has a lua package on windows to test.
    def _get_lua(self, prefix):
        binary_name = "luajit" if "2" == self.CONDA_LUA[0] else "lua"
        if sys.platform == 'win32':
            res = join(prefix, '{}.exe'.format(binary_name))
        else:
            res = join(prefix, 'bin/{}'.format(binary_name))
        return res

    def _get_r(self, prefix):
        if sys.platform == 'win32':
            res = join(prefix, 'Scripts', 'R.exe')
        else:
            res = join(prefix, 'bin/R')
        return res

    @property
    def build_id(self):
        """This is a per-build (almost) unique id, consisting of the package being built, and the
        time since the epoch, in ms.  It is appended to build and test prefixes, and used to create
        unique work folders for build and test."""
        return self._build_id

    def compute_build_id(self, package_name, reset=False):
        if not self._build_id or reset:
            assert not os.path.isabs(package_name), ("package name should not be a absolute path, "
                                                     "to preserve croot during path joins")
            build_folders = sorted([build_folder for build_folder in get_build_folders(self.croot)
                                if package_name in build_folder])

            if self.dirty and build_folders:
                # Use the most recent build with matching recipe name
                self._build_id = build_folders[-1]
            else:
                # here we uniquely name folders, so that more than one build can happen concurrently
                #    keep 6 decimal places so that prefix < 80 chars
                build_id = package_name + "_" + str(int(time.time() * 1000))
                # important: this is recomputing prefixes and determines where work folders are.
                self._build_id = build_id

    @build_id.setter
    def build_id(self, _build_id):
        _build_id = _build_id.rstrip("/").rstrip("\\")
        assert not os.path.isabs(_build_id), ("build_id should not be a absolute path, "
                                              "to preserve croot during path joins")
        self._build_id = _build_id

    @property
    def prefix_length(self):
        return self._prefix_length

    @prefix_length.setter
    def prefix_length(self, length):
        self._prefix_length = length

    @property
    def _short_build_prefix(self):
        return join(self.build_folder, '_b_env')

    @property
    def _short_test_prefix(self):
        return join(self.build_folder, '_t_env')

    def _long_prefix(self, base_prefix):
        placeholder_length = self.prefix_length - len(base_prefix)
        placeholder = '_placehold'
        repeats = int(math.ceil(placeholder_length / len(placeholder)) + 1)
        placeholder = (base_prefix + repeats * placeholder)[:self.prefix_length]
        return max(base_prefix, placeholder)

    @property
    def build_prefix(self):
        """The temporary folder where the build environment is created"""
        if on_win:
            return self._short_build_prefix
        return self._long_prefix(self._short_build_prefix)

    @property
    def test_prefix(self):
        """The temporary folder where the test environment is created"""
        if on_win or not self.long_test_prefix:
            return self._short_test_prefix
        return self._long_prefix(self._short_test_prefix)

    @property
    def build_python(self):
        return self.python_bin(self.build_prefix)

    @property
    def test_python(self):
        return self.python_bin(self.test_prefix)

    def python_bin(self, prefix):
        return self._get_python(prefix)

    def perl_bin(self, prefix):
        return self._get_perl(prefix)

    def lua_bin(self, prefix):
        return self._get_lua(prefix)

    def r_bin(self, prefix):
        return self._get_r(prefix)

    @property
    def info_dir(self):
        """Path to the info dir in the build prefix, where recipe metadata is stored"""
        path = join(self.build_prefix, 'info')
        _ensure_dir(path)
        return path

    @property
    def meta_dir(self):
        """Path to the conda-meta dir in the build prefix, where package index json files are
        stored"""
        path = join(self.build_prefix, 'conda-meta')
        _ensure_dir(path)
        return path

    @property
    def broken_dir(self):
        """Where packages that fail the test phase are placed"""
        path = join(self.croot, "broken")
        _ensure_dir(path)
        return path

    @property
    def bldpkgs_dir(self):
        """ Dir where the package is saved. """
        if self.noarch:
            path = join(self.croot, "noarch")
        else:
            path = join(self.croot, self.subdir)
        _ensure_dir(path)
        return path

    @property
    def bldpkgs_dirs(self):
        """ Dirs where previous build packages might be. """
        return join(self.croot, self.subdir), join(self.croot, "noarch")

    @property
    def src_cache(self):
        """Where tarballs and zip files are downloaded and stored"""
        path = join(self.croot, 'src_cache')
        _ensure_dir(path)
        return path

    @property
    def git_cache(self):
        """Where local clones of git sources are stored"""
        path = join(self.croot, 'git_cache')
        _ensure_dir(path)
        return path

    @property
    def hg_cache(self):
        """Where local clones of hg sources are stored"""
        path = join(self.croot, 'hg_cache')
        _ensure_dir(path)
        return path

    @property
    def svn_cache(self):
        """Where local checkouts of svn sources are stored"""
        path = join(self.croot, 'svn_cache')
        _ensure_dir(path)
        return path

    @property
    def work_dir(self):
        """Where the source for the build is extracted/copied to.  If only a single folder is in
        that folder, this function returns that level one deeper."""
        path = join(self.build_folder, 'work')
        _ensure_dir(path)
        if os.path.isdir(path):
            lst = [fn for fn in os.listdir(path) if not fn.startswith('.')]
            if len(lst) == 1:
                dir_path = join(path, lst[0])
                if os.path.isdir(dir_path):
                    return dir_path
        return path

    @property
    def test_dir(self):
        """The temporary folder where test files are copied to, and where tests start execution"""
        path = join(self.build_folder, 'test_tmp')
        _ensure_dir(path)
        return path

    def clean(self, remove_folders=True):
        # build folder is the whole burrito containing envs and source folders
        #   It will only exist if we download source, or create a build or test environment
        if remove_folders and not getattr(self, 'dirty'):
            if self.build_id:
                if os.path.isdir(self.build_folder):
                    rm_rf(self.build_folder)
            else:
                for path in [self.work_dir, self.test_dir, self.build_prefix, self.test_prefix]:
                    if os.path.isdir(path):
                        rm_rf(path)
            if os.path.isfile(os.path.join(self.build_folder, 'prefix_files')):
                rm_rf(os.path.join(self.build_folder, 'prefix_files'))
        else:
            print("\nLeaving build/test directories:"
                  "\n  Work:\t", os.path.relpath(self.work_dir),
                  "\n  Test:\t", os.path.relpath(self.test_dir),
                  "\nLeaving build/test environments:"
                  "\n  Test:\tsource activate ", os.path.relpath(self.test_prefix),
                  "\n  Build:\tsource activate ",os.path.relpath(self.build_prefix),
                  "\n\n")

        for lock in get_conda_operation_locks(self):
            rm_rf(lock.lock_file)

    def clean_pkgs(self):
        for folder in self.bldpkgs_dirs:
            rm_rf(folder)

    # context management - automatic cleanup if self.dirty or self.keep_old_work is not True
    def __enter__(self):
        pass

    def __exit__(self, e_type, e_value, traceback):
        if not getattr(self, 'dirty') and e_type is None:
            if not getattr(self, 'keep_old_work'):
                logging.getLogger(__name__).info("--dirty flag and --keep-old-work not specified."
                                                 "Removing build/test folder after successful build/test.\n")
                self.clean()
            else:
                self.clean(remove_folders=False)

    def copy(self):
        new = copy.copy(self)
        return new


def get_or_merge_config(config, **kwargs):
    if not config:
        config = Config()
    if kwargs:
        config.set_keys(**kwargs)
    return config


def show(config):
    print('CONDA_PY:', config.CONDA_PY)
    print('CONDA_NPY:', config.CONDA_NPY)
    print('subdir:', config.subdir)
    print('croot:', config.croot)
    print('build packages directory:', config.bldpkgs_dir)


# legacy exports for conda
croot = Config().croot


if __name__ == '__main__':
    show(Config())
