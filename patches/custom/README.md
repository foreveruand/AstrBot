# Custom patch queue

This directory stores the local AstrBot custom patch queue. The patches are
generated from `custom-patch` and are meant to be applied on top of
`upstream/master`.

## Apply

```bash
scripts/custom_patches/check.sh
scripts/custom_patches/apply.sh
```

`apply.sh` applies every patch listed in `patches/custom/series` with
`git apply --3way --index` and commits each patch as a separate conventional
commit.

## Sync with upstream

```bash
git fetch upstream
git switch master
git reset --hard upstream/master
scripts/custom_patches/check.sh
scripts/custom_patches/apply.sh
```

Only use `git reset --hard` after confirming there is no local work to keep.
If `check.sh` reports conflicts, resolve them on a temporary branch, update the
`custom-patch` source branch, then run:

```bash
scripts/custom_patches/refresh.sh
scripts/custom_patches/check.sh
```

## Refresh

```bash
scripts/custom_patches/refresh.sh
```

By default, `refresh.sh` regenerates this queue from `custom-patch` onto
`upstream/master`. Override the refs when needed:

```bash
SOURCE_REF=my-custom-branch BASE_REF=upstream/master scripts/custom_patches/refresh.sh
```

The queue is split into dependency-safe topic stages. Some commits touch more
than one subsystem, so the generated stages preserve the original dependency
order instead of cherry-picking non-contiguous topics independently.

## Remove

```bash
scripts/custom_patches/remove.sh
```

`remove.sh` reverses the queue in reverse `series` order using
`git apply -R --3way --index`, then creates one removal commit. The patch
maintenance files remain in the repository.
