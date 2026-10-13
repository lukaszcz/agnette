# Package Store and Installation

Installed packages live in a store under the AgM home ([config.md](config.md)). An *immutable* installation is a content-hashed tree copied into the store; an *editable* installation mounts a live source tree. An activation index selects one version of each package globally; project `[packages]` pins override it per invocation. Dependency installation and validation share the same selection rule.

## Distribution

A source directory ships as its *distribution*: the normalized manifest plus files surviving gitignore and standard exclusions, with symlinked files copied as regular files. One module (`distribution.py`) owns that selection, so archives, store staging, and the content hash agree, and a directory and its archive install identically. The normalized manifest includes `@command` registrations ([packages.md](packages.md)), so an immutable installation carries a complete command table and is never rescanned; an editable one re-derives its commands from live source.

## Install and Uninstall

- Installation resolves the closure, stages the distribution beside its final path, verifies it, and publishes by atomic rename. `--reinstall` keeps the displaced tree until activation commits. Store and index changes are serialized by a store lock, and dry runs validate without persisting.
- Before activation is published, under the lock, command shadowing is checked and the active selection's Python requirements are synced into AgM's interpreter as one union (`python_deps.py`); failure rolls back. `agm pkg sync` re-syncs that union. Uninstall never removes Python distributions.
- `pkg switch NAME@VERSION` activates an existing tree without copying.
- Each installed tree carries a SHA-256 `RECORD` of the files the install created; uninstall removes exactly those, via a tombstone rename so interrupted cleanup is retryable. `RECORD` is verified only against an outside anchor; nothing re-hashes a package to read or run it.
- Archives (`.agmpkg`) are deterministic ZIPs read with bounded metadata, sizes, and depth. URL dependencies download through the shared HTTP seam ([core.md](core.md)) with digest check, size cap, and an inactivity timeout.

## The Managed `std` Package

The standard library ships as a managed store package whose version must exactly match the running AgM; only the shipped source can be installed, and it cannot be uninstalled. A development `std` checkout containing the anchored path outranks it; otherwise `agnette.stdlib_locator` supplies the shipped tree when the store is unpopulated. The selected tree's `src/` mounts as `std`. The selection carries its origin: the store and shipped trees are installed, a development checkout and `AGM_STDLIB` are local.

## Code Entry Points

- `src/agnette/packages/store.py`, `errors.py` — store layout, enumeration, lifecycle errors (no installation or compiler imports).
- `src/agnette/packages/distribution.py`, `record.py`, `archive.py` — distribution view, integrity records, archives.
- `src/agnette/packages/activation.py`, `install.py`, `fetch.py` — activation, lifecycle operations, downloads.
- `src/agnette/packages/python_deps.py` — Python-requirement union and sync.
- `src/agnette/packages/stdlib.py` — anchor-aware `std` selection; `src/agnette/stdlib_locator.py` — shipped fallback.
- `src/agnette/agm/commands/pkg/` — `install`, `switch`, `uninstall`, `list`, `info`, `create`, `check`, `sync`.
