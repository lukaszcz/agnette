# Package Store and Installation

Installed packages live in a store under the AgM home ([config.md](config.md)), and inside a project optionally in a second, project-local store. An *immutable* installation is a content-hashed tree copied into the store; an *editable* installation mounts a live source tree. Each store's activation index selects one version of each package; project `[packages]` pins override the selection per invocation. Dependency installation and validation share the same selection rule.

## The Store Handle

`PackageStore(root, scope)` is the handle every store, activation, and install API takes in place of the AgM home; `StoreScope` is `GLOBAL` or `PROJECT`. It owns every path beneath its root: activation index, operation lock, package trees, provenance sidecars, and the `RECORD`s inside trees, so each store is self-contained. `context_layers(home, proj_dir, env)` is the single resolver of a context's `StoreLayers`: the project store (`<proj_dir>/packages`, created by the first `--project` install; `project_packages_dir` in `project/layout.py`) over the global one beneath the AgM home, or the global one alone outside a project. The config-aware selectors in `activation.py` and development discovery take layers; `global_store` is the global store alone (`std`, tooling), and `agm pkg` commands use `cli_support/pkg_store.py`. Enumeration skips dot-prefixed entries (bookkeeping, never package names) and linked directories. Callers outside the package domain build the handle at the call boundary.

## Distribution

A source directory ships as its *distribution*: the normalized manifest plus files surviving gitignore and standard exclusions, with symlinked files copied as regular files. One module (`distribution.py`) owns that selection, so archives, store staging, and the content hash agree, and a directory and its archive install identically. The normalized manifest includes `@command` registrations ([packages.md](packages.md)), so an immutable installation carries a complete command table and is never rescanned; an editable one re-derives its commands from live source.

## Install and Uninstall

- Installation resolves the closure, stages the distribution beside its final path, verifies it, and publishes by atomic rename. `--reinstall` keeps the displaced tree until activation commits. Store and index changes are serialized by a store lock, and dry runs validate without persisting.
- Before activation is published, under the lock, command shadowing is checked and the Python requirements a sync of the store covers are synced as one union (`python_deps.py`); failure rolls back. Coverage is cwd-independent: the store's index selection plus its packages that `[packages]` pins select from the install-prefix and AgM home layers, and for a project store the project layer too (`synced_packages`; never the invocation directory). A global store syncs into AgM's interpreter (`agm pkg sync` re-syncs; uninstall never removes distributions); a project store rebuilds its overlay.
- `pkg switch NAME@VERSION` activates an existing tree without copying.
- Every operation targets one store, `layers.stores[0]` (`--project` selects the project store). A project operation's layers also carry the global store beneath it: its active selection satisfies dependencies for names the project index does not select (`satisfying_inherited`) (missing path/URL ones install into the project store) and completes the requirement check of the effective selection; a global operation never sees project stores, so inside a project it only warns when the layered selection no longer validates. Non-install operations on an absent project store do nothing. `std` is global-only.
- Each installed tree carries a SHA-256 `RECORD` of the files the install created; uninstall removes exactly those, via a tombstone rename so interrupted cleanup is retryable. `RECORD` is verified only against an outside anchor; nothing re-hashes a package to read or run it.
- Archives (`.agmpkg`) are deterministic ZIPs read with bounded metadata, sizes, and depth. URL dependencies download through the shared HTTP seam ([core.md](core.md)) with digest check, size cap, and an inactivity timeout.

## The Managed `std` Package

The standard library ships as a managed store package whose version must exactly match the running AgM; only the shipped source can be installed, and it cannot be uninstalled. A development `std` checkout containing the anchored path outranks it; otherwise `agnette.stdlib_locator` supplies the shipped tree when the store is unpopulated. The selected tree's `src/` mounts as `std`. The selection carries its origin: the store and shipped trees are installed, a development checkout and `AGM_STDLIB` are local.

## Effective Selection

Inside a project the selection layers the stores: global index entries, then project index entries replacing same-named ones, then `[packages]` pins, each pin resolving in the project store first, then the global one; development packages precede all. Every selected `PackageInfo` records the `StoreScope` of the store that selected it. Project store trees are installed (warning-capped) unless editable. Development discovery treats trees of both stores as store entries, not checkouts. Resolutions are memoized per store root and selection stamp. Command registrations are layered the same way ([packages.md](packages.md)); `pkg list` and `pkg info` show this same effective selection (`effective_selections`, pins included; a pin to an uninstalled version fails) with a store label, and mark index selections it replaces as shadowed.

## The Project Python Overlay

A project store's requirements never touch AgM's interpreter. `<project store>/.python` is a symlink to a generation directory holding the joint install of the effective project-store selection (the project index plus project-store packages selected by pins, added to it rather than replacing it; packages selected only by other layers' pins are outside it, as for the global sync), rebuilt on project activation changes and by `pkg sync --project` (`python_deps.py`). A rebuild installs a new generation first (failure aborts before publication), the activation is then published, and only then one atomic rename switches the link; a failed publication discards the generation, and an empty union removes the link. A generation records its requirements and constraints, so an activation change that leaves both as built skips the rebuild (`sync` never does). The constraints pin every distribution of AgM's interpreter, so the overlay cannot shadow anything AgM has; if the interpreter later differs from the overlay, the overlay is *stale*: never put on `sys.path`, its requirements fail with the `pkg sync --project` hint.

A package's overlay is decided by the store resolving it (`requirement_store`; a development checkout, carrying no scope, by the store whose activation selects it). A process fixes one generation per store on first use and holds a shared lock on it; rebuilds delete an old generation only if they can lock it exclusively. At run time only the companions of such packages put the overlay on `sys.path` (ahead of site-packages), and requirement checks consult it before the interpreter's distributions. Global and `std` packages' checks never consult it directly, but once on `sys.path` it is process-wide, holding only versions pinned to what AgM's interpreter has.

## Code Entry Points

- `src/agnette/packages/store.py`, `errors.py` — `PackageStore`, enumeration, lifecycle errors (no installation or compiler imports).
- `src/agnette/packages/distribution.py`, `record.py`, `archive.py` — distribution view, integrity records, archives.
- `src/agnette/packages/activation.py`, `install.py`, `fetch.py` — activation, lifecycle operations, downloads.
- `src/agnette/packages/python_deps.py` — Python-requirement union and sync.
- `src/agnette/packages/stdlib.py` — anchor-aware `std` selection; `src/agnette/stdlib_locator.py` — shipped fallback.
- `src/agnette/agm/commands/pkg/` — `install`, `switch`, `uninstall`, `list`, `info`, `create`, `check`, `sync`.
