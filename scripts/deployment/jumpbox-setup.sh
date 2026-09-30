#!/bin/bash
# jumpbox-setup.sh
#
# Provisioning script for rsbc-ai-jumpbox, run once via the VM's
# CustomScriptExtension (see infrastructure/bicep/modules/compute/jumpbox-vm.bicep).
# Captures, as IaC, everything that was previously done to this VM by hand:
# an RDP-reachable desktop (both XFCE and GNOME, switchable), and the
# sb_queue_viewer.py debug tool pre-installed and ready to run.
#
# Runs as root (CustomScriptExtension's default) -- no sudo needed anywhere
# in this file.
#
# Arguments:
#   $1  adminUsername    -- the VM's local login user (the one who actually
#                            logs into the desktop over RDP; not the AAD
#                            identity used for `az network bastion ssh`,
#                            which is a different account -- see the
#                            "why a fixed home directory" note below).
#   $2  queueViewerRawUrl -- raw URL to fetch scripts/debug/sb_queue_viewer.py
#                            from. Points at `main` by default (see
#                            jumpbox-vm.bicep's default parameter value) --
#                            deliberately not pinned to a release/commit SHA,
#                            since this is an internal debug tool, not a
#                            production artifact; re-running this extension
#                            just picks up whatever's newest on the branch.
#
# Why a fixed home directory rather than $HOME/`~`: every step below writes
# to /home/$ADMIN_USERNAME explicitly. Earlier manual setup hit a real bug
# here -- commands run through an AAD-authenticated SSH session land in
# *that* identity's own home directory, not this VM's actual local RDP
# login user's home. Since this script runs via the VM extension (root,
# no session/HOME context tied to any particular login identity at all),
# using an explicit path sidesteps that whole class of bug rather than
# relying on $HOME resolving to the right place.
set -euo pipefail

ADMIN_USERNAME="${1:?adminUsername argument required}"
QUEUE_VIEWER_RAW_URL="${2:?queueViewerRawUrl argument required}"
HOME_DIR="/home/${ADMIN_USERNAME}"

export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# 1. Desktop environments -- both XFCE and GNOME, switchable (see step 4).
#    Neither is left running its own display manager on the console: this
#    VM only ever has RDP sessions, never a real console login, so gdm3/
#    lightdm fighting over the console (the "already have a console
#    session in progress" class of bug hit during manual setup) is avoided
#    entirely by disabling both consistently -- xrdp-sesman starts its own
#    X session per RDP connection independent of either.
# ---------------------------------------------------------------------------
apt-get update

apt-get install -y xfce4 xfce4-goodies xrdp

echo "gdm3 shared/default-x-display-manager select gdm3" | debconf-set-selections
apt-get install -y ubuntu-desktop gdm3

snap install firefox

systemctl disable --now gdm3 || true
systemctl disable --now lightdm || true

# ---------------------------------------------------------------------------
# 2. xrdp TLS fix.
#    Manual setup hit "Cannot read private key file /etc/xrdp/key.pem:
#    Permission denied", silently downgrading every session to unencrypted
#    RDP security. Root cause: the xrdp package's own postinst is supposed
#    to add the xrdp user to the ssl-cert group (which owns /etc/xrdp/key.pem)
#    -- this step makes sure that actually happened, rather than replaying
#    the same bug into a fresh VM.
# ---------------------------------------------------------------------------
adduser xrdp ssl-cert

# ---------------------------------------------------------------------------
# 3. Default session: XFCE.
#    Proven stable across this whole setup; GNOME is left available as an
#    equally-supported option (switch-desktop.sh), not a fallback.
# ---------------------------------------------------------------------------
echo "xfce4-session" > "${HOME_DIR}/.xsession"

# ---------------------------------------------------------------------------
# 4. switch-desktop.sh -- flips which session xrdp launches on next login.
#    Kept as a real script on the VM (not a one-off manual edit) so
#    switching back and forth stays a documented, repeatable action rather
#    than tribal knowledge from this session's chat history.
# ---------------------------------------------------------------------------
cat > "${HOME_DIR}/switch-desktop.sh" << 'SWITCHEOF'
#!/bin/bash
# Usage: ./switch-desktop.sh xfce|gnome
# Changes which desktop xrdp launches for this user's next RDP login.
# Log out and reconnect afterwards -- an already-open RDP session keeps
# running whatever it started with.
set -euo pipefail
case "${1:-}" in
  xfce)
    echo "xfce4-session" > "$HOME/.xsession"
    echo "Switched to XFCE. Reconnect via RDP to take effect."
    ;;
  gnome)
    echo 'env GNOME_SHELL_SESSION_MODE=ubuntu /usr/bin/gnome-session --session=ubuntu' > "$HOME/.xsession"
    echo "Switched to GNOME. Reconnect via RDP to take effect."
    ;;
  *)
    echo "Usage: $0 xfce|gnome" >&2
    exit 1
    ;;
esac
SWITCHEOF
chmod +x "${HOME_DIR}/switch-desktop.sh"

# ---------------------------------------------------------------------------
# 5. Python venv + sb_queue_viewer.py's dependencies.
#    --system-site-packages keeps python3-tk (needed for the GUI, installed
#    system-wide) visible from inside the venv while still isolating the
#    Azure SDK packages -- see sb_queue_viewer.py's own docstring for why.
# ---------------------------------------------------------------------------
apt-get install -y python3-tk python3-venv

python3 -m venv --system-site-packages "${HOME_DIR}/sbvenv"
"${HOME_DIR}/sbvenv/bin/pip" install --upgrade pip
"${HOME_DIR}/sbvenv/bin/pip" install azure-servicebus azure-identity

# ---------------------------------------------------------------------------
# 6. sb_queue_viewer.py itself -- fetched from source control, not
#    copy-pasted. See this script's header for why the URL isn't pinned to
#    a release/commit for this particular (internal, low-stakes) tool.
# ---------------------------------------------------------------------------
curl -fsSL "${QUEUE_VIEWER_RAW_URL}" -o "${HOME_DIR}/sb_queue_viewer.py"

# ---------------------------------------------------------------------------
# 7. Desktop shortcut -- app-grid/Whisker-menu entry + Desktop icon, both
#    desktop environments read the same ~/.local/share/applications entry.
# ---------------------------------------------------------------------------
mkdir -p "${HOME_DIR}/.local/share/applications" "${HOME_DIR}/Desktop"
cat > "${HOME_DIR}/.local/share/applications/sb-queue-viewer.desktop" << DESKTOPEOF
[Desktop Entry]
Type=Application
Name=SB Queue Viewer
Comment=Peek/receive DMER pipeline Service Bus queue messages
Exec=${HOME_DIR}/sbvenv/bin/python ${HOME_DIR}/sb_queue_viewer.py
Icon=utilities-terminal
Terminal=false
Categories=Utility;
DESKTOPEOF
chmod +x "${HOME_DIR}/.local/share/applications/sb-queue-viewer.desktop"
cp "${HOME_DIR}/.local/share/applications/sb-queue-viewer.desktop" "${HOME_DIR}/Desktop/"
chmod +x "${HOME_DIR}/Desktop/sb-queue-viewer.desktop"

# ---------------------------------------------------------------------------
# 8. Fix ownership -- everything above was written as root.
# ---------------------------------------------------------------------------
chown -R "${ADMIN_USERNAME}:${ADMIN_USERNAME}" \
  "${HOME_DIR}/.xsession" \
  "${HOME_DIR}/switch-desktop.sh" \
  "${HOME_DIR}/sbvenv" \
  "${HOME_DIR}/sb_queue_viewer.py" \
  "${HOME_DIR}/.local" \
  "${HOME_DIR}/Desktop"

systemctl restart xrdp
systemctl restart xrdp-sesman

echo "jumpbox-setup.sh complete."
