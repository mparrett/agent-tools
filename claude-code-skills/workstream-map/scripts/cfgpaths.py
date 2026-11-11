"""Path resolution shared by fetch.py and build.py, which must agree on where
data_dir points or the build reads a different directory than the fetch wrote."""

import os


def config_relative(cfg_file, path):
    """Paths written in the config resolve against the config's directory, not the
    shell's cwd: a build run from another checkout otherwise writes into that one."""
    return os.path.join(os.path.dirname(os.path.abspath(cfg_file)), os.path.expanduser(path))
