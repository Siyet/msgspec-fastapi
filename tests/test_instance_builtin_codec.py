import copy
import dataclasses
import sys
import types
from typing import Any

import pytest
from pydantic_core import SchemaValidator, core_schema

from msgspec_fastapi.defaults import copy_body_default, copy_metadata


@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_record_instance_builtin_copier_does_not_invoke_pydantic_codec(operation: Any) -> None:
    @dataclasses.dataclass
    class Record:
        value: int

    validator = SchemaValidator(core_schema.any_schema())
    copier = validator.validate_python
    assert type(copier) is types.BuiltinFunctionType
    original = Record(7)
    original.__deepcopy__ = copier
    calls = []

    def observe(frame: Any, event: str, arg: Any) -> None:
        if event == 'c_call' and getattr(arg, '__self__', None) is validator:
            if getattr(arg, '__name__', None) == 'validate_python':
                calls.append('validate_python')

    previous = sys.getprofile()
    try:
        sys.setprofile(observe)
        copy.deepcopy(original)
        assert calls == ['validate_python']
        calls.clear()
        with pytest.raises(TypeError, match='Instance copy methods'):
            operation(original)
    finally:
        sys.setprofile(previous)
    assert calls == []
