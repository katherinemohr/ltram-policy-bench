#!/usr/bin/env python3
"""
Monitor read-only vs read-write memory in processes.
Tracks actual write access patterns using soft-dirty PTEs.
"""

import argparse
import os
import struct
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Set


@dataclass
class MemoryRegion:
    start: int
    end: int
    perms: str
    offset: int
    dev: str
    inode: int
    pathname: str
    size: int
    rss: int  # Resident set size

    def __hash__(self):
        return hash((self.start, self.end, self.pathname))


class MemoryMonitor:
    def __init__(self, pid: int, interval: float = 1.0):
        self.pid = pid
        self.interval = interval
        self.previous_regions: Set[MemoryRegion] = set()
        self.soft_dirty_support = self._check_soft_dirty()

    def _check_soft_dirty(self) -> bool:
        """Check if kernel supports soft-dirty tracking"""
        try:
            with open(f"/proc/{self.pid}/clear_refs", "w") as f:
                f.write("4\n")  # Clear soft-dirty bits
            return True
        except (IOError, PermissionError) as e:
            print(f"Warning: Soft-dirty tracking not available: {e}", file=sys.stderr)
            return False

    def _parse_maps(self) -> List[MemoryRegion]:
        """Parse /proc/[pid]/maps"""
        regions = []
        try:
            with open(f"/proc/{self.pid}/maps", "r") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 5:
                        continue

                    addr_range = parts[0].split("-")
                    start = int(addr_range[0], 16)
                    end = int(addr_range[1], 16)
                    perms = parts[1]
                    offset = int(parts[2], 16)
                    dev = parts[3]
                    inode = int(parts[4])
                    pathname = parts[5] if len(parts) > 5 else ""

                    regions.append(
                        MemoryRegion(
                            start=start,
                            end=end,
                            perms=perms,
                            offset=offset,
                            dev=dev,
                            inode=inode,
                            pathname=pathname,
                            size=end - start,
                            rss=0,  # Will fill from smaps
                        )
                    )
        except FileNotFoundError:
            print(f"Process {self.pid} no longer exists")
            sys.exit(1)

        return regions

    def _get_smaps_data(self) -> Dict[tuple, dict]:
        """Parse /proc/[pid]/smaps for detailed memory info"""
        smaps_data = {}
        current_region = None
        current_data = {}

        try:
            with open(f"/proc/{self.pid}/smaps", "r") as f:
                for line in f:
                    if "-" in line and len(line.split()) >= 5:
                        # New region header
                        if current_region:
                            smaps_data[current_region] = current_data

                        parts = line.split()
                        addr_range = parts[0].split("-")
                        current_region = (
                            int(addr_range[0], 16),
                            int(addr_range[1], 16),
                        )
                        current_data = {}
                    elif ":" in line:
                        # Data line
                        key, value = line.split(":", 1)
                        key = key.strip()
                        value = value.strip().split()[0] if value.strip() else "0"
                        try:
                            current_data[key] = int(value)
                        except ValueError:
                            current_data[key] = value

                if current_region:
                    smaps_data[current_region] = current_data
        except FileNotFoundError:
            pass

        return smaps_data

    def _check_soft_dirty_pages(self, region: MemoryRegion) -> tuple[int, int]:
        """
        Check how many pages in a region are soft-dirty (written to).
        Returns (total_pages, dirty_pages)
        """
        if not self.soft_dirty_support:
            return (0, 0)

        page_size = 4096
        total_pages = (region.end - region.start) // page_size
        dirty_pages = 0

        try:
            with open(f"/proc/{self.pid}/pagemap", "rb") as f:
                # Each page has 8 bytes in pagemap
                offset = (region.start // page_size) * 8
                f.seek(offset)

                for _ in range(total_pages):
                    data = f.read(8)
                    if len(data) < 8:
                        break

                    entry = struct.unpack("Q", data)[0]
                    # Bit 55: soft-dirty
                    if entry & (1 << 55):
                        dirty_pages += 1
        except (IOError, PermissionError):
            pass

        return (total_pages, dirty_pages)

    def clear_soft_dirty(self):
        """Clear soft-dirty bits to start fresh tracking"""
        if self.soft_dirty_support:
            try:
                with open(f"/proc/{self.pid}/clear_refs", "w") as f:
                    f.write("4\n")
            except (IOError, PermissionError):
                pass

    def monitor_iteration(self):
        """Single monitoring iteration"""
        regions = self._parse_maps()
        smaps_data = self._get_smaps_data()

        # Enhance regions with smaps data
        for region in regions:
            key = (region.start, region.end)
            if key in smaps_data:
                region.rss = smaps_data[key].get("Rss", 0)

        current_regions = set(regions)

        # Detect allocations
        new_regions = current_regions - self.previous_regions
        for region in new_regions:
            print(
                f"[ALLOC] {region.perms} {region.size:12} bytes @ 0x{region.start:016x}-0x{region.end:016x} {region.pathname}"
            )

        # Detect deallocations
        freed_regions = self.previous_regions - current_regions
        for region in freed_regions:
            print(
                f"[FREE ] {region.perms} {region.size:12} bytes @ 0x{region.start:016x}-0x{region.end:016x} {region.pathname}"
            )

        # Calculate statistics
        total_size = 0
        total_rss = 0
        readonly_size = 0
        readonly_rss = 0
        readonly_never_written_size = 0
        readonly_never_written_rss = 0

        for region in regions:
            total_size += region.size
            total_rss += region.rss * 1024  # RSS is in KB

            # Check if read-only (r--p or r-xp)
            if region.perms.startswith("r") and region.perms[1] != "w":
                readonly_size += region.size
                readonly_rss += region.rss * 1024

                # Check if actually written (soft-dirty)
                if self.soft_dirty_support:
                    total_pages, dirty_pages = self._check_soft_dirty_pages(region)
                    if total_pages > 0:
                        clean_ratio = (total_pages - dirty_pages) / total_pages
                        never_written = region.rss * 1024 * clean_ratio
                        readonly_never_written_rss += never_written
                        readonly_never_written_size += region.size * clean_ratio

        # Output statistics
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[STATS @ {timestamp}]")
        print(f"  Total virtual:           {total_size:12} bytes")
        print(f"  Total RSS:               {total_rss:12} bytes")
        print(
            f"  Read-only virtual:       {readonly_size:12} bytes ({100 * readonly_size / total_size if total_size > 0 else 0:.1f}%)"
        )
        print(
            f"  Read-only RSS:           {readonly_rss:12} bytes ({100 * readonly_rss / total_rss if total_rss > 0 else 0:.1f}%)"
        )

        if self.soft_dirty_support:
            print(
                f"  Read-only never-written: {readonly_never_written_rss:12.0f} bytes ({100 * readonly_never_written_rss / total_rss if total_rss > 0 else 0:.1f}%)"
            )

        self.previous_regions = current_regions

    def run(self):
        """Main monitoring loop"""
        print(f"Monitoring PID {self.pid} every {self.interval}s")
        print(
            f"Soft-dirty tracking: {'enabled' if self.soft_dirty_support else 'disabled'}"
        )
        print("=" * 80)

        try:
            while True:
                self.clear_soft_dirty()
                time.sleep(self.interval)
                self.monitor_iteration()
        except KeyboardInterrupt:
            print("\nMonitoring stopped")


def main():
    parser = argparse.ArgumentParser(
        description="Monitor read-only memory in a process"
    )
    parser.add_argument("pid", type=int, help="Process ID to monitor")
    parser.add_argument(
        "-i",
        "--interval",
        type=float,
        default=1.0,
        help="Sampling interval in seconds (default: 1.0)",
    )

    args = parser.parse_args()

    # Check if running as root
    if os.geteuid() != 0:
        print(
            "Warning: Not running as root. Soft-dirty tracking may not work.",
            file=sys.stderr,
        )
        print("Run with sudo for full functionality.", file=sys.stderr)

    monitor = MemoryMonitor(args.pid, args.interval)
    monitor.run()


if __name__ == "__main__":
    main()
