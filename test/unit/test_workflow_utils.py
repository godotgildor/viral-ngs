"""Unit tests for workflow_utils.py"""

__author__ = "ilya@broadinstitute.org"

import os
import sys
import collections
import argparse
import logging
import itertools
import platform
import unittest

import pytest

if not platform.python_version().startswith('2.7'):
    pytest.skip("skipping py27-only tests for workflow_utils", allow_module_level=True)

import util.cmd
import util.file
import util.misc
import workflow_utils
import tools.git_annex

_log = logging.getLogger(__name__)  # pylint: disable=invalid-name

class TestCommandHelp(unittest.TestCase):

    def test_help_parser_for_each_command(self):
        for cmd_name, parser_fun in workflow_utils.__commands__:
            _log.info('looking at commmand %s', cmd_name)
            parser = parser_fun(argparse.ArgumentParser())
            assert parser, 'No parser for command {}'.format(cmd_name)
            helpstring = parser.format_help()

def test_git_annex_basic():
    ga = tools.git_annex.GitAnnexTool()
    ga.execute(['version'])

def test_git_annex_get(tmpdir_function):
    ga = tools.git_annex.GitAnnexTool()
    with util.file.pushd_popd(tmpdir_function):
        dir_remote = os.path.join(tmpdir_function, 'dir_remote')
        util.file.mkdir_p(dir_remote)
        util.file.mkdir_p('ga_repo')
        with util.file.pushd_popd('ga_repo'):
            ga.init_repo()
            file_A = 'testfile.txt'
            util.file.dump_file(file_A, 'some contents')
            ga.add(file_A)
            ga.commit('one file')
            assert os.path.isfile(file_A)
            assert ga._get_link_into_annex(file_A)[0] == file_A
            
            dir_remote_name = 'my_dir_remote'
            ga.initremote(dir_remote_name, 'directory', directory=dir_remote)
            
            ga.move(file_A, to_remote_name=dir_remote_name)
            assert not os.path.isfile(file_A)
            assert ga._get_link_into_annex(file_A)[0] == file_A
            ga.get(file_A)
            assert os.path.isfile(file_A)

            ga.drop(file_A)
            assert not os.path.isfile(file_A)






