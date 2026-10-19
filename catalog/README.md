# Shared catalog (pinned copy)

`catalog.json` is a copy of the HospedaPlus shared catalog, the single
definition of hotels, room types, channels, currencies and seasons used by
F2, F3, F4, F5 and the data platform. The seed loads it into Neon, and
`python -m seed check` fails if the database and this file disagree.

- **Do not edit this copy.** Change the shared catalog repository, publish a
  new version (tag) and copy the file here in the same pull request.
- The version is the `catalog_version` field at the top of the file.
- To try another file locally: `HOSPEDAPLUS_CATALOG=/path/to/catalog.json`.

After copying a new version: `python -m seed reset`, `python -m seed all`,
then reload Neon (see `DEPLOYMENT.md`), because hotel codes are part of the data.
