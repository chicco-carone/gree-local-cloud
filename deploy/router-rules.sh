#!/bin/sh
# Optional fallback for an iptables gateway, run as root.
# Configure one AC per invocation; prefer native controller policies when available.
set -eu
SERVER_IP=${GREE_SERVER_IP:?Set GREE_SERVER_IP to the Docker host LAN address}
AC_IP=${GREE_DEVICE_IP:?Set GREE_DEVICE_IP to the AC IPv4 address}
AC_MAC=${GREE_DEVICE_MAC:?Set GREE_DEVICE_MAC to the colon-separated AC MAC}
LAN_CIDR=${GREE_LAN_CIDR:?Set GREE_LAN_CIDR to your local IPv4 network CIDR}
TAG=gree-local-cloud
ensure() {
    table=$1; chain=$2; shift 2
    iptables -t "$table" -C "$chain" "$@" 2>/dev/null || iptables -t "$table" -I "$chain" 1 "$@"
}
install_device() {
    ac_ip=$1; ac_mac=$2; dns_port=$3; dispatcher_port=$4; mqtt_port=$5
    for port in 1982 1992; do
        ensure nat PREROUTING -s "$ac_ip" -p tcp --dport "$port" -m comment --comment "$TAG" -j DNAT --to-destination "$SERVER_IP:$mqtt_port"
    done
    ensure nat PREROUTING -s "$ac_ip" -p tcp --dport 1813 -m comment --comment "$TAG" -j DNAT --to-destination "$SERVER_IP:$dispatcher_port"
    ensure nat PREROUTING -s "$ac_ip" -p udp --dport 53 -m comment --comment "$TAG" -j DNAT --to-destination "$SERVER_IP:$dns_port"
    ensure filter FORWARD -s "$ac_ip" ! -d "$LAN_CIDR" -m comment --comment "$TAG-offline" -j REJECT
    ensure filter FORWARD -s "$ac_ip" -d "$SERVER_IP" -p tcp -m multiport --dports "$dispatcher_port,$mqtt_port" -m comment --comment "$TAG" -j ACCEPT
    ensure filter FORWARD -s "$ac_ip" -d "$SERVER_IP" -p udp --dport "$dns_port" -m comment --comment "$TAG" -j ACCEPT
    if command -v ip6tables >/dev/null 2>&1; then
        ip6tables -C FORWARD -m mac --mac-source "$ac_mac" -m comment --comment "$TAG-offline" -j REJECT 2>/dev/null ||
            ip6tables -I FORWARD 1 -m mac --mac-source "$ac_mac" -m comment --comment "$TAG-offline" -j REJECT
    fi
}
install_device "$AC_IP" "$AC_MAC" "${GREE_DNS_PORT:-1053}" "${GREE_DISPATCHER_PORT:-1813}" "${GREE_MQTT_PORT:-1982}"
