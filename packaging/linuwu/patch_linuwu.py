#!/usr/bin/env python3
"""Apply Nitro Control's compatibility and safety fixes to Linuwu-Sense (src/linuwu_sense.c).

Every fix must match exactly the expected number of times, otherwise nothing is
written: an unexpected upstream change stops the install instead of producing a
half-patched kernel module.
"""
import sys

FIXES = [
    # Linux 7.x removed strncpy(). Each call copies a bounded length and adds the
    # terminating NUL by hand, so memcpy() is an exact replacement.
    ("strncpy(input, buf, len);", "memcpy(input, buf, len);", 1),
    ("strncpy(input_buf, buf, len);", "memcpy(input_buf, buf, len);", 1),
    ("strncpy(str_buf, buf, len);", "memcpy(str_buf, buf, len);", 1),
    # filp_open() returns an ERR_PTR on failure, never NULL. Without this, a failed
    # state save on module unload dereferences an error pointer (kernel oops).
    ("if(!file) {", "if (IS_ERR(file)) {", 2),
    # An empty write (len == 0) read buf[-1]: guard the trailing-newline checks.
    ("if(input[len-1] == '\\n'){", "if(len && input[len-1] == '\\n'){", 1),
    ("if(input_buf[len-1] == '\\n'){", "if(len && input_buf[len-1] == '\\n'){", 1),
    ("if(str_buf[len-1] == '\\n'){", "if(len && str_buf[len-1] == '\\n'){", 1),
    # Static mode was dark on AN515-58 (BIOS V2.18, upstream issue #99) while effects
    # worked. Send it the way PredatorSense-compatible drivers do: zone colours first,
    # then the mode command, with byte 8 = 0 for static (3 is only used by effects).
    ("u8 gmInput[16] = {mode, speed, brightness, 0, direction, red, green, blue, 3, 1, 0, 0, 0, 0, 0, 0};",
     "u8 gmInput[16] = {mode, speed, brightness, 0, direction, red, green, blue, mode == 0 ? 0 : 3, 1, 0, 0, 0, 0, 0, 0};", 1),
    ('     status = set_kb_status(0, 0, input->brightness, 0, 0, 0, 0);\n     if (ACPI_FAILURE(status)) {\n         pr_err("Error setting KB status.\\n");\n         return -ENODEV;\n     }\n \n     for (int i = 0; i < 4; i++) {\n         *zones[i] = (cpu_to_be64(*zones[i]) >> 32) | zone_ids[i];\n         status = WMI_gaming_execute_u64(ACER_WMID_SET_GAMING_RGB_KB_METHODID, *zones[i], NULL);\n         if (ACPI_FAILURE(status)) {\n             pr_err("Error setting KB color (zone %d): %s\\n", i + 1, acpi_format_exception(status));\n             return status;\n         }\n     }\n',
     '     for (int i = 0; i < 4; i++) {\n         *zones[i] = (cpu_to_be64(*zones[i]) >> 32) | zone_ids[i];\n         status = WMI_gaming_execute_u64(ACER_WMID_SET_GAMING_RGB_KB_METHODID, *zones[i], NULL);\n         if (ACPI_FAILURE(status)) {\n             pr_err("Error setting KB color (zone %d): %s\\n", i + 1, acpi_format_exception(status));\n             return status;\n         }\n     }\n \n     /* Switch to static mode only after the zone colours are stored */\n     status = set_kb_status(0, 0, input->brightness, 0, 0, 0, 0);\n     if (ACPI_FAILURE(status)) {\n         pr_err("Error setting KB status.\\n");\n         return -ENODEV;\n     }\n', 1),
    # A failed write closed the file twice.
    ('pr_info("state_access - Error writing to file: %ld\\n", len);\n         filp_close(file, NULL);\n',
     'pr_info("state_access - Error writing to file: %ld\\n", len);\n', 1),
    ('pr_err("kb_state_access - Error writing to file: %ld\\n", len);\n         filp_close(file, NULL);\n',
     'pr_err("kb_state_access - Error writing to file: %ld\\n", len);\n', 1),
]


def main(path):
    with open(path) as f:
        src = f.read()
    for old, new, count in FIXES:
        found = src.count(old)
        if found != count:
            sys.exit("patch_linuwu: expected %d match(es) of %r, found %d" % (count, old.splitlines()[0], found))
        src = src.replace(old, new)
    with open(path, "w") as f:
        f.write(src)


if __name__ == "__main__":
    main(sys.argv[1])
