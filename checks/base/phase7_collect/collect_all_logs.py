#!/usr/bin/env python3

import os
import sys
import json
import subprocess
import re


def get_dmesg_errors():
    """Get error and warning counts from dmesg"""
    try:
        # Get error/critical messages (tail 20)
        result_err = subprocess.run(['dmesg', '--level=err,crit,alert,emerg'],
                                  capture_output=True, text=True, timeout=10)
        error_lines = 0
        if result_err.returncode == 0:
            lines = result_err.stdout.strip().split('\n')
            # Get last 20 lines
            error_lines = len([line for line in lines[-20:] if line.strip()])

        # Get warning messages (tail 50)
        result_warn = subprocess.run(['dmesg', '--level=warn'],
                                   capture_output=True, text=True, timeout=10)
        warn_lines = 0
        if result_warn.returncode == 0:
            lines = result_warn.stdout.strip().split('\n')
            # Get last 50 lines
            warn_lines = len([line for line in lines[-50:] if line.strip()])

        return error_lines, warn_lines
    except Exception:
        return 0, 0


def get_dmesg_special_events():
    """Get special events from dmesg"""
    try:
        result = subprocess.run(['dmesg'], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return 0, 0, 0, []

        content = result.stdout
        lines = content.split('\n')

        # Count MCE/machine check events
        mce_count = 0
        for line in lines:
            if re.search(r'(mce|machine.check)', line, re.IGNORECASE):
                mce_count += 1

        # Count OOM events
        oom_count = 0
        for line in lines:
            if re.search(r'(Out of memory|oom.killer)', line, re.IGNORECASE):
                oom_count += 1

        # Count GPU reset events
        gpu_reset_count = 0
        for line in lines:
            if re.search(r'(GPU.reset|reset GPU)', line, re.IGNORECASE):
                gpu_reset_count += 1

        # Extract NVIDIA XID errors
        xid_numbers = []
        for line in lines:
            match = re.search(r'NVRM.*Xid.*?(\d+)', line, re.IGNORECASE)
            if match:
                xid_numbers.append(match.group(1))

        return mce_count, oom_count, gpu_reset_count, xid_numbers
    except Exception:
        return 0, 0, 0, []


def get_journal_errors():
    """Get error count from systemd journal (last 24h)"""
    try:
        result = subprocess.run(['journalctl', '-p', 'err', '--since', '24h ago',
                               '--no-pager', '-q'],
                              capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            return len([line for line in lines if line.strip()])
        return 0
    except Exception:
        return 0


def get_reboot_info():
    """Get reboot information"""
    try:
        result = subprocess.run(['last', 'reboot'], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return 0, "unknown"

        lines = result.stdout.strip().split('\n')
        reboot_count = 0
        last_reboot = "unknown"

        for line in lines:
            if 'reboot' in line and 'system boot' in line:
                reboot_count += 1
                if last_reboot == "unknown":
                    # Extract date from first reboot line
                    parts = line.split()
                    if len(parts) >= 5:
                        last_reboot = f"{parts[3]}_{parts[4]}_{parts[5]}"

        return reboot_count, last_reboot
    except Exception:
        return 0, "unknown"


def get_file_handle_info():
    """Get file handle information"""
    try:
        # Get file-max
        with open('/proc/sys/fs/file-max', 'r') as f:
            file_max = int(f.read().strip())

        # Get current file handles
        with open('/proc/sys/fs/file-nr', 'r') as f:
            file_nr_parts = f.read().strip().split()
            file_used = int(file_nr_parts[0])

        return file_max, file_used
    except Exception:
        return 0, 0


def get_load_average():
    """Get load average ratio"""
    try:
        # Get load average
        with open('/proc/loadavg', 'r') as f:
            load_parts = f.read().strip().split()
            load_1min = float(load_parts[0])

        # Get number of processors
        nproc_result = subprocess.run(['nproc'], capture_output=True, text=True, timeout=5)
        if nproc_result.returncode == 0:
            nproc = int(nproc_result.stdout.strip())
            load_ratio = round(load_1min / nproc, 3)
        else:
            load_ratio = 0.0

        return load_1min, load_ratio
    except Exception:
        return 0.0, 0.0


def main():
    try:
        # Get dmesg error/warning counts
        dmesg_errors, dmesg_warnings = get_dmesg_errors()

        # Get special dmesg events
        mce_count, oom_count, gpu_reset_count, xid_numbers = get_dmesg_special_events()

        # Get journal errors
        journal_errors = get_journal_errors()

        # Get reboot info
        reboot_count, last_reboot = get_reboot_info()

        # Get file handle info
        file_max, file_used = get_file_handle_info()

        # Get load average
        load_1min, load_ratio = get_load_average()

        # Determine status
        status = "pass"
        if mce_count > 0:
            status = "warn"
        elif oom_count > 0:
            status = "warn"
        elif len(xid_numbers) > 0:
            status = "warn"
        elif journal_errors > 100:
            status = "warn"
        elif load_ratio >= 0.9:
            status = "warn"

        # Build detail string
        detail_parts = [
            f"dmesg_errors={dmesg_errors}",
            f"dmesg_warnings={dmesg_warnings}",
            f"mce_count={mce_count}",
            f"oom_count={oom_count}",
            f"journal_errors={journal_errors}",
            f"xid_count={len(xid_numbers)}",
            f"xid_numbers={','.join(xid_numbers) if xid_numbers else 'none'}",
            f"gpu_reset_count={gpu_reset_count}",
            f"reboot_count={reboot_count}",
            f"last_reboot={last_reboot}",
            f"file_max={file_max}",
            f"file_used={file_used}",
            f"load_1min={load_1min}",
            f"load_ratio={load_ratio}"
        ]

        detail = "|".join(detail_parts)

        result = {
            "check": "collect_all_logs",
            "status": status,
            "detail": detail
        }

        print(json.dumps(result))

    except Exception as e:
        # Log error to stderr and output fail status
        print(f"Error in collect_all_logs check: {e}", file=sys.stderr)
        result = {
            "check": "collect_all_logs",
            "status": "fail",
            "detail": "error=exception"
        }
        print(json.dumps(result))


if __name__ == "__main__":
    main()