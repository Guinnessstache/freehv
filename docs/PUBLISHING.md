# Publishing FreeHV to GitHub

This repo is ready to publish as-is (screenshots, README, LICENSE, and
`.gitignore` are all in place). Pick one of the two methods below.

## Method 1 — GitHub CLI (one command)

If you have the [`gh` CLI](https://cli.github.com/) installed and authenticated
(`gh auth login`):

```sh
cd freehv            # the repo root (this folder)
git init
git add .
git commit -m "Initial commit: FreeHV — open-source KVM hypervisor manager"
git branch -M main
gh repo create Guinnessstache/freehv --public --source=. --remote=origin --push
```

That creates the public repo and pushes in one step.

## Method 2 — create the repo on github.com, then push

1. Go to <https://github.com/new>, name it **freehv**, set it **Public**, and
   create it **without** a README/license/.gitignore (this repo already has
   them).
2. Then run:

```sh
cd freehv
git init
git add .
git commit -m "Initial commit: FreeHV — open-source KVM hypervisor manager"
git branch -M main
git remote add origin https://github.com/Guinnessstache/freehv.git
git push -u origin main
```

## Automated installer ISO (so users never build it)

This repo includes `.github/workflows/build-iso.yml`. Once the repo is on
GitHub, it builds a ready-to-flash `freehv-installer.iso` **in the cloud** —
GitHub's runners can reach Debian's mirrors, download the current netinst,
remaster it with the FreeHV payload, and publish the result. End users just
download and flash it; no build step on their end.

How to cut a release with an attached ISO:

```sh
git tag v1.0.0
git push origin v1.0.0
```

The workflow then:
1. downloads the current Debian netinst ISO and verifies its SHA256,
2. runs `appliance/build-appliance.sh` to remaster it,
3. creates a GitHub Release for the tag and attaches `freehv-installer.iso`
   plus its `.sha256` checksum.

You can also trigger it manually from the repo's **Actions** tab
(workflow_dispatch); manual/branch runs upload the ISO as a downloadable
*workflow artifact* instead of a release asset.

> Note: the first time the workflow runs, the build itself is verified, but the
> resulting ISO should still get one real-hardware install test before you
> advertise the release widely.

## After publishing

- The README screenshots render automatically (they're committed under
  `docs/screenshots/`).
- Add topics on the repo page for discoverability: `kvm`, `libvirt`,
  `hypervisor`, `virtualization`, `vmware-alternative`, `self-hosted`.
- Consider enabling Issues and Discussions so others can report back.
- When you cut a release, attach a prebuilt `freehv-installer.iso` to a GitHub
  Release so people can skip the build step.

## Notes

- `.gitignore` keeps build artifacts (`*.iso`), Python caches, and runtime
  secrets (`auth.json`) out of the repo.
- The vendored noVNC client under `freehv-manager/static/novnc/` keeps its own
  MIT license file; that's fine to redistribute.
EOF
echo "PUBLISHING.md written"