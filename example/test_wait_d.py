import time

import pytest


@pytest.mark.parametrize("n", range(6))
def test_wait(n):
    time.sleep(1)
