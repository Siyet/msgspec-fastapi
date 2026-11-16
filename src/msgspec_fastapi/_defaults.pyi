from collections.abc import Callable
from types import ModuleType
from typing import Any, final

@final
class _Guard:
    def __init__(
        self,
        fallback: Callable[[Any], None],
        registry: dict[type[Any], Any],
        trusted_union_reducer: Any,
        union_type: type[Any],
        function_type: type[Any],
        builtin_function_type: type[Any],
        getset_descriptor_type: type[Any],
        member_descriptor_type: type[Any],
        method_descriptor_type: type[Any],
        wrapper_descriptor_type: type[Any],
        struct_type: type[Any],
        enum_type: type[Any],
        atomic_ids: set[int],
        reference_ids: set[int],
        model_brands: tuple[str, ...],
        native_state_methods: tuple[tuple[str, tuple[Any, ...]], ...],
        state_names: tuple[str, ...],
        enum_namespace: Any,
        missing: Any,
        mro_descriptor: Any,
        dict_descriptor: Any,
        name_descriptor: Any,
    ) -> None: ...
    def check(self, value: Any, /) -> None: ...
    def class_info(self, cls: type[Any], /) -> tuple[str, Any, tuple[Any, ...]]: ...

@final
class _Copier:
    def __init__(
        self,
        copy_module: ModuleType,
        deepcopy: Callable[..., Any],
        list_copier: Callable[..., Any],
        dict_copier: Callable[..., Any],
        tuple_copier: Callable[..., Any],
        atomic_copier: Callable[..., Any] | None,
        reconstruct: Callable[..., Any],
        atomic_first: bool,
        memo_sentinel: object,
    ) -> None: ...
    def __call__(self, value: Any, memo: dict[int, Any] | None = None, /) -> Any: ...
    def deepcopy(self, value: Any, memo: dict[int, Any] | None = None, /) -> Any: ...
