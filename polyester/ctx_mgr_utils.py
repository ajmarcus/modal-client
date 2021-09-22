import asyncio
import atexit


class CtxMgrMeta(type):
    def __new__(metacls, name, bases, dct):
        dct = dct | dict(_running_instances=set())
        cls = type.__new__(metacls, name, bases, dct)
        atexit.register(cls._stop_running_instances)
        return cls


class CtxMgr(metaclass=CtxMgrMeta):
    """Make it possible to use an object as a context manager, but also as a Singleton

    Designed as a mixin for now.

    The plan is for clients and sessions to use this."""

    @classmethod
    def _create(cls):
        raise NotImplementedError(f"{cls}._create() not implemented")

    async def _start(self):
        raise NotImplementedError(f"{type(self)}._start() not implemented")

    async def _stop(self, hard):
        raise NotImplementedError(f"{type(self)}._stop() not implemented")

    async def __aenter__(self):
        await self._start()
        self._running_instances.add(self)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._stop(hard=False)
        self._running_instances.remove(self)

    @classmethod
    async def current(cls):
        if len(cls._running_instances) == 0:
            instance = cls._create()
            await instance._start()
            cls._running_instances.add(instance)
        elif len(cls._running_instances) > 1:
            raise Exception(f"Multiple instances of {cls} running: need to be explicit about which one to use")
        (instance,) = cls._running_instances
        return instance

    @classmethod
    def _stop_running_instances(cls):
        for instance in cls._running_instances:
            asyncio.run(instance._stop(hard=True))
