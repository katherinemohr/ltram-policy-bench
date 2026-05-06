#!/usr/bin/env python3
"""
Parse /proc/[pid]/smaps to collect memory statistics.
MicroPython compatible version.
"""

import sys


class SmapsParser:
    def __init__(self, pid=None):
        """
        Initialize parser for a specific PID or self.

        Args:
            pid: Process ID to analyze. If None, uses current process.
        """
        self.pid = pid or "self"
        self.smaps_path = "/proc/{}/smaps".format(self.pid)

    def parse(self):
        """
        Parse smaps file and return statistics.

        Returns:
            dict: Dictionary containing memory statistics
        """
        stats = {
            "total_size": 0,
            "total_rss": 0,
            "total_pss": 0,
            "readonly_size": 0,
            "readonly_rss": 0,
            "writable_size": 0,
            "writable_rss": 0,
            "executable_size": 0,
            "executable_rss": 0,
            "shared_clean": 0,
            "shared_dirty": 0,
            "private_clean": 0,
            "private_dirty": 0,
            "swap": 0,
            "anonymous": 0,
            "file_backed": 0,
            "heap_size": 0,
            "heap_rss": 0,
            "stack_size": 0,
            "stack_rss": 0,
            "regions": [],
        }

        try:
            f = open(self.smaps_path, "r")
        except OSError:
            raise Exception("Cannot access {}".format(self.smaps_path))

        current_region = {}

        try:
            for line in f:
                line = line.strip()

                # Parse memory region header
                if self._is_region_header(line):
                    # Save previous region if exists
                    if current_region:
                        self._update_stats(stats, current_region)
                        stats["regions"].append(current_region)

                    # Parse new region
                    current_region = self._parse_region_header(line)

                # Parse field values
                elif ":" in line and current_region:
                    parts = line.split(":", 1)
                    key = parts[0].strip()
                    value = parts[1].strip()

                    # Extract numeric value (in kB)
                    num = self._extract_number(value)
                    if num is not None:
                        current_region[key] = num

            # Don't forget the last region
            if current_region:
                self._update_stats(stats, current_region)
                stats["regions"].append(current_region)
        finally:
            f.close()

        return stats

    def _is_region_header(self, line):
        """Check if line is a region header."""
        if not line:
            return False
        # Check for hex address range pattern
        parts = line.split("-", 1)
        if len(parts) != 2:
            return False
        try:
            # Try to parse as hex
            int(parts[0], 16)
            # Get first part of second component (before space)
            int(parts[1].split()[0], 16)
            return True
        except (ValueError, IndexError):
            return False

    def _extract_number(self, text):
        """Extract first number from text."""
        num_str = ""
        for char in text:
            if char.isdigit():
                num_str += char
            elif num_str:  # Stop at first non-digit after finding digits
                break

        if num_str:
            return int(num_str)
        return None

    def _parse_region_header(self, line):
        """Parse the memory region header line."""
        # Format: address perms offset dev inode pathname
        parts = line.split(None, 5)

        address_range = parts[0]
        addr_parts = address_range.split("-")
        start = addr_parts[0]
        end = addr_parts[1] if len(addr_parts) > 1 else ""

        region = {
            "start_address": start,
            "end_address": end,
            "permissions": parts[1] if len(parts) > 1 else "",
            "offset": parts[2] if len(parts) > 2 else "",
            "dev": parts[3] if len(parts) > 3 else "",
            "inode": parts[4] if len(parts) > 4 else "",
            "pathname": parts[5] if len(parts) > 5 else "",
        }

        return region

    def _update_stats(self, stats, region):
        """Update statistics based on current region."""
        size = region.get("Size", 0)
        rss = region.get("Rss", 0)
        pss = region.get("Pss", 0)
        perms = region.get("permissions", "")
        pathname = region.get("pathname", "")

        # Total memory
        stats["total_size"] += size
        stats["total_rss"] += rss
        stats["total_pss"] += pss

        # Read-only vs writable
        if "w" in perms:
            stats["writable_size"] += size
            stats["writable_rss"] += rss
        else:
            stats["readonly_size"] += size
            stats["readonly_rss"] += rss

        # Executable
        if "x" in perms:
            stats["executable_size"] += size
            stats["executable_rss"] += rss

        # Shared/Private
        stats["shared_clean"] += region.get("Shared_Clean", 0)
        stats["shared_dirty"] += region.get("Shared_Dirty", 0)
        stats["private_clean"] += region.get("Private_Clean", 0)
        stats["private_dirty"] += region.get("Private_Dirty", 0)

        # Swap and Anonymous
        stats["swap"] += region.get("Swap", 0)
        stats["anonymous"] += region.get("Anonymous", 0)

        # File-backed (Rss - Anonymous)
        stats["file_backed"] += rss - region.get("Anonymous", 0)

        # Heap
        if "[heap]" in pathname:
            stats["heap_size"] += size
            stats["heap_rss"] += rss

        # Stack
        if "[stack]" in pathname or "[stack:" in pathname:
            stats["stack_size"] += size
            stats["stack_rss"] += rss


def format_size(kb):
    """Format size in KB to human-readable format."""
    if kb >= 1024 * 1024:
        return "{:.2f} GB ({} kB)".format(kb / (1024.0 * 1024.0), kb)
    elif kb >= 1024:
        return "{:.2f} MB ({} kB)".format(kb / 1024.0, kb)
    else:
        return "{} kB".format(kb)


def print_stats(stats):
    """Print formatted statistics."""
    print("=" * 60)
    print("MEMORY STATISTICS")
    print("=" * 60)

    print("\n--- TOTAL MEMORY ---")
    print("Virtual Size (Total):  {}".format(format_size(stats["total_size"])))
    print("RSS (Resident):        {}".format(format_size(stats["total_rss"])))
    print("PSS (Proportional):    {}".format(format_size(stats["total_pss"])))

    print("\n--- BY PERMISSIONS ---")
    print("Read-Only Size:        {}".format(format_size(stats["readonly_size"])))
    print("Read-Only RSS:         {}".format(format_size(stats["readonly_rss"])))
    print("Writable Size:         {}".format(format_size(stats["writable_size"])))
    print("Writable RSS:          {}".format(format_size(stats["writable_rss"])))
    print("Executable Size:       {}".format(format_size(stats["executable_size"])))
    print("Executable RSS:        {}".format(format_size(stats["executable_rss"])))

    print("\n--- BY SHARING ---")
    print("Shared Clean:          {}".format(format_size(stats["shared_clean"])))
    print("Shared Dirty:          {}".format(format_size(stats["shared_dirty"])))
    print("Private Clean:         {}".format(format_size(stats["private_clean"])))
    print("Private Dirty:         {}".format(format_size(stats["private_dirty"])))

    print("\n--- BY BACKING ---")
    print("Anonymous:             {}".format(format_size(stats["anonymous"])))
    print("File-backed:           {}".format(format_size(stats["file_backed"])))
    print("Swap:                  {}".format(format_size(stats["swap"])))

    print("\n--- SPECIAL REGIONS ---")
    print("Heap Size:             {}".format(format_size(stats["heap_size"])))
    print("Heap RSS:              {}".format(format_size(stats["heap_rss"])))
    print("Stack Size:            {}".format(format_size(stats["stack_size"])))
    print("Stack RSS:             {}".format(format_size(stats["stack_rss"])))

    print("\n--- REGION BREAKDOWN ---")
    print("Total Regions:         {}".format(len(stats["regions"])))

    # Count by type (manual defaultdict replacement)
    region_types = {}
    for region in stats["regions"]:
        pathname = region.get("pathname", "anonymous")
        if not pathname:
            pathname = "anonymous"
        elif pathname.startswith("["):
            key = pathname
        elif "/" in pathname:
            # Simplify path for binaries/libraries
            if ".so" in pathname:
                key = "libraries"
            else:
                key = "files"
        else:
            key = pathname

        if key in region_types:
            region_types[key] += 1
        else:
            region_types[key] = 1

    # Sort keys for consistent output
    sorted_keys = sorted(region_types.keys())
    for region_type in sorted_keys:
        count = region_types[region_type]
        print("  {}: {}".format(region_type, count))


def main():
    """Main entry point."""
    # Simple argument parsing without argparse
    verbose = False
    pid = None

    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg in ["-v", "--verbose"]:
            verbose = True
        elif arg in ["-h", "--help"]:
            print("Usage: {} [PID] [-v|--verbose]".format(sys.argv[0]))
            print("\nParse /proc/[pid]/smaps and display memory statistics")
            print("\nArguments:")
            print("  PID                Process ID to analyze (default: self)")
            print("  -v, --verbose      Show detailed region information")
            return
        else:
            pid = arg
        i += 1

    try:
        parser_obj = SmapsParser(pid)
        stats = parser_obj.parse()
        print_stats(stats)

        if verbose:
            print("\n" + "=" * 60)
            print("DETAILED REGION INFORMATION")
            print("=" * 60)
            for i in range(len(stats["regions"])):
                region = stats["regions"][i]
                print("\n--- Region {} ---".format(i + 1))
                print(
                    "Address: {}-{}".format(
                        region["start_address"], region["end_address"]
                    )
                )
                print("Permissions: {}".format(region["permissions"]))
                pathname = region.get("pathname", "(anonymous)")
                if not pathname:
                    pathname = "(anonymous)"
                print("Path: {}".format(pathname))
                print("Size: {}".format(format_size(region.get("Size", 0))))
                print("RSS: {}".format(format_size(region.get("Rss", 0))))

    except Exception as e:
        print("Error: {}".format(str(e)))
        sys.exit(1)


if __name__ == "__main__":
    main()
