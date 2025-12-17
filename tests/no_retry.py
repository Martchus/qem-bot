# Copyright SUSE LLC
# SPDX-License-Identifier: MIT

import pytest


@pytest.fixture(scope="session")
def no_retry():
    return "foobar"
