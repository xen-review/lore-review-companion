Python API
==========

These interfaces describe the current development implementation, not a stable
third-party API. They require the installed package and its runtime dependencies.
Publication functions can write local state or interact with Forgejo; use a
disposable fixture and explicitly reviewed policy when experimenting.

Continuous worker
-----------------

The continuous entry point calls the round function until interrupted. It
sets the API write guard, logs each result or exception as JSON, drains backlog
without delay, and sleeps between idle or failed rounds. Its ``forge_path``
selects the filesystem checked by the resource guard.

.. autofunction:: lore_review_companion.import_worker.run_import_worker

Run one bounded scheduling round. ``refresh_source`` controls source refresh;
``poll_source`` controls the time-based discovery step. The function can
update the ledger, Git repository, scheduler state, and Forgejo. The continuous
worker passes a resource guard; callers providing their own guard are
responsible for its behavior.

.. autofunction:: lore_review_companion.import_worker.run_import_worker_round

The returned result contains the policy digest, source and publication
cursors, counts of revisions considered and publications and effects delivered,
diagnostics, and whether more backlog remains. Counts are not proof that all
source archive messages have been discovered.

.. autoclass:: lore_review_companion.import_worker.ImportWorkerRound
   :members:

Wait until the host load and the free space at ``forge_path`` meet the worker's
resource thresholds.

.. autofunction:: lore_review_companion.import_worker.wait_for_resources

Policy
------

The immutable policy binds the source and destination identities, trust and
resource limits, and retry behavior. ``assert_current`` re-reads the policy and
checks the Forgejo capability response before import activity.

.. autoclass:: lore_review_companion.import_policy.ImportPolicy
   :members:

Read and validate a bounded policy JSON file. Invalid, disabled, expired, or
unavailable policy raises an exception instead of authorizing work.

.. autofunction:: lore_review_companion.import_policy.read_import_policy

NNTP source
-----------

The discovery functions queue Message-IDs from time-based ``NEWNEWS``, article
numbers, or a public-inbox thread; ``fetch_nntp`` fetches queued articles into
the ledger. The number scan requires an initialized time-based checkpoint, and
thread discovery requires the anchor to have been fetched. These functions
perform network and ledger operations; use test ledgers for experiments.

.. automodule:: lore_review_companion.nntp_source
   :members: discover_nntp, discover_nntp_numbers, fetch_nntp, discover_nntp_thread, NNTPSourceError