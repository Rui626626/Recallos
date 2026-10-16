"""Host-agnostic core.

Must never import anything DSH- or WPS-specific — that is the whole point of
the `adapters/` boundary.

The modules split along the three questions the index has to answer:

* `schema` / `store`  — what is stored, and how it is queried
* `ids` / `normalize` / `hashing` / `versions` — what counts as "the same"
* `chunk` / `tokenize` / `source` — how text becomes citable, searchable units

Bumping `schema.SCHEMA_VERSION` is the only supported way to change the shape of
`index/recall.db`; because that database is derived data, the response to a
mismatch is always "rebuild", never "migrate".
"""

from . import (  # noqa: F401
    chunk, hashing, ids, normalize, schema, source, store, tokenize, versions,
)

__all__ = [
    "chunk", "hashing", "ids", "normalize", "schema", "source", "store",
    "tokenize", "versions",
]
