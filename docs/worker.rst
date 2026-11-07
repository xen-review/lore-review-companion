Worker operation
================

Scheduling
----------

The ``run-import-worker`` command runs a continuous process. It starts a round
immediately; there is no NNTP push subscription or IDLE connection. When a round
finishes with more backlog, it starts another round without waiting. Once the
backlog is drained, it sleeps for 600 seconds and polls again. A failed round
logs an error and waits 600 seconds before retrying, without creating a persistent
incident latch. The intervals are implementation constants, not currently CLI
or policy settings. Restarting the service starts a round immediately.

While healthy, a round may do work even when no new submission arrives. It
checks policy and Forgejo capabilities, discovers and fetches NNTP messages,
reconciles retained revisions and existing pull requests, and processes retained
discussion and pending effects. This allows late replies and incomplete work to
be handled. A large retained history or backlog can therefore use CPU, disk,
network, and API requests between new submissions; it is not an event-only
pipeline. The worker only sleeps after a round has drained its reported backlog.

Before work and before Forgejo writes, a resource guard waits in ten-second
intervals if the one-minute load average exceeds the number of online CPUs or
the filesystem containing the configured Forgejo path has less than 1 GB free.
This is a gate, not a CPU utilization limit.

Round lifecycle
---------------

1. Read the committed policy and acquire the worker-state lock. Verify the
   destination, importer, source, capabilities, and source integrity before
   proceeding. A second round cannot use the same worker-state file at once.
2. When source refresh is enabled, discover NNTP messages by time and article
   number, fetch pending articles, and retain messages and relationships in the
   mail ledger. The default Xen policy uses a 240-hour initial lookback; it is
   configurable and is not proof of complete archive coverage.
3. Discover and reconcile retained submission revisions. Recover available
   source threads, validate membership and signoffs, assemble patches against
   the policy's pinned base, and create or update eligible pull requests.
   Incomplete or blocked submissions can produce a skipped-PR issue rather
   than a pull request.
4. Queue and deliver eligible discussion, retaining idempotency and uncertain
   write state. Persist scheduling cursors and effect enrollment so later rounds
   can resume or revisit work.

Not every source message becomes a pull request: excluded recipients,
incomplete ancestry, assembly problems, missing signoffs, authorization
failures, and other validation errors can prevent publication. The exact reasons
are reported in issues or worker diagnostics. Do not treat a running service as
proof that imports are succeeding.

State and configuration
-----------------------

The committed Xen profile is
``lore_review_companion/policies/xen-development.json``. It identifies the
source, repository and importer, pinned Git base, notification policy, and
other limits. An installed copy is the default; ``--policy-file`` can select a
different committed profile. Credentials are separate private files and must
never be committed or printed.

The usual development installation stores source and publication records in
``~/.local/state/lore-review-companion/mail.sqlite3``, worker cursors and
effect enrollment in ``worker.sqlite3``, and its assembly Git repository in
``xen.git``. These are development paths, not universal defaults: inspect the
service command and policy for the active deployment. The worker-state lock
is adjacent to its SQLite file. Stop the resident service before running a
separate worker round against the same state.

Monitoring and troubleshooting
------------------------------

For the development user service:

.. code-block:: sh

   systemctl --user status lore-review-companion.service --no-pager
   journalctl --user -u lore-review-companion.service -n 100 --no-pager
   systemctl --user show lore-review-companion.service -p MainPID -p CPUUsageNSec

Successful rounds print JSON with ``considered``, ``published``, ``delivered``,
and ``diagnostics``. Failed rounds print ``error_type`` and ``error``. CPU time
reported by systemd or ``top`` is accumulated over the process lifetime; a
current CPU percentage is a separate measurement. If the service has been
running an older version, verify its installed revision before assuming it has
the retry interval described here.

A Forgejo capabilities HTTP 403 blocks the round before source refresh. Check
the response and the repository's import grant, importer permissions, policy
identity, and review-only settings. For unattended import Forgejo also requires
Actions to be disabled, code and pull requests to be enabled, merge and
maintainer-edit options to be disabled, and no applicable active webhooks or
secrets. Do not weaken these checks or silently change a repository's settings
to hide the error. Restore the intended grant and review-only configuration
only after reviewing the impact, then wait for the next retry or restart the
service. Other authorization failures can have different causes.

Source/archive errors and uncertain writes require their own diagnostics;
they are not resolved merely by restarting the worker. Keep tokens out of shell
arguments, logs, issue reports, and documentation. Use the existing setup and
development guides for provisioning and scoped credential handling.