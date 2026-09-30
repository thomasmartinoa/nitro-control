#!/usr/bin/env python3
"""Apply Nitro Control's fixes to Linuwu-Sense (src/linuwu_sense.c).

Every fix must match exactly the expected number of times, otherwise nothing is
written: an unexpected upstream change stops the install instead of producing a
half-patched kernel module.

The AN515-58 keyboard and power-source fixes are ported from
https://github.com/fabiannabil1/Linuwu-AN515-58-Linuwu-Sense-Fix (tested on an
AN515-58), limited to the parts needed and gated behind a model quirk so other
laptops keep upstream behaviour.
"""
import re
import sys

# (old, new, expected count). Plain strings unless old is a compiled regex.
FIXES = [
    # --- Linux 7.x compatibility -------------------------------------------------
    # strncpy() was removed. Each call copies a bounded length and adds the
    # terminating NUL by hand, so memcpy() is an exact replacement.
    ("strncpy(input, buf, len);", "memcpy(input, buf, len);", 1),
    ("strncpy(input_buf, buf, len);", "memcpy(input_buf, buf, len);", 1),
    ("strncpy(str_buf, buf, len);", "memcpy(str_buf, buf, len);", 1),

    # --- Safety ---------------------------------------------------------------
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

    # --- Power source ---------------------------------------------------------
    # The firmware's BAT_STATUS is unreliable on some models (AN515-58), which made
    # the driver reject Quiet/Performance and restore the wrong per-source state.
    # Ask the kernel's power-supply core first; fall back to the firmware.
    ("#include <linux/bitmap.h>\n", "#include <linux/bitmap.h>\n#include <linux/power_supply.h>\n", 1),
    (" static acpi_status WMID_gaming_set_u64(u64 value, u32 cap)\n",
     """ static acpi_status acer_query_on_ac(u64 *on_AC)
 {
     int supplied = power_supply_is_system_supplied();

     if (supplied >= 0) {
         *on_AC = supplied > 0;
         return AE_OK;
     }
     return WMI_gaming_execute_u64(ACER_WMID_GET_GAMING_SYS_INFO_METHODID,
                                   ACER_WMID_CMD_GET_PREDATOR_V4_BAT_STATUS, on_AC);
 }

 static acpi_status WMID_gaming_set_u64(u64 value, u32 cap)
""", 1),
    (re.compile(r"WMI_gaming_execute_u64\(\s*ACER_WMID_GET_GAMING_SYS_INFO_METHODID,\s*"
                r"ACER_WMID_CMD_GET_PREDATOR_V4_BAT_STATUS, &on_AC\)"),
     "acer_query_on_ac(&on_AC)", 4),

    # --- AN515-58 keyboard ----------------------------------------------------
    ("     u8 four_zone_kb;\n };\n", "     u8 four_zone_kb;\n     u8 an515_58_rgb_fix;\n };\n", 1),
    (" static struct quirk_entry quirk_acer_nitro_an515_58 = {\n    .nitro_v4 = 1,\n    .four_zone_kb = 1,\n };",
     " static struct quirk_entry quirk_acer_nitro_an515_58 = {\n    .nitro_v4 = 1,\n    .four_zone_kb = 1,\n"
     "    .an515_58_rgb_fix = 1,\n };", 1),
    # Effect payload in the PredatorSense/facer layout: byte 3 carries wave's flag, byte 8 unused.
    ("     u8 gmInput[16] = {mode, speed, brightness, 0, direction, red, green, blue, 3, 1, 0, 0, 0, 0, 0, 0};\n",
     """     u8 gmInput[16] = {mode, speed, brightness, 0, direction, red, green, blue, 3, 1, 0, 0, 0, 0, 0, 0};

     if (quirks->an515_58_rgb_fix) {
         gmInput[3] = mode == 0x3 ? 8 : 0;
         gmInput[8] = 0;
     }
""", 1),
    # Breathing keeps its speed (speed 0 leaves the AN515-58 keyboard dark).
    ("         case 0x1:  // Breathing mode: Ignore speed\n             speed = 0;\n",
     "         case 0x1:  // Breathing mode: Ignore speed (except AN515-58)\n"
     "             if (!quirks->an515_58_rgb_fix)\n                 speed = 0;\n", 1),
    # Static colours: switch the LED zones on (SET_GAMING_LED), write each zone as
    # {zone, r, g, b} scaled by brightness, then select static mode.
    (" static acpi_status set_per_zone_color(struct per_zone_color *input) {\n",
     """ #define NC_GAMING_KBL_SET_ON BIT(3)
 #define NC_GAMING_KBL_SET_ALL_ZONES GENMASK_ULL(43, 40)

 struct an515_58_static_rgb_param {
     u8 zone;
     u8 red;
     u8 green;
     u8 blue;
 } __packed;

 static acpi_status an515_58_set_static(struct per_zone_color *input) {
     u8 zone_ids[] = { 0x1, 0x2, 0x4, 0x8 };
     u64 colors[] = { input->zone1, input->zone2, input->zone3, input->zone4 };
     u8 gmInput[16] = {0};
     struct acpi_buffer mode_input = { (acpi_size)sizeof(gmInput), (void *)gmInput };
     acpi_status status;

     status = WMI_gaming_execute_u64(ACER_WMID_GET_GAMING_SYS_INFO_METHODID, 0, NULL);
     if (ACPI_FAILURE(status))
         return status;
     status = WMI_gaming_execute_u64(ACER_WMID_SET_GAMING_LED_METHODID,
                                     NC_GAMING_KBL_SET_ON | NC_GAMING_KBL_SET_ALL_ZONES, NULL);
     if (ACPI_FAILURE(status))
         return status;

     for (int i = 0; i < 4; i++) {
         struct an515_58_static_rgb_param params = {
             .zone = zone_ids[i],
             .red = (((colors[i] >> 16) & 0xff) * input->brightness) / 100,
             .green = (((colors[i] >> 8) & 0xff) * input->brightness) / 100,
             .blue = ((colors[i] & 0xff) * input->brightness) / 100,
         };
         struct acpi_buffer zone_input = { (acpi_size)sizeof(params), &params };

         status = wmi_evaluate_method(WMID_GUID4, 0, ACER_WMID_SET_GAMING_RGB_KB_METHODID, &zone_input, NULL);
         if (ACPI_FAILURE(status))
             return status;
     }

     gmInput[2] = input->brightness;
     gmInput[9] = 1;
     return wmi_evaluate_method(WMID_GUID4, 0, ACER_WMID_SET_GAMING_KB_BACKLIGHT_METHODID, &mode_input, NULL);
 }

 static acpi_status set_per_zone_color(struct per_zone_color *input) {
     if (quirks->an515_58_rgb_fix) {
         acpi_status an_status = an515_58_set_static(input);

         if (ACPI_FAILURE(an_status)) {
             pr_err("Error setting AN515-58 static RGB: %s\\n", acpi_format_exception(an_status));
             return an_status;
         }
         current_kb_state.per_zone = 1;
         return AE_OK;
     }
""", 1),
]


def main(path):
    with open(path) as f:
        src = f.read()
    for old, new, count in FIXES:
        if isinstance(old, re.Pattern):
            src, found = old.subn(new, src)
            label = old.pattern
        else:
            found = src.count(old)
            src = src.replace(old, new)
            label = old.splitlines()[0]
        if found != count:
            sys.exit("patch_linuwu: expected %d match(es) of %r, found %d" % (count, label, found))
    with open(path, "w") as f:
        f.write(src)


if __name__ == "__main__":
    main(sys.argv[1])
