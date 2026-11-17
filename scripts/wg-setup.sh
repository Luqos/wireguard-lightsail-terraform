#!/usr/bin/env bash
# WireGuard on UDP 443 for Amazon Lightsail (Ubuntu 24.04), with an egress port allowlist
#
# Usage:
#   sudo wg-setup install            # one-time server setup (Terraform runs this via user_data)
#   sudo wg-setup add <name>         # new peer (prints QR + saves .conf)
#   sudo wg-setup remove <name>      # revoke a peer
#   sudo wg-setup list               # show peers + last handshake
#   sudo wg-setup filter on|off      # toggle the Discord/VALORANT port allowlist
#   sudo wg-setup filter status      # show the active egress rules
#   sudo wg-setup stats              # per-peer usage as TSV (no keys), for the monitor app
#   sudo wg-setup monitor '<pubkey>' # read-only "wgmon" SSH account that can only run "stats"
#   sudo wg-setup admin '<pubkey>'   # "wgadmin" SSH account that can only add/remove peers
#   sudo wg-setup ssh-from <ip>...   # only these tunnel IPs may SSH in over wg0 (none = any peer)
#
# Internal (called through the wgadmin gate): api-add <name>, api-remove <name>
# Internal (called by wg-quick on interface up/down): fw-up, fw-down

set -euo pipefail

WG_DIR=/etc/wireguard
WG_CONF=$WG_DIR/wg0.conf
EGRESS_CONF=$WG_DIR/egress.conf
SSH_PEERS=$WG_DIR/ssh-peers   # tunnel IPs allowed to SSH in over wg0
WG_PORT=443
WG_NET=10.8.0
WG_NET_RE="${WG_NET//./\\.}"
CLIENT_DIR=/root/wg-clients
SELF=/usr/local/sbin/wg-setup
ADMIN_GATE=/usr/local/sbin/wg-admin-gate
ADMIN_LOG=/var/log/wg-admin.log
MAX_ADDS_PER_DAY=10
APT="apt-get -o DPkg::Lock::Timeout=300"

[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

reload() { wg syncconf wg0 <(wg-quick strip wg0); }
default_iface() { ip route show default | awk '{print $5; exit}'; }

write_egress_conf() {
  cat > "$EGRESS_CONF" <<'EOF'
# Egress allowlist for traffic leaving the tunnel.
# EGRESS_FILTER=1 -> only the ports below are forwarded; everything else is rejected.
# EGRESS_FILTER=0 -> everything is forwarded (plain full tunnel).
EGRESS_FILTER=1

# VALORANT ports: Riot Support, "How to Set Up Port Forwarding".
# Discord voice: commonly used UDP 50000-65535 (Discord publishes no official list).
# iptables multiport takes max 15 ports per rule; a range counts as 2.
#   TCP: DNS, HTTP, HTTPS, PVP.Net 2099/5222/5223, spectator 8088, installer 8393-8400
ALLOW_TCP="53,80,443,2099,5222,5223,8088,8393:8400"
#   UDP: DNS, STUN 3478, VALORANT game 7000-8000, spectator 8088, 8180-8181,
#        Discord voice + VALORANT AP/SEA voice (54000-54012, inside 50000-65535)
ALLOW_UDP="53,3478,7000:8000,8088,8180:8181,50000:65535"
EOF
  chmod 600 "$EGRESS_CONF"
}

fw_up() {
  local IFACE; IFACE=$(default_iface)
  # shellcheck source=/dev/null
  source "$EGRESS_CONF"
  iptables -t nat -A POSTROUTING -s "${WG_NET}.0/24" -o "$IFACE" -j MASQUERADE
  iptables -N WG_EGRESS 2>/dev/null || iptables -F WG_EGRESS
  iptables -A WG_EGRESS -o wg0 -j DROP                 # peers can't reach each other
  iptables -A WG_EGRESS -d 169.254.0.0/16 -j DROP      # peers can't reach instance metadata
  if [[ ${EGRESS_FILTER:-1} == 1 ]]; then
    iptables -A WG_EGRESS -p tcp -m multiport --dports "$ALLOW_TCP" -j ACCEPT
    iptables -A WG_EGRESS -p udp -m multiport --dports "$ALLOW_UDP" -j ACCEPT
    iptables -A WG_EGRESS -p icmp -j ACCEPT
    iptables -A WG_EGRESS -p tcp -j REJECT --reject-with tcp-reset
    iptables -A WG_EGRESS -j REJECT --reject-with icmp-port-unreachable
  else
    iptables -A WG_EGRESS -j ACCEPT
  fi
  iptables -I FORWARD 1 -i wg0 -j WG_EGRESS
  iptables -I FORWARD 2 -o wg0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT

  # SSH over the tunnel (how the monitor app connects). Limited to $SSH_PEERS once set.
  local PEER
  if [[ -s $SSH_PEERS ]]; then
    iptables -N WG_SSH 2>/dev/null || iptables -F WG_SSH
    for PEER in $(cat "$SSH_PEERS"); do iptables -A WG_SSH -s "$PEER" -j ACCEPT; done
    iptables -A WG_SSH -p tcp -j REJECT --reject-with tcp-reset
    iptables -I INPUT 1 -i wg0 -p tcp --dport 22 -j WG_SSH
  fi
}

fw_down() {
  local IFACE; IFACE=$(default_iface)
  iptables -t nat -D POSTROUTING -s "${WG_NET}.0/24" -o "$IFACE" -j MASQUERADE 2>/dev/null || true
  iptables -D FORWARD -i wg0 -j WG_EGRESS 2>/dev/null || true
  iptables -D FORWARD -o wg0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
  iptables -F WG_EGRESS 2>/dev/null || true
  iptables -X WG_EGRESS 2>/dev/null || true
  iptables -D INPUT -i wg0 -p tcp --dport 22 -j WG_SSH 2>/dev/null || true
  iptables -F WG_SSH 2>/dev/null || true
  iptables -X WG_SSH 2>/dev/null || true
}

show_ssh_peers() {
  if [[ -s $SSH_PEERS ]]; then
    echo "SSH over the tunnel allowed from: $(tr '\n' ' ' < "$SSH_PEERS")"
  else
    echo "SSH over the tunnel allowed from: any peer (still needs an SSH key)"
  fi
}

# "ssh-from 10.8.0.2" = only that peer; "ssh-from any" = every peer; no args = show.
set_ssh_peers() {
  local IP
  if (( $# == 0 )); then show_ssh_peers; return; fi
  if [[ $1 == any ]]; then
    rm -f "$SSH_PEERS"
  else
    for IP in "$@"; do
      [[ $IP =~ ^${WG_NET_RE}\.([2-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-4])$ ]] \
        || { echo "Not a peer address in ${WG_NET}.0/24: $IP" >&2; exit 1; }
    done
    printf '%s\n' "$@" > "$SSH_PEERS"
    chmod 600 "$SSH_PEERS"
  fi
  fw_down; fw_up
  show_ssh_peers
}

install_server() {
  if [[ -f $WG_CONF ]]; then echo "Already installed ($WG_CONF exists)."; exit 0; fi

  $APT update
  DEBIAN_FRONTEND=noninteractive $APT install -y wireguard qrencode iptables curl

  echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-wireguard.conf
  sysctl --system >/dev/null

  # Keep a copy at a fixed path so wg-quick's PostUp/PostDown can call it.
  [[ "$(readlink -f "$0")" == "$SELF" ]] || install -m 700 "$0" "$SELF"

  umask 077
  mkdir -p "$WG_DIR" "$CLIENT_DIR"
  wg genkey | tee "$WG_DIR/server.key" | wg pubkey > "$WG_DIR/server.pub"
  write_egress_conf

  cat > "$WG_CONF" <<EOF
[Interface]
Address = ${WG_NET}.1/24
ListenPort = ${WG_PORT}
PrivateKey = $(cat "$WG_DIR/server.key")
PostUp = ${SELF} fw-up
PostDown = ${SELF} fw-down
EOF

  systemctl enable --now wg-quick@wg0
  echo "Server up on UDP ${WG_PORT}, egress filter ON. Add peers: sudo wg-setup add <name>"
}

# Peer names end up in file paths and wg0.conf comments, so keep them boring.
valid_name() { [[ $1 =~ ^[a-z0-9][a-z0-9._-]{0,31}$ ]]; }
require_name() {
  valid_name "$1" || { echo "Bad name '$1': use a-z 0-9 . _ - (max 32, starting with a letter or digit)." >&2; exit 1; }
}

# Creates a peer; sets CONF (client config path) and PEER_IP.
create_peer() {
  local NAME=$1
  require_name "$NAME"
  [[ -f $WG_CONF ]] || { echo "Run install first." >&2; exit 1; }
  [[ -f $CLIENT_DIR/$NAME.conf ]] && { echo "Peer '$NAME' already exists." >&2; exit 1; }

  local LAST NEXT PRIV PUB PSK ENDPOINT
  LAST=$( (grep -oP "AllowedIPs = ${WG_NET_RE}\.\K[0-9]+" "$WG_CONF" || true) | sort -n | tail -1)
  NEXT=$(( ${LAST:-1} + 1 ))
  if (( NEXT > 254 )); then echo "Subnet full."; exit 1; fi

  umask 077
  PRIV=$(wg genkey)
  PUB=$(echo "$PRIV" | wg pubkey)
  PSK=$(wg genpsk)
  ENDPOINT=$(curl -s https://checkip.amazonaws.com)

  cat >> "$WG_CONF" <<EOF

# ${NAME}
[Peer]
PublicKey = ${PUB}
PresharedKey = ${PSK}
AllowedIPs = ${WG_NET}.${NEXT}/32
EOF

  cat > "$CLIENT_DIR/$NAME.conf" <<EOF
[Interface]
PrivateKey = ${PRIV}
Address = ${WG_NET}.${NEXT}/32
DNS = 1.1.1.1
MTU = 1280

[Peer]
PublicKey = $(cat "$WG_DIR/server.pub")
PresharedKey = ${PSK}
Endpoint = ${ENDPOINT}:${WG_PORT}
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
EOF

  reload
  CONF=$CLIENT_DIR/$NAME.conf
  PEER_IP=${WG_NET}.${NEXT}
}

add_peer() {
  local NAME=${1:?Give the peer a name, e.g. add luqos}
  create_peer "$NAME"
  echo "Peer '$NAME' -> ${PEER_IP}. Config: $CONF"
  echo "Scan with the WireGuard phone app:"
  qrencode -t ansiutf8 -m 1 < "$CONF"
}

remove_peer() {
  local NAME=${1:?Which peer? e.g. remove friend}
  require_name "$NAME"
  grep -Fqx "# ${NAME}" "$WG_CONF" || { echo "No peer '$NAME'." >&2; exit 1; }
  awk -v n="# ${NAME}" '$0==n{skip=1;next} skip && /^$/{skip=0} !skip' "$WG_CONF" > "$WG_CONF.new"
  mv "$WG_CONF.new" "$WG_CONF"; chmod 600 "$WG_CONF"
  rm -f "$CLIENT_DIR/$NAME.conf"
  reload
  echo "Removed '$NAME'."
}

set_filter() {
  case "${1:-status}" in
    on|off)
      local v=1
      if [[ $1 == off ]]; then v=0; fi
      sed -i "s/^EGRESS_FILTER=.*/EGRESS_FILTER=${v}/" "$EGRESS_CONF"
      fw_down; fw_up
      echo "Egress filter: $1" ;;
    status)
      grep '^EGRESS_FILTER' "$EGRESS_CONF"; iptables -S WG_EGRESS ;;
    *) echo "Usage: wg-setup filter on|off|status"; exit 1 ;;
  esac
}

# "wg show wg0 dump" would print the server private key and every preshared key,
# so stats joins the key-free views instead. Public keys are only used to join.
# Output: "now<TAB>epoch", then one "name ip handshake rx tx" line per peer.
peer_stats() {
  printf 'now\t%s\n' "$(date +%s)"
  awk -v OFS='\t' '
    FNR == 1                 { f++ }
    f == 1 && /^# /          { name = substr($0, 3) }
    f == 1 && $1 == "PublicKey"  { pk = $3 }
    f == 1 && $1 == "AllowedIPs" { ip = $3; sub(/\/.*/, "", ip)
                                   nm[pk] = (name == "" ? "?" : name); addr[pk] = ip
                                   order[++k] = pk; name = "" }
    f == 2                   { hs[$1] = $2 }
    f == 3                   { rx[$1] = $2; tx[$1] = $3 }
    END { for (i = 1; i <= k; i++) { p = order[i]; print nm[p], addr[p], hs[p] + 0, rx[p] + 0, tx[p] + 0 } }
  ' "$WG_CONF" <(wg show wg0 latest-handshakes) <(wg show wg0 transfer)
}

# A system account whose SSH key is pinned to one command: no shell, no port
# forwarding, no TTY. root owns its .ssh so it can't change its own restrictions.
# Re-run with a new key to rotate it.
restricted_account() {
  local ACCT=$1 FORCED=$2 KEY=$3 SUDO_RULE=$4
  local HOME_DIR=/home/$ACCT SUDOERS=/etc/sudoers.d/$ACCT
  [[ $KEY =~ ^ssh-ed25519\ [A-Za-z0-9+/]+=*(\ [A-Za-z0-9._@-]+)?$ ]] || { echo "Not an ed25519 public key."; exit 1; }

  id "$ACCT" &>/dev/null || useradd --system --create-home --home-dir "$HOME_DIR" --shell /bin/sh "$ACCT"
  passwd -l "$ACCT" >/dev/null
  install -d -m 755 -o root -g root "$HOME_DIR/.ssh"
  printf 'restrict,command="%s" %s\n' "$FORCED" "$KEY" > "$HOME_DIR/.ssh/authorized_keys"
  chmod 644 "$HOME_DIR/.ssh/authorized_keys"

  printf '%s ALL=(root) NOPASSWD: %s\n' "$ACCT" "$SUDO_RULE" > "$SUDOERS.tmp"
  visudo -cqf "$SUDOERS.tmp" || { rm -f "$SUDOERS.tmp"; echo "sudoers check failed."; exit 1; }
  install -m 440 "$SUDOERS.tmp" "$SUDOERS"; rm -f "$SUDOERS.tmp"
}

show_host_key() {
  echo "Compare this host key with the one the monitor app pins:"
  ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
}

# Read-only: sudo is limited to exactly "wg-setup stats".
setup_monitor() {
  restricted_account wgmon "sudo -n $SELF stats" \
    "${1:?Pass the monitor app public key, e.g. monitor 'ssh-ed25519 AAAA... wg-monitor'}" "$SELF stats"
  echo "wgmon ready."; show_host_key
}

# Add/remove peers from the monitor app. The key's forced command is a gate that
# only accepts "add <name>" or "remove <name>" and runs the api-* commands below.
# sudo allows nothing else. Every call is logged to syslog and $ADMIN_LOG.
setup_admin() {
  local KEY=${1:?Pass the admin public key, e.g. admin 'ssh-ed25519 AAAA... wg-admin'}
  cat > "$ADMIN_GATE.tmp" <<'EOF'
#!/bin/sh
# Forced command for the wgadmin SSH key (installed by "wg-setup admin").
# Accepts only "add <name>" or "remove <name>"; anything else is refused and logged.
set -eu
export LC_ALL=C
deny() { logger -t wg-admin "refused: $1"; echo "denied: $1" >&2; exit 2; }
cmd=${SSH_ORIGINAL_COMMAND:-}
case "$cmd" in *" "*) ;; *) deny "malformed command" ;; esac
action=${cmd%% *}
name=${cmd#* }
case "$action" in add|remove) ;; *) deny "unknown action" ;; esac
case "$name" in ""|*[!a-z0-9._-]*|[!a-z0-9]*) deny "bad name" ;; esac
[ "${#name}" -le 32 ] || deny "name too long"
exec sudo -n /usr/local/sbin/wg-setup "api-$action" "$name"
EOF
  install -m 755 -o root -g root "$ADMIN_GATE.tmp" "$ADMIN_GATE"; rm -f "$ADMIN_GATE.tmp"
  restricted_account wgadmin "$ADMIN_GATE" "$KEY" "$SELF api-add *, $SELF api-remove *"
  echo "wgadmin ready (max $MAX_ADDS_PER_DAY adds per 24 h, log: $ADMIN_LOG)."; show_host_key
}

audit() {
  printf '%s %s\n' "$(date +%s)" "$*" >> "$ADMIN_LOG"
  chmod 600 "$ADMIN_LOG"
  logger -t wg-admin "$*" || true
}

# Machine-readable add for the app: "ip", "conf" and "qr" (SVG) lines, base64 where needed.
api_add() {
  local NAME=${1:-} RECENT
  # The log doesn't exist before the first add; awk would exit 2 and, under pipefail, kill us silently.
  RECENT=0
  if [[ -f $ADMIN_LOG ]]; then
    RECENT=$(awk -v since="$(( $(date +%s) - 86400 ))" '$1 >= since && $2 == "add"' "$ADMIN_LOG" | wc -l)
  fi
  if (( RECENT >= MAX_ADDS_PER_DAY )); then
    audit "add-refused $NAME (rate limit)"
    echo "Rate limit: $MAX_ADDS_PER_DAY adds in the last 24 h. Use the browser SSH if this is really you." >&2
    exit 1
  fi
  create_peer "$NAME"
  audit "add $NAME"
  printf 'ip\t%s\n' "$PEER_IP"
  printf 'conf\t%s\n' "$(base64 -w0 < "$CONF")"
  printf 'qr\t%s\n' "$(qrencode -t SVG -m 2 -o - < "$CONF" | base64 -w0)"
}

api_remove() {
  local NAME=${1:-}
  remove_peer "$NAME" >/dev/null
  audit "remove $NAME"
  printf 'removed\t%s\n' "$NAME"
}

case "${1:-}" in
  install) install_server ;;
  add)     add_peer "${2:-}" ;;
  remove)  remove_peer "${2:-}" ;;
  list)    grep "^# " "$WG_CONF" | sed 's/^# /peer: /'; echo; wg show wg0 ;;
  filter)  set_filter "${2:-status}" ;;
  stats)   peer_stats ;;
  monitor) setup_monitor "${2:-}" ;;
  admin)   setup_admin "${2:-}" ;;
  ssh-from) shift; set_ssh_peers "$@" ;;
  api-add)    api_add "${2:-}" ;;
  api-remove) api_remove "${2:-}" ;;
  fw-up)   fw_up ;;
  fw-down) fw_down ;;
  *)       sed -n '2,17p' "$0" ;;
esac
