/* SPDX-License-Identifier: GPL-2.0 */
#ifndef NVPCF_HOOK_TEXT_H
#define NVPCF_HOOK_TEXT_H
#ifdef __KERNEL__
#include <linux/string.h>
#else
#include <stddef.h>
#include <string.h>
#endif

/* Return one complete field value, never a matching substring on another line. */
static inline char *nh_field(char *text, const char *key)
{
	size_t n = strlen(key);
	char *line = text, *end, *value;

	while (*line) {
		end = strchr(line, '\n');
		if (!strncmp(line, key, n)) {
			value = line + n;
			if (end)
				*end = '\0';
			while (*value == ' ' || *value == '\t')
				value++;
			end = value + strlen(value);
			while (end > value && (end[-1] == ' ' || end[-1] == '\t' || end[-1] == '\r'))
				*--end = '\0';
			return value;
		}
		if (!end)
			break;
		line = end + 1;
	}
	return NULL;
}
#endif
