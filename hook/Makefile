# SPDX-License-Identifier: GPL-2.0
KERNELRELEASE ?= $(shell uname -r)
KDIR ?= /lib/modules/$(KERNELRELEASE)/build
CC ?= cc
HOSTCFLAGS ?= -std=c11 -O2 -g -Wall -Wextra -Werror -Wconversion -pedantic -pthread

.PHONY: all modules check clean
all: modules
modules:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

check: tests/state_test
	./tests/state_test
	python3 -I -B tests/check_control.py

tests/state_test: tests/state_test.c state.h lifecycle.h text.h
	$(CC) $(HOSTCFLAGS) $< -o $@

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
	$(RM) tests/state_test
