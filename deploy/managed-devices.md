# Managed devices (admin guide)

A **managed device** is a machine your organization enrolled with brindle
Enterprise. Its **managed device file** carries a **device token** and a
**signed policy**, so the organization's rules apply there without anyone
signing in. This page is for org admins.

## Create the managed device file

Run, as an Enterprise admin signed in to brindle:

```sh
brindle org device-file --name "build laptop" [--allow-airgap] [--audit-sink URL] [-o PATH]
```

- `--name` labels the device in `--list`.
- `--allow-airgap` lets the device run in air-gap mode. Without it, air-gap
  mode is not allowed on the device.
- `--audit-sink URL` names where the device sends audit events: an `https://`
  URL, or a `udp://`, `tcp://` or `tls://` syslog collector.
- `-o PATH` is a directory or a file path. The default is `managed.json` in
  the current directory.

brindle checks the file's signatures before it writes it, and prints where
your MDM should install it. brindle never installs it for you. The file holds
the device token, which is shown only inside the file, so store it as you
would any secret (brindle writes it readable by you only). If the signed
policy inside the file has already lapsed, brindle says so; the device
fetches a fresh policy with its device token.

## Install it with your MDM

Deploy the file as `managed.json` into the system directory:

| OS      | Path                                                |
|---------|-----------------------------------------------------|
| macOS   | `/Library/Application Support/brindle/managed.json` |
| Linux   | `/etc/brindle/managed.json`                         |
| Windows | `%ProgramData%\brindle\managed.json`                |

The directory should be writable only by administrators.

## What applies on the device

- **The organization's rules apply whoever is signed in**, or nobody. The
  device needs no sign-in to be governed.
- **If a person signs in to a second org, the stricter rule wins.** Allowed
  lists are intersected, limits take the lower value, and protected paths,
  required items and denies from both are combined.
- **No sign-in lockout.** Work is never blocked for lack of a sign-in; the
  device policy is simply in force.
- **Air-gap mode only with `allow_airgap`.** A managed device may run in
  air-gap mode only when its file was created with `--allow-airgap`.
- **Offline policy files** on a managed device must be signed policy exports
  from your org (see below).

The device keeps its policy current with its device token, and uses the
newest valid signed policy it has.

## List and revoke device tokens

```sh
brindle org device-file --list
brindle org device-file --revoke DEVICE_ID
```

Revoking ends the device token's access to the org's policy. Listing and
revoking stay available to admins even after the Enterprise plan lapses.

## Offline policy export

For machines that cannot reach brindle, export the org's signed policy:

```sh
brindle account org policy --export [--days N]
```

`--days` is the export's lifetime, from 1 to 90 (default 30). The result is
written to `.brindle/policy.json` in the current repo. It is an **offline
policy export** signed by your org; copy it to the repos or machines that
need it, and export again before it expires.

`brindle account org policy` without `--export` shows the verified policy and
where it came from: the device (a copy fetched with the device token), the
managed file's snapshot, your account, or the offline file.

## Claude Code and gateways

brindle's agents inherit the company's own Claude Code setup. If your
organization sends Claude Code through its own gateway, proxy or cloud
provider, the agents brindle starts use that same setup; brindle adds no
separate route of its own.
