"""Resubmit dead-lettered messages from a queue's dead-letter sub-queue back to
the queue itself.

For re-running messages after the cause of their failure has been fixed (e.g.
di-processor's first dev run dead-lettered three dmer-raw messages with
SOURCE_DOWNLOAD_FAILED before the storage fix). Each message is re-sent with
its original body, message_id, content type and application properties, and
only then removed from the dead-letter queue -- so a crash in between leaves a
duplicate (which consumer idempotency ignores), never a lost message.

Run on the jump box, same as sb_queue_viewer.py (same Managed Identity auth;
the host identity needs "Azure Service Bus Data Owner", or Data Receiver +
Data Sender, on the queue):

    ~/sbvenv/bin/python sb_dlq_resubmit.py dmer-raw            # list only
    ~/sbvenv/bin/python sb_dlq_resubmit.py dmer-raw --resubmit # move back

Message bodies are never printed (they can carry document identifiers); only
message_id, delivery count and dead-letter reason.
"""

from __future__ import annotations

import argparse

from azure.identity import ManagedIdentityCredential
from azure.servicebus import ServiceBusClient, ServiceBusMessage, ServiceBusSubQueue

NAMESPACE_FQDN = "sb-rsbc-dmer-shared-dev-001.servicebus.windows.net"


def _body_bytes(msg) -> bytes:
    body = msg.body
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    return b"".join(bytes(part) for part in body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("queue", help="queue name, e.g. dmer-raw")
    parser.add_argument(
        "--resubmit",
        action="store_true",
        help="send each dead-lettered message back to the queue (default: list only)",
    )
    parser.add_argument("--max", type=int, default=50, help="max messages to handle")
    args = parser.parse_args()

    credential = ManagedIdentityCredential()
    with ServiceBusClient(NAMESPACE_FQDN, credential) as client:
        dlq = client.get_queue_receiver(
            args.queue, sub_queue=ServiceBusSubQueue.DEAD_LETTER, max_wait_time=10
        )
        with dlq, client.get_queue_sender(args.queue) as sender:
            if not args.resubmit:
                # Peek: read-only, takes no lock, leaves every message in place.
                peeked = dlq.peek_messages(max_message_count=args.max)
                for msg in peeked:
                    _describe(msg)
                print(
                    f"{len(peeked)} message(s) in the dead-letter queue (listed only)"
                )
                return
            handled = 0
            while handled < args.max:
                batch = dlq.receive_messages(
                    max_message_count=min(10, args.max - handled), max_wait_time=10
                )
                if not batch:
                    break
                for msg in batch:
                    handled += 1
                    _describe(msg)
                    sender.send_messages(
                        ServiceBusMessage(
                            _body_bytes(msg),
                            message_id=msg.message_id,
                            content_type=msg.content_type,
                            application_properties=msg.application_properties,
                        )
                    )
                    dlq.complete_message(msg)  # only after the copy is sent
                    print("  -> resubmitted")
            print(f"{handled} message(s) resubmitted")


def _describe(msg) -> None:
    print(
        f"message_id={msg.message_id} "
        f"deliveries={msg.delivery_count} "
        f"reason={msg.dead_letter_reason}"
    )


if __name__ == "__main__":
    main()
