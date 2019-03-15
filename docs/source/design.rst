Design Decisions
================

PyTAPS is based on `Python asyncio <https://docs.python.org/3/library/asyncio.html>`_, a library that allows to define functions as *coroutines* and to execute them asynchronously in an *event loop*.

This page explains several design decisions that were made when developing PyTAPS.

API functions are coroutines
----------------------------

PyTAPS defines all API functions as *coroutines*, and requires that all callback functions be defined as *coroutines* as well.

Roughly speaking, a coroutine is an asynchronous function that allows execution to be suspended and resumed.

An application can not call a coroutine in the same way as regular, synchronous functions in Python. Instead, an application has to create a task from the coroutine within an event loop, and then run the event loop, as described in `the API documentation <api.rst>`_.

It would have been equally possible to define the API functions as regular functions instead of coroutines, as the functions themselves are not blocking, but returning immediately.
However, the design decision to make all API functions coroutines was made for two reasons:

First of all, since the API is heavily callback based, it is required for the application to not exit until every task inside the event loop is finished, not simply once the end of the main function has been reached. To make sure of this, it is required of developers using the API to make use of the
``asyncio.loop.run_forever()`` method to make sure the event loop does not stop until it is explicitly told to do so with ``asyncio.loop.stop()``.

Secondly, since this required developers to come in direct contact with ``asyncio`` anyway, we deemed it favorable to allow the developers additional flexibility that a coroutine and thus an awaitable object offers over a regular function (see the `asyncio documentation <https://docs.python.org/3/library/asyncio-task.html#coroutine>`_ for more information).
For example, an application can explicitly define that the execution of one coroutine depends on another coroutine, or that the coroutine should be put at the end of the event loop.

Since the ``await`` keyword can only be used from within an asynchronous functions, such as a coroutine, we accordingly also decided to require callbacks to be coroutines.
