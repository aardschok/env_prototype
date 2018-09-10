import logging
import json
import re
import os
import platform
import subprocess
import sys

from . import lib


PLATFORM = platform.system().lower()

logging.basicConfig()
log = logging.getLogger()


class CycleError(ValueError):
    """A cyclic dependency in dynamic environment"""
    pass


class DynamicKeyClashError(ValueError):
    """A dynamic key clash on compute"""
    pass


def build(env,
          dynamic_keys=True,
          allow_cycle=False,
          allow_key_clash=False,
          cleanup=True):
    """Compute the result from recursive dynamic environment.

    Note: Keys that are not present in the data will remain unformatted as the
        original keys. So they can be formatted against the current user
        environment when merging. So {"A": "{key}"} will remain {key} if not
        present in the dynamic environment.

    """
    # TODO: A reference to itself should be "maintained" and not cause cycle
    #       Thus format dynamic values and keys, except those referencing
    #       itself since they are intended to append to current user env
    env = env.copy()

    # Collect dependencies
    dependencies = []
    for key, value in env.items():
        dependent_keys = re.findall("{(.+?)}", value)
        for dependency in dependent_keys:
            # Ignore direct references to itself because
            # we don't format with itself anyway
            if dependency == key:
                continue

            dependencies.append((key, dependency))

    result = lib.topological_sort(dependencies)

    # Check cycle
    if result.cyclic:
        if not allow_cycle:
            raise CycleError("A cycle is detected on: "
                             "{0}".format(result.cyclic))
        log.warning("Cycle detected. Result might "
                    "be unexpected for: %s", result.cyclic)

    # Format dynamic values
    for key in reversed(result.sorted):
        if key in env:
            data = env.copy()
            data.pop(key)    # format without itself
            env[key] = lib.partial_format(env[key], data=data)

    # Format cyclic values
    for key in result.cyclic:
        if key in env:
            data = env.copy()
            data.pop(key)   # format without itself
            env[key] = lib.partial_format(env[key], data=data)

    # Format dynamic keys
    if dynamic_keys:
        formatted = {}
        for key, value in env.items():
            new_key = lib.partial_format(key, data=env)

            if new_key in formatted:
                if not allow_key_clash:
                    raise DynamicKeyClashError("Key clashes on: {0} "
                                               "(source: {1})".format(new_key,
                                                                      key))
                log.warning("Key already in formatted dict: %s", new_key)

            formatted[new_key] = value
        env = formatted

    if cleanup:
        separator = os.pathsep
        for key, value in env.items():
            paths = value.split(separator)

            # Keep unique path entries: {A};{A};{B} -> {A};{B}
            paths = lib.uniqify_ordered(paths)

            # Remove empty values
            paths = [p for p in paths if p.strip()]

            value = separator.join(paths)
            env[key] = value

    return env


def prepare(env, platform_name=None):
    """Parse environment for platform-specific values

    Args:
        env (dict): The source environment to read.
        platform_name (str, Optional): Name of platform to parse for.
            This can be "windows", "darwin" or "linux".
            Defaults to the currently active platform.

    Returns:
        dict: The flattened environment for a platform.

    """

    platform_name = platform_name or PLATFORM

    lookup = {"windows": ["/", "\\"],
              "linux": ["\\", "/"],
              "darwin": ["\\", "/"]}

    translate = lookup.get(platform_name, None)
    if translate is None:
        raise KeyError("Given platform name `%s` is not supported" % platform)

    result = {}
    for variable, value in env.items():

        # Platform specific values
        if isinstance(value, dict):
            value = value.get(platform_name, "")

        if not value:
            continue

        # Allow to have lists as values in the tool data
        if isinstance(value, (list, tuple)):
            value = ";".join(value)

        result[variable] = value

    return result


def join(env, env_b):
    """Append paths of environment b into environment

    Returns:
        env (dict)
    """
    env = env.copy()
    for variable, value in env_b.items():
        for path in value.split(";"):
            if not path:
                continue

            lib.append_path(env, variable, path)

    return env


def discover(tools, platform_name=None):
    """Return combined environment for the given set of tools.

    This will find and merge all the required environment variables of the
    input tools into a single dictionary. Then it will do a recursive format to
    format all dynamic keys and values using the same dictionary. (So that
    tool X can rely on variables of tool Y).

    Examples:
        get_tools(["maya2018", "yeti2.01", "mtoa2018"])
        get_tools(["global", "fusion9", "ofxplugins"])

    Args:
        tools (list): List of tool names.
        platform_name (str, Optional): The name of the platform to retrieve
            for. This defaults to the current platform you're running on.
            Possible values are: "darwin", "linux", "windows"

    Returns:
        dict: The environment required for the tools.

    """

    try:
        env_paths = os.environ['TOOL_ENV'].split(os.pathsep)
    except KeyError:
        raise KeyError(
            '"TOOL_ENV" environment variable not found. '
            'Please create it and point it to a folder with your .json '
            'config files.'
         )

    # Collect the tool files to load
    tool_paths = []
    for env_path in env_paths:
        for tool in tools:
            tool_paths.append(os.path.join(env_path, tool + ".json"))

    environment = dict()
    for tool_path in tool_paths:

        # Load tool environment
        try:
            with open(tool_path, "r") as f:
                tool_env = json.load(f)
            log.debug('Read tool successfully: {}'.format(tool_path))
        except IOError:
            log.error(
                'Unable to find the environment file: "{}"'.format(tool_path)
            )
            continue
        except ValueError as e:
            log.error(
                'Unable to read the environment file: "{0}", due to:'
                '\n{1}'.format(tool_path, e)
            )
            continue

        tool_env = prepare(tool_env, platform_name=platform_name)
        environment = join(environment, tool_env)

    return environment


def merge(env, current_env):
    """Merge the tools environment with the 'current_env'.

    This finalizes the join with a current environment by formatting the
    remainder of dynamic variables with that from the current environment.

    Remaining missing variables result in an empty value.

    Args:
        env (dict): The dynamic environment
        current_env (dict): The "current environment" to merge the dynamic
            environment into.

    Returns:
        dict: The resulting environment after the merge.

    """

    result = current_env.copy()
    for key, value in env.items():
        value = lib.partial_format(value, data=current_env, missing="")
        result[key] = value

    return result


def locate(program, env):
    """Locate `program` in PATH

    Ensure `PATHEXT` is declared in the environment if you want to alter the
    priority of the system extensions:

        Example : ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC"

    Arguments:
        program (str): Name of program, e.g. "python"
        env (dict): an environment dictionary

    """

    def is_exe(fpath):
        if os.path.isfile(fpath) and os.access(fpath, os.X_OK):
            return True
        return False

    paths = env["PATH"].split(os.pathsep)
    extensions = env.get("PATHEXT", os.getenv("PATHEXT", ""))

    for path in paths:
        for ext in extensions.split(os.pathsep):
            fname = program + ext.lower()
            abspath = os.path.join(path.strip('"'), fname)
            if is_exe(abspath):
                return abspath

    return None


def launch(executable, args=None, environment=None, cwd=None):
    """Launch a new subprocess of `args`

    Arguments:
        executable (str): Relative or absolute path to executable
        args (list): Command passed to `subprocess.Popen`
        environment (dict, optional): Custom environment passed
            to Popen instance.
        cwd (str): the current working directory

    Returns:
        Popen instance of newly spawned process

    Exceptions:
        OSError on internal error
        ValueError on `executable` not found

    """

    CREATE_NO_WINDOW = 0x08000000
    CREATE_NEW_CONSOLE = 0x00000010
    IS_WIN32 = sys.platform == "win32"
    PY2 = sys.version_info[0] == 2

    abspath = executable

    env = (environment or os.environ)

    if PY2:
        # Protect against unicode, and other unsupported
        # types amongst environment variables
        enc = sys.getfilesystemencoding()
        env = {k.encode(enc): v.encode(enc) for k, v in env.items()}

    kwargs = dict(
        args=[abspath] + args or list(),
        env=env,
        cwd=cwd,

        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,

        # Output `str` through stdout on Python 2 and 3
        universal_newlines=True,
    )

    if env.get("CREATE_NEW_CONSOLE"):
        kwargs["creationflags"] = CREATE_NEW_CONSOLE
        kwargs.pop("stdout")
        kwargs.pop("stderr")
    else:
        if IS_WIN32:
            kwargs["creationflags"] = CREATE_NO_WINDOW

    popen = subprocess.Popen(**kwargs)

    return popen
