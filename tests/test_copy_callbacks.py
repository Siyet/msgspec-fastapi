import dataclasses
from copy import deepcopy
from typing import Any

import msgspec
import pytest

from msgspec_fastapi.defaults import copy_body_default, copy_metadata


@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_copy_hook_scalar_memo_override_matches_stdlib(operation: Any) -> None:
    @dataclasses.dataclass
    class Record:
        value: int

        def __deepcopy__(self, memo: dict[int, Any]) -> Any:
            memo[id(42)] = 'overridden'
            return Record(self.value)

    original = [Record(0), 42]
    expected = deepcopy(original)
    result = operation(original)
    assert result == expected
    assert original == [Record(0), 42]


@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_cached_post_init_can_add_copy_hook_for_later_items(operation: Any) -> None:
    active = False
    calls = []

    class Record(msgspec.Struct):
        value: int

        def __post_init__(self) -> None:
            if active and self.value == 1:

                def copied(self: Any, memo: dict[int, Any]) -> Any:
                    calls.append('copy')
                    return Record(self.value + 10)

                Record.__deepcopy__ = copied

    original = [Record(1), Record(2)]
    del Record.__post_init__
    active = True
    result = operation(original)
    assert [item.value for item in result] == [1, 12]
    assert [item.value for item in original] == [1, 2]
    assert calls == ['copy']


@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_copy_hook_memo_override_applies_to_later_mutable_value(operation: Any) -> None:
    target = bytearray(b'value')

    @dataclasses.dataclass
    class Record:
        value: int

        def __deepcopy__(self, memo: dict[int, Any]) -> Any:
            memo[id(target)] = 'overridden'
            return Record(self.value)

    original = [Record(0), target]
    expected = deepcopy(original)
    assert expected == [Record(0), 'overridden']
    result = operation(original)
    assert result == expected
    assert original == [Record(0), bytearray(b'value')]
    assert original[1] is target
