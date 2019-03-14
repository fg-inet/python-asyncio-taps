Design Decisions
================

Coroutines
----------

We decided to make all our API functions coroutines. This wasnt strictly required as they are all designed to return immediately anyway. However, both from a usability as well as a clarity perspective we made this descision.

First of all, since the API is heavily callback based, it is required for the application to not exit until every task inside the event loop is finished, not simply once the end of the main function has been reached. To make sure of this, it is required of developers using the API to make use of the
``asyncio.loop.run_forever()`` method to make sure the event loop doesn't stop until it is explicitly told to do so with ``asyncio.loop.stop()``.

Since this required developers to come in direct contact with ``asyncio`` anyway, we deemed it favorable to allow the developers additional flexibility that a coroutine and thus an awaitable object offers over a regular function (see the `asyncio documentation <https://docs.python.org/3/library/asyncio-task.html#coroutine>`_ for more information).

Since the ``await`` keyword can only be used in asynchronous functions, we accordingly also decided to require callbacks to be coroutines.
