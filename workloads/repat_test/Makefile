CC = gcc
CFLAGS = -O2 -static
all: ltram_repat_test ltram_repat_suite ltram_repat_stress ltram_migrate_test ltram_migrate_suite ltram_migrate_stress ltram_scan_test
ltram_repat_test: ltram_repat_test.c
	$(CC) $(CFLAGS) ltram_repat_test.c -o ltram_repat_test
ltram_repat_suite: ltram_repat_suite.c
	$(CC) $(CFLAGS) -pthread ltram_repat_suite.c -o ltram_repat_suite
ltram_repat_stress: ltram_repat_stress.c
	$(CC) $(CFLAGS) ltram_repat_stress.c -o ltram_repat_stress
ltram_migrate_test: ltram_migrate_test.c
	$(CC) $(CFLAGS) ltram_migrate_test.c -o ltram_migrate_test
clean:
	rm -f ltram_repat_test ltram_repat_suite ltram_repat_stress ltram_migrate_test

ltram_migrate_suite: ltram_migrate_suite.c
	$(CC) $(CFLAGS) ltram_migrate_suite.c -o ltram_migrate_suite ltram_migrate_stress ltram_scan_test

ltram_migrate_stress: ltram_migrate_stress.c
	$(CC) $(CFLAGS) ltram_migrate_stress.c -o ltram_migrate_stress ltram_scan_test

ltram_scan_test: ltram_scan_test.c
	$(CC) $(CFLAGS) ltram_scan_test.c -o ltram_scan_test
