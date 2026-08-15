"""RFC 9622 Section 8.1.5 and 9.2.4/9.2.6: Connection Group send scheduling."""
import asyncio

import pytest

import pytaps as taps
from pytaps.transportProperties import (
    CONNECTION_SCHEDULERS,
    PRIORITY_AWARE_SCHEDULERS,
    normalize_conn_scheduler,
)


class RecordingTransport:
    """Minimal transport that records the order Messages reach the stack."""

    def __init__(self, label, log):
        self.label = label
        self.log = log

    def send(self, data, message_context=None, end_of_message=True, send_call_id=None):
        self.log.append((self.label, data, message_context))
        return len(self.log)


def _group(labels=("A", "B"), protocol="tcp"):
    """Build entangled, established Connections that share one send log."""
    loop = asyncio.new_event_loop()
    preconnection = taps.Preconnection(
        remote_endpoint=taps.RemoteEndpoint().with_address("192.0.2.10").with_port(443),
        event_loop=loop,
    )
    log = []
    connections = []
    first = None
    for label in labels:
        connection = taps.Connection(preconnection)
        connection.protocol = protocol
        connection.state = taps.ConnectionState.ESTABLISHED
        connection.transports = [RecordingTransport(label, log)]
        if first is None:
            first = connection
        else:
            first.connection_group.add_connection(connection)
        connections.append(connection)
    return loop, connections, log


def _labels(log):
    return [entry[0] for entry in log]


# --- Section 8.1.5: connScheduler is an Enumeration over the RFC 8260 set ---


def test_section_8_1_5_conn_scheduler_default_is_weighted_fair_queueing():
    loop, (connection, _other), _log = _group()

    assert connection.get_property("connScheduler") == "Weighted Fair Queueing"
    assert connection.connection_group.get_properties()["connScheduler"] == (
        "Weighted Fair Queueing"
    )
    loop.close()


@pytest.mark.parametrize("scheduler", CONNECTION_SCHEDULERS)
def test_section_8_1_5_every_rfc8260_scheduler_is_accepted(scheduler):
    loop, (connection, _other), _log = _group()

    connection.set_property("connScheduler", scheduler)

    assert connection.get_property("connScheduler") == scheduler
    assert connection.state is taps.ConnectionState.ESTABLISHED
    loop.close()


@pytest.mark.parametrize(
    ("spelling", "canonical"),
    [
        ("wfq", "Weighted Fair Queueing"),
        ("SCTP_SS_WFQ", "Weighted Fair Queueing"),
        ("weighted fair queuing", "Weighted Fair Queueing"),
        ("fcfs", "First-Come, First-Served"),
        ("First Come, First Served", "First-Come, First-Served"),
        ("rr", "Round-Robin"),
        ("rr-p", "Round-Robin per Packet"),
        ("SCTP_SS_RR_PKT", "Round-Robin per Packet"),
        ("prio", "Priority-Based"),
        ("sctp_ss_fc", "Fair Capacity"),
    ],
)
def test_section_8_1_5_scheduler_aliases_canonicalize(spelling, canonical):
    assert normalize_conn_scheduler(spelling) == canonical


@pytest.mark.parametrize("value", ["Totally Made Up", "", "   ", 7, None])
def test_section_8_1_5_scheduler_rejects_values_outside_the_enumeration(value):
    loop, (connection, _other), _log = _group()
    with pytest.raises(ValueError):
        connection.set_property("connScheduler", value)

    assert connection.get_property("connScheduler") == "Weighted Fair Queueing"
    assert connection.state is taps.ConnectionState.ESTABLISHED
    loop.close()


def test_section_7_4_conn_scheduler_is_entangled_across_the_group():
    loop, (first, second), _log = _group()

    first.set_property("connScheduler", "Priority-Based")

    assert second.get_property("connScheduler") == "Priority-Based"
    loop.close()


# --- Section 9.2.6: connPriority is ordered over msgPriority ---


@pytest.mark.parametrize("scheduler", sorted(PRIORITY_AWARE_SCHEDULERS))
def test_section_9_2_6_conn_priority_outranks_msg_priority(scheduler):
    loop, (first, second), log = _group(labels=("A", "B"))
    first.set_property("connScheduler", scheduler)
    first.set_property("connPriority", 1)
    second.set_property("connPriority", 0)

    # The higher-priority Message is enqueued first, on the lower-priority
    # Connection. RFC 9622 Section 9.2.6 still sends the priority 1 Message of
    # the priority 0 Connection ahead of it.
    first.enqueue_message(b"a", taps.MessageContext(priority=0))
    second.enqueue_message(b"b", taps.MessageContext(priority=1))

    loop.run_until_complete(first.flush_group_messages())

    assert _labels(log) == ["B", "A"]
    loop.close()


def test_section_9_2_6_msg_priority_still_orders_within_one_connection():
    loop, (first, second), log = _group(labels=("A", "B"))
    first.set_property("connPriority", 0)
    second.set_property("connPriority", 1)

    first.enqueue_message(b"low", taps.MessageContext(priority=100))
    first.enqueue_message(b"high", taps.MessageContext(priority=1))
    second.enqueue_message(b"other", taps.MessageContext(priority=0))

    loop.run_until_complete(first.flush_group_messages())

    assert [(label, data) for label, data, _context in log] == [
        ("A", b"high"),
        ("A", b"low"),
        ("B", b"other"),
    ]
    loop.close()


def test_section_9_1_3_5_final_message_is_scheduled_after_the_whole_group():
    loop, (first, second), log = _group(labels=("A", "B"))
    first.set_property("connPriority", 0)
    second.set_property("connPriority", 1)

    # The Final Message carries the best msgPriority and sits on the
    # best-priority Connection, yet it is still sorted to the very end.
    first.enqueue_message(b"plain", taps.MessageContext(priority=100))
    first.enqueue_message(b"final", taps.MessageContext(priority=0, final=True))
    second.enqueue_message(b"other", taps.MessageContext(priority=100))

    loop.run_until_complete(first.flush_group_messages())

    assert [(label, data) for label, data, _context in log] == [
        ("A", b"plain"),
        ("B", b"other"),
        ("A", b"final"),
    ]
    loop.close()


def test_section_8_1_5_first_come_first_served_ignores_priority():
    loop, (first, second), log = _group(labels=("A", "B"))
    first.set_property("connScheduler", "First-Come, First-Served")
    first.set_property("connPriority", 100)
    second.set_property("connPriority", 0)

    first.enqueue_message(b"a1", taps.MessageContext(priority=100))
    second.enqueue_message(b"b1", taps.MessageContext(priority=0))
    first.enqueue_message(b"a2", taps.MessageContext(priority=0))

    loop.run_until_complete(first.flush_group_messages())

    assert [data for _label, data, _context in log] == [b"a1", b"b1", b"a2"]
    loop.close()


@pytest.mark.parametrize("scheduler", ["Round-Robin", "Fair Capacity"])
def test_section_8_1_5_capacity_sharing_schedulers_cycle_connections(scheduler):
    loop, (first, second), log = _group(labels=("A", "B"))
    first.set_property("connScheduler", scheduler)

    first.enqueue_message(b"a1", taps.MessageContext())
    first.enqueue_message(b"a2", taps.MessageContext())
    first.enqueue_message(b"a3", taps.MessageContext())
    second.enqueue_message(b"b1", taps.MessageContext())
    second.enqueue_message(b"b2", taps.MessageContext())

    loop.run_until_complete(first.flush_group_messages())

    assert _labels(log) == ["A", "B", "A", "B", "A"]
    loop.close()


def test_section_8_1_5_round_robin_switches_on_every_message():
    """RFC 8260 Section 3.2 cycles by Message count, ignoring Message length."""
    loop, (first, second), log = _group(labels=("A", "B"))
    first.set_property("connScheduler", "Round-Robin")

    first.enqueue_message(b"x" * 900, taps.MessageContext())
    first.enqueue_message(b"x" * 900, taps.MessageContext())
    second.enqueue_message(b"y", taps.MessageContext())
    second.enqueue_message(b"y", taps.MessageContext())

    loop.run_until_complete(first.flush_group_messages())

    assert _labels(log) == ["A", "B", "A", "B"]
    loop.close()


def test_section_8_1_5_round_robin_per_packet_bundles_one_connection():
    """RFC 8260 Section 3.3 only switches when starting a new packet."""
    loop, (first, second), log = _group(labels=("A", "B"))
    first.set_property("connScheduler", "Round-Robin per Packet")

    # Three 600-byte Messages overflow one 1500-byte packet after the third,
    # so the scheduler switches to B only once the packet is full.
    for _ in range(3):
        first.enqueue_message(b"a" * 600, taps.MessageContext())
    for _ in range(3):
        second.enqueue_message(b"b" * 600, taps.MessageContext())

    loop.run_until_complete(first.flush_group_messages())

    assert _labels(log) == ["A", "A", "A", "B", "B", "B"]
    loop.close()


def test_section_8_1_5_round_robin_per_packet_switches_between_packets():
    loop, (first, second), log = _group(labels=("A", "B"))
    first.set_property("connScheduler", "Round-Robin per Packet")

    # Each Message alone fills a packet, so this degenerates to plain
    # Round-Robin: one Message per Connection per round.
    for _ in range(2):
        first.enqueue_message(b"a" * 1500, taps.MessageContext())
    for _ in range(2):
        second.enqueue_message(b"b" * 1500, taps.MessageContext())

    loop.run_until_complete(first.flush_group_messages())

    assert _labels(log) == ["A", "B", "A", "B"]
    loop.close()


# --- Section 8.1.5: the capacity-aware schedulers account for bytes ---


def test_section_8_1_5_fair_capacity_equalizes_bytes_not_message_count():
    """RFC 8260 Section 3.5 keeps capacity equal, so many small Messages of one
    Connection go out while another sends a single large one."""
    loop, (first, second), log = _group(labels=("A", "B"))
    first.set_property("connScheduler", "Fair Capacity")

    second.enqueue_message(b"b" * 400, taps.MessageContext())
    for _ in range(4):
        first.enqueue_message(b"a" * 100, taps.MessageContext())

    loop.run_until_complete(first.flush_group_messages())

    # A's four 100-byte Messages reach 400 bytes only on the last one, so the
    # first three precede B's single 400-byte Message.
    assert _labels(log) == ["A", "A", "A", "B", "A"]
    loop.close()


def test_section_8_1_5_weighted_fair_queueing_splits_capacity_by_priority():
    """RFC 8260 Section 3.6: double the weight earns double the capacity."""
    loop, (first, second), log = _group(labels=("A", "B"))
    first.set_property("connScheduler", "Weighted Fair Queueing")
    first.set_property("connPriority", 0)   # weight 1/1
    second.set_property("connPriority", 1)  # weight 1/2

    for _ in range(4):
        first.enqueue_message(b"a" * 100, taps.MessageContext())
    for _ in range(4):
        second.enqueue_message(b"b" * 100, taps.MessageContext())

    loop.run_until_complete(first.flush_group_messages())

    # Over the first six sends, A gets twice the capacity of B.
    labels = _labels(log)
    assert labels[:6].count("A") == 4
    assert labels[:6].count("B") == 2
    loop.close()


def _labels_for_scheduler(scheduler):
    loop, (first, second), log = _group(labels=("A", "B"))
    first.set_property("connScheduler", scheduler)
    first.set_property("connPriority", 0)   # weight 1/1
    second.set_property("connPriority", 1)  # weight 1/2

    for _ in range(6):
        first.enqueue_message(b"a" * 100, taps.MessageContext())
    for _ in range(3):
        second.enqueue_message(b"b" * 100, taps.MessageContext())

    loop.run_until_complete(first.flush_group_messages())
    labels = _labels(log)
    loop.close()
    return labels


def test_section_8_1_5_weighted_fair_queueing_is_not_strict_priority():
    """WFQ shares capacity 2:1; Priority-Based starves the lower priority.

    Both schedulers are priority aware, so both satisfy the RFC 9622
    Section 9.2.6 first-send rule, but only Priority-Based drains one
    Connection before starting the other.
    """
    assert _labels_for_scheduler("Weighted Fair Queueing") == [
        "A", "A", "B", "A", "A", "B", "A", "A", "B",
    ]
    assert _labels_for_scheduler("Priority-Based") == [
        "A", "A", "A", "A", "A", "A", "B", "B", "B",
    ]


def test_section_8_1_5_priority_based_is_strict():
    """RFC 8260 Section 3.4 drains the higher-priority Connection completely."""
    loop, (first, second), log = _group(labels=("A", "B"))
    first.set_property("connScheduler", "Priority-Based")
    first.set_property("connPriority", 0)
    second.set_property("connPriority", 3)

    for _ in range(3):
        first.enqueue_message(b"a" * 100, taps.MessageContext())
    for _ in range(2):
        second.enqueue_message(b"b" * 100, taps.MessageContext())

    loop.run_until_complete(first.flush_group_messages())

    assert _labels(log) == ["A", "A", "A", "B", "B"]
    loop.close()


def test_section_9_1_3_5_final_message_is_last_under_every_scheduler():
    for scheduler in CONNECTION_SCHEDULERS:
        loop, (first, second), log = _group(labels=("A", "B"))
        first.set_property("connScheduler", scheduler)

        first.enqueue_message(b"final", taps.MessageContext(final=True))
        second.enqueue_message(b"plain", taps.MessageContext())

        loop.run_until_complete(first.flush_group_messages())

        assert [data for _label, data, _context in log][-1] == b"final", (
            f"{scheduler} must sort the Final Message last"
        )
        loop.close()


def test_group_flush_drains_every_member_queue_once():
    loop, (first, second), log = _group(labels=("A", "B"))
    first.enqueue_message(b"a", taps.MessageContext())
    second.enqueue_message(b"b", taps.MessageContext())

    message_ids = loop.run_until_complete(first.flush_group_messages())

    assert len(message_ids) == 2
    assert first._queued_messages == []
    assert second._queued_messages == []

    # A second flush has nothing left to send.
    assert loop.run_until_complete(second.flush_group_messages()) == []
    assert len(log) == 2
    loop.close()


def test_flush_group_messages_on_a_lone_connection_flushes_itself():
    loop, (only,), log = _group(labels=("A",))
    only.enqueue_message(b"high", taps.MessageContext(priority=1))
    only.enqueue_message(b"low", taps.MessageContext(priority=9))

    loop.run_until_complete(only.flush_group_messages())

    assert [data for _label, data, _context in log] == [b"high", b"low"]
    loop.close()


# --- Section 9.2.4: StartBatch / EndBatch ---


def test_section_9_2_4_start_batch_defers_sends_until_end_batch():
    loop, (connection, _other), log = _group()

    batch_id = connection.start_batch()
    loop.run_until_complete(connection.send(b"one"))
    loop.run_until_complete(connection.send(b"two"))

    assert log == [], "Messages in an open batch must not reach the stack yet"

    loop.run_until_complete(connection.end_batch())

    assert [data for _label, data, _context in log] == [b"one", b"two"]
    assert batch_id is not None
    loop.close()


def test_section_9_2_4_batch_is_scheduled_across_the_group():
    loop, (first, second), log = _group(labels=("A", "B"))
    first.set_property("connPriority", 1)
    second.set_property("connPriority", 0)

    first.start_batch()
    second.start_batch()
    loop.run_until_complete(first.send(b"a"))
    loop.run_until_complete(second.send(b"b"))
    loop.run_until_complete(first.end_batch())

    # Ending one batch flushes the group through connScheduler.
    assert _labels(log) == ["B", "A"]
    loop.run_until_complete(second.end_batch())
    loop.close()


def test_section_9_2_4_nested_batches_flush_once():
    loop, (connection, _other), log = _group()

    outer = connection.start_batch()
    inner = connection.start_batch()
    assert outer == inner, "a nested batch reuses the open batch identifier"

    loop.run_until_complete(connection.send(b"one"))
    assert loop.run_until_complete(connection.end_batch()) == []
    assert log == []

    loop.run_until_complete(connection.end_batch())
    assert [data for _label, data, _context in log] == [b"one"]
    loop.close()


def test_section_9_2_4_end_batch_without_start_batch_is_an_error():
    loop, (connection, _other), _log = _group()

    with pytest.raises(RuntimeError, match="StartBatch"):
        loop.run_until_complete(connection.end_batch())

    assert connection.state is taps.ConnectionState.ESTABLISHED
    loop.close()


def test_section_9_2_4_batched_messages_share_a_batch_id():
    loop, (connection, _other), log = _group()

    first_batch = connection.start_batch()
    loop.run_until_complete(connection.send(b"one"))
    loop.run_until_complete(connection.send(b"two"))
    loop.run_until_complete(connection.end_batch())

    second_batch = connection.start_batch()
    loop.run_until_complete(connection.send(b"three"))
    loop.run_until_complete(connection.end_batch())

    batch_ids = [context.batch_id for _label, _data, context in log]

    assert batch_ids == [first_batch, first_batch, second_batch]
    assert first_batch != second_batch
    loop.close()


def test_section_9_2_4_send_batch_is_scheduled_across_the_group():
    loop, (first, second), log = _group(labels=("A", "B"))
    first.set_property("connPriority", 1)
    second.set_property("connPriority", 0)

    second.enqueue_message(b"b", taps.MessageContext(priority=100))
    loop.run_until_complete(first.send_batch([(b"a", None, True)]))

    assert _labels(log) == ["B", "A"]
    loop.close()


def test_section_9_2_4_send_batch_nested_in_an_open_batch_defers():
    loop, (connection, _other), log = _group()

    connection.start_batch()
    loop.run_until_complete(connection.send_batch([(b"a", None, True)]))

    assert log == [], "an outer batch is still open, so nothing is sent yet"

    loop.run_until_complete(connection.end_batch())

    assert [data for _label, data, _context in log] == [b"a"]
    loop.close()


def test_section_9_2_4_sends_outside_a_batch_are_not_deferred():
    loop, (connection, _other), log = _group()

    loop.run_until_complete(connection.send(b"immediate"))

    assert [data for _label, data, _context in log] == [b"immediate"]
    assert log[0][2].batch_id is None
    loop.close()
