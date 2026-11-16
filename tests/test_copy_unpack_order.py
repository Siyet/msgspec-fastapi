"""Failed state unpacking preserves cleanup order and versioned errors."""

import copy
import copyreg
import dataclasses
import sys
from typing import Any

import pytest

from msgspec_fastapi import defaults


@pytest.mark.parametrize('exact_pair', [False, True])
def test_failed_state_pair_unpack_matches_stdlib(exact_pair: bool) -> None:
    def run(copier: Any) -> Any:
        events = []
        state = {'value': 7}
        old = copy._deepcopy_dispatch[dict]

        class Extra:
            def __del__(self) -> None:
                events.append('extra')

        class Iterator:
            def __init__(self) -> None:
                self.index = 0

            def __iter__(self) -> Any:
                return self

            def __next__(self) -> Any:
                self.index += 1
                if self.index == 1:
                    return 'key'
                if self.index == 2:
                    return 1
                if self.index == 3:
                    return Extra()
                raise StopIteration

            def __del__(self) -> None:
                events.append('iterator')

        class Pair:
            def __iter__(self) -> Any:
                return Iterator()

        class Slots:
            def items(self) -> Any:
                return [['key', 1, 2]] if exact_pair else [Pair()]

        def state_copy(value: Any, memo: Any) -> Any:
            return (None, Slots()) if value is state else old(value, memo)

        class Product:
            pass

        @dataclasses.dataclass
        class Original:
            value: int

        def construct() -> Any:
            copyreg.dispatch_table.pop(Original, None)
            copy._deepcopy_dispatch[dict] = state_copy
            return Product()

        def reduced(value: Any) -> Any:
            return construct, (), state

        @dataclasses.dataclass
        class Setter:
            value: int

            def __deepcopy__(self, memo: Any) -> Any:
                copyreg.dispatch_table[Original] = reduced
                return self

        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(defaults, 'deepcopy', copier)
                with pytest.raises(ValueError) as error:
                    defaults.copy_metadata([Setter(0), Original(7)])
                return str(error.value), events
        finally:
            copy._deepcopy_dispatch[dict] = old
            copyreg.dispatch_table.pop(Original, None)

    message = (
        'too many values to unpack (expected 2, got 3)'
        if exact_pair and sys.version_info >= (3, 14)
        else 'too many values to unpack (expected 2)'
    )
    expected = message, [] if exact_pair else ['extra', 'iterator']
    assert run(copy.deepcopy) == expected
    assert run(defaults.deepcopy) == expected
