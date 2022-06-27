"""
This script will install Qdbg and its dependencies
in isolation from the rest of the system.

It does, in order:

  - Downloads the latest stable (or pre-release) version of qdbg.
  - Downloads all its dependencies in the qdbg/_vendor directory.
  - Copies it and all extra files in $QDBG_HOME.
  - Updates the PATH in a system-specific way.

There will be a `qdbg` script that will be installed in $QDBG_HOME/bin
which will act as the qdbg command but is slightly different in the sense
that it will use the current Python installation.

What this means is that one qdbg installation can serve for multiple
Python versions.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

from contextlib import closing
from contextlib import contextmanager
from functools import cmp_to_key
from gzip import GzipFile
from io import UnsupportedOperation
from io import open


try:
    from urllib.error import HTTPError
    from urllib.request import Request
    from urllib.request import urlopen
except ImportError:
    from urllib2 import HTTPError
    from urllib2 import Request
    from urllib2 import urlopen

try:
    input = raw_input
except NameError:
    pass

try:
    u = unicode
except NameError:
    u = str

SHELL = os.getenv("SHELL", "")

if sys.platform.startswith("win") or (sys.platform == "cli" and os.name == "nt"):
    raise NameError("qdbg is not supported on Windows")

FOREGROUND_COLORS = {
    "black": 30,
    "red": 31,
    "green": 32,
    "yellow": 33,
    "blue": 34,
    "magenta": 35,
    "cyan": 36,
    "white": 37,
}

BACKGROUND_COLORS = {
    "black": 40,
    "red": 41,
    "green": 42,
    "yellow": 43,
    "blue": 44,
    "magenta": 45,
    "cyan": 46,
    "white": 47,
}

OPTIONS = {"bold": 1, "underscore": 4, "blink": 5, "reverse": 7, "conceal": 8}


def style(fg, bg, options):
    codes = []

    if fg:
        codes.append(FOREGROUND_COLORS[fg])

    if bg:
        codes.append(BACKGROUND_COLORS[bg])

    if options:
        if not isinstance(options, (list, tuple)):
            options = [options]

        for option in options:
            codes.append(OPTIONS[option])

    return "\033[{}m".format(";".join(map(str, codes)))


STYLES = {
    "info": style("green", None, None),
    "comment": style("yellow", None, None),
    "error": style("red", None, None),
    "warning": style("yellow", None, None),
}


def is_decorated():
    if not hasattr(sys.stdout, "fileno"):
        return False

    try:
        return os.isatty(sys.stdout.fileno())
    except UnsupportedOperation:
        return False


def is_interactive():
    if not hasattr(sys.stdin, "fileno"):
        return False

    try:
        return os.isatty(sys.stdin.fileno())
    except UnsupportedOperation:
        return False


def colorize(style, text):
    if not is_decorated():
        return text

    return "{}{}\033[0m".format(STYLES[style], text)


@contextmanager
def temporary_directory(*args, **kwargs):
    try:
        from tempfile import TemporaryDirectory
    except ImportError:
        name = tempfile.mkdtemp(*args, **kwargs)

        yield name

        shutil.rmtree(name)
    else:
        with TemporaryDirectory(*args, **kwargs) as name:
            yield name


def string_to_bool(value):
    value = value.lower()

    return value in {"true", "1", "y", "yes"}


def expanduser(path):
    """
    Expand ~ and ~user constructions.

    Includes a workaround for http://bugs.python.org/issue14768
    """
    expanded = os.path.expanduser(path)
    if path.startswith("~/") and expanded.startswith("//"):
        expanded = expanded[1:]

    return expanded


HOME = expanduser("~")
QDBG_HOME = os.environ.get("QDBG_HOME") or os.path.join(HOME, ".qdbg")
QDBG_BIN = os.path.join(QDBG_HOME, "bin")
QDBG_ENV = os.path.join(QDBG_HOME, "env")
QDBG_LIB = os.path.join(QDBG_HOME, "lib")
QDBG_LIB_BACKUP = os.path.join(QDBG_HOME, "lib-backup")


BIN = """# -*- coding: utf-8 -*-
import glob
import logging
import sys
import os

lib = os.path.normpath(os.path.join(os.path.realpath(__file__), "../..", "lib"))
vendors = os.path.join(lib, "qdbg", "_vendor")
current_vendors = os.path.join(
    vendors, "py{}".format(".".join(str(v) for v in sys.version_info[:2]))
)

sys.path.insert(0, lib)
sys.path.insert(0, current_vendors)

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARN)
    from qdbg import main
    from qdbg import QdbgError

    if len(sys.argv) < 2:
        logging.error('Qdbg requires a command')
        raise QdbgError

    main(sys.argv[1:])
"""


PRE_MESSAGE = """# Welcome to {qdbg}!

This will download and install the latest version of {qdbg},
a dependency and package manager for Python.

It will add the `qdbg` command to {qdbg}'s bin directory, located at:

{qdbg_home_bin}

{platform_msg}

You can uninstall at any time by executing this script with the --uninstall option,
and these changes will be reverted.
"""

PRE_UNINSTALL_MESSAGE = """# We are sorry to see you go!

This will uninstall {qdbg}.

It will remove the `qdbg` command from {qdbg}'s bin directory, located at:

{qdbg_home_bin}

This will also remove {qdbg} from your system's PATH.
"""


PRE_MESSAGE_UNIX = """This path will then be added to your `PATH` environment variable by
modifying the profile file{plural} located at:

{rcfiles}"""


PRE_MESSAGE_FISH = """This path will then be added to your `PATH` environment variable by
modifying the `fish_user_paths` universal variable."""

PRE_MESSAGE_NO_MODIFY_PATH = """This path needs to be in your `PATH` environment variable,
but will not be added automatically."""

POST_MESSAGE_UNIX = """{qdbg} ({version}) is installed now. Great!

To get started you need {qdbg}'s bin directory ({qdbg_home_bin}) in your `PATH`
environment variable. Next time you log in this will be done
automatically.

To configure your current shell run `source {qdbg_home_env}`
"""

POST_MESSAGE_FISH = """{qdbg} ({version}) is installed now. Great!

{qdbg}'s bin directory ({qdbg_home_bin}) has been added to your `PATH`
environment variable by modifying the `fish_user_paths` universal variable.
"""

POST_MESSAGE_UNIX_NO_MODIFY_PATH = """{qdbg} ({version}) is installed now. Great!

To get started you need {qdbg}'s bin directory ({qdbg_home_bin}) in your `PATH`
environment variable.

To configure your current shell run `source {qdbg_home_env}`
"""

POST_MESSAGE_FISH_NO_MODIFY_PATH = """{qdbg} ({version}) is installed now. Great!

To get started you need {qdbg}'s bin directory ({qdbg_home_bin})
in your `PATH` environment variable, which you can add by running
the following command:

    set -U fish_user_paths {qdbg_home_bin} $fish_user_paths
"""


class Installer:

    CURRENT_PYTHON = sys.executable
    CURRENT_PYTHON_VERSION = sys.version_info[:2]
    METADATA_URL = "https://pypi.org/pypi/qdbg/json"
    VERSION_REGEX = re.compile(
        r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?"
        "("
        "[._-]?"
        r"(?:(stable|beta|b|rc|RC|alpha|a|patch|pl|p)((?:[.-]?\d+)*)?)?"
        "([.-]?dev)?"
        ")?"
        r"(?:\+[^\s]+)?"
    )

    REPOSITORY_URL = "https://github.com/hermgerm29/qdbg"
    BASE_URL = REPOSITORY_URL + "/archive/refs/tags/"
    FALLBACK_BASE_URL = "https://github.com/hermgerm29/qdbg/releases/download/"

    def __init__(
        self,
        version=None,
        preview=False,
        force=False,
        modify_path=True,
        accept_all=False,
        file=None,
        base_url=BASE_URL,
    ):
        self._version = version
        self._preview = preview
        self._force = force
        self._modify_path = modify_path
        self._accept_all = accept_all
        self._offline_file = file
        self._base_url = base_url

    def allows_prereleases(self):
        return self._preview

    def run(self):
        version, current_version = self.get_version()

        if version is None:
            return 0

        self.customize_install()
        self.display_pre_message()
        self.ensure_home()

        try:
            self.install(
                version, upgrade=current_version is not None, file=self._offline_file
            )
        except subprocess.CalledProcessError as e:
            print(colorize("error", "An error has occurred: {}".format(str(e))))
            print(e.output.decode())

            return e.returncode

        self.display_post_message(version)

        return 0

    def uninstall(self):
        self.display_pre_uninstall_message()

        if not self.customize_uninstall():
            return

        self.remove_home()
        self.remove_from_path()

    def get_version(self):
        current_version = None
        if os.path.exists(QDBG_LIB):
            with open(
                os.path.join(QDBG_LIB, "qdbg", "__version__.py"), encoding="utf-8"
            ) as f:
                version_content = f.read()

            current_version_re = re.match(
                '(?ms).*__version__ = "(.+)".*', version_content
            )
            if not current_version_re:
                print(
                    colorize(
                        "warning",
                        "Unable to get the current qdbg version. Assuming None",
                    )
                )
            else:
                current_version = current_version_re.group(1)

        # Skip retrieving online release versions if install file is specified
        if self._offline_file is not None:
            if current_version is not None and not self._force:
                print("There is a version of qdbg already installed.")
                return None, current_version

            return "from an offline file", current_version

        print(colorize("info", "Retrieving qdbg metadata"))

        metadata = json.loads(self._get(self.METADATA_URL).decode())

        def _compare_versions(x, y):
            mx = self.VERSION_REGEX.match(x)
            my = self.VERSION_REGEX.match(y)

            vx = tuple(int(p) for p in mx.groups()[:3]) + (mx.group(5),)
            vy = tuple(int(p) for p in my.groups()[:3]) + (my.group(5),)

            if vx < vy:
                return -1
            elif vx > vy:
                return 1

            return 0

        print("")
        releases = sorted(
            metadata["releases"].keys(), key=cmp_to_key(_compare_versions)
        )

        if self._version and self._version not in releases:
            print(colorize("error", "Version {} does not exist.".format(self._version)))

            return None, None

        version = self._version
        if not version:
            for release in reversed(releases):
                m = self.VERSION_REGEX.match(release)
                if m.group(5) and not self.allows_prereleases():
                    continue

                version = release
                break

        current_version = None

        if os.path.exists(QDBG_LIB):
            with open(
                os.path.join(QDBG_LIB, "qdbg", "__version__.py"), encoding="utf-8"
            ) as f:
                version_content = f.read()

            current_version_re = re.match(
                '(?ms).*__version__ = "(.+)".*', version_content
            )
            if not current_version_re:
                print(
                    colorize(
                        "warning",
                        "Unable to get the current qdbg version. Assuming None",
                    )
                )
            else:
                current_version = current_version_re.group(1)

        if current_version == version and not self._force:
            print("Latest version already installed.")
            return None, current_version

        return version, current_version

    def customize_install(self):
        if not self._accept_all:
            print("Before we start, please answer the following questions.")
            print("You may simply press the Enter key to leave unchanged.")

            modify_path = input("Modify PATH variable? ([y]/n) ") or "y"
            if modify_path.lower() in {"n", "no"}:
                self._modify_path = False

            print("")

    def customize_uninstall(self):
        if not self._accept_all:
            print()

            uninstall = (
                input("Are you sure you want to uninstall qdbg? (y/[n]) ") or "n"
            )
            if uninstall.lower() not in {"y", "yes"}:
                return False

            print("")

        return True

    def ensure_home(self):
        """
        Ensures that $QDBG_HOME exists or create it.
        """
        if not os.path.exists(QDBG_HOME):
            os.mkdir(QDBG_HOME, 0o755)

    def remove_home(self):
        """
        Removes $QDBG_HOME.
        """
        if not os.path.exists(QDBG_HOME):
            return

        shutil.rmtree(QDBG_HOME)

    def install(self, version, upgrade=False, file=None):
        """
        Installs qdbg in $QDBG_HOME.
        """
        if file is not None:
            print("Attempting to install from file: " + colorize("info", file))
        else:
            print("Installing version: " + colorize("info", version))

        self.make_lib(version)
        self.make_bin()
        self.make_env()
        self.update_path()

        return 0

    def make_lib(self, version):
        """
        Packs everything into a single lib/ directory.
        """
        if os.path.exists(QDBG_LIB_BACKUP):
            shutil.rmtree(QDBG_LIB_BACKUP)

        # Backup the current installation
        if os.path.exists(QDBG_LIB):
            shutil.copytree(QDBG_LIB, QDBG_LIB_BACKUP)
            shutil.rmtree(QDBG_LIB)

        try:
            self._make_lib(version)
            shutil.move(glob.glob(f"{QDBG_LIB}/*/qdbg")[-1], QDBG_LIB)
            shutil.rmtree(glob.glob(f"{QDBG_LIB}/qdbg-*")[-1])

        except Exception:
            if not os.path.exists(QDBG_LIB_BACKUP):
                raise

            shutil.copytree(QDBG_LIB_BACKUP, QDBG_LIB)
            shutil.rmtree(QDBG_LIB_BACKUP)

            raise
        finally:
            if os.path.exists(QDBG_LIB_BACKUP):
                shutil.rmtree(QDBG_LIB_BACKUP)

    def _make_lib(self, version):
        # Check if an offline installer file has been specified
        if self._offline_file is not None:
            try:
                self.extract_lib(self._offline_file)
                return
            except Exception:
                raise RuntimeError("Could not install from offline file.")

        url = os.path.join(self._base_url + f"v{version}.tar.gz")

        try:
            r = urlopen(url)
        except HTTPError as e:
            if e.code == 404:
                raise RuntimeError("Could not find file at {}".format(url))
            raise

        current = 0
        block_size = 8192

        sha = hashlib.sha256()
        with temporary_directory(prefix="qdbg-installer-") as dir_:
            tar = os.path.join(dir_, f"v{version}.tar.gz")
            with open(tar, "wb") as f:
                while True:
                    buffer = r.read(block_size)
                    if not buffer:
                        break

                    current += len(buffer)
                    f.write(buffer)
                    sha.update(buffer)

            self.extract_lib(tar)

    def extract_lib(self, filename):
        gz = GzipFile(filename, mode="rb")
        try:
            with tarfile.TarFile(filename, fileobj=gz, format=tarfile.PAX_FORMAT) as f:
                f.extractall(QDBG_LIB)
        finally:
            gz.close()

    def _which_python(self):
        """Decides which python executable we'll embed in the launcher script."""
        allowed_executables = ["python3", "python"]

        # \d in regex ensures we can convert to int later
        version_matcher = re.compile(r"^Python (?P<major>\d+)\.(?P<minor>\d+)\..+$")
        fallback = None
        for executable in allowed_executables:
            try:
                raw_version = subprocess.check_output(
                    executable + " --version", stderr=subprocess.STDOUT, shell=True
                ).decode("utf-8")
            except subprocess.CalledProcessError:
                continue

            match = version_matcher.match(raw_version.strip())
            if match:
                return executable

            if fallback is None:
                # keep this one as the fallback; it was the first valid executable we
                # found.
                fallback = executable

        if fallback is None:
            raise RuntimeError(
                "No python executable found in shell environment. Tried: "
                + str(allowed_executables)
            )

        return fallback

    def make_bin(self):
        if not os.path.exists(QDBG_BIN):
            os.mkdir(QDBG_BIN, 0o755)

        python_executable = self._which_python()

        with open(os.path.join(QDBG_BIN, "qdbg"), "w", encoding="utf-8") as f:
            f.write(u("#!/usr/bin/env {}\n".format(python_executable)))
            f.write(u(BIN))

        # Making the file executable
        st = os.stat(os.path.join(QDBG_BIN, "qdbg"))
        os.chmod(os.path.join(QDBG_BIN, "qdbg"), st.st_mode | stat.S_IEXEC)

    def make_env(self):
        with open(os.path.join(QDBG_HOME, "env"), "w") as f:
            f.write(u(self.get_export_string()))

    def update_path(self):
        """
        Tries to update the $PATH automatically.
        """
        if not self._modify_path:
            return

        if "fish" in SHELL:
            return self.add_to_fish_path()

        # Updating any profile we can on UNIX systems
        export_string = self.get_export_string()

        addition = "\n{}\n".format(export_string)

        profiles = self.get_unix_profiles()
        for profile in profiles:
            if not os.path.exists(profile):
                continue

            with open(profile, "r") as f:
                content = f.read()

            if addition not in content:
                with open(profile, "a") as f:
                    f.write(u(addition))

    def add_to_fish_path(self):
        """
        Ensure QDBG_BIN directory is on Fish shell $PATH
        """
        current_path = os.environ.get("PATH", None)
        if current_path is None:
            print(
                colorize(
                    "warning",
                    "\nUnable to get the PATH value. It will not be updated"
                    " automatically.",
                )
            )
            self._modify_path = False

            return

        if QDBG_BIN not in current_path:
            fish_user_paths = subprocess.check_output(
                ["fish", "-c", "echo $fish_user_paths"]
            ).decode("utf-8")
            if QDBG_BIN not in fish_user_paths:
                cmd = "set -U fish_user_paths {} $fish_user_paths".format(QDBG_BIN)
                set_fish_user_path = ["fish", "-c", "{}".format(cmd)]
                subprocess.check_output(set_fish_user_path)
        else:
            print(
                colorize(
                    "warning",
                    "\nPATH already contains {} and thus was not modified.".format(
                        QDBG_BIN
                    ),
                )
            )

    def remove_from_path(self):
        if "fish" in SHELL:
            return self.remove_from_fish_path()

        return self.remove_from_unix_path()

    def remove_from_fish_path(self):
        fish_user_paths = subprocess.check_output(
            ["fish", "-c", "echo $fish_user_paths"]
        ).decode("utf-8")
        if QDBG_BIN in fish_user_paths:
            cmd = "set -U fish_user_paths (string match -v {} $fish_user_paths)".format(
                QDBG_BIN
            )
            set_fish_user_path = ["fish", "-c", "{}".format(cmd)]
            subprocess.check_output(set_fish_user_path)

    def remove_from_unix_path(self):
        # Updating any profile we can on UNIX systems
        export_string = self.get_export_string()

        addition = "{}\n".format(export_string)

        profiles = self.get_unix_profiles()
        for profile in profiles:
            if not os.path.exists(profile):
                continue

            with open(profile, "r") as f:
                content = f.readlines()

            if addition not in content:
                continue

            new_content = []
            for line in content:
                if line == addition:
                    if new_content and not new_content[-1].strip():
                        new_content = new_content[:-1]

                    continue

                new_content.append(line)

            with open(profile, "w") as f:
                f.writelines(new_content)

    def get_export_string(self):
        path = QDBG_BIN.replace(os.getenv("HOME", ""), "$HOME")
        export_string = 'export PATH="{}:$PATH"'.format(path)

        return export_string

    def get_unix_profiles(self):
        profiles = [os.path.join(HOME, ".profile")]

        if "zsh" in SHELL:
            zdotdir = os.getenv("ZDOTDIR", HOME)
            profiles.append(os.path.join(zdotdir, ".zshrc"))

        bash_profile = os.path.join(HOME, ".bash_profile")
        if os.path.exists(bash_profile):
            profiles.append(bash_profile)

        return profiles

    def display_pre_message(self):
        home = QDBG_BIN.replace(os.getenv("HOME", ""), "$HOME")

        kwargs = {
            "qdbg": colorize("info", "qdbg"),
            "qdbg_home_bin": colorize("comment", home),
        }

        if not self._modify_path:
            kwargs["platform_msg"] = PRE_MESSAGE_NO_MODIFY_PATH
        else:
            if "fish" in SHELL:
                kwargs["platform_msg"] = PRE_MESSAGE_FISH
            else:
                profiles = [
                    colorize("comment", p.replace(os.getenv("HOME", ""), "$HOME"))
                    for p in self.get_unix_profiles()
                ]
                kwargs["platform_msg"] = PRE_MESSAGE_UNIX.format(
                    rcfiles="\n".join(profiles), plural="s" if len(profiles) > 1 else ""
                )

        print(PRE_MESSAGE.format(**kwargs))

    def display_pre_uninstall_message(self):
        home_bin = QDBG_BIN
        home_bin = home_bin.replace(os.getenv("HOME", ""), "$HOME")

        kwargs = {
            "qdbg": colorize("info", "qdbg"),
            "qdbg_home_bin": colorize("comment", home_bin),
        }

        print(PRE_UNINSTALL_MESSAGE.format(**kwargs))

    def display_post_message(self, version):
        print("")

        kwargs = {
            "qdbg": colorize("info", "qdbg"),
            "version": colorize("comment", version),
        }

        if "fish" in SHELL:
            message = POST_MESSAGE_FISH
            if not self._modify_path:
                message = POST_MESSAGE_FISH_NO_MODIFY_PATH

            qdbg_home_bin = QDBG_BIN.replace(os.getenv("HOME", ""), "$HOME")
        else:
            message = POST_MESSAGE_UNIX
            if not self._modify_path:
                message = POST_MESSAGE_UNIX_NO_MODIFY_PATH

            qdbg_home_bin = QDBG_BIN.replace(os.getenv("HOME", ""), "$HOME")
            kwargs["qdbg_home_env"] = colorize(
                "comment", QDBG_ENV.replace(os.getenv("HOME", ""), "$HOME")
            )

        kwargs["qdbg_home_bin"] = colorize("comment", qdbg_home_bin)

        print(message.format(**kwargs))

    def call(self, *args):
        return subprocess.check_output(args, stderr=subprocess.STDOUT)

    def _get(self, url):
        request = Request(url, headers={"User-Agent": "Python qdbg"})

        with closing(urlopen(request)) as r:
            return r.read()


def main():
    parser = argparse.ArgumentParser(
        description="Installs the latest (or given) version of qdbg"
    )
    parser.add_argument(
        "-p",
        "--preview",
        help="install preview version",
        dest="preview",
        action="store_true",
        default=False,
    )
    parser.add_argument("--version", help="install named version", dest="version")
    parser.add_argument(
        "-f",
        "--force",
        help="install on top of existing version",
        dest="force",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--no-modify-path",
        help="do not modify $PATH",
        dest="no_modify_path",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "-y",
        "--yes",
        help="accept all prompts",
        dest="accept_all",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--uninstall",
        help="uninstall qdbg",
        dest="uninstall",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--file",
        dest="file",
        action="store",
        help=(
            "Install from a local file instead of fetching the latest version "
            "of qdbg available online."
        ),
    )

    args = parser.parse_args()

    base_url = Installer.BASE_URL

    if args.file is None:
        try:
            urlopen(Installer.REPOSITORY_URL)
        except HTTPError as e:
            if e.code == 404:
                base_url = Installer.FALLBACK_BASE_URL
            else:
                raise

    installer = Installer(
        version=args.version or os.getenv("QDBG_VERSION"),
        preview=args.preview or string_to_bool(os.getenv("QDBG_PREVIEW", "0")),
        force=args.force,
        modify_path=not args.no_modify_path,
        accept_all=args.accept_all
        or string_to_bool(os.getenv("QDBG_ACCEPT", "0"))
        or not is_interactive(),
        file=args.file,
        base_url=base_url,
    )

    if args.uninstall or string_to_bool(os.getenv("QDBG_UNINSTALL", "0")):
        return installer.uninstall()

    return installer.run()


if __name__ == "__main__":
    sys.exit(main())
