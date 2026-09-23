"""sb_queue_viewer.py -- a minimal desktop GUI for peeking Service Bus queue
messages on sb-rsbc-dmer-shared-dev-001. The queue dropdown is populated by
listing whatever queues actually exist on the namespace at startup, not a
hardcoded name list -- so it never needs editing when a queue is renamed or
added (e.g. across the raw-dmer-queue/extracted-dmer-queue -> dmer-ingest/
dmer-raw/dmer-extracted/driver-decision rename).

Why this exists: the Azure Portal's own Service Bus Explorer (Peek/Send/
Receive) refuses to operate against a namespace with
publicNetworkAccess=Disabled at all -- confirmed via the browser's own
DevTools Network tab: it never even sends the request, regardless of
whether the browser actually has network access to the private endpoint
(e.g. from a jump box inside the VNet). That's a hard, unconditional
Portal-side limitation for this namespace (see infrastructure/bicep/main.bicep
and its header comment on why Premium + a private endpoint is required here),
not something fixable by network configuration -- so this script talks to
the Service Bus SDK directly instead.

Where this needs to run: anywhere with genuine network access to the
namespace's private endpoint -- in practice, that means a jump box or other
compute inside the same VNet (see docs/deployment/environment-setup.md for
the jump box). It will NOT work from an arbitrary laptop with no route to
the private endpoint.

Peek is non-destructive (peek_messages() doesn't lock or remove anything).
Receive mirrors the Azure Portal's own Service Bus Explorer "Receive
messages" panel, including its Receive Mode choice:

  - "Peek Lock" (the default) locks the received messages -- they're held,
    not deleted, and stay in the queue/sub-queue until you explicitly act
    on one via the Complete/Abandon/Dead-letter buttons (same three
    actions Explorer offers per message). Complete permanently deletes it;
    Abandon releases the lock so it goes straight back to being available
    (e.g. you decide it shouldn't be removed after all); Dead-letter moves
    it straight to the DLQ. An unactioned lock also just expires on its
    own after the queue's lock duration, same as it would in Explorer.
  - "Receive and Delete" removes the message immediately as part of
    receiving it -- one step, no separate Complete call, no lock to act
    on afterwards. This is the one to reach for when you just want to
    drain a queue by hand (e.g. clearing out bad test messages).

Setup: automated by scripts/deployment/jumpbox-setup.sh on a fresh jump box
(see infrastructure/bicep/jumpbox.bicep and docs/deployment/jumpbox-setup.md)
-- this file itself is fetched by that script, not copy-pasted by hand.
Manual setup, on a Linux jump box with a desktop environment already
installed (needs a real display for the Tk GUI -- won't work over a plain
SSH session with no X server):

    sudo apt install -y python3-tk python3-venv
    python3 -m venv --system-site-packages ~/sbvenv
    ~/sbvenv/bin/pip install azure-servicebus azure-identity

Run:

    ~/sbvenv/bin/python sb_queue_viewer.py

Auth: uses ManagedIdentityCredential -- authenticates as the host's own
system-assigned managed identity, no browser/device-code prompt at all.
That identity needs "Azure Service Bus Data Owner" on the namespace --
not just Data Receiver/Sender, because the queue dropdown is populated at
startup via ServiceBusAdministrationClient.list_queues(), a management-
plane operation every data-plane role below Owner is refused for. Only
works when run on a host with a usable managed identity (e.g. an Azure VM)
-- won't work run on a machine with no managed identity to fall back to.
"""

from __future__ import annotations

import json
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from azure.identity import ManagedIdentityCredential
from azure.servicebus import (
    ServiceBusClient,
    ServiceBusReceiveMode,
    ServiceBusSubQueue,
)
from azure.servicebus.management import ServiceBusAdministrationClient

NAMESPACE_FQDN = "sb-rsbc-dmer-shared-dev-001.servicebus.windows.net"
RECEIVE_MODES = ["Peek Lock", "Receive and Delete"]


def _list_queue_names(credential) -> list[str]:
    """Queue names as they exist on the namespace right now, sorted --
    queried fresh every startup rather than hardcoded, so a rename/add on
    the Bicep side is picked up here with no code change.
    """
    with ServiceBusAdministrationClient(NAMESPACE_FQDN, credential) as admin_client:
        return sorted(q.name for q in admin_client.list_queues())


def _extract_body_bytes(body) -> bytes:
    """message.body on a received/peeked ServiceBusMessage is usually a
    generator of byte chunks (AMQP data sections), not a plain bytes
    object -- but it's plain bytes/str for some SDK versions/paths too.
    Handle both rather than assuming one.
    """
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode()
    return b"".join(body)


class QueueViewer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Service Bus Queue Viewer (private endpoint)")
        self.geometry("1180x560")

        # Created eagerly (not lazily on first Peek/Receive, like the old
        # per-click ServiceBusClient below still is) -- listing queues at
        # startup needs it right away.
        self._credential = ManagedIdentityCredential()
        # Peek Lock leaves the receiver connection open (locks are tied to
        # it) between Receive and whatever Complete/Abandon/Dead-letter
        # click comes after -- unlike Peek, which opens/closes per click.
        self._client = None
        self._receiver = None
        # sequence_number (str) -> live ServiceBusReceivedMessage, only
        # populated by a Peek Lock receive; lets Complete/Abandon/
        # Dead-letter act on the exact message the user selected.
        self._locked_rows: dict[str, object] = {}

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=8)

        ttk.Label(top, text="Queue:").pack(side="left")
        self.queue_var = tk.StringVar()
        self.queue_combo = ttk.Combobox(
            top, textvariable=self.queue_var, values=[], state="readonly", width=28
        )
        self.queue_combo.pack(side="left", padx=(4, 12))
        self.refresh_queues_btn = ttk.Button(
            top, text="Refresh queues", command=self._on_refresh_queues
        )
        self.refresh_queues_btn.pack(side="left", padx=(0, 12))
        self._refresh_queue_list(initial=True)

        self.dlq_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Dead-letter sub-queue", variable=self.dlq_var).pack(
            side="left", padx=(0, 12)
        )

        ttk.Label(top, text="Max messages:").pack(side="left")
        self.count_var = tk.StringVar(value="50")
        ttk.Entry(top, textvariable=self.count_var, width=6).pack(
            side="left", padx=(4, 12)
        )

        self.peek_btn = ttk.Button(top, text="Peek", command=self._on_peek)
        self.peek_btn.pack(side="left")

        ttk.Label(top, text="Receive mode:").pack(side="left", padx=(12, 0))
        self.receive_mode_var = tk.StringVar(value=RECEIVE_MODES[0])
        ttk.Combobox(
            top,
            textvariable=self.receive_mode_var,
            values=RECEIVE_MODES,
            state="readonly",
            width=16,
        ).pack(side="left", padx=(4, 12))

        self.receive_btn = ttk.Button(top, text="Receive", command=self._on_receive)
        self.receive_btn.pack(side="left")

        # Per-message actions -- only meaningful for messages a Peek Lock
        # receive is still holding a lock on (see _locked_rows); disabled
        # otherwise, same as Explorer disables them with nothing selected.
        actions = ttk.Frame(self)
        actions.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(actions, text="Selected locked message(s):").pack(side="left")
        self.complete_btn = ttk.Button(
            actions, text="Complete", command=self._on_complete, state="disabled"
        )
        self.complete_btn.pack(side="left", padx=(8, 4))
        self.abandon_btn = ttk.Button(
            actions, text="Abandon", command=self._on_abandon, state="disabled"
        )
        self.abandon_btn.pack(side="left", padx=4)
        self.deadletter_btn = ttk.Button(
            actions, text="Dead-letter", command=self._on_deadletter, state="disabled"
        )
        self.deadletter_btn.pack(side="left", padx=4)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status_var, anchor="w").pack(fill="x", padx=8)

        # A PanedWindow (not two plain .pack()'d widgets) so the split between
        # the message list and the raw body is user-draggable, not fixed.
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        tree_frame = ttk.Frame(paned)
        columns = (
            "document_id",
            "document_guid",
            "driver_key",
            "blob_url",
            "enqueued_at",
            "delivery_count",
            "sequence_number",
        )
        widths = (140, 140, 140, 300, 180, 90, 100)
        # selectmode="extended" -- ctrl/shift-click to select several rows
        # at once; Complete/Abandon/Dead-letter act on all of them together.
        self.tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="headings",
            height=14,
            selectmode="extended",
        )
        for col, width in zip(columns, widths):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        paned.add(tree_frame, weight=3)

        body_frame = ttk.Frame(paned)
        ttk.Label(body_frame, text="Raw message body:").pack(anchor="w")
        self.raw_text = tk.Text(body_frame, wrap="word")
        self.raw_text.pack(fill="both", expand=True)
        paned.add(body_frame, weight=1)

        self._rows: dict[str, dict] = {}

    def _refresh_queue_list(self, initial: bool = False):
        """(Re-)populate the queue dropdown from the namespace itself.

        On startup, a failure here (e.g. the role grant hasn't propagated
        yet) still lets the window open -- just with an empty dropdown and
        an error dialog -- rather than crashing before the user can see
        what went wrong.
        """
        try:
            names = _list_queue_names(self._credential)
        except Exception as exc:  # noqa: BLE001 -- shown to the user, not swallowed
            messagebox.showerror("Could not list queues", str(exc))
            if initial:
                names = []
            else:
                return
        previous = self.queue_var.get()
        self.queue_combo["values"] = names
        if previous in names:
            self.queue_var.set(previous)
        elif names:
            self.queue_var.set(names[0])
        else:
            self.queue_var.set("")

    def _on_refresh_queues(self):
        self._refresh_queue_list()

    def _on_peek(self):
        self._set_controls_state("disabled")
        self.status_var.set("Peeking...")
        threading.Thread(target=self._peek_worker, daemon=True).start()

    def _peek_worker(self):
        try:
            queue_name = self.queue_var.get()
            max_count = int(self.count_var.get() or "50")
            sub_queue = ServiceBusSubQueue.DEAD_LETTER if self.dlq_var.get() else None

            with (
                ServiceBusClient(NAMESPACE_FQDN, self._credential) as client,
                client.get_queue_receiver(queue_name, sub_queue=sub_queue) as receiver,
            ):
                messages = receiver.peek_messages(max_message_count=max_count)

            self.after(0, self._populate, messages, "Peeked", False)
        except Exception as exc:  # noqa: BLE001 -- shown to the user, not swallowed
            self.after(0, self._show_error, exc)

    def _on_receive(self):
        queue_name = self.queue_var.get()
        target = "dead-letter sub-queue" if self.dlq_var.get() else "queue"
        max_count = self.count_var.get() or "50"
        mode = self.receive_mode_var.get()
        if mode == "Receive and Delete" and not messagebox.askyesno(
            "Confirm delete",
            f"Receive and Delete mode will RECEIVE AND PERMANENTLY DELETE up "
            f"to {max_count} message(s) from the {target} '{queue_name}' as "
            "soon as they're received -- there's no separate Complete step "
            "and no undo. Continue?",
        ):
            return
        self._set_controls_state("disabled")
        self.status_var.set("Receiving...")
        threading.Thread(target=self._receive_worker, daemon=True).start()

    def _receive_worker(self):
        try:
            queue_name = self.queue_var.get()
            max_count = int(self.count_var.get() or "50")
            sub_queue = ServiceBusSubQueue.DEAD_LETTER if self.dlq_var.get() else None
            peek_lock = self.receive_mode_var.get() == "Peek Lock"
            receive_mode = (
                ServiceBusReceiveMode.PEEK_LOCK
                if peek_lock
                else ServiceBusReceiveMode.RECEIVE_AND_DELETE
            )

            # Any previous Peek Lock receive's connection is done with as
            # soon as a new receive starts -- its still-locked messages (if
            # any weren't actioned) just fall back to their natural lock
            # expiry, same as walking away from them in Explorer would.
            self._close_receiver()

            if peek_lock:
                # Kept open (not a `with` block) -- Complete/Abandon/
                # Dead-letter need this same receiver later to act on the
                # lock these messages were just given.
                self._client = ServiceBusClient(NAMESPACE_FQDN, self._credential)
                self._receiver = self._client.get_queue_receiver(
                    queue_name, sub_queue=sub_queue, receive_mode=receive_mode
                )
                messages = self._receiver.receive_messages(
                    max_message_count=max_count, max_wait_time=5
                )
                self.after(0, self._populate, messages, "Received (locked)", True)
            else:
                with (
                    ServiceBusClient(NAMESPACE_FQDN, self._credential) as client,
                    client.get_queue_receiver(
                        queue_name, sub_queue=sub_queue, receive_mode=receive_mode
                    ) as receiver,
                ):
                    messages = receiver.receive_messages(
                        max_message_count=max_count, max_wait_time=5
                    )
                self.after(0, self._populate, messages, "Received & deleted", False)
        except Exception as exc:  # noqa: BLE001 -- shown to the user, not swallowed
            self.after(0, self._show_error, exc)

    def _close_receiver(self):
        """Release whatever the last Peek Lock receive was holding open."""
        if self._receiver is not None:
            try:
                self._receiver.close()
            except Exception:  # noqa: BLE001, S110 -- best-effort close, we're
                # discarding this receiver either way and there's no user-facing
                # action to take on a close failure.
                pass
            self._receiver = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001, S110 -- same reasoning as above.
                pass
            self._client = None
        self._locked_rows.clear()

    def _populate(self, messages, verb, locked):
        self.tree.delete(*self.tree.get_children())
        self._rows.clear()
        self._locked_rows.clear()
        for msg in messages:
            try:
                body = json.loads(_extract_body_bytes(msg.body))
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = {"_raw": _extract_body_bytes(msg.body).decode(errors="replace")}
            iid = str(msg.sequence_number)
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    body.get("documentId", ""),
                    body.get("documentGuid", ""),
                    body.get("driverKey", ""),
                    body.get("blobUrl", ""),
                    body.get("enqueuedAt", ""),
                    msg.delivery_count,
                    msg.sequence_number,
                ),
            )
            self._rows[iid] = body
            if locked:
                self._locked_rows[iid] = msg
        self.status_var.set(f"{verb} {len(messages)} message(s).")
        self._set_controls_state("normal")
        self._update_action_buttons()

    def _on_select(self, _event):
        selected = self.tree.selection()
        self.raw_text.delete("1.0", "end")
        if len(selected) == 1:
            body = self._rows.get(selected[0], {})
            self.raw_text.insert("1.0", json.dumps(body, indent=2))
        elif len(selected) > 1:
            # Showing N raw bodies at once isn't very readable -- just say
            # how many are selected; each one's still viewable by itself.
            self.raw_text.insert("1.0", f"{len(selected)} messages selected.")
        self._update_action_buttons()

    def _update_action_buttons(self):
        selected = self.tree.selection()
        # Enabled as soon as *any* selected row has a lock to act on --
        # Complete/Abandon/Dead-letter only apply to that locked subset,
        # so mixing in a few Peeked/unlocked rows in the same selection is
        # fine, they're just ignored when the action runs.
        has_lock = any(iid in self._locked_rows for iid in selected)
        state = "normal" if has_lock else "disabled"
        self.complete_btn.config(state=state)
        self.abandon_btn.config(state=state)
        self.deadletter_btn.config(state=state)

    def _on_complete(self):
        self._act_on_selected(
            "complete_message",
            "Completed",
            "Permanently delete {n} message(s)?",
        )

    def _on_abandon(self):
        self._act_on_selected(
            "abandon_message",
            "Abandoned",
            "Release the lock and return {n} message(s) to the queue?",
        )

    def _on_deadletter(self):
        self._act_on_selected(
            "dead_letter_message",
            "Dead-lettered",
            "Move {n} message(s) to the dead-letter sub-queue?",
        )

    def _act_on_selected(self, receiver_method_name, verb, confirm_template):
        selected = self.tree.selection()
        locked_iids = [iid for iid in selected if iid in self._locked_rows]
        if not locked_iids:
            return
        if not messagebox.askyesno(
            "Confirm", confirm_template.format(n=len(locked_iids))
        ):
            return
        self._set_controls_state("disabled")
        self.complete_btn.config(state="disabled")
        self.abandon_btn.config(state="disabled")
        self.deadletter_btn.config(state="disabled")
        self.status_var.set(f"{verb}...")
        msgs = [(iid, self._locked_rows[iid]) for iid in locked_iids]
        threading.Thread(
            target=self._act_worker,
            args=(receiver_method_name, verb, msgs),
            daemon=True,
        ).start()

    def _act_worker(self, receiver_method_name, verb, msgs):
        # One at a time, not a batch call -- so one failure partway through
        # (e.g. a lock that already expired) still leaves everything before
        # it actioned, rather than an all-or-nothing outcome.
        succeeded = []
        errors = []
        for iid, msg in msgs:
            try:
                getattr(self._receiver, receiver_method_name)(msg)
                succeeded.append(iid)
            except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
                errors.append((iid, exc))
        self.after(0, self._on_act_done, verb, succeeded, errors)

    def _on_act_done(self, verb, succeeded, errors):
        for iid in succeeded:
            self._locked_rows.pop(iid, None)
            self._rows.pop(iid, None)
            if self.tree.exists(iid):
                self.tree.delete(iid)
        self.raw_text.delete("1.0", "end")
        if errors:
            self.status_var.set(
                f"{verb} {len(succeeded)} message(s); {len(errors)} failed."
            )
            messagebox.showerror(
                "Some operations failed",
                "\n".join(f"seq {iid}: {exc}" for iid, exc in errors),
            )
        else:
            self.status_var.set(f"{verb} {len(succeeded)} message(s).")
        self._set_controls_state("normal")
        self._update_action_buttons()

    def _set_controls_state(self, state):
        self.peek_btn.config(state=state)
        self.receive_btn.config(state=state)

    def _show_error(self, exc):
        self.status_var.set("Error -- see dialog.")
        self._set_controls_state("normal")
        self._update_action_buttons()
        messagebox.showerror("Operation failed", str(exc))

    def _on_close(self):
        self._close_receiver()
        self.destroy()


if __name__ == "__main__":
    app = QueueViewer()
    app.mainloop()
