# Optional Linux gateway scripts

This is an alternative for gateways where native LAN destination NAT is unavailable. Prefer controller-managed rules when supported; do not install both methods. These scripts are not required for a UniFi native-policy setup.

For an `iptables` gateway, copy `deploy/router-rules.sh`, `deploy/router-rollback.sh`, and `deploy/routing.env.example` to `/data/gree-local/` on the gateway. Rename the environment example to `routing.env`, and set:

```dotenv
GREE_SERVER_IP=192.0.2.10
GREE_DEVICE_IP=192.0.2.20
GREE_DEVICE_MAC=aa:bb:cc:dd:ee:ff
GREE_LAN_CIDR=192.0.2.0/24
GREE_DNS_PORT=1053
GREE_DISPATCHER_PORT=1813
GREE_MQTT_PORT=1982
```

Use the colon-separated Wi-Fi MAC for `GREE_DEVICE_MAC`. Set `GREE_LAN_CIDR` to the local IPv4 network range you want to permit; it must include the Docker host. The script rejects other IPv4 forwarding from the AC and, when `ip6tables` is available, all IPv6 forwarding from its MAC. These example rules need review for gateways using nftables, hardware offloading, or different forwarding chains.

Apply on the gateway as root:

```sh
set -a
. /data/gree-local/routing.env
set +a
sh /data/gree-local/router-rules.sh
```

The script is idempotent. Reconnect the AC’s Wi-Fi or restart its Wi-Fi module if an existing cloud connection remains open. Confirm the local dispatcher and MQTT connection in the container logs, then test an MQTT command (or HA control) and its device readback. The local diagnostic interface is bound to container loopback on TCP 1983; do not publish it.

## OpenRC startup and rule maintenance

On a gateway running OpenRC, copy `deploy/router-watch.sh` to `/data/gree-local/` and install the init script:

```sh
install -m 755 deploy/gree-local-routing.openrc /etc/init.d/gree-local-routing
rc-update add gree-local-routing default
rc-service gree-local-routing start
```

Run the install command from a copy of this repository on the gateway. OpenRC supervises the foreground worker, which reloads `/data/gree-local/routing.env` and reapplies the rules every 30 seconds. Failed updates retry on the next cycle; a failed worker is restarted by `supervise-daemon`. Gateway upgrades may require reinstalling the init script.

Check or restart it with:

```sh
rc-service gree-local-routing status
rc-service gree-local-routing restart
```

Stopping the service stops rule maintenance but leaves the existing firewall rules in place. To restore cloud routing, stop and disable the service, then remove the project’s rules:

```sh
rc-service gree-local-routing stop
rc-update del gree-local-routing default
sh /data/gree-local/router-rollback.sh
```

Skip the OpenRC commands if the service was never installed. Rollback removes **all** rules tagged by this project, including rules for other configured ACs. Reconnect the AC afterward if necessary.

This service is only for the optional shell-rule setup. Native UniFi policies are maintained by the controller and do not need it.
