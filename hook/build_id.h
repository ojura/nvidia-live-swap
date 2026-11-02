/* SPDX-License-Identifier: GPL-2.0 */
#ifndef NVPCF_HOOK_BUILD_ID_H
#define NVPCF_HOOK_BUILD_ID_H
#ifdef __KERNEL__
#include <linux/errno.h>
#include <linux/string.h>
#include <linux/types.h>
#else
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#endif

#define NH_BUILD_ID_MAX 64
#define NH_BUILD_ID_HEX_SIZE (2 * NH_BUILD_ID_MAX + 1)
struct nh_build_id {
	unsigned char bytes[NH_BUILD_ID_MAX];
	size_t size;
};

static inline int nh_hex_digit(unsigned char value)
{
	if (value >= '0' && value <= '9')
		return value - '0';
	if (value >= 'a' && value <= 'f')
		return value - 'a' + 10;
	if (value >= 'A' && value <= 'F')
		return value - 'A' + 10;
	return -1;
}

static inline int nh_parse_build_id(const char *text, struct nh_build_id *id)
{
	size_t size = 0, i;

	id->size = 0;
	if (!text)
		return -EINVAL;
	while (size < NH_BUILD_ID_HEX_SIZE && text[size])
		size++;
	if (size == NH_BUILD_ID_HEX_SIZE)
		return -E2BIG;
	if (!size || size % 2)
		return -EINVAL;
	for (i = 0; i < size / 2; ++i) {
		int high = nh_hex_digit((unsigned char)text[2 * i]);
		int low = nh_hex_digit((unsigned char)text[2 * i + 1]);

		if (high < 0 || low < 0)
			return -EINVAL;
		id->bytes[i] = (unsigned char)((high << 4) | low);
	}
	id->size = size / 2;
	return 0;
}

static inline unsigned int nh_note_u32(const unsigned char *bytes)
{
	return (unsigned int)bytes[0] | ((unsigned int)bytes[1] << 8) |
		((unsigned int)bytes[2] << 16) | ((unsigned int)bytes[3] << 24);
}

/* x86 ELF notes use little-endian 32-bit fields and four-byte alignment.
 * GNU ld can omit padding after the final descriptor (including one-byte IDs).
 */
static inline int nh_note_build_id(const unsigned char *notes, size_t size,
		struct nh_build_id *id)
{
	struct nh_build_id candidate = { { 0 }, 0 };
	size_t offset = 0;
	bool found = false;

	id->size = 0;
	while (offset < size) {
		size_t name_size, desc_size, name_pad, desc_pad, desc_end;
		unsigned int type;

		if (size - offset < 12)
			return -EBADMSG;
		name_size = nh_note_u32(notes + offset);
		desc_size = nh_note_u32(notes + offset + 4);
		type = nh_note_u32(notes + offset + 8);
		offset += 12;
		if (name_size > size - offset)
			return -EBADMSG;
		name_pad = (name_size + 3) & ~(size_t)3;
		if (name_pad > size - offset || desc_size > size - offset - name_pad)
			return -EBADMSG;
		if (type == 3 && name_size == 4 && !memcmp(notes + offset, "GNU\0", 4)) {
			if (found || !desc_size)
				return -EBADMSG;
			if (desc_size > NH_BUILD_ID_MAX)
				return -E2BIG;
			candidate.size = desc_size;
			memcpy(candidate.bytes, notes + offset + name_pad, desc_size);
			found = true;
		}
		offset += name_pad;
		desc_end = offset + desc_size;
		desc_pad = (desc_size + 3) & ~(size_t)3;
		if (desc_pad > size - offset) {
			/* Bounds above proved the descriptor is complete. Only its
			 * final alignment padding is absent or partially present.
			 */
			offset = size;
		} else {
			offset += desc_pad;
		}
		if (offset < desc_end)
			return -EBADMSG;
	}
	if (!found)
		return -ENODATA;
	*id = candidate;
	return 0;
}

static inline int nh_compare_build_id(const struct nh_build_id *actual,
		const struct nh_build_id *expected)
{
	if (!actual->size || !expected->size || actual->size > NH_BUILD_ID_MAX ||
	    expected->size > NH_BUILD_ID_MAX)
		return -EINVAL;
	return actual->size == expected->size &&
		!memcmp(actual->bytes, expected->bytes, expected->size) ? 0 : -ESTALE;
}

static inline void nh_build_id_hex(const struct nh_build_id *id, char *text)
{
	static const char digits[] = "0123456789abcdef";
	size_t i;

	for (i = 0; i < id->size; ++i) {
		text[2 * i] = digits[id->bytes[i] >> 4];
		text[2 * i + 1] = digits[id->bytes[i] & 15];
	}
	text[2 * id->size] = '\0';
}
#endif
