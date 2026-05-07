CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra

all: dirty_sweep

dirty_sweep: dirty_sweep.c
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -f dirty_sweep

.PHONY: all clean
