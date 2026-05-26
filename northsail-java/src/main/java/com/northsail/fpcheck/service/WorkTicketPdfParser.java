package com.northsail.fpcheck.service;

import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Parse work ticket PDF (OE, DPI, Tier, measurements from text).
 */
public final class WorkTicketPdfParser {

    private static final int TIER_ENDURANCE_EDGE = 360;

    public static Map<String, Object> parse(Path filepath) {
        Map<String, Object> data = new HashMap<>();
        try {
            String content = PdfService.readPdfAll(filepath);
            String fileName = filepath.getFileName().toString();

            String oe = ParseHelper.getOe(content);
            if (oe == null) oe = ParseHelper.getOe(fileName);
            if (oe != null) data.put("OE Number", oe);

            Double dpi = ParseHelper.getDpi(content, "(\\d+[,\\d]*)\\s*Dpi");
            if (dpi != null) data.put("DPI", dpi);

            String tierStr = ParseHelper.getTierAfter(content);
            if (tierStr == null) tierStr = ParseHelper.getTierBefore(content);
            if (tierStr != null) {
                try {
                    data.put("Tier", Integer.parseInt(tierStr));
                } catch (NumberFormatException ignored) {}
            }
            if (ParseHelper.isEnduranceEdge(content)) {
                data.put("Tier", TIER_ENDURANCE_EDGE);
            }

            extractMeasurements(content, data);
        } catch (Exception e) {
            // log
        }
        return data;
    }

    private static void extractMeasurements(String text, Map<String, Object> data) {
        if (text == null) return;
        String[] keys = {"Head", "Luff", "Leech", "Foot", "LP"};
        for (String key : keys) {
            Pattern p = Pattern.compile(key + "\\s*:\\s*([0-9]+[.,][0-9]+)", Pattern.CASE_INSENSITIVE | Pattern.DOTALL);
            Matcher m = p.matcher(text);
            if (m.find()) {
                String numStr = m.group(1).replace(",", ".");
                try {
                    data.put(key, Double.parseDouble(numStr));
                } catch (NumberFormatException ignored) {}
            }
        }
    }
}
