from typing import Any, Dict

from modal_utils.async_utils import synchronizer

from ._factory import make_shared_object_factory_class
from .config import logger


class ObjectMeta(type):
    prefix_to_type: Dict[str, Any] = {}

    def __new__(metacls, name, bases, dct, type_prefix=None):
        new_cls = type.__new__(metacls, name, bases, dct)

        # If this is a synchronized wrapper, just return early
        # (actually, it shouldn't be possible)
        assert not synchronizer.is_synchronized(new_cls)

        # Needed for serialization, also for loading objects dynamically
        if type_prefix is not None:
            new_cls._type_prefix = type_prefix  # type: ignore
            metacls.prefix_to_type[type_prefix] = new_cls

        # Create factory class and shared object class
        if type_prefix is not None:
            new_cls._shared_object_factory_class = make_shared_object_factory_class(new_cls)  # type: ignore

        logger.debug(f"Created Object class {name}")
        return new_cls
