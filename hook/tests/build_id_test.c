/* SPDX-License-Identifier: GPL-2.0 */
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include "../build_id.h"

static void u32(unsigned char *bytes, unsigned int value)
{
	unsigned int i;

	for (i = 0; i < 4; ++i)
		bytes[i] = (unsigned char)(value >> (8 * i));
}

static size_t note(unsigned char *bytes, size_t length, bool padding)
{
	size_t i, size = 16 + length;

	memset(bytes, 0, 512);
	u32(bytes, 4);
	u32(bytes + 4, (unsigned int)length);
	u32(bytes + 8, 3);
	memcpy(bytes + 12, "GNU\0", 4);
	for (i = 0; i < length; ++i)
		bytes[16 + i] = (unsigned char)i;
	return padding ? (size + 3) & ~(size_t)3 : size;
}

static void expected_input(void)
{
	struct nh_build_id id = { { 0 }, 0 };
	char hex[NH_BUILD_ID_HEX_SIZE], oversized[NH_BUILD_ID_HEX_SIZE + 2];
	const char *bad[] = { NULL, "", "0", "0x01", "aa:bb", "gg", " aa", "aa\n" };
	size_t i;

	for (i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i) {
		assert(nh_parse_build_id(bad[i], &id) == -EINVAL);
		assert(id.size == 0);
	}
	memset(oversized, 'a', sizeof(oversized) - 1);
	oversized[sizeof(oversized) - 1] = '\0';
	assert(nh_parse_build_id(oversized, &id) == -E2BIG && !id.size);
	assert(!nh_parse_build_id("A1b2C3", &id) && id.size == 3);
	nh_build_id_hex(&id, hex);
	assert(!strcmp(hex, "a1b2c3"));
	id.size = 0;
	assert(nh_compare_build_id(&id, &id) == -EINVAL);
}

static void descriptors(void)
{
	unsigned char bytes[512], concatenated[1024];
	struct nh_build_id actual, expected;
	char hex[NH_BUILD_ID_HEX_SIZE];
	size_t length, size, cut, first;
	unsigned int padding;

	for (length = 1; length <= NH_BUILD_ID_MAX; ++length)
	for (padding = 0; padding < 2; ++padding) {
		size = note(bytes, length, padding != 0);
		assert(!nh_note_build_id(bytes, size, &actual));
		assert(actual.size == length);
		nh_build_id_hex(&actual, hex);
		assert(!nh_parse_build_id(hex, &expected));
		assert(!nh_compare_build_id(&actual, &expected));
		for (cut = 0; cut < size; ++cut) {
			int error = nh_note_build_id(bytes, cut, &actual);

			if (cut >= 16 + length)
				assert(!error); /* Terminal alignment padding is optional. */
			else
				assert(error == (cut ? -EBADMSG : -ENODATA) && !actual.size);
		}
	}
	/* A second GNU descriptor is ambiguous, even if the values agree. */
	first = note(bytes, 3, true);
	memcpy(concatenated, bytes, first);
	size = note(bytes, 20, false);
	memcpy(concatenated + first, bytes, size);
	assert(nh_note_build_id(concatenated, first + size, &actual) == -EBADMSG && !actual.size);
	/* Skip an unrelated note with a one-byte name and three-byte descriptor. */
	memset(concatenated, 0, 20);
	u32(concatenated, 1);
	u32(concatenated + 4, 3);
	u32(concatenated + 8, 1);
	concatenated[12] = 'X';
	memcpy(concatenated + 20, bytes, size);
	assert(!nh_note_build_id(concatenated, 20 + size, &actual) && actual.size == 20);
	assert(nh_note_build_id(concatenated, 17, &actual) == -EBADMSG);
	/* Header/length/name/type failures cannot produce an accepted ID. */
	size = note(bytes, 20, false);
	u32(bytes, 0xffffffffU);
	assert(nh_note_build_id(bytes, size, &actual) == -EBADMSG);
	size = note(bytes, 20, false);
	u32(bytes + 4, 0xffffffffU);
	assert(nh_note_build_id(bytes, size, &actual) == -EBADMSG);
	size = note(bytes, 20, false);
	bytes[12] = 'X';
	assert(nh_note_build_id(bytes, size, &actual) == -ENODATA);
	size = note(bytes, 20, false);
	u32(bytes + 8, 1);
	assert(nh_note_build_id(bytes, size, &actual) == -ENODATA);
	size = note(bytes, 0, false);
	assert(nh_note_build_id(bytes, size, &actual) == -EBADMSG);
	size = note(bytes, 65, false);
	assert(nh_note_build_id(bytes, size, &actual) == -E2BIG);
	/* The expected descriptor must change for a replacement artifact. */
	size = note(bytes, 20, false);
	assert(!nh_note_build_id(bytes, size, &expected));
	bytes[16] ^= 1;
	assert(!nh_note_build_id(bytes, size, &actual));
	assert(nh_compare_build_id(&actual, &expected) == -ESTALE);
	nh_build_id_hex(&actual, hex);
	assert(!nh_parse_build_id(hex, &expected));
	assert(!nh_compare_build_id(&actual, &expected));
}

int main(int argc, char **argv)
{
	if (argc == 3) {
		unsigned char bytes[512];
		struct nh_build_id actual, expected;
		FILE *file = fopen(argv[2], "rb");
		size_t size;

		assert(file);
		size = fread(bytes, 1, sizeof(bytes), file);
		assert(!ferror(file) && size < sizeof(bytes));
		assert(!fclose(file));
		assert(!nh_parse_build_id(argv[1], &expected));
		assert(!nh_note_build_id(bytes, size, &actual));
		assert(!nh_compare_build_id(&actual, &expected));
		return 0;
	}
	assert(argc == 1);
	expected_input();
	descriptors();
	puts("PASS: required build-ID hex, 1..64-byte descriptors, note bounds/alignment, truncation, duplicates and replacement mismatch");
	return 0;
}
