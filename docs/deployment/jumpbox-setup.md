
# Jump box setup

`rsbc-ai-jumpbox` exists for exactly one reason: reaching Azure resources
with `publicNetworkAccess: Disabled` (Service Bus, Document Intelligence)
from a real desktop session. Neither the Azure Portal nor a laptop with no
VNet route can reach a private endpoint directly — see
`docs/deployment/deployment-guide.md`'s "Recommended replacement for ad hoc
tunnelling" section for the same reasoning applied to Document
Intelligence Studio.

It is a debug/access tool, not part of the DMER pipeline itself. The only
thing it needs to keep working is a route to those private endpoints and a
desktop session to run `sb_queue_viewer.py` (`scripts/debug/`) in.

## Deploying it

```bash
az deployment group what-if \
  --resource-group rsbc-dmer-ai-optimization-rg \
  --template-file infrastructure/bicep/jumpbox.bicep \
  --parameters @deployment/dev/jumpbox.parameters.json \
  --parameters adminPassword="<supply via pipeline secret, not a parameters.json file>"

# Repeat with `create` once the plan looks right.
```

Same control-plane-only methodology as `docs/deployment/deployment-guide.md`
— no VNet route needed to run this, only to actually use the RDP/SSH
sessions it creates afterwards.

**This template captures the setup, it does not adopt the specific VM and
Bastion host that already exist.** Both were originally created by hand,
before this Bicep existed — `jumpbox.bicep` is written so a *fresh* jump
box can be reproduced from the repo alone, not to reconcile itself against
whatever's already live. Fill in `bastionSubnetAddressPrefix` in
`deployment/dev/jumpbox.parameters.json` (verify a free `/26`+ against
`az network vnet subnet list` on the target VNet first) before actually
deploying this against a real environment.

Not created here, platform-policy-owned: `AzureMonitorLinuxAgent`,
`AzurePolicyforLinux`, `ChangeTracking-Linux` — this landing zone's own
Azure Policy Deploy-if-not-exists rules attach these to any VM
automatically.

## What gets provisioned on the VM

`scripts/deployment/jumpbox-setup.sh`, run once via a `CustomScript`
extension (see `jumpbox-vm.bicep`), sets up:

- **Both XFCE and GNOME**, switchable — see below. Neither desktop's own
  display manager (`gdm3`/`lightdm`) is left running on the console; this
  VM only ever has RDP sessions, never a real console login, so disabling
  both avoids the "already have a console session in progress" class of
  bug entirely, rather than fighting over which one owns `:0`.
- **The xrdp TLS fix** (`adduser xrdp ssl-cert`) — manual setup on the
  original VM hit `Cannot read private key file /etc/xrdp/key.pem:
  Permission denied`, silently downgrading every session to unencrypted
  RDP security. This is the actual upstream fix, not a workaround.
- **A Python venv + `sb_queue_viewer.py` itself**, fetched from this
  repo's own raw GitHub URL (public repo, no auth needed) rather than
  copy-pasted by hand — see the script's own docstring and
  `scripts/debug/README.md`.
- **A desktop shortcut** for it, in both the app-grid/Whisker-menu and on
  the Desktop.
- **`switch-desktop.sh`**, in the login user's home directory.

## Switching between XFCE and GNOME

```bash
./switch-desktop.sh xfce
./switch-desktop.sh gnome
```

Log out and reconnect afterwards — an already-open RDP session keeps
running whatever it started with.

Both are genuinely supported, not "XFCE with GNOME as a fallback." Neither
is meaningfully faster than the other on this VM — see "Known issues"
below for why, and don't expect switching desktops to fix it.

## Known issues (found while using this VM — not yet resolved)

**RDP feels laggy regardless of desktop environment, and it isn't
GNOME's compositor.** Diagnosed by watching `top` on the VM while
dragging a window: `xrdp` itself — the screen-capture-and-encode
process — spikes CPU, not `Xorg` or `gnome-shell`. That's why XFCE
(which has no compositor at all) was just as laggy as GNOME: the cost is
in xrdp's own encoding pipeline, not in whichever window manager is
drawing above it.

- A GPU would **not** fix this — a GPU only accelerates rendering, and
  the bottleneck isn't rendering.
- Lowering `max_bpp` in `/etc/xrdp/xrdp.ini` was tried as a bounded,
  reversible mitigation — **it made things worse**: GNOME Shell's
  compositor appears unable to run at 16bpp and exits immediately after
  connecting (a clean `exit code 0`, no crash signal — just an
  unsupported color depth), tearing down the whole RDP session with no
  client-visible error. Left at `32` (the default) in `jumpbox-setup.sh`.
- The client (Windows' RDP client) offers a modern codec xrdp logs as
  `unknown codec id 5` — likely the H.264/AVC444 "Graphics Pipeline"
  codec, a feature added to xrdp later than the `0.9.24` version this VM
  runs. RemoteFX encode support (`librfxencode`) is present in this
  xrdp build (confirmed via `dpkg -L xrdp`), but it's never actually
  negotiated in practice — `/etc/xrdp/xrdp.ini` has no explicit toggle
  for it, and getting it working would mean digging into xrdp's
  negotiation/build internals rather than a config change. Left
  unresolved — the same category of open-ended investigation as the
  item below, and not worth chasing further without a specific reason to.

**Dynamic resolution/resize doesn't work.** Confirmed via direct
`xrandr` testing — the session stays at `800x600` regardless of the RDP
window's actual size, despite the Xorg backend reporting a much larger
maximum. Root-caused to the `xrdp`/`xorgxrdp` combination itself, not a
client-side setting. Not fixed — chasing it further looked like an
open-ended `xrdp`/`xorgxrdp` version/configuration investigation with no
clear end, so it was deliberately abandoned in favour of just using a
fixed resolution.

**RDP client-side drive redirection is blocked by Group Policy**, not by
anything on the VM/Bastion side — confirmed by the "Drives" option being
greyed out (disabled, not merely unchecked) in mstsc's Local Resources
dialog, which is exactly how Windows presents a policy-blocked option.
Use `scp` over an `az network bastion tunnel` instead for moving files
onto the VM — see the next section.

## Getting files onto the VM

Drive redirection is blocked (see above), and large multi-line pastes into
an SSH terminal are unreliable (heredocs can silently corrupt over a slow
or lossy remote terminal). `scp` through a Bastion tunnel is the reliable
path:

```bash
az network bastion tunnel --name rsbc-dmer-ai-bastion --resource-group rsbc-dmer-ai-optimization-rg --target-resource-id <vm-resource-id> --resource-port 22 --port 2222 &
TUNNEL_PID=$!
scp -P 2222 <local-file> rsbc-dmer@127.0.0.1:<destination-path>
kill $TUNNEL_PID
```

Prompts for the local admin account's password (set via `sudo passwd
<username>` on the VM if it's never been set).

## A recurring gotcha: which home directory

Two different identities exist on this VM, with two different home
directories:

- **The local admin account** (`adminUsername`, e.g. `rsbc-dmer`) — the
  actual RDP login user. Its home directory is what `jumpbox-setup.sh`
  writes to, and what actually matters for the desktop session.
- **The AAD identity** used by `az network bastion ssh --auth-type AAD` —
  a completely different account, with its own separate home directory.

Anything created by hand through an AAD SSH session (rather than through
`jumpbox-setup.sh`, which runs as root with explicit paths) lands in the
*AAD identity's* home directory, not the RDP login user's — this caused
real, repeated confusion during manual setup (e.g. a `.xsession` written
to the wrong home directory, which GNOME/XFCE never picks up). If you're
troubleshooting something that "isn't taking effect," check `whoami` and
`echo $HOME` in whichever session you're actually typing into before
assuming the change itself is wrong.

## Using sb_queue_viewer.py

See the script's own docstring (`scripts/debug/sb_queue_viewer.py`) for
its Peek vs. Receive (Peek Lock / Receive and Delete) modes, multi-select
Complete/Abandon/Dead-letter actions, and why it authenticates via
`ManagedIdentityCredential` rather than an interactive login. The VM's
managed identity is granted **Azure Service Bus Data Receiver** on
`sb-rsbc-dmer-shared-dev-001` by `jumpbox.bicep` — sufficient for
everything the tool does (peek, receive, complete, abandon, dead-letter).
