import asyncio
import inspect
import sys
import threading
import traceback
import typing
import uuid

import aiostream
import google.protobuf.json_format

from .async_utils import synchronizer
from .buffer_utils import buffered_rpc_read, buffered_rpc_write
from .client import Client
from .config import logger
from .function import Function, pack_output_buffer_item, unpack_input_buffer_item
from .grpc_utils import BLOCKING_REQUEST_TIMEOUT, GRPC_REQUEST_TIMEOUT
from .object import Object
from .proto import api_pb2


@synchronizer
class FunctionContext:
    """This class isn't much more than a helper method for some gRPC calls."""

    def __init__(self, container_args, client):
        self.task_id = container_args.task_id
        self.function_id = container_args.function_id
        self.input_buffer_id = container_args.input_buffer_id
        self.session_id = container_args.session_id
        self.module_name = container_args.module_name
        self.function_name = container_args.function_name
        self.client = client

    async def get_function(self) -> typing.Callable:
        """Note that this also initializes the session."""
        fun = Function.get_function(self.module_name, self.function_name)
        session = fun.session
        await session.initialize(self.session_id, self.client)
        return fun.get_raw_f()

    async def generate_inputs(
        self,
    ) -> typing.AsyncIterator[api_pb2.BufferReadResponse]:
        request = api_pb2.FunctionGetNextInputRequest(
            function_id=self.function_id,
            task_id=self.task_id,
        )
        while True:
            response = await buffered_rpc_read(
                self.client.stub.FunctionGetNextInput, request, self.input_buffer_id, timeout=GRPC_REQUEST_TIMEOUT
            )

            if response.status == api_pb2.BufferReadResponse.BufferReadStatus.TIMEOUT:
                logger.info(f"Task {self.task_id} input request timed out.")
                break

            yield response.item

            if response.item.EOF:
                logger.info(f"Task {self.task_id} input got EOF.")
                break

    async def output(self, request):
        # No timeout so this can block forever.
        await buffered_rpc_write(self.client.stub.FunctionOutput, request)


def make_output_request(input_id, output_buffer_id, **kwargs):
    result = api_pb2.GenericResult(**kwargs)
    item = pack_output_buffer_item(result)
    buffer_req = api_pb2.BufferWriteRequest(item=item, buffer_id=output_buffer_id)

    return api_pb2.FunctionOutputRequest(input_id=input_id, buffer_req=buffer_req)


def make_eof_request(output_buffer_id):
    item = api_pb2.BufferItem(EOF=True)
    buffer_req = api_pb2.BufferWriteRequest(item=item, buffer_id=output_buffer_id)
    return api_pb2.FunctionOutputRequest(buffer_req=buffer_req)


def call_function(
    function: typing.Callable,
    buffer_item: api_pb2.BufferItem,
    serializer: typing.Callable[[typing.Any], bytes],
    deserializer: typing.Callable[[bytes], typing.Any],
    function_context: FunctionContext,
):
    input = unpack_input_buffer_item(buffer_item)
    output_buffer_id = input.output_buffer_id

    if buffer_item.EOF:
        # Let the caller know that all inputs have been processed.
        # TODO: This isn't exactly part of the function call, so could be separated out.
        function_context.output(make_eof_request(output_buffer_id))
        return

    input_id = buffer_item.item_id
    args = deserializer(input.args)
    kwargs = deserializer(input.kwargs)

    try:
        res = function(*args, **kwargs)

        if inspect.iscoroutine(res):
            res = asyncio.run(res)

        if inspect.isasyncgen(res):
            res = synchronizer._run_generator_sync(res)

        if inspect.isgenerator(res):
            for value in res:
                function_context.output(make_output_request(
                    input_id,
                    output_buffer_id,
                    status=api_pb2.GenericResult.Status.SUCCESS,
                    data=serializer(value),
                    gen_status=api_pb2.GenericResult.GeneratorStatus.INCOMPLETE,
                ))

            # send EOF
            function_context.output(make_output_request(
                input_id,
                output_buffer_id,
                status=api_pb2.GenericResult.Status.SUCCESS,
                gen_status=api_pb2.GenericResult.GeneratorStatus.COMPLETE,
            ))
        else:
            function_context.output(make_output_request(
                input_id,
                output_buffer_id,
                status=api_pb2.GenericResult.Status.SUCCESS,
                data=serializer(res),
            ))

    except Exception as exc:
        # Note: we're not serializing the traceback since it contains
        # local references that means we can't unpickle it. We *are*
        # serializing the exception, which may have some issues (there
        # was an earlier note about it that it might not be possible
        # to unpickle it in some cases). Let's watch oout for issues.
        function_context.output(make_output_request(
            input_id,
            output_buffer_id,
            status=api_pb2.GenericResult.Status.FAILURE,
            data=serializer(exc),
            exception=repr(exc),
            traceback=traceback.format_exc(),
        ))


def main(container_args, client=None):
    # Note that we're creating the client in a synchronous context, but it will be running in a separate thread.
    # This is good because if the function is long running then we the client can still send heartbeats
    # The only caveat is a bunch of calls will now cross threads, which adds a bit of overhead?
    if client is None:
        client = Client.from_env()

    function_context = FunctionContext(container_args, client)
    function = function_context.get_function()

    for buffer_item in function_context.generate_inputs():
        # Note: this blocks the call_function as well. In the future we might want to stream outputs
        # back asynchronously, but then block the call_function if there is back-pressure.
        call_function(function, buffer_item, client.serialize, client.deserialize, function_context)


if __name__ == "__main__":
    logger.debug("Container: starting")
    container_args = google.protobuf.json_format.Parse(
        sys.argv[1],
        api_pb2.ContainerArguments(),
    )
    main(container_args)
    logger.debug("Container: done")
