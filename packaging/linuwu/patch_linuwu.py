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
