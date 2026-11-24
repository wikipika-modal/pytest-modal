import socket

import pytest


def test_needs_the_image():
    import numpy

    assert numpy.arange(4).sum() == 6


def test_fails_with_output():
    print("running on", socket.gethostname())
    assert sorted([3, 1, 2]) == [1, 2, 4]


def test_skipped():
    pytest.skip("nothing to do")


@pytest.mark.xfail
def test_expected_failure():
    assert False
