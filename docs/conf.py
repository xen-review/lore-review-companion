project = "Lore Review Companion"
extensions = ["sphinx.ext.autodoc"]
html_theme = "alabaster"
exclude_patterns = ["_build"]
nitpick_ignore = [
	("py:class", "pathlib.Path"),
	("py:class", "lore_review_companion.ledger.Ledger"),
	("py:class", "lore_review_companion.forge_api.ForgeAPI"),
	("py:class", "lore_review_companion.git_branches.GitHttpCredentials"),
	("py:class", "lore_review_companion.nntp_source.NNTPFetch"),
]