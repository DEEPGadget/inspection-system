#!/usr/bin/env python3

import os
import sys
import json
import glob
import subprocess
import re


def get_network_interfaces():
    """Get network interface information"""
    try:
        interfaces = []
        net_path = '/sys/class/net'

        # Get all network interfaces
        all_ifaces = os.listdir(net_path)

        # Filter out virtual/excluded interfaces
        excluded_prefixes = ['lo', 'docker', 'veth', 'virbr', 'br-', 'tun', 'tap']
        physical_ifaces = []

        for iface in all_ifaces:
            if not any(iface.startswith(prefix) for prefix in excluded_prefixes):
                physical_ifaces.append(iface)

        up_interfaces = []
        down_interfaces = []

        for iface in physical_ifaces:
            try:
                # Get operstate
                with open(f'{net_path}/{iface}/operstate', 'r') as f:
                    operstate = f.read().strip()

                # Get speed
                speed_mbps = 0
                speed_unit = "unknown"
                try:
                    with open(f'{net_path}/{iface}/speed', 'r') as f:
                        speed_mbps = int(f.read().strip())

                    if speed_mbps >= 1000000:
                        speed_unit = f"{speed_mbps//1000000}TbE"
                    elif speed_mbps >= 1000:
                        speed_unit = f"{speed_mbps//1000}GbE"
                    else:
                        speed_unit = f"{speed_mbps}MbE"
                except Exception:
                    speed_unit = "unknown"

                # Get MTU
                mtu = 0
                try:
                    with open(f'{net_path}/{iface}/mtu', 'r') as f:
                        mtu = int(f.read().strip())
                except Exception:
                    mtu = 0

                iface_info = {
                    'name': iface,
                    'state': operstate,
                    'speed': speed_unit,
                    'mtu': mtu
                }

                if operstate == 'up':
                    up_interfaces.append(iface_info)
                else:
                    down_interfaces.append(iface_info)

            except Exception:
                continue

        return up_interfaces, down_interfaces
    except Exception:
        return [], []


def check_infiniband():
    """Check InfiniBand status"""
    try:
        result = subprocess.run(['ibstat'], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            port_count = 0
            active_count = 0

            for line in result.stdout.split('\n'):
                if 'Port ' in line and ':' in line:
                    port_count += 1
                elif 'State:' in line and 'Active' in line:
                    active_count += 1

            return port_count, active_count
        return 0, 0
    except Exception:
        return 0, 0


def check_loopback():
    """Check loopback connectivity"""
    try:
        result = subprocess.run(['ping', '-c1', '-W1', '127.0.0.1'],
                              capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def main():
    try:
        # Get network interfaces
        up_interfaces, down_interfaces = get_network_interfaces()

        # Check InfiniBand
        ib_ports, ib_active = check_infiniband()

        # Check loopback
        loopback_ok = check_loopback()

        # Count active NICs
        active_nic_count = len(up_interfaces)

        # Determine status
        status = "pass"
        if active_nic_count == 0:
            status = "fail"
        elif not loopback_ok:
            status = "fail"
        elif len(down_interfaces) > 0:
            status = "warn"
        elif ib_ports > 0 and ib_active == 0:
            status = "warn"

        # Build up/down interface strings
        up_ifaces_str = "|".join([f"{iface['name']}({iface['speed']},mtu{iface['mtu']})"
                                 for iface in up_interfaces]) if up_interfaces else "none"

        down_ifaces_str = "|".join([iface['name'] for iface in down_interfaces]) if down_interfaces else "none"

        # Build detail string
        detail_parts = [
            f"active_nics={active_nic_count}",
            f"up_ifaces={up_ifaces_str}",
            f"down_ifaces={down_ifaces_str}",
            f"ib_ports={ib_ports}",
            f"ib_active={ib_active}",
            f"loopback={'ok' if loopback_ok else 'fail'}"
        ]

        detail = "|".join(detail_parts)

        result = {
            "check": "sw_network",
            "status": status,
            "detail": detail
        }

        print(json.dumps(result))

    except Exception as e:
        # Log error to stderr and output fail status
        print(f"Error in sw_network check: {e}", file=sys.stderr)
        result = {
            "check": "sw_network",
            "status": "fail",
            "detail": "error=exception"
        }
        print(json.dumps(result))


if __name__ == "__main__":
    main()