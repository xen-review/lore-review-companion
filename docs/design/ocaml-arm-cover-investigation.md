# OCaml ARM Bindings: Cover-Message Ambiguity

The importer has retained two distinct cover emails, both from Julian Vetter
and both with subject `[PATCH 0/2] tools/ocaml: bindings needed to build ARM
domains`:

- [First cover](https://lore.kernel.org/xen-devel/1792679039.8631fc262581453bbf619ec5b2062170.1a163ff5306000f79f@vates.tech/)
  has no email parent and describes two missing OCaml bindings.
- [Second cover](https://lore.kernel.org/xen-devel/1792679271.8631fc262581453bbf619ec5b2062170.1a16402dab1000f79f@vates.tech/)
  replies to the first cover, has the same subject, but has different body text.

The retained patch messages are [1/2](https://lore.kernel.org/xen-devel/1792679271.8631fc262581453bbf619ec5b2062170.1a16402dca1000f79f@vates.tech/)
and [2/2](https://lore.kernel.org/xen-devel/1792679272.8631fc262581453bbf619ec5b2062170.1a16402de81000f79f@vates.tech/).
Both patches' `In-Reply-To` header names
`20261022142744.338622-1-julian.vetter@vates.tech`, which is not retained in the
current mail ledger. Other reference headers connect them to the first cover.

The importer groups the four messages into one revision and refuses to select
one of two different cover bodies for the PR description. "Duplicate cover
classification" meant exactly this; it did not mean the two patch files were
duplicates or missing. The actual relationship to the missing parent has not
been established. Recover that parent from the archive and inspect the explicit
thread headers before choosing a cover; do not guess or silently discard a body.