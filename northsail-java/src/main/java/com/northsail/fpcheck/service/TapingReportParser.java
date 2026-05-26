package com.northsail.fpcheck.service;

import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;

/**
 * Parse taping report PDF (OE, DPI, Tier, pocket list, Cunningham, Helix).
 */
public final class TapingReportParser {

    public static Map<String, Object> parse(Path filepath) {
        Map<String, Object> data = new HashMap<>();
        try {
            String content = PdfService.readPdfAll(filepath);
            String fileName = filepath.getFileName().toString();

            String oe = ParseHelper.getOe(content);
            if (oe == null) oe = ParseHelper.getOe(fileName);
            if (oe != null) data.put("OE Number", oe);

            Double dpi = ParseHelper.getDpi(content, "(\\d+)\\s*DPI");
            if (dpi != null) data.put("DPI", dpi);

            String tierStr = ParseHelper.getTierBeforeTaping(content);
            if (tierStr != null) {
                try {
                    data.put("Tier", Integer.parseInt(tierStr));
                } catch (NumberFormatException ignored) {}
            }
        } catch (Exception e) {
            // log
        }
        return data;
    }
}
