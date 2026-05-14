# Goal

The goal of this (currently unnamed) project is to get NOR flash connected directly to a processor as
a somewhat slower, but much cheaper, replacement for DRAM. 

To do so, we have a CPU with 2 NUMA nodes, a normal DRAM node and our NOR (aka Long Term RAM, LtRAM) node.
The normal DRAM node looks as you would expect, but the LtRAM node is connected to an FPGA that controls a NOR flash chip.
The FPGA is really a memory controller for the NOR flash, so we can try to use NOR flash as another memory tier.

# Constraints/General Design Goals
The NOR flash has significant read/write asymmetry, where writes are significantly more expensive. So, we would like to isolate that memory from the rest of the Linux kernel but still 
1. be able to take advantage of existing memory management in the Linux kernel
2. be able to migrate pages from the DDR4 connected to the ThunderX to the NOR flash with explicit migration calls (ie. memory should NEVER fallback to being placed on the NOR flash without explicit permission)

The NOR flash is expected to be used in the following way:
1. Because reads are fine but writes are expensive, read-only pages can be immediately placed onto NOR flash.
2. All other pages will need to be explicitly migrated once they are deemed cold or read-mostly. A page should only ever exist in NOR flash or DDR4, not both, so coherency shouldn't be a large concern.
3. If the user attempts to write to a page in NOR flash, the page should be migrated back to DDR4 before writing.

NOR flash also is not durable, so wear-leveling is a concern, and we may need to modify allocation algorithms (aka the buddy allocator) with that in mind.

We envision this working like
* the NOR flash has its own free list of pages (via a new kernel memory zone, `ZONE_LTRAM`)
* the standard buddy allocator is used to allocate pages
* there is an explicit migrate() call that must be used to move data from ddr4 to nor flash
* nor flash pages are by default promoted back to ddr4 on write (via copy-on-write)
* whenever a nor flash page is freed, we should try to asynchronously erase the page to make writes faster (the majority of write time is actually for erasing the page, not writing to it)
* and similarly, we should only ever write to already-erased pages
* writes to nor flash should be over dma or otherwise non cacheable so coherency isnt an issue

# Specs

## Enzian

Node 0:
- Marvell Cavium ThunderX-1 CN8890-NT CPU @ 2 GHz (48 x ARMv8.1 cores)
- 128 GiB DDR4, 4x 32 GiB DIMMS @ 2133 MT/s
- PCIe Gen3 x8 slot
- 3 x NVMe connectors
- 4 x SATA connectors
- 2 x 40Gb/s Ethernet QSFP28 connectors
- USB3, serial UARTs
- JTAG

Node 1:
- Xilinx CVU9P FPGA
- 512 GiB DDR4, 4x 128 GiB DIMMS @ 2133 MT/s **or** 64 GiB DDR4, 4x 16 GiB DIMMS @ 2400 MT/s
  - Note: these can be used later for a more apples-to-apples comparison
- PCIe Gen3 x16 slot
- 1 x NVMe connector
- FMC connector
- 16 x 25Gb/s serial lines in 4 x QSFP28 cages, configurable as 16 25Gb/s or 4 x 100Gb/s Ethernet
- JTAG
- 0.25GB NOR Flash RAM

## NOR flash

Size: 0.25 GB

Speeds:
  * Read (128B): 490ns
  * Write (256B): 16us
  * Erase (4KB): 18ms

Cost: 1/3 of DRAM

## Linux kernel
Version: 6.8
Config: Refer to `configs/linux-config`

# Current Implementation
TODO
