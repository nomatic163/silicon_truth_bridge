import time

import pytest

from stb.cursors import CursorRegistry
from stb.errors import StbError


def test_cursor_state_is_replayable_and_capacity_has_no_eviction() -> None:
    registry = CursorRegistry(ttl_sec=10, maximum=2)
    first = registry.issue({"offset": 1})
    second = registry.issue({"offset": 2})
    assert registry.get(first) == {"offset": 1}
    assert registry.get(first) == {"offset": 1}
    assert registry.get(second) == {"offset": 2}
    with pytest.raises(StbError, match="active cursor limit"):
        registry.issue({"offset": 3})


def test_cursor_expiry_is_explicit() -> None:
    registry = CursorRegistry(ttl_sec=0.001, maximum=2)
    token = registry.issue({"offset": 1})
    time.sleep(0.01)
    with pytest.raises(StbError) as error:
        registry.get(token)
    assert error.value.code == "cursor_expired"
