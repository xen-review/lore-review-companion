Lore Review Companion
=====================

Lore Review Companion imports mailing-list patch submissions and their review
discussion into a dedicated Forgejo repository. It is an early pre-alpha
development experiment, not a production service. The repository's
``CONSTITUTION.md`` defines its development and safety contract.

The companion polls a public-inbox NNTP server; it does not maintain a live
subscription. It retains source messages and publication state so it can
reconcile incomplete work and later replies. Importing requires the matching
Forgejo review-import API, which is not part of stock Forgejo.

.. toctree::
   :maxdepth: 2

   worker
   python-api

Build locally
-------------

From the companion checkout, install the documentation dependencies and the
package in a Python 3.11 or newer virtual environment, then build:

.. code-block:: sh

   python -m pip install -r docs/requirements-docs.txt .
   python -m sphinx -n -W -b html docs docs/_build/html

Open ``docs/_build/html/index.html`` in a browser. The committed
``.readthedocs.yaml`` installs the same requirements and package on Read the
Docs. Import the repository there to enable hosted builds; committing the file
alone does not create a hosted project.

The repository's ``README.md``, ``docs/INSTALL-XEN.md``, and
``docs/DEVELOPMENT.md`` contain installation, bootstrap, and development
deployment procedures. Credentials must stay out of the documentation and Git.